"""
base.py — Abstract base class that all trading algorithms must implement.

Every algorithm receives the same inputs and must return the same
output type. The engine never knows which algorithm it is running —
it only calls this interface.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional

logger = logging.getLogger(__name__)


class TradeAction(Enum):
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TradeSignal:
    """
    A recommendation produced by an algorithm.

    ticker:      the stock or ETF identifier (e.g. "ERIC-B.ST")
    action:      BUY, SELL, or HOLD
    fraction:    what fraction of available capital to deploy (0.0–1.0)
    confidence:  algorithm's confidence in this signal (0.0–1.0)
    reason:      human-readable explanation logged and shown on display
    algorithm:   name of the algorithm that produced this signal
    """
    ticker:    str
    action:    TradeAction
    fraction:  float        # 0.0–1.0 of available capital
    confidence: float       # 0.0–1.0
    reason:    str
    algorithm: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.fraction <= 1.0:
            raise ValueError(
                f"Signal fraction must be 0.0–1.0, got {self.fraction}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"Signal confidence must be 0.0–1.0, got {self.confidence}"
            )


@dataclass
class MarketSnapshot:
    """
    A validated snapshot of market data passed to algorithms.

    prices:       dict of ticker → current price in SEK
    history:      dict of ticker → list of historical closing prices (oldest first)
    liquid_sek:   available cash in SEK for new positions
    risk_level:   current risk dial setting (0.0–1.0)
    """
    prices:     dict   # ticker → float
    history:    dict   # ticker → list[float]
    liquid_sek: float
    risk_level: float
    total_value_sek: float = 0.0   # cash + market value of all positions


class BaseAlgorithm(ABC):
    """
    Abstract base class for all trading algorithms.

    Concrete subclasses must implement all abstract methods.
    The engine will call evaluate() at the appropriate interval.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger(
            f"{__name__}.{self.__class__.__name__}"
        )

    # ── Required interface ────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Short machine-readable name. e.g. 'dual_momentum'"""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for display. Max 8 chars. e.g. 'DUAL MOM'"""

    @property
    @abstractmethod
    def description(self) -> str:
        """One sentence description of the strategy."""

    @property
    @abstractmethod
    def evaluation_interval_seconds(self) -> int:
        """
        How often the engine should call evaluate().
        e.g. 3600 for hourly, 86400 for daily.
        """

    @abstractmethod
    def evaluate(self, snapshot: MarketSnapshot) -> List[TradeSignal]:
        """
        Core algorithm logic. Receives a market snapshot and returns
        a list of trade signals (may be empty if no action is warranted).

        Must never raise — catch all internal errors and log them.
        Return an empty list if evaluation cannot be completed safely.
        """

    @abstractmethod
    def risk_description(self, risk_level: float) -> str:
        """
        Explain what the given risk level means for this specific algorithm.
        Shown on display when the user adjusts the dial.
        Max 80 characters.
        """

    # ── Optional overrides ────────────────────────────────────

    def on_trade_executed(self, signal: TradeSignal, success: bool) -> None:
        """
        Called after a trade based on this algorithm's signal is executed.
        Override to update internal state if the algorithm needs it.
        """

    def on_market_closed(self) -> None:
        """Called when the market closes. Override to reset daily state."""

    def on_market_opened(self) -> None:
        """Called when the market opens. Override to prepare for the day."""

    # ── Shared helpers ────────────────────────────────────────

    def _position_size(
        self,
        liquid_sek: float,
        risk_level: float,
        max_fraction: float = 1.0,
        min_fraction: float = 0.1
    ) -> float:
        """
        Calculate position size as a fraction of available capital.

        risk_level 0.0 → min_fraction of capital
        risk_level 1.0 → max_fraction of capital
        """
        fraction = min_fraction + (max_fraction - min_fraction) * risk_level
        return round(min(max(fraction, min_fraction), max_fraction), 3)

    def _validate_history(
        self,
        ticker: str,
        history: list,
        min_periods: int
    ) -> bool:
        """
        Return True if there is enough history to evaluate.
        Logs a warning and returns False otherwise.
        """
        if len(history) < min_periods:
            self._logger.warning(
                "%s: insufficient history for %s "
                "(have %d, need %d)",
                self.name, ticker, len(history), min_periods
            )
            return False
        return True