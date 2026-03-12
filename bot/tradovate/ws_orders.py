from __future__ import annotations
import asyncio
import json
import logging
from typing import Callable, Awaitable

import aiohttp
import websockets

from bot.config import TradovateConfig
from bot.tradovate.auth import TradovateAuth

log = logging.getLogger("orfvg.ws_orders")


class OrdersWS:
    def __init__(
        self,
        config: TradovateConfig,
        auth: TradovateAuth,
        on_fill: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self._config = config
        self._auth = auth
        self._on_fill = on_fill
        self._ws = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._fill_waiters: dict[int, asyncio.Future] = {}  # order_id -> future(price)
        self._running = False
        self._reconnect_delay = 1.0
        self._connected = asyncio.Event()
        self._rest_base = config.rest_url
        self._http_session: aiohttp.ClientSession | None = None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def connect(self):
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                log.warning("Orders WS disconnected: %s", e)
                self._connected.clear()
            except Exception as e:
                log.error("Orders WS error: %s", e, exc_info=True)
                self._connected.clear()

            if not self._running:
                break

            log.info("Orders WS reconnecting in %.1fs...", self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    async def _connect_and_listen(self):
        url = self._config.orders_ws_url
        log.info("Connecting to orders WS: %s", url)

        async with websockets.connect(url, ping_interval=None, ping_timeout=None) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0

            msg = await ws.recv()
            if not msg.startswith("o"):
                log.error("Expected open frame, got: %s", msg[:100])
                return

            auth_msg = f"authorize\n{self._next_id()}\n\n{self._auth.token}"
            await ws.send(auth_msg)
            resp = await ws.recv()
            log.debug("Orders auth response: %s", resp[:200])

            self._connected.set()
            log.info("Orders WS connected and authorized")

            heartbeat_task = asyncio.ensure_future(self._heartbeat_loop(ws))

            try:
                async for raw in ws:
                    await self._handle_message(raw)
            finally:
                heartbeat_task.cancel()

    async def _heartbeat_loop(self, ws):
        try:
            while True:
                await asyncio.sleep(2.5)
                try:
                    await ws.send("[]")
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, raw: str):
        if raw == "h":
            return
        if not raw.startswith("a"):
            return

        try:
            payloads = json.loads(raw[1:])
        except json.JSONDecodeError:
            return

        for payload in payloads:
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    continue

            if not isinstance(payload, dict):
                continue

            # Check for request responses
            req_id = payload.get("i")
            if req_id and req_id in self._pending:
                self._pending[req_id].set_result(payload)

            # Check for order status updates with fill info
            if payload.get("e") == "props":
                data = payload.get("d", {})
                entity_type = data.get("entityType")

                # Capture fill price from order fill events
                if entity_type == "fill":
                    order_id = data.get("orderId")
                    price = data.get("price")
                    if order_id and price and order_id in self._fill_waiters:
                        if not self._fill_waiters[order_id].done():
                            self._fill_waiters[order_id].set_result(price)

                # Also check executionReport
                if entity_type == "executionReport":
                    if self._on_fill:
                        try:
                            await self._on_fill(data)
                        except Exception as e:
                            log.error("Error in fill callback: %s", e)

    async def _send_ws_request(self, endpoint: str, payload: dict, timeout: float = 10.0) -> dict | None:
        await self._connected.wait()

        req_id = self._next_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future

        msg = f"{endpoint}\n{req_id}\n\n{json.dumps(payload)}"
        await self._ws.send(msg)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            self._pending.pop(req_id, None)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            log.error("WS request timed out: %s", endpoint)
            return None

    async def _get_fill_price(self, order_id: int, timeout: float = 3.0) -> float | None:
        """Get fill price — try WS event first, fall back to REST."""
        # Try WS fill event (fastest)
        if order_id in self._fill_waiters:
            try:
                price = await asyncio.wait_for(self._fill_waiters[order_id], timeout=1.0)
                self._fill_waiters.pop(order_id, None)
                log.info("Fill price from WS: %.2f", price)
                return price
            except asyncio.TimeoutError:
                self._fill_waiters.pop(order_id, None)

        # Fall back to REST (reliable)
        session = await self._get_http()
        end_time = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < end_time:
            try:
                url = f"{self._rest_base}/fill/deps?masterid={order_id}"
                async with session.get(url, headers=self._auth.auth_headers()) as resp:
                    if resp.status == 200:
                        fills = await resp.json()
                        if fills and isinstance(fills, list) and len(fills) > 0:
                            total_qty = sum(f.get("qty", 0) for f in fills)
                            if total_qty > 0:
                                avg = sum(f.get("price", 0) * f.get("qty", 0) for f in fills) / total_qty
                                log.info("Fill price from REST: %.2f", avg)
                                return avg
            except Exception as e:
                log.error("REST fill check error: %s", e)
            await asyncio.sleep(0.2)
        return None

    async def place_bracket_order(self, position: dict) -> dict | None:
        """Place entry at market, get fill, place SL/TP from point offsets.
        Guardian handles backup SL monitoring — no REST verification needed.
        """
        await self._connected.wait()

        is_long = position["is_long"]
        action = "Buy" if is_long else "Sell"
        opposite = "Sell" if is_long else "Buy"
        symbol = self._config.symbol if hasattr(self._config, "symbol") else "MESM6"
        tick = 0.25

        sl_points = position["sl_points"]
        tp_points = position["tp_points"]

        # Register fill waiter before placing order (so we catch the WS event)
        # We don't know the order ID yet, so we'll register after placement

        log.info(
            "Placing %s market qty=%d (SL=%.2f pts, TP=%.2f pts)",
            action, position["qty"], sl_points, tp_points,
        )

        # Place market entry
        entry_payload = {
            "accountSpec": self._auth.account_spec,
            "accountId": self._auth.account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": position["qty"],
            "orderType": "Market",
            "isAutomated": True,
        }

        result = await self._send_ws_request("order/placeOrder", entry_payload)
        if result is None or result.get("s") != 200:
            log.error("Entry order failed: %s", result)
            return None

        entry_order_id = result.get("d", {}).get("orderId")

        # Register fill waiter for this order
        self._fill_waiters[entry_order_id] = asyncio.get_running_loop().create_future()

        # Get fill price (WS first, REST fallback)
        actual_fill = await self._get_fill_price(entry_order_id, timeout=3.0)
        if actual_fill is None:
            log.error("CRITICAL: No fill price — EMERGENCY CLOSE!")
            await self._send_emergency_stop(symbol, position["qty"], opposite)
            return None

        # Calculate SL/TP from fill + point offsets
        if is_long:
            actual_sl = round((actual_fill - sl_points) / tick) * tick
            actual_tp = round((actual_fill + tp_points) / tick) * tick
        else:
            actual_sl = round((actual_fill + sl_points) / tick) * tick
            actual_tp = round((actual_fill - tp_points) / tick) * tick

        log.info("Fill=%.2f → SL=%.2f TP=%.2f", actual_fill, actual_sl, actual_tp)

        # Place SL and TP in parallel
        sl_payload = {
            "accountSpec": self._auth.account_spec,
            "accountId": self._auth.account_id,
            "action": opposite,
            "symbol": symbol,
            "orderQty": position["qty"],
            "orderType": "Stop",
            "stopPrice": actual_sl,
            "isAutomated": True,
        }
        tp_payload = {
            "accountSpec": self._auth.account_spec,
            "accountId": self._auth.account_id,
            "action": opposite,
            "symbol": symbol,
            "orderQty": position["qty"],
            "orderType": "Limit",
            "price": actual_tp,
            "isAutomated": True,
        }

        sl_task = asyncio.ensure_future(self._send_ws_request("order/placeOrder", sl_payload))
        tp_task = asyncio.ensure_future(self._send_ws_request("order/placeOrder", tp_payload))

        sl_result, tp_result = await asyncio.gather(sl_task, tp_task)

        sl_ok = sl_result is not None and sl_result.get("s") == 200
        tp_ok = tp_result is not None and tp_result.get("s") == 200

        sl_order_id = sl_result.get("d", {}).get("orderId") if sl_ok else None
        tp_order_id = tp_result.get("d", {}).get("orderId") if tp_ok else None

        if not sl_ok:
            log.error("SL order failed: %s — EMERGENCY CLOSE!", sl_result)
            if tp_order_id:
                await self._send_ws_request("order/cancelorder", {"orderId": tp_order_id}, timeout=3.0)
            await self._send_emergency_stop(symbol, position["qty"], opposite)
            return None

        if not tp_ok:
            log.warning("TP order failed — SL still active, guardian armed")

        log.info(
            "Orders placed: SL=%s @ %.2f, TP=%s @ %.2f",
            sl_order_id, actual_sl,
            tp_order_id if tp_ok else "FAILED", actual_tp,
        )

        return {
            "orderId": entry_order_id,
            "slOrderId": sl_order_id,
            "tpOrderId": tp_order_id,
            "fillPrice": actual_fill,
            "sl": actual_sl,
            "tp": actual_tp,
        }

    async def _send_emergency_stop(self, symbol: str, qty: int, action: str):
        log.error("EMERGENCY: Closing position — SL could not be placed!")
        close_payload = {
            "accountSpec": self._auth.account_spec,
            "accountId": self._auth.account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,
        }
        result = await self._send_ws_request("order/placeOrder", close_payload, timeout=5.0)
        if result and result.get("s") == 200:
            log.info("Emergency close executed successfully")
        else:
            log.error("EMERGENCY CLOSE FAILED: %s — MANUAL INTERVENTION REQUIRED!", result)

    async def close_all_positions(self, reason: str):
        if not self._connected.is_set():
            log.warning("Orders WS not connected, can't close positions: %s", reason)
            return

        log.info("Closing all positions: %s", reason)
        payload = {"accountId": self._auth.account_id}
        req_id = self._next_id()
        msg = f"order/liquidatePosition\n{req_id}\n\n{json.dumps(payload)}"
        await self._ws.send(msg)

    def stop(self):
        self._running = False
        if self._ws:
            asyncio.ensure_future(self._ws.close())
        if self._http_session and not self._http_session.closed:
            asyncio.ensure_future(self._http_session.close())
