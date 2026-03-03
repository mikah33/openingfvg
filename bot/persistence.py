import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

STATE_FILE = "state.json"


@dataclass
class PersistedState:
    trading_day: int = 0
    last_or_date: str = ""  # "2026-03-03" prevents double-counting on restart
    paused: bool = False
    last_signal_time: str = ""
    daily_trades: int = 0


def load_state(path: str = STATE_FILE) -> PersistedState:
    p = Path(path)
    if not p.exists():
        return PersistedState()
    with open(p) as f:
        data = json.load(f)
    return PersistedState(
        trading_day=data.get("trading_day", 0),
        last_or_date=data.get("last_or_date", ""),
        paused=data.get("paused", False),
        last_signal_time=data.get("last_signal_time", ""),
        daily_trades=data.get("daily_trades", 0),
    )


def save_state(state: PersistedState, path: str = STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(state), f, indent=2)
    os.replace(tmp, path)
