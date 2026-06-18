"""
mean_reversion.py — Mean Reversion strategy using 2-period RSI (Larry Connors).

Identifies extremely oversold large-cap Swedish stocks and buys
expecting a short-term bounce. Exits after RSI recovers or after
a maximum holding period.

Risk dial effect:
  Low risk (0.0)  → only enter below RSI 10, exit above RSI 65, small position
  High risk (1.0) → enter below RSI 25, exit above RSI 75, larger position
"""

import logging
from typing import List

from algorithms.base import BaseAlgorithm, MarketSnapshot, TradeSignal, TradeAction
from trading.tickers import REGISTRY, AssetClass

logger = logging.getLogger(__name__)

# Pull Swedish large-cap equities from the central registry.
# Only individual stocks — not ETFs — are appropriate for mean reversion.
UNIVERSE: list[str] = [
    t for t, i in REGISTRY.items()
    if i.asset_class == AssetClass.EQUITY
]

# RSI parameters
RSI_PERIOD  = 2
MIN_HISTORY = RSI_PERIOD + 10

# Maximum trading days to hold a position before forced exit
MAX_HOLD_DAYS = 5


class MeanReversionAlgorithm(BaseAlgorithm):

    def __init__(self) -> None:
        super().__init__()
        self._holding_days: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "mean_reversion"

    @property
    def display_name(self) -> str:
        return "MEAN REV"

    @property
    def description(self) -> str:
        return (
            "Buys large-cap Swedish stocks when 2-period RSI is "
            "extremely oversold. Exits when RSI recovers."
        )

    @property
    def evaluation_interval_seconds(self) -> int:
        return 3_600   # hourly during market hours

    def risk_description(self, risk_level: float) -> str:
        entry = self._entry_threshold(risk_level)
        exit_ = self._exit_threshold(risk_level)
        size  = self._position_size(1.0, risk_level, max_fraction=0.2)
        return (
            f"Risk {risk_level:.0%}: enter RSI<{entry:.0f}, "
            f"exit RSI>{exit_:.0f}, size {size:.0%}"
        )

    def evaluate(self, snapshot: MarketSnapshot) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        try:
            entry_threshold = self._entry_threshold(snapshot.risk_level)
            exit_threshold  = self._exit_threshold(snapshot.risk_level)
            size_fraction   = self._position_size(
                snapshot.liquid_sek,
                snapshot.risk_level,
                max_fraction=0.20,
                min_fraction=0.05,
            )

            for ticker in UNIVERSE:
                history = snapshot.history.get(ticker, [])
                if not self._validate_history(ticker, history, MIN_HISTORY):
                    continue

                rsi = self._compute_rsi(history, RSI_PERIOD)
                if rsi is None:
                    continue

                current_price = snapshot.prices.get(ticker)
                if current_price is None or current_price <= 0:
                    logger.warning(
                        "MeanReversion: missing/invalid price for %s", ticker
                    )
                    continue

                # ── Exit ─────────────────────────────────────
                if ticker in self._holding_days:
                    self._holding_days[ticker] += 1
                    days_held = self._holding_days[ticker]

                    if rsi > exit_threshold or days_held >= MAX_HOLD_DAYS:
                        reason = (
                            f"RSI {rsi:.1f} > {exit_threshold:.0f}"
                            if rsi > exit_threshold
                            else f"Max hold {MAX_HOLD_DAYS}d reached"
                        )
                        logger.info("MeanReversion: SELL %s — %s", ticker, reason)
                        signals.append(TradeSignal(
                            ticker=ticker,
                            action=TradeAction.SELL,
                            fraction=1.0,
                            confidence=0.85,
                            reason=reason,
                            algorithm=self.name,
                        ))
                        del self._holding_days[ticker]

                # ── Entry ─────────────────────────────────────
                elif rsi < entry_threshold:
                    logger.info(
                        "MeanReversion: BUY %s — RSI %.1f < %.0f",
                        ticker, rsi, entry_threshold,
                    )
                    signals.append(TradeSignal(
                        ticker=ticker,
                        action=TradeAction.BUY,
                        fraction=size_fraction,
                        confidence=1.0 - (rsi / entry_threshold),
                        reason=f"RSI {rsi:.1f} < {entry_threshold:.0f} (oversold)",
                        algorithm=self.name,
                    ))
                    self._holding_days[ticker] = 0

        except Exception:
            logger.exception("MeanReversion: unexpected error during evaluation")
            return []

        return signals

    def on_market_closed(self) -> None:
        logger.debug(
            "MeanReversion: market closed. Active positions: %s",
            list(self._holding_days.keys()),
        )

    # ── Internal ──────────────────────────────────────────────

    def _compute_rsi(self, prices: list, period: int) -> float | None:
        if len(prices) < period + 1:
            return None
        closes = prices[-(period + 5):]
        gains, losses = [], []
        for i in range(1, len(closes)):
            change = closes[i] - closes[i - 1]
            if change > 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(change))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs  = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    def _entry_threshold(self, risk_level: float) -> float:
        return 10.0 + 15.0 * risk_level

    def _exit_threshold(self, risk_level: float) -> float:
        return 65.0 + 10.0 * risk_level