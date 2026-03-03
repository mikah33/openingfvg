import asyncio
import logging

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from bot.config import TelegramConfig

log = logging.getLogger("orfvg.telegram")


class TelegramInterface:
    def __init__(self, config: TelegramConfig):
        self._config = config
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._processor = None  # set after init via set_processor
        self._rest = None       # set after init via set_rest
        self._enabled = bool(config.bot_token and config.chat_id)

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

    def _register_handlers(self):
        dp = self._dp

        @dp.message(Command("start", "help"))
        async def cmd_help(message: types.Message):
            if not self._is_authorized(message):
                return
            await message.answer(
                "OR FVG Bot Commands:\n"
                "/status — current bot state\n"
                "/close — close all positions now\n"
                "/pause — pause trading (signals logged but not executed)\n"
                "/resume — resume trading\n"
                "/pnl — today's equity and trade count\n"
                "/help — show this message"
            )

        @dp.message(Command("status"))
        async def cmd_status(message: types.Message):
            if not self._is_authorized(message):
                return
            if not self._processor:
                await message.answer("Processor not ready yet")
                return

            state = self._processor.state
            status = "PAUSED" if state.paused else "ACTIVE"
            in_trade = "Yes" if self._processor.in_trade else "No"
            last = state.last_signal_time or "None"

            await message.answer(
                f"Status: {status}\n"
                f"In trade: {in_trade}\n"
                f"Trading day: {state.trading_day}\n"
                f"Daily trades: {state.daily_trades}\n"
                f"Last signal: {last}"
            )

        @dp.message(Command("close"))
        async def cmd_close(message: types.Message):
            if not self._is_authorized(message):
                return
            if not self._processor:
                await message.answer("Processor not ready yet")
                return

            from bot.tradovate.ws_orders import OrdersWS
            await self._processor._orders_ws.close_all_positions("Manual Telegram /close")
            self._processor._in_trade = False
            await message.answer("Close all positions sent")

        @dp.message(Command("pause"))
        async def cmd_pause(message: types.Message):
            if not self._is_authorized(message):
                return
            if not self._processor:
                await message.answer("Processor not ready yet")
                return

            self._processor.state.paused = True
            await message.answer("Trading PAUSED — signals will be logged but not executed")

        @dp.message(Command("resume"))
        async def cmd_resume(message: types.Message):
            if not self._is_authorized(message):
                return
            if not self._processor:
                await message.answer("Processor not ready yet")
                return

            self._processor.state.paused = False
            await message.answer("Trading RESUMED — signals will be executed")

        @dp.message(Command("pnl"))
        async def cmd_pnl(message: types.Message):
            if not self._is_authorized(message):
                return

            if self._rest:
                try:
                    equity = await self._rest.get_cash_balance()
                    trades = self._processor.state.daily_trades if self._processor else 0
                    await message.answer(
                        f"Equity: ${equity:.2f}\n"
                        f"Daily trades: {trades}"
                    )
                except Exception as e:
                    await message.answer(f"Error fetching equity: {e}")
            else:
                await message.answer("REST client not ready yet")

    def _is_authorized(self, message: types.Message) -> bool:
        return message.chat.id == self._config.chat_id

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
            # Keep coroutine alive so gather doesn't exit
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
