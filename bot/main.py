import asyncio
import signal
import sys
from pathlib import Path

import uvicorn

# Add parent dir to path so imports work when running from openingfvg/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config
from bot.logger import setup_logger
from bot.persistence import load_state, save_state
from bot.tradovate.auth import TradovateAuth
from bot.tradovate.rest import TradovateREST
from bot.tradovate.ws_orders import OrdersWS
from bot.webhook import create_app
from bot.processor import AlertProcessor
from bot.telegram_bot import TelegramInterface

log = setup_logger()


class Bot:
    def __init__(self, config_path: str = "config.yaml"):
        self._config = load_config(config_path)
        self._auth = TradovateAuth(self._config.tradovate)
        self._rest = TradovateREST(self._config.tradovate, self._auth)
        self._state = load_state()
        self._orders_ws: OrdersWS | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._processor: AlertProcessor | None = None
        self._telegram: TelegramInterface | None = None
        self._shutdown_event = asyncio.Event()

    async def run(self):
        log.info("=" * 50)
        log.info("OR FVG Webhook Bot starting — mode: %s", self._config.tradovate.mode)
        log.info("Symbol: %s | R:R: %.2f", self._config.strategy.symbol, self._config.strategy.rr_ratio)
        log.info("Webhook: /%s on port %d", self._config.webhook.secret_path, self._config.webhook.port)
        log.info("=" * 50)

        # 1. Authenticate with Tradovate
        await self._auth.login()
        await self._auth.fetch_account_info()

        # 2. Fetch initial equity
        equity = await self._rest.get_cash_balance()
        log.info("Account equity: $%.2f", equity)

        # 3. Create orders WebSocket
        self._orders_ws = OrdersWS(
            self._config.tradovate,
            self._auth,
            on_fill=self._on_fill,
        )
        self._config.tradovate.symbol = self._config.strategy.symbol

        # 4. Create Telegram interface
        self._telegram = TelegramInterface(self._config.telegram)

        # 5. Create alert processor
        self._processor = AlertProcessor(
            config=self._config,
            queue=self._queue,
            rest=self._rest,
            orders_ws=self._orders_ws,
            state=self._state,
            notify=self._telegram.send,
        )

        # Wire Telegram to processor and REST
        self._telegram.set_processor(self._processor)
        self._telegram.set_rest(self._rest)

        # 6. Create FastAPI webhook app
        app = create_app(self._config.webhook, self._queue)

        # 7. Create uvicorn server
        uvi_config = uvicorn.Config(
            app,
            host=self._config.webhook.host,
            port=self._config.webhook.port,
            log_level="warning",
        )
        server = uvicorn.Server(uvi_config)

        # 8. Send startup notification
        await self._telegram.send(
            f"Bot started\n"
            f"Mode: {self._config.tradovate.mode}\n"
            f"Equity: ${equity:.2f}\n"
            f"Paused: {self._state.paused}"
        )

        # 9. Run everything concurrently
        log.info("Starting all services...")
        try:
            await asyncio.gather(
                server.serve(),
                self._orders_ws.connect(),
                self._auth.refresh_loop(),
                self._processor.run(),
                self._telegram.start_polling(),
                self._wait_for_shutdown(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

    async def _wait_for_shutdown(self):
        await self._shutdown_event.wait()

    async def _on_fill(self, data: dict):
        log.info("Fill received: %s", data)
        try:
            equity = await self._rest.get_cash_balance()
            log.info("Equity updated: $%.2f", equity)
            if self._telegram:
                await self._telegram.send(f"Fill received — equity now ${equity:.2f}")
        except Exception as e:
            log.error("Failed to refresh equity after fill: %s", e)

    async def _cleanup(self):
        log.info("Shutting down...")

        # Save state
        if self._processor:
            self._state = self._processor.state
        save_state(self._state)
        log.info("State saved (trading_day=%d, paused=%s)", self._state.trading_day, self._state.paused)

        # Stop services
        if self._processor:
            self._processor.stop()
        if self._telegram:
            await self._telegram.stop()
        if self._orders_ws:
            self._orders_ws.stop()
        await self._rest.close()
        await self._auth.close()
        log.info("Bot stopped cleanly")

    def shutdown(self):
        log.info("Shutdown signal received")
        self._shutdown_event.set()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    bot = Bot(config_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Graceful shutdown on Ctrl+C / SIGTERM
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot.shutdown)
    else:
        signal.signal(signal.SIGINT, lambda s, f: bot.shutdown())

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        bot.shutdown()
        loop.run_until_complete(bot._cleanup())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
