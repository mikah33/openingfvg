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
        guardian=None,
    ):
        self._config = config
        self._queue = queue
        self._rest = rest
        self._orders_ws = orders_ws
        self._state = state
        self._risk = RiskManager(config.strategy)
        self._risk.set_trading_day(state.trading_day)
        self._notify = notify  # async callable for Telegram notifications
        self._guardian = guardian  # PriceGuardian backup SL monitor
        self._in_trade = False
        self._running = False
        self._last_entry_time = None  # dedup: timestamp of last entry signal
        self._dedup_window = 60  # ignore duplicate entry signals within 60 seconds

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

        if payload.is_or_identified():
            await self._handle_or_identified(payload)
        elif payload.is_entry():
            await self._handle_entry(payload)
        elif payload.is_close():
            await self._handle_close(payload)
        else:
            log.warning("Unknown action: %s", payload.action)

    async def _handle_or_identified(self, payload: WebhookPayload):
        from datetime import datetime
        today = datetime.now().strftime("%B %d, %Y")
        or_range = (payload.or_high or 0) - (payload.or_low or 0)
        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"  📐 Opening Range Identified\n"
            f"  📅 {today}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"  High:   {payload.or_high:.2f}\n"
            f"  Low:    {payload.or_low:.2f}\n"
            f"  Range:  {or_range:.2f} pts\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"  Scanning for FVG + breakout..."
        )
        log.info("OR identified: High=%.2f Low=%.2f Range=%.2f", payload.or_high, payload.or_low, or_range)
        await self._send_notification(msg)

    async def _handle_entry(self, payload: WebhookPayload):
        signal = payload.to_trade_signal()
        is_long = signal.action == Action.LONG_ENTRY
        direction = "LONG" if is_long else "SHORT"

        log.info(
            "Entry signal: %s SL=%.2f pts, TP1=%.2f pts, TP2=%.2f pts",
            direction, signal.sl_points, signal.tp1_points, signal.tp2_points,
        )

        # Dedup: ignore duplicate entry signals within window
        now = datetime.utcnow()
        if self._last_entry_time is not None:
            elapsed = (now - self._last_entry_time).total_seconds()
            if elapsed < self._dedup_window:
                log.warning("DUPLICATE signal ignored (%.1fs since last entry)", elapsed)
                return
        self._last_entry_time = now

        # Block if already in a trade
        if self._in_trade:
            msg = f"Signal received ({direction}) but ALREADY IN TRADE — skipping"
            log.info(msg)
            await self._send_notification(msg)
            return

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

        # Calculate position size from SL points
        sl_points = signal.sl_points
        risk_per_contract = sl_points * self._config.strategy.point_value
        pos = self._risk.calculate_position_from_points(
            sl_points=sl_points,
            risk_per_contract=risk_per_contract,
        )

        if pos is None:
            msg = f"{direction} signal but can't afford position — skipping"
            log.warning(msg)
            await self._send_notification(msg)
            return

        total_qty = pos["qty"]

        # Split into TP1 (scale out half) and TP2 (runner)
        if total_qty >= 2:
            qty1 = total_qty // 2
            qty2 = total_qty - qty1
        else:
            qty1 = 0
            qty2 = total_qty

        log.info(
            "Placing %s: total=%d, TP1 x%d (%.2f pts), TP2 x%d (%.2f pts), SL=%.2f pts",
            direction, total_qty, qty1, signal.tp1_points, qty2, signal.tp2_points, sl_points,
        )

        # Build bracket positions
        brackets = []
        if qty1 > 0:
            brackets.append({
                "qty": qty1, "sl_points": sl_points, "tp_points": signal.tp1_points,
                "is_long": is_long, "label": "TP1",
            })
        brackets.append({
            "qty": qty2, "sl_points": sl_points, "tp_points": signal.tp2_points,
            "is_long": is_long, "label": "TP2",
        })

        # Place all brackets in parallel
        tasks = [self._orders_ws.place_bracket_order(b) for b in brackets]
        results = await asyncio.gather(*tasks)

        success = all(r is not None for r in results)
        actual_fill = None
        actual_sl = 0.0
        actual_tp1 = 0.0
        actual_tp2 = 0.0

        for i, r in enumerate(results):
            if r is None:
                continue
            actual_fill = r.get("fillPrice", actual_fill)
            actual_sl = r.get("sl", actual_sl)
            if i == 0 and qty1 > 0:
                actual_tp1 = r.get("tp", 0)
            else:
                actual_tp2 = r.get("tp", 0)

        if success and actual_fill:
            self._in_trade = True
            self._state.daily_trades += 1
            self._state.trading_day += 1
            self._risk.set_trading_day(self._state.trading_day)

            # Arm the price guardian as backup SL
            if self._guardian:
                self._guardian.set_position(
                    symbol=self._config.strategy.symbol,
                    is_long=is_long,
                    sl_price=actual_sl,
                    qty=total_qty,
                )

            msg = (
                f"ENTERED {direction}\n"
                f"Total Qty: {total_qty}\n"
                f"Fill: {actual_fill:.2f}\n"
                f"SL: {actual_sl:.2f} ({sl_points:.2f} pts)\n"
                f"TP1: {actual_tp1:.2f} x{qty1}\n"
                f"TP2: {actual_tp2:.2f} x{qty2}\n"
                f"Risk: ${pos['risk_dollars']:.2f}\n"
                f"Guardian: ARMED"
            )
            log.info(msg)
            await self._send_notification(msg)
        else:
            msg = f"Order placement FAILED for {direction} signal — emergency close triggered if entry filled"
            log.error(msg)
            await self._send_notification(msg)

    async def _handle_close(self, payload: WebhookPayload):
        signal = payload.to_close_signal()
        reason = signal.reason or "Manual"

        log.info("Close signal: reason=%s", reason)

        await self._orders_ws.close_all_positions(reason)
        self._in_trade = False

        # Disarm guardian
        if self._guardian:
            self._guardian.clear_position()

        msg = f"CLOSED ALL — {reason}"
        log.info(msg)
        await self._send_notification(msg)

    async def _send_notification(self, message: str):
        if self._notify:
            try:
                await self._notify(message)
            except Exception as e:
                log.error("Failed to send notification: %s", e)
