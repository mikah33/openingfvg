import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class TradovateConfig:
    username: str
    password: str
    app_id: str
    app_version: str
    cid: int
    sec: str
    device_id: str = ""
    mode: str = "demo"

    @property
    def rest_url(self) -> str:
        if self.mode == "live":
            return "https://live.tradovateapi.com/v1"
        return "https://demo.tradovateapi.com/v1"

    @property
    def md_ws_url(self) -> str:
        if self.mode == "live":
            return "wss://md.tradovateapi.com/v1/websocket"
        return "wss://md-demo.tradovateapi.com/v1/websocket"

    @property
    def orders_ws_url(self) -> str:
        if self.mode == "live":
            return "wss://live.tradovateapi.com/v1/websocket"
        return "wss://demo.tradovateapi.com/v1/websocket"


@dataclass
class StrategyConfig:
    symbol: str = "MESM5"
    timezone: str = "America/New_York"
    or_start: str = "09:30"
    or_end: str = "09:35"
    setup_expiry_minutes: int = 120
    rr_ratio: float = 1.75
    risk_day1_pct: float = 0.33
    risk_day2_pct: float = 0.25
    risk_day3_15_pct: float = 0.15
    risk_day16_plus_pct: float = 0.05
    max_risk_dollars: float = 2500.0
    fvg_max_age_bars: int = 15
    atr_period: int = 14
    fvg_tolerance_atr_mult: float = 0.1
    point_value: float = 5.0
    tick_size: float = 0.25


@dataclass
class WebhookConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    secret_path: str = "webhook"
    passphrase: str = ""


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: int = 0


@dataclass
class BotConfig:
    tradovate: TradovateConfig = field(default_factory=TradovateConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


def load_config(path: str = "config.yaml") -> BotConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yaml to config.yaml and fill in your credentials."
        )

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    tv_raw = raw.get("tradovate", {})
    strat_raw = raw.get("strategy", {})
    wh_raw = raw.get("webhook", {})
    tg_raw = raw.get("telegram", {})

    tv = TradovateConfig(
        username=tv_raw["username"],
        password=tv_raw["password"],
        app_id=tv_raw.get("app_id", "OR_FVG_Bot"),
        app_version=tv_raw.get("app_version", "1.0.0"),
        cid=int(tv_raw["cid"]),
        sec=str(tv_raw["sec"]),
        device_id=tv_raw.get("device_id", ""),
        mode=tv_raw.get("mode", "demo"),
    )

    # Auto-generate device_id if empty
    if not tv.device_id:
        tv.device_id = str(uuid.uuid4())

    strat = StrategyConfig(**{k: v for k, v in strat_raw.items() if v is not None})

    wh = WebhookConfig(**{k: v for k, v in wh_raw.items() if v is not None})

    tg = TelegramConfig(
        bot_token=tg_raw.get("bot_token", ""),
        chat_id=int(tg_raw.get("chat_id", 0)),
    )

    return BotConfig(tradovate=tv, strategy=strat, webhook=wh, telegram=tg)
