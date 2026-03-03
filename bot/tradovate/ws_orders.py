import asyncio
import json
import logging
from typing import Callable, Awaitable

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
        self._running = False
        self._reconnect_delay = 1.0
        self._connected = asyncio.Event()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

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

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0

            # Wait for open frame
            msg = await ws.recv()
            if not msg.startswith("o"):
                log.error("Expected open frame, got: %s", msg[:100])
                return

            # Authorize
            auth_msg = f"authorize\n{self._next_id()}\n\n{self._auth.token}"
            await ws.send(auth_msg)
            resp = await ws.recv()
            log.debug("Orders auth response: %s", resp[:200])

            self._connected.set()
            log.info("Orders WS connected and authorized")

            async for raw in ws:
                await self._handle_message(raw)

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

            # Check for fill notifications
            if payload.get("e") == "props" and payload.get("d", {}).get("entityType") == "executionReport":
                if self._on_fill:
                    try:
                        await self._on_fill(payload.get("d", {}))
                    except Exception as e:
                        log.error("Error in fill callback: %s", e)

    async def place_bracket_order(self, position: dict) -> dict | None:
        """Place an OSO bracket order (entry + SL + TP).

        Args:
            position: dict with keys: qty, entry, sl, tp, is_long, risk_dollars
        """
        await self._connected.wait()

        action = "Buy" if position["is_long"] else "Sell"
        opposite = "Sell" if position["is_long"] else "Buy"

        payload = {
            "accountSpec": self._auth.account_spec,
            "accountId": self._auth.account_id,
            "action": action,
            "symbol": self._config.symbol if hasattr(self._config, "symbol") else "MESM5",
            "orderQty": position["qty"],
            "orderType": "Market",
            "isAutomated": True,
        }

        # OSO brackets: SL (stop) and TP (limit)
        bracket_payload = {
            "action": action,
            "symbol": payload["symbol"],
            "orderQty": position["qty"],
            "orderType": "Market",
            "isAutomated": True,
            "bracket1": {
                "action": opposite,
                "orderType": "Stop",
                "stopPrice": position["sl"],
            },
            "bracket2": {
                "action": opposite,
                "orderType": "Limit",
                "price": position["tp"],
            },
        }

        req_id = self._next_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future

        msg = f"order/placeOSO\n{req_id}\n\n{json.dumps(bracket_payload)}"
        log.info(
            "Placing %s bracket: qty=%d SL=%.2f TP=%.2f",
            action, position["qty"], position["sl"], position["tp"],
        )
        await self._ws.send(msg)

        try:
            result = await asyncio.wait_for(future, timeout=10.0)
            self._pending.pop(req_id, None)
            if "s" in result and result["s"] == 200:
                log.info("Order placed successfully: %s", result.get("d", {}))
                return result.get("d")
            else:
                log.error("Order failed: %s", result)
                return None
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            log.error("Order placement timed out")
            return None

    async def close_all_positions(self, reason: str):
        """Cancel all working orders and flatten positions."""
        if not self._connected.is_set():
            log.warning("Orders WS not connected, can't close positions: %s", reason)
            return

        log.info("Closing all positions: %s", reason)
        # Use liquidatePosition endpoint
        payload = {
            "accountId": self._auth.account_id,
        }
        req_id = self._next_id()
        msg = f"order/liquidatePosition\n{req_id}\n\n{json.dumps(payload)}"
        await self._ws.send(msg)

    def stop(self):
        self._running = False
        if self._ws:
            asyncio.ensure_future(self._ws.close())
