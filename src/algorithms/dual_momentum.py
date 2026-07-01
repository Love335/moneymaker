"""
dual_momentum.py — Dual Momentum strategy (Gary Antonacci).

Combines absolute momentum (does the asset beat cash?) with
relative momentum (which asset is strongest?). Evaluates daily
but only rebalances when signals change.

Risk dial effect:
  Low risk (0.0)  → hold 1 asset, strict momentum threshold
  High risk (1.0) → hold top 3 assets, lenient threshold
"""

import logging
from typing import List

from algorithms.base import BaseAlgorithm, MarketSnapshot, TradeSignal, TradeAction
from trading.tickers import REGISTRY, AssetClass

logger = logging.getLogger(__name__)

# Pull candidate tickers from the central registry by asset class.
# All of these have verified Avanza orderbook IDs and yfinance symbols.
CANDIDATE_TICKERS: list[str] = [
    t for t, i in REGISTRY.items()
    if i.asset_class in (
        AssetClass.ETF_EQUITY,
        AssetClass.ETF_BOND,
        AssetClass.ETF_GOLD,
        AssetClass.ETF_BULL,
    )
]

# Safe haven when no asset clears absolute momentum threshold
SAFE_HAVEN_TICKER = "XACT-OBLIGATION.ST"

# Proxy for risk-free rate (~4% annual)
ABSOLUTE_MOMENTUM_THRESHOLD = 0.04

# How many monthly data points we need (13 = 12-month return, skip last month)
REQUIRED_MONTHS = 13


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
        return 86_400   # daily check, monthly rebalancing signal

    def risk_description(self, risk_level: float) -> str:
        n         = self._assets_to_hold(risk_level)
        threshold = self._threshold(risk_level)
        return (
            f"Risk {risk_level:.0%}: top {n} asset(s), "
            f"threshold {threshold:.0%}"
        )

    def evaluate(self, snapshot: MarketSnapshot) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        try:
            scores = self._score_assets(snapshot)
            if not scores:
                logger.warning("DualMomentum: no scoreable assets, holding")
                return []

            n_assets  = self._assets_to_hold(snapshot.risk_level)
            threshold = self._threshold(snapshot.risk_level)

            ranked  = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            winners = [t for t, s in ranked[:n_assets] if s >= threshold]

            if not winners:
                winners = [SAFE_HAVEN_TICKER]
                reason  = f"No asset beats {threshold:.0%} threshold; safe haven"
                logger.info(
                    "DualMomentum: no asset clears %.1f%% threshold — "
                    "routing to safe haven %s",
                    threshold * 100, SAFE_HAVEN_TICKER,
                )
            else:
                reason = None

            # Sell anything currently held that is not in the winner list.
            # We can identify held assets because their price was passed in.
            # The engine passes prices for all tickers it fetched — if a
            # candidate ticker has a price, the broker may be holding it.
            # Sell signals must come BEFORE buy signals so the engine
            # executes them first and frees up cash.
            for ticker in CANDIDATE_TICKERS:
                if ticker not in winners and ticker in snapshot.prices:
                    # Only signal a sell if we might actually hold it —
                    # the broker will reject silently if we don't
                    signals.append(TradeSignal(
                        ticker=ticker,
                        action=TradeAction.SELL,
                        fraction=1.0,
                        confidence=0.95,
                        reason=f"Rotating out — {ticker} no longer a winner",
                        algorithm=self.name,
                    ))

            # Now signal buys for the winners
            fraction_each = round(1.0 / len(winners), 3)
            for ticker in winners:
                score = scores.get(ticker)
                sig_reason = reason or f"12-1mo return {score:.1%} beats threshold"
                logger.info(
                    "DualMomentum: BUY %s score=%s fraction=%.0f%%",
                    ticker,
                    f"{score:.1%}" if score is not None else "safe haven",
                    fraction_each * 100,
                )
                signals.append(TradeSignal(
                    ticker=ticker,
                    action=TradeAction.BUY,
                    fraction=fraction_each,
                    confidence=min(score / 0.3, 1.0) if score else 0.9,
                    reason=sig_reason,
                    algorithm=self.name,
                ))

        except Exception:
            logger.exception("DualMomentum: unexpected error during evaluation")
            return []

        return signals

    # ── Internal ──────────────────────────────────────────────

    def _score_assets(self, snapshot: MarketSnapshot) -> dict:
        scores = {}
        for ticker in CANDIDATE_TICKERS:
            history = snapshot.history.get(ticker, [])
            if not self._validate_history(ticker, history, REQUIRED_MONTHS):
                continue
            price_12mo_ago = history[-13]
            price_1mo_ago  = history[-2]
            if price_12mo_ago <= 0:
                logger.warning(
                    "DualMomentum: zero/negative price for %s", ticker
                )
                continue
            scores[ticker] = (price_1mo_ago - price_12mo_ago) / price_12mo_ago
        return scores

    def _assets_to_hold(self, risk_level: float) -> int:
        if risk_level < 0.34:
            return 1
        if risk_level < 0.67:
            return 2
        return 3

    def _threshold(self, risk_level: float) -> float:
        # 0.0 risk → 8%, 1.0 risk → 2%
        return ABSOLUTE_MOMENTUM_THRESHOLD + (0.08 - 0.02) * (1.0 - risk_level)