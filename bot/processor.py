import asyncio
import logging
from datetime import datetime

from bot.config import BotConfig
from bot.models import WebhookPayload, Action
from bot.strategy.risk import RiskManager
from bot.tradovate.rest import TradovateREST
from bot.tradovate.ws_orders import OrdersWS
from bot.persistence import PersistedState

log = logging.getLogger("orfvg.processor")


class AlertProcessor:
    def __init__(
        self,
        config: BotConfig,
        queue: asyncio.Queue,
        rest: TradovateREST,
        orders_ws: OrdersWS,
        state: PersistedState,
        notify=None,
    ):
        self._config = config
        self._queue = queue
        self._rest = rest
        self._orders_ws = orders_ws
        self._state = state
        self._risk = RiskManager(config.strategy)
        self._risk.set_trading_day(state.trading_day)
        self._notify = notify  # async callable for Telegram notifications
        self._in_trade = False
        self._running = False

    @property
    def state(self) -> PersistedState:
        return self._state

    @property
    def in_trade(self) -> bool:
        return self._in_trade

    async def run(self):
        self._running = True
        log.info("AlertProcessor started")
        while self._running:
            try:
                payload = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._process(payload)
            except Exception as e:
                log.error("Error processing alert: %s", e, exc_info=True)
                await self._send_notification(f"Error processing alert: {e}")

    def stop(self):
        self._running = False

    async def _process(self, payload: WebhookPayload):
        self._state.last_signal_time = datetime.utcnow().isoformat()

        if payload.is_entry():
            await self._handle_entry(payload)
        elif payload.is_close():
            await self._handle_close(payload)
        else:
            log.warning("Unknown action: %s", payload.action)

    async def _handle_entry(self, payload: WebhookPayload):
        signal = payload.to_trade_signal()
        is_long = signal.action == Action.LONG_ENTRY
        direction = "LONG" if is_long else "SHORT"

        log.info(
            "Entry signal: %s entry=%.2f SL=%.2f TP1=%.2f TP2=%.2f qty=%d",
            direction, signal.entry_price, signal.stop_loss, signal.tp1, signal.tp2, signal.qty,
        )

        # Check if paused
        if self._state.paused:
            msg = f"Signal received ({direction}) but bot is PAUSED — skipping"
            log.info(msg)
            await self._send_notification(msg)
            return

        # Fetch latest equity
        try:
            equity = await self._rest.get_cash_balance()
            self._risk.set_equity(equity)
            log.info("Current equity: $%.2f", equity)
        except Exception as e:
            msg = f"Failed to fetch equity, skipping trade: {e}"
            log.error(msg)
            await self._send_notification(msg)
            return

        # Calculate position using our risk manager
        pos = self._risk.calculate_position(
            entry_price=signal.entry_price,
            sl_price=signal.stop_loss,
            is_long=is_long,
        )

        if pos is None:
            msg = f"{direction} signal but can't afford position — skipping"
            log.warning(msg)
            await self._send_notification(msg)
            return

        total_qty = pos["qty"]
        sl = pos["sl"]

        # Split into TP1 (scale out half) and TP2 (runner)
        if total_qty >= 2:
            qty1 = total_qty // 2       # half rounded down for TP1
            qty2 = total_qty - qty1     # rest for TP2
        else:
            qty1 = 0                    # can't split 1 contract
            qty2 = total_qty

        tp1 = self._risk._snap_to_tick(signal.tp1)
        tp2 = self._risk._snap_to_tick(signal.tp2)

        log.info(
            "Placing %s: total=%d, TP1 x%d @ %.2f, TP2 x%d @ %.2f, SL=%.2f",
            direction, total_qty, qty1, tp1, qty2, tp2, sl,
        )

        success = True

        # Place TP1 bracket (scale out half at 1.4R)
        if qty1 > 0:
            pos1 = {"qty": qty1, "entry": pos["entry"], "sl": sl, "tp": tp1, "is_long": is_long, "risk_dollars": 0}
            r1 = await self._orders_ws.place_bracket_order(pos1)
            if r1 is None:
                log.error("TP1 bracket order failed")
                success = False

        # Place TP2 bracket (runner at 1.85R)
        pos2 = {"qty": qty2, "entry": pos["entry"], "sl": sl, "tp": tp2, "is_long": is_long, "risk_dollars": 0}
        r2 = await self._orders_ws.place_bracket_order(pos2)
        if r2 is None:
            log.error("TP2 bracket order failed")
            success = False

        if success:
            self._in_trade = True
            self._state.daily_trades += 1
            self._state.trading_day += 1
            self._risk.set_trading_day(self._state.trading_day)
            msg = (
                f"ENTERED {direction}\n"
                f"Total Qty: {total_qty}\n"
                f"Entry: {pos['entry']:.2f}\n"
                f"SL: {sl:.2f}\n"
                f"TP1: {tp1:.2f} x{qty1}\n"
                f"TP2: {tp2:.2f} x{qty2}\n"
                f"Risk: ${pos['risk_dollars']:.2f}"
            )
            log.info(msg)
            await self._send_notification(msg)
        else:
            msg = f"Order placement FAILED for {direction} signal"
            log.error(msg)
            await self._send_notification(msg)

    async def _handle_close(self, payload: WebhookPayload):
        signal = payload.to_close_signal()
        reason = signal.reason or "Manual"

        log.info("Close signal: reason=%s", reason)

        await self._orders_ws.close_all_positions(reason)
        self._in_trade = False

        msg = f"CLOSED ALL — {reason}"
        log.info(msg)
        await self._send_notification(msg)

    async def _send_notification(self, message: str):
        if self._notify:
            try:
                await self._notify(message)
            except Exception as e:
                log.error("Failed to send notification: %s", e)
