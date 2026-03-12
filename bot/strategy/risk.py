from __future__ import annotations
import logging
import math

from bot.config import StrategyConfig

log = logging.getLogger("orfvg.risk")


class RiskManager:
    def __init__(self, config: StrategyConfig):
        self._config = config
        self._trading_day: int = 0
        self._equity: float = 0.0

    def set_trading_day(self, day: int):
        self._trading_day = day

    def set_equity(self, equity: float):
        self._equity = equity

    @property
    def risk_pct(self) -> float:
        if self._trading_day <= 1:
            return self._config.risk_day1_pct
        elif self._trading_day == 2:
            return self._config.risk_day2_pct
        elif self._trading_day <= 15:
            return self._config.risk_day3_15_pct
        else:
            return self._config.risk_day16_plus_pct

    def calculate_position(
        self, entry_price: float, sl_price: float, is_long: bool
    ) -> dict | None:
        """Calculate position size. Returns dict with qty/sl/tp/risk or None if can't afford."""
        risk_pts = abs(entry_price - sl_price)
        if risk_pts <= 0:
            return None

        risk_per_contract = risk_pts * self._config.point_value
        budget = self._equity * self.risk_pct
        budget = min(budget, self._config.max_risk_dollars)

        qty = math.ceil(budget / risk_per_contract)
        if qty < 1:
            return None

        if is_long:
            tp_price = entry_price + (risk_pts * self._config.rr_tp2)
        else:
            tp_price = entry_price - (risk_pts * self._config.rr_tp2)

        tp_price = self._snap_to_tick(tp_price)
        sl_price = self._snap_to_tick(sl_price)

        actual_risk = risk_per_contract * qty

        log.info(
            "Position calc: day=%d equity=$%.2f risk_pct=%.0f%% budget=$%.2f "
            "risk/ct=$%.2f qty=%d actual_risk=$%.2f",
            self._trading_day, self._equity, self.risk_pct * 100,
            budget, risk_per_contract, qty, actual_risk,
        )

        return {
            "qty": qty,
            "entry": entry_price,
            "sl": sl_price,
            "tp": tp_price,
            "risk_dollars": actual_risk,
            "is_long": is_long,
        }

    def calculate_position_from_points(
        self, sl_points: float, risk_per_contract: float
    ) -> dict | None:
        """Calculate position size from SL distance in points."""
        if sl_points <= 0 or risk_per_contract <= 0:
            return None

        budget = self._equity * self.risk_pct
        budget = min(budget, self._config.max_risk_dollars)

        qty = math.ceil(budget / risk_per_contract)
        if qty < 1:
            return None

        actual_risk = risk_per_contract * qty

        log.info(
            "Position calc: day=%d equity=$%.2f risk_pct=%.0f%% budget=$%.2f "
            "risk/ct=$%.2f qty=%d actual_risk=$%.2f",
            self._trading_day, self._equity, self.risk_pct * 100,
            budget, risk_per_contract, qty, actual_risk,
        )

        return {
            "qty": qty,
            "risk_dollars": actual_risk,
        }

    def _snap_to_tick(self, price: float) -> float:
        tick = self._config.tick_size
        return round(round(price / tick) * tick, 2)
