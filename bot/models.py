from enum import Enum

from pydantic import BaseModel, Field


class Action(str, Enum):
    LONG_ENTRY = "LONG_ENTRY"
    SHORT_ENTRY = "SHORT_ENTRY"
    CLOSE_ALL = "CLOSE_ALL"


class TradeSignal(BaseModel):
    action: Action
    entry_price: float
    stop_loss: float
    take_profit: float
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
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    qty: int | None = None
    reason: str | None = None

    def is_entry(self) -> bool:
        return self.action in (Action.LONG_ENTRY, Action.SHORT_ENTRY)

    def is_close(self) -> bool:
        return self.action == Action.CLOSE_ALL

    def to_trade_signal(self) -> TradeSignal:
        return TradeSignal(
            action=Action(self.action),
            entry_price=self.entry_price,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            qty=self.qty,
            passphrase=self.passphrase,
        )

    def to_close_signal(self) -> CloseSignal:
        return CloseSignal(
            action=Action(self.action),
            reason=self.reason or "",
            passphrase=self.passphrase,
        )
