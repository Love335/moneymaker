"""
dual_momentum.py — Dual Momentum strategy (Gary Antonacci).

Combines absolute momentum (does the asset beat cash?) with
relative momentum (which asset is strongest?). Evaluates monthly.

Risk dial effect:
  Low risk (0.0)  → hold 1 asset, only enter if momentum is strong
  High risk (1.0) → hold top 3 assets, enter on moderate momentum
"""

import logging
from typing import List

from algorithms.base import (
    BaseAlgorithm,
    MarketSnapshot,
    TradeSignal,
    TradeAction,
)

logger = logging.getLogger(__name__)

# Assets evaluated for relative momentum
# Mix of Swedish, global, bond, and gold ETFs available on Avanza
CANDIDATE_TICKERS = [
    "XACT-OMXS30.ST",   # Swedish large cap
    "XACT-BULL.ST",     # Swedish bull ETF
    "XACT-OBL.ST",      # Swedish government bonds
    "GOLD.ST",          # Gold ETF
    "SPY",              # S&P 500 (via Avanza international)
]

# Minimum annualised return to prefer over cash (proxy for risk-free rate ~4%)
ABSOLUTE_MOMENTUM_THRESHOLD = 0.04

# How many months of history we need (13 = 12-month return skipping last month)
REQUIRED_MONTHS = 13

# Safe haven when no asset clears absolute momentum
SAFE_HAVEN_TICKER = "XACT-OBL.ST"


class DualMomentumAlgorithm(BaseAlgorithm):

    @property
    def name(self) -> str:
        return "dual_momentum"

    @property
    def display_name(self) -> str:
        return "DUAL MOM"

    @property
    def description(self) -> str:
        return (
            "Ranks assets by 12-1 month return. Holds winners only if "
            "they beat the risk-free rate; otherwise moves to bonds."
        )

    @property
    def evaluation_interval_seconds(self) -> int:
        # Evaluate once per day — actual rebalancing is monthly,
        # but we check daily so we never miss a signal
        return 86_400

    def risk_description(self, risk_level: float) -> str:
        n = self._assets_to_hold(risk_level)
        return (
            f"Risk {risk_level:.0%}: hold top {n} asset(s), "
            f"threshold {self._threshold(risk_level):.0%} abs. momentum"
        )

    def evaluate(self, snapshot: MarketSnapshot) -> List[TradeSignal]:
        signals: List[TradeSignal] = []

        try:
            scores = self._score_assets(snapshot)
            if not scores:
                logger.warning("DualMomentum: no scoreable assets, holding")
                return []

            n_assets = self._assets_to_hold(snapshot.risk_level)
            threshold = self._threshold(snapshot.risk_level)

            # Relative momentum: rank by score descending
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            winners = [t for t, s in ranked[:n_assets] if s >= threshold]

            if not winners:
                # Absolute momentum fails — move to safe haven
                logger.info(
                    "DualMomentum: no asset clears threshold %.2f%%, "
                    "routing to safe haven %s",
                    threshold * 100, SAFE_HAVEN_TICKER
                )
                signals.append(TradeSignal(
                    ticker=SAFE_HAVEN_TICKER,
                    action=TradeAction.BUY,
                    fraction=1.0,
                    confidence=0.9,
                    reason=f"No asset beats {threshold:.0%} threshold; safe haven",
                    algorithm=self.name,
                ))
            else:
                fraction_each = round(1.0 / len(winners), 3)
                for ticker in winners:
                    score = scores[ticker]
                    logger.info(
                        "DualMomentum: BUY %s score=%.2f%% fraction=%.1f%%",
                        ticker, score * 100, fraction_each * 100
                    )
                    signals.append(TradeSignal(
                        ticker=ticker,
                        action=TradeAction.BUY,
                        fraction=fraction_each,
                        confidence=min(score / 0.3, 1.0),  # normalise
                        reason=f"12-1mo return {score:.1%} beats threshold",
                        algorithm=self.name,
                    ))

        except Exception:
            logger.exception("DualMomentum: unexpected error during evaluation")
            return []

        return signals

    # ── Internal helpers ──────────────────────────────────────

    def _score_assets(self, snapshot: MarketSnapshot) -> dict:
        """Compute 12-1 month momentum score for each candidate."""
        scores = {}
        for ticker in CANDIDATE_TICKERS:
            history = snapshot.history.get(ticker, [])
            if not self._validate_history(ticker, history, REQUIRED_MONTHS):
                continue
            # 12-1 month return: skip the most recent month to avoid reversal
            price_now       = history[-2]    # 1 month ago
            price_12mo_ago  = history[-13]   # 12 months ago
            if price_12mo_ago <= 0:
                logger.warning("DualMomentum: zero/negative price for %s", ticker)
                continue
            score = (price_now - price_12mo_ago) / price_12mo_ago
            scores[ticker] = score
        return scores

    def _assets_to_hold(self, risk_level: float) -> int:
        """Low risk = 1 asset, high risk = 3 assets."""
        if risk_level < 0.34:
            return 1
        if risk_level < 0.67:
            return 2
        return 3

    def _threshold(self, risk_level: float) -> float:
        """Low risk = strict threshold, high risk = lenient."""
        # 0.0 risk → 8% threshold, 1.0 risk → 2% threshold
        return ABSOLUTE_MOMENTUM_THRESHOLD + (0.08 - 0.02) * (1.0 - risk_level)