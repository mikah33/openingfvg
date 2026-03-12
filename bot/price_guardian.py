from __future__ import annotations
import asyncio
import json
import logging

import websockets

from bot.config import TradovateConfig
from bot.tradovate.auth import TradovateAuth

log = logging.getLogger("orfvg.guardian")


class PriceGuardian:
    """Backup stop loss monitor. Watches real-time price via market data WebSocket
    and force closes position if price breaches SL level.

    This is a safety net — runs independently of Tradovate's stop orders.
    """

    def __init__(self, config: TradovateConfig, auth: TradovateAuth):
        self._config = config
        self._auth = auth
        self._ws = None
        self._running = False
        self._connected = asyncio.Event()

        # Active position tracking
        self._active = False
        self._is_long = False
        self._sl_price = 0.0
        self._qty = 0
        self._symbol = ""
        self._last_price = 0.0
        self._sl_triggered = False

        # Callback to notify when guardian triggers
        self._on_sl_breach = None

    def set_sl_breach_callback(self, callback):
        """Set async callback for when guardian triggers SL breach."""
        self._on_sl_breach = callback

    def set_position(self, symbol: str, is_long: bool, sl_price: float, qty: int):
        """Register an active position to guard."""
        self._symbol = symbol
        self._is_long = is_long
        self._sl_price = sl_price
        self._qty = qty
        self._active = True
        self._sl_triggered = False
        direction = "LONG" if is_long else "SHORT"
        log.info(
            "Guardian ARMED: %s %s x%d, SL=%.2f",
            direction, symbol, qty, sl_price,
        )

    def clear_position(self):
        """Remove position guard (trade closed normally)."""
        if self._active:
            log.info("Guardian DISARMED")
        self._active = False
        self._sl_triggered = False

    async def run(self):
        """Main loop — connects to MD WebSocket and monitors price."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_monitor()
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                log.warning("Guardian MD WS disconnected: %s", e)
                self._connected.clear()
            except Exception as e:
                log.error("Guardian MD WS error: %s", e, exc_info=True)
                self._connected.clear()

            if not self._running:
                break

            await asyncio.sleep(2.0)

    async def _connect_and_monitor(self):
        url = self._config.md_ws_url
        log.info("Guardian connecting to MD WS: %s", url)

        async with websockets.connect(url, ping_interval=None, ping_timeout=None) as ws:
            self._ws = ws

            # Wait for open frame
            msg = await ws.recv()
            if not msg.startswith("o"):
                log.error("Guardian expected open frame, got: %s", msg[:100])
                return

            # Authorize
            auth_msg = f"authorize\n1\n\n{self._auth.token}"
            await ws.send(auth_msg)
            resp = await ws.recv()
            log.debug("Guardian MD auth response: %s", resp[:200])

            self._connected.set()
            log.info("Guardian MD WS connected")

            # Subscribe to quotes for our symbol
            symbol = self._config.symbol if hasattr(self._config, "symbol") else "MESM6"
            sub_msg = f"md/subscribeQuote\n2\n\n{{\"symbol\":\"{symbol}\"}}"
            await ws.send(sub_msg)
            log.info("Guardian subscribed to %s quotes", symbol)

            # Start heartbeat
            heartbeat_task = asyncio.ensure_future(self._heartbeat(ws))

            try:
                async for raw in ws:
                    await self._handle_md_message(raw)
            finally:
                heartbeat_task.cancel()

    async def _heartbeat(self, ws):
        try:
            while True:
                await asyncio.sleep(2.5)
                try:
                    await ws.send("[]")
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_md_message(self, raw: str):
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

            # Extract price from quote data
            data = payload.get("d", payload)
            if isinstance(data, dict):
                # Tradovate MD sends quotes with "entries" containing bid/ask/trade
                entries = data.get("entries", {})

                # Try to get last trade price
                trade = entries.get("Trade", {})
                price = trade.get("price")

                if price is None:
                    # Try bid/ask midpoint
                    bid = entries.get("Bid", {}).get("price")
                    ask = entries.get("Offer", {}).get("price")
                    if bid and ask:
                        price = (bid + ask) / 2.0

                if price and price > 0:
                    self._last_price = price
                    await self._check_sl_breach(price)

    async def _check_sl_breach(self, price: float):
        """Check if current price has breached the stop loss level."""
        if not self._active or self._sl_triggered:
            return

        breached = False
        if self._is_long and price <= self._sl_price:
            breached = True
        elif not self._is_long and price >= self._sl_price:
            breached = True

        if breached:
            self._sl_triggered = True
            direction = "LONG" if self._is_long else "SHORT"
            log.error(
                "GUARDIAN SL BREACH! %s position, price=%.2f hit SL=%.2f — FORCE CLOSING!",
                direction, price, self._sl_price,
            )

            # Force close via liquidate
            if self._on_sl_breach:
                try:
                    await self._on_sl_breach(
                        f"GUARDIAN TRIGGERED: Price {price:.2f} breached SL {self._sl_price:.2f} — force closing!"
                    )
                except Exception as e:
                    log.error("Guardian callback error: %s", e)

    @property
    def last_price(self) -> float:
        return self._last_price

    @property
    def is_armed(self) -> bool:
        return self._active

    def stop(self):
        self._running = False
        if self._ws:
            asyncio.ensure_future(self._ws.close())
