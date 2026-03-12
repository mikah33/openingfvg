from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from bot.config import TelegramConfig

log = logging.getLogger("orfvg.telegram")


class TelegramInterface:
    def __init__(self, config: TelegramConfig):
        self._config = config
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._processor = None
        self._rest = None
        self._enabled = bool(config.bot_token and config.chat_id)
        self._start_time = datetime.now(timezone.utc)

        if not self._enabled:
            log.warning("Telegram not configured — notifications disabled")
            return

        self._bot = Bot(token=config.bot_token)
        self._dp = Dispatcher()
        self._register_handlers()

    def set_processor(self, processor):
        self._processor = processor

    def set_rest(self, rest):
        self._rest = rest

    # ── Keyboards ──────────────────────────────────────────────────

    def _main_menu(self) -> InlineKeyboardMarkup:
        state = self._processor.state if self._processor else None
        paused = state.paused if state else False
        in_trade = self._processor.in_trade if self._processor else False

        toggle_btn = InlineKeyboardButton(
            text="▶️ Resume" if paused else "⏸ Pause",
            callback_data="resume" if paused else "pause",
        )

        rows = [
            [InlineKeyboardButton(text="📊 Dashboard", callback_data="dashboard")],
            [
                InlineKeyboardButton(text="💰 Balance", callback_data="balance"),
                InlineKeyboardButton(text="📈 Positions", callback_data="positions"),
            ],
            [
                toggle_btn,
                InlineKeyboardButton(text="🔴 Force Close", callback_data="close_confirm"),
            ],
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="refresh")],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _close_confirm_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yes, close all", callback_data="close_yes"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="close_no"),
            ]
        ])

    # ── Dashboard Text ─────────────────────────────────────────────

    async def _dashboard_text(self) -> str:
        state = self._processor.state if self._processor else None
        paused = state.paused if state else False
        in_trade = self._processor.in_trade if self._processor else False

        status_icon = "🔴 PAUSED" if paused else "🟢 ACTIVE"
        trade_icon = "📈 IN TRADE" if in_trade else "⏳ Waiting"

        equity_str = "—"
        if self._rest:
            try:
                equity = await self._rest.get_cash_balance()
                equity_str = f"${equity:,.2f}"
            except Exception:
                equity_str = "Error"

        uptime = datetime.now(timezone.utc) - self._start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m"

        daily_trades = state.daily_trades if state else 0
        last_signal = state.last_signal_time if state else "None"
        if last_signal and last_signal != "None":
            last_signal = last_signal[:19]  # trim to readable

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "    🤖 OR FVG Bot v11",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            f"  Status:     {status_icon}",
            f"  Position:   {trade_icon}",
            f"  Balance:    {equity_str}",
            f"  Uptime:     {uptime_str}",
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"  Trades today:  {daily_trades}",
            f"  Last signal:   {last_signal}",
            f"  Updated:       {now_str}",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        return "\n".join(lines)

    # ── Handlers ───────────────────────────────────────────────────

    def _register_handlers(self):
        dp = self._dp

        @dp.message(Command("start", "help"))
        async def cmd_help(message: types.Message):
            if not self._is_authorized(message):
                return
            text = await self._dashboard_text()
            await message.answer(text, reply_markup=self._main_menu())

        @dp.message(Command("menu"))
        async def cmd_menu(message: types.Message):
            if not self._is_authorized(message):
                return
            text = await self._dashboard_text()
            await message.answer(text, reply_markup=self._main_menu())

        @dp.message(Command("status"))
        async def cmd_status(message: types.Message):
            if not self._is_authorized(message):
                return
            text = await self._dashboard_text()
            await message.answer(text, reply_markup=self._main_menu())

        @dp.message(Command("close"))
        async def cmd_close(message: types.Message):
            if not self._is_authorized(message):
                return
            await message.answer(
                "⚠️ Are you sure you want to close ALL positions?",
                reply_markup=self._close_confirm_kb(),
            )

        @dp.message(Command("pause"))
        async def cmd_pause(message: types.Message):
            if not self._is_authorized(message):
                return
            if self._processor:
                self._processor.state.paused = True
                await message.answer("⏸ Trading PAUSED", reply_markup=self._main_menu())

        @dp.message(Command("resume"))
        async def cmd_resume(message: types.Message):
            if not self._is_authorized(message):
                return
            if self._processor:
                self._processor.state.paused = False
                await message.answer("▶️ Trading RESUMED", reply_markup=self._main_menu())

        @dp.message(Command("pnl", "balance"))
        async def cmd_pnl(message: types.Message):
            if not self._is_authorized(message):
                return
            if self._rest:
                try:
                    equity = await self._rest.get_cash_balance()
                    await message.answer(f"💰 Balance: ${equity:,.2f}")
                except Exception as e:
                    await message.answer(f"Error: {e}")

        # ── Callback Queries (button presses) ──────────────────────

        @dp.callback_query(lambda c: c.data == "dashboard")
        async def cb_dashboard(callback: CallbackQuery):
            if not self._is_authorized_cb(callback):
                return
            text = await self._dashboard_text()
            await callback.message.edit_text(text, reply_markup=self._main_menu())
            await callback.answer()

        @dp.callback_query(lambda c: c.data == "balance")
        async def cb_balance(callback: CallbackQuery):
            if not self._is_authorized_cb(callback):
                return
            if self._rest:
                try:
                    equity = await self._rest.get_cash_balance()
                    await callback.answer(f"Balance: ${equity:,.2f}", show_alert=True)
                except Exception as e:
                    await callback.answer(f"Error: {e}", show_alert=True)
            else:
                await callback.answer("Not ready", show_alert=True)

        @dp.callback_query(lambda c: c.data == "positions")
        async def cb_positions(callback: CallbackQuery):
            if not self._is_authorized_cb(callback):
                return
            if not self._rest:
                await callback.answer("Not ready", show_alert=True)
                return

            try:
                positions = await self._rest.get_positions()
                if not positions:
                    await callback.answer("No open positions", show_alert=True)
                    return

                lines = ["📈 Open Positions:\n"]
                for pos in positions:
                    name = pos.get("contractId", "?")
                    qty = pos.get("netPos", 0)
                    price = pos.get("netPrice", 0)
                    direction = "LONG" if qty > 0 else "SHORT"
                    lines.append(
                        f"  {direction} x{abs(qty)} @ {price:.2f}"
                    )

                back_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Back", callback_data="dashboard")]
                ])
                await callback.message.edit_text(
                    "\n".join(lines), reply_markup=back_kb
                )
            except Exception as e:
                await callback.answer(f"Error: {e}", show_alert=True)
            await callback.answer()

        @dp.callback_query(lambda c: c.data == "pause")
        async def cb_pause(callback: CallbackQuery):
            if not self._is_authorized_cb(callback):
                return
            if self._processor:
                self._processor.state.paused = True
            text = await self._dashboard_text()
            await callback.message.edit_text(text, reply_markup=self._main_menu())
            await callback.answer("⏸ Paused")

        @dp.callback_query(lambda c: c.data == "resume")
        async def cb_resume(callback: CallbackQuery):
            if not self._is_authorized_cb(callback):
                return
            if self._processor:
                self._processor.state.paused = False
            text = await self._dashboard_text()
            await callback.message.edit_text(text, reply_markup=self._main_menu())
            await callback.answer("▶️ Resumed")

        @dp.callback_query(lambda c: c.data == "close_confirm")
        async def cb_close_confirm(callback: CallbackQuery):
            if not self._is_authorized_cb(callback):
                return
            await callback.message.edit_text(
                "⚠️ Are you sure you want to CLOSE ALL positions?",
                reply_markup=self._close_confirm_kb(),
            )
            await callback.answer()

        @dp.callback_query(lambda c: c.data == "close_yes")
        async def cb_close_yes(callback: CallbackQuery):
            if not self._is_authorized_cb(callback):
                return
            if self._processor:
                await self._processor._orders_ws.close_all_positions("Manual Telegram Close")
                self._processor._in_trade = False
            text = "✅ All positions closed"
            await callback.message.edit_text(text, reply_markup=self._main_menu())
            await callback.answer("Positions closed")

        @dp.callback_query(lambda c: c.data == "close_no")
        async def cb_close_no(callback: CallbackQuery):
            if not self._is_authorized_cb(callback):
                return
            text = await self._dashboard_text()
            await callback.message.edit_text(text, reply_markup=self._main_menu())
            await callback.answer("Cancelled")

        @dp.callback_query(lambda c: c.data == "refresh")
        async def cb_refresh(callback: CallbackQuery):
            if not self._is_authorized_cb(callback):
                return
            text = await self._dashboard_text()
            await callback.message.edit_text(text, reply_markup=self._main_menu())
            await callback.answer("Refreshed")

    def _is_authorized(self, message: types.Message) -> bool:
        return message.chat.id == self._config.chat_id

    def _is_authorized_cb(self, callback: CallbackQuery) -> bool:
        return callback.message.chat.id == self._config.chat_id

    async def send(self, text: str):
        if not self._enabled or not self._bot:
            return
        try:
            await self._bot.send_message(chat_id=self._config.chat_id, text=text)
        except Exception as e:
            log.error("Failed to send Telegram message: %s", e)

    async def start_polling(self):
        if not self._enabled:
            log.info("Telegram disabled, skipping polling")
            while True:
                await asyncio.sleep(3600)
            return

        log.info("Starting Telegram bot polling")
        try:
            await self._dp.start_polling(self._bot)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Telegram polling error: %s", e, exc_info=True)

    async def stop(self):
        if self._dp:
            await self._dp.stop_polling()
        if self._bot:
            await self._bot.session.close()
