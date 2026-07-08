"""
mean_reversion.py — Mean Reversion strategy using 2-period RSI (Larry Connors).

Identifies oversold large-cap Swedish stocks in uptrends and buys
expecting a short-term bounce. Exits after RSI recovers or after
a maximum holding period.

Position tracking (_held_since) is persisted to disk so restarts
never lose track of open positions.

Risk dial effect:
  Low risk (0.0)  → only enter below RSI 10, exit above RSI 65, small position
  High risk (1.0) → enter below RSI 25, exit above RSI 75, larger position
"""

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from algorithms.base import BaseAlgorithm, MarketSnapshot, TradeSignal, TradeAction
from trading.tickers import REGISTRY, AssetClass

logger = logging.getLogger(__name__)

UNIVERSE: list[str] = [
    t for t, i in REGISTRY.items()
    if i.asset_class == AssetClass.EQUITY
]

# RSI parameters
RSI_PERIOD  = 2
MIN_HISTORY = RSI_PERIOD + 10

# Maximum trading days to hold a position before forced exit
MAX_HOLD_DAYS = 5

# Trend filter: only buy dips in stocks trading above their long SMA.
# This is the classic Connors RSI-2 design — a 2-day dip in an uptrend
# is statistically a buying opportunity; the same dip in a downtrend
# is often the start of further decline. Set to False to restore the
# unfiltered behaviour for comparison.
USE_TREND_FILTER = True
TREND_SMA_PERIOD = 200

# Where open-position tracking is persisted across restarts
STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "mean_reversion_state.json"


class MeanReversionAlgorithm(BaseAlgorithm):

    def __init__(self) -> None:
        super().__init__()
        # ticker → date the position was opened.
        # Loaded from disk so a restart never loses track of positions.
        self._held_since: dict[str, date] = self._load_state()
        if self._held_since:
            logger.info(
                "MeanReversion: restored %d tracked position(s) from disk: %s",
                len(self._held_since),
                {t: str(d) for t, d in self._held_since.items()}
            )

    # ── Required interface ────────────────────────────────────

    @property
    def name(self) -> str:
        return "mean_reversion"

    @property
    def display_name(self) -> str:
        return "MEAN REV"

    @property
    def description(self) -> str:
        return (
            "Buys large-cap Swedish stocks in uptrends when 2-period RSI "
            "is extremely oversold. Exits when RSI recovers."
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

    # ── Core logic ────────────────────────────────────────────

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
            today = date.today()

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

                # ── Exit (checked for anything we track as held) ──
                if ticker in self._held_since:
                    open_date = self._held_since[ticker]
                    days_held = self._trading_days_between(open_date, today)

                    if rsi > exit_threshold or days_held >= MAX_HOLD_DAYS:
                        reason = (
                            f"RSI {rsi:.1f} > {exit_threshold:.0f}"
                            if rsi > exit_threshold
                            else f"Max hold {MAX_HOLD_DAYS} trading days reached"
                        )
                        logger.info(
                            "MeanReversion: SELL %s — %s (held %d trading days)",
                            ticker, reason, days_held
                        )
                        signals.append(TradeSignal(
                            ticker=ticker,
                            action=TradeAction.SELL,
                            fraction=1.0,
                            confidence=0.85,
                            reason=reason,
                            algorithm=self.name,
                        ))
                        # Tracking updated in on_trade_executed only,
                        # after confirmed execution.

                # ── Entry ─────────────────────────────────────
                elif rsi < entry_threshold:
                    # Trend filter: skip dips in downtrends
                    if USE_TREND_FILTER:
                        sma = self._compute_sma(history, TREND_SMA_PERIOD)
                        if sma is None:
                            logger.debug(
                                "MeanReversion: %s — insufficient history "
                                "for %d-day SMA, skipping entry",
                                ticker, TREND_SMA_PERIOD
                            )
                            continue
                        if current_price < sma:
                            logger.info(
                                "MeanReversion: %s oversold (RSI %.1f) but "
                                "below %d-day SMA (%.2f < %.2f) — downtrend, "
                                "skipping",
                                ticker, rsi, TREND_SMA_PERIOD,
                                current_price, sma
                            )
                            continue

                    logger.info(
                        "MeanReversion: BUY %s — RSI %.1f < %.0f",
                        ticker, rsi, entry_threshold,
                    )
                    signals.append(TradeSignal(
                        ticker=ticker,
                        action=TradeAction.BUY,
                        fraction=size_fraction,
                        confidence=1.0 - (rsi / entry_threshold),
                        reason=f"RSI {rsi:.1f} < {entry_threshold:.0f} (oversold, uptrend)",
                        algorithm=self.name,
                    ))
                    # Tracking updated in on_trade_executed only.

        except Exception:
            logger.exception("MeanReversion: unexpected error during evaluation")
            return []

        return signals

    def on_trade_executed(self, signal: TradeSignal, success: bool) -> None:
        """
        Update and persist position tracking only after confirmed execution.
        """
        if not success:
            logger.warning(
                "MeanReversion: trade FAILED for %s — tracking unchanged",
                signal.ticker
            )
            return

        if signal.action == TradeAction.SELL:
            self._held_since.pop(signal.ticker, None)
            logger.info(
                "MeanReversion: position closed — %s removed from tracking",
                signal.ticker
            )
        elif signal.action == TradeAction.BUY:
            self._held_since[signal.ticker] = date.today()
            logger.info(
                "MeanReversion: position opened — %s held since %s",
                signal.ticker, date.today()
            )

        self._save_state()

    def on_market_closed(self) -> None:
        logger.info(
            "MeanReversion: market closed. Tracked positions: %s",
            {t: str(d) for t, d in self._held_since.items()} or "none"
        )

    # ── Persistence ───────────────────────────────────────────

    def _load_state(self) -> dict[str, date]:
        """Load tracked positions from disk. Returns {} on any failure."""
        try:
            if STATE_FILE.exists():
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                return {
                    ticker: date.fromisoformat(iso)
                    for ticker, iso in raw.get("held_since", {}).items()
                }
        except Exception as exc:
            logger.error(
                "MeanReversion: could not load state file, starting "
                "with empty tracking: %s", exc
            )
        return {}

    def _save_state(self) -> None:
        """Persist tracked positions atomically."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "held_since": {
                    ticker: d.isoformat()
                    for ticker, d in self._held_since.items()
                }
            }
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except Exception as exc:
            logger.error("MeanReversion: could not save state file: %s", exc)

    # ── Internal ──────────────────────────────────────────────

    @staticmethod
    def _trading_days_between(start: date, end: date) -> int:
        """Count weekdays (Mon–Fri) between start and end inclusive."""
        count   = 0
        current = start
        while current <= end:
            if current.weekday() < 5:
                count += 1
            current += timedelta(days=1)
        return count

    @staticmethod
    def _compute_sma(prices: list, period: int) -> Optional[float]:
        """Simple moving average of the last `period` closes."""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

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
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    def _entry_threshold(self, risk_level: float) -> float:
        return 10.0 + 15.0 * risk_level

    def _exit_threshold(self, risk_level: float) -> float:
        return 65.0 + 10.0 * risk_level