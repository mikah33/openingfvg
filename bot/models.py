from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Action(str, Enum):
    LONG_ENTRY = "LONG_ENTRY"
    SHORT_ENTRY = "SHORT_ENTRY"
    CLOSE_ALL = "CLOSE_ALL"


class TradeSignal(BaseModel):
    action: Action
    sl_points: float
    tp1_points: float
    tp2_points: float
    qty: int
    passphrase: str = ""


class CloseSignal(BaseModel):
    action: Action
    reason: str = ""
    passphrase: str = ""


class WebhookPayload(BaseModel):
    """Raw incoming payload — action field determines which model to parse into."""
    action: str
    passphrase: str = ""
    # Point-based offsets (new format)
    sl_points: Optional[float] = None
    tp1_points: Optional[float] = None
    tp2_points: Optional[float] = None
    qty: Optional[int] = None
    reason: Optional[str] = None
    or_high: Optional[float] = None
    or_low: Optional[float] = None

    def is_entry(self) -> bool:
        return self.action in (Action.LONG_ENTRY, Action.SHORT_ENTRY)

    def is_close(self) -> bool:
        return self.action == Action.CLOSE_ALL

    def is_or_identified(self) -> bool:
        return self.action == "OR_IDENTIFIED"

    def to_trade_signal(self) -> TradeSignal:
        return TradeSignal(
            action=Action(self.action),
            sl_points=self.sl_points,
            tp1_points=self.tp1_points,
            tp2_points=self.tp2_points,
            qty=self.qty,
            passphrase=self.passphrase,
        )

    def to_close_signal(self) -> CloseSignal:
        return CloseSignal(
            action=Action(self.action),
            reason=self.reason or "",
            passphrase=self.passphrase,
        )
