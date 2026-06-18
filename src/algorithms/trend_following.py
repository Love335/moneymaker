"""
trend_following.py — Trend Following using 200-day and 50-day SMA.

Stays invested when price is above its long-term trend.
Moves to bonds when the trend breaks down. Uses a confirmation
filter on exit to reduce whipsawing.

Risk dial effect:
  Low risk (0.0)  → exit immediately on trend break, 50% position
  High risk (1.0) → wait 3 days for confirmation, 100% position
"""

import logging
from typing import List

from algorithms.base import BaseAlgorithm, MarketSnapshot, TradeSignal, TradeAction
from trading.tickers import REGISTRY

logger = logging.getLogger(__name__)

# Primary trend asset — Swedish large-cap index ETF
PRIMARY_TICKER = "XACT-OMXS30.ST"

# Safe haven when trend breaks down — Swedish government bonds
BOND_TICKER = "XACT-OBLIGATION.ST"

SMA_LONG    = 200
SMA_SHORT   = 50
MIN_HISTORY = SMA_LONG + 10


class TrendFollowingAlgorithm(BaseAlgorithm):

    def __init__(self) -> None:
        super().__init__()
        self._days_below_trend: int  = 0
        self._in_market:        bool = False

    @property
    def name(self) -> str:
        return "trend_following"

    @property
    def display_name(self) -> str:
        return "TREND   "

    @property
    def description(self) -> str:
        return (
            "Holds OMXS30 ETF when price > 200-day SMA. "
            "Moves to bonds when trend breaks down."
        )

    @property
    def evaluation_interval_seconds(self) -> int:
        return 86_400   # daily

    def risk_description(self, risk_level: float) -> str:
        confirm = self._exit_confirmation_days(risk_level)
        size    = self._position_size(1.0, risk_level, max_fraction=1.0)
        return (
            f"Risk {risk_level:.0%}: {confirm}d exit confirmation, "
            f"{size:.0%} position"
        )

    def evaluate(self, snapshot: MarketSnapshot) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        try:
            history = snapshot.history.get(PRIMARY_TICKER, [])
            if not self._validate_history(PRIMARY_TICKER, history, MIN_HISTORY):
                return []

            sma_long  = self._sma(history, SMA_LONG)
            sma_short = self._sma(history, SMA_SHORT)
            price_now = snapshot.prices.get(PRIMARY_TICKER)

            if price_now is None or price_now <= 0:
                logger.warning(
                    "TrendFollowing: missing/invalid price for %s", PRIMARY_TICKER
                )
                return []

            confirm_days  = self._exit_confirmation_days(snapshot.risk_level)
            size_fraction = self._position_size(
                snapshot.liquid_sek,
                snapshot.risk_level,
                max_fraction=1.0,
                min_fraction=0.5,
            )

            trend_is_up = price_now > sma_long and sma_short > sma_long

            logger.debug(
                "TrendFollowing: price=%.2f SMA50=%.2f SMA200=%.2f up=%s",
                price_now, sma_short, sma_long, trend_is_up,
            )

            if trend_is_up:
                self._days_below_trend = 0
                if not self._in_market:
                    logger.info(
                        "TrendFollowing: BUY %s — price %.2f > SMA200 %.2f",
                        PRIMARY_TICKER, price_now, sma_long,
                    )
                    signals.append(TradeSignal(
                        ticker=PRIMARY_TICKER,
                        action=TradeAction.BUY,
                        fraction=size_fraction,
                        confidence=self._trend_strength(price_now, sma_long),
                        reason=(
                            f"Price {price_now:.2f} > SMA200 {sma_long:.2f}, "
                            f"SMA50 {sma_short:.2f} > SMA200"
                        ),
                        algorithm=self.name,
                    ))
                    self._in_market = True

            else:
                self._days_below_trend += 1
                logger.info(
                    "TrendFollowing: trend broken day %d/%d",
                    self._days_below_trend, confirm_days,
                )

                if self._days_below_trend >= confirm_days and self._in_market:
                    logger.info(
                        "TrendFollowing: SELL %s — %d days below trend",
                        PRIMARY_TICKER, self._days_below_trend,
                    )
                    signals.append(TradeSignal(
                        ticker=PRIMARY_TICKER,
                        action=TradeAction.SELL,
                        fraction=1.0,
                        confidence=0.9,
                        reason=(
                            f"Trend broken {self._days_below_trend}d. "
                            f"Price {price_now:.2f} < SMA200 {sma_long:.2f}"
                        ),
                        algorithm=self.name,
                    ))
                    signals.append(TradeSignal(
                        ticker=BOND_TICKER,
                        action=TradeAction.BUY,
                        fraction=1.0,
                        confidence=0.9,
                        reason="Trend breakdown — moving to bonds",
                        algorithm=self.name,
                    ))
                    self._in_market        = False
                    self._days_below_trend = 0

        except Exception:
            logger.exception("TrendFollowing: unexpected error during evaluation")
            return []

        return signals

    def on_market_opened(self) -> None:
        logger.debug("TrendFollowing: market opened")

    def on_market_closed(self) -> None:
        logger.debug(
            "TrendFollowing: market closed. in_market=%s days_below=%d",
            self._in_market, self._days_below_trend,
        )

    # ── Internal ──────────────────────────────────────────────

    def _sma(self, prices: list, period: int) -> float:
        if len(prices) < period:
            raise ValueError(
                f"Need {period} prices for SMA, have {len(prices)}"
            )
        return sum(prices[-period:]) / period

    def _trend_strength(self, price: float, sma: float) -> float:
        if sma <= 0:
            return 0.5
        return min((price - sma) / sma * 10, 1.0)

    def _exit_confirmation_days(self, risk_level: float) -> int:
        return max(1, round(1 + 2 * risk_level))