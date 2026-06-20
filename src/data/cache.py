"""
cache.py — Thread-safe in-memory price cache with TTL.

Prevents redundant API calls when multiple components
need the same data in a short window.
"""

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Time-to-live for cached values in seconds
PRICE_TTL_SECONDS   = 60       # current prices refresh every 60s
HISTORY_TTL_SECONDS = 3_600    # history refreshes every hour


class PriceCache:
    """
    Thread-safe cache for current prices and price history.

    Entries expire after their TTL and are re-fetched on next access.
    All returned collections are copies — callers cannot mutate
    internal cache state through returned references.
    """

    def __init__(self) -> None:
        self._lock           = threading.Lock()
        # ticker → (price, timestamp)
        self._prices:    Dict[str, Tuple[float, float]]        = {}
        # ticker → (history_list, timestamp)
        self._histories: Dict[str, Tuple[List[float], float]]  = {}

    # ── Price cache ───────────────────────────────────────────

    def get_price(self, ticker: str) -> Optional[float]:
        """Return cached price if still fresh, else None."""
        with self._lock:
            entry = self._prices.get(ticker)
            if entry is None:
                return None
            price, timestamp = entry
            if time.monotonic() - timestamp > PRICE_TTL_SECONDS:
                del self._prices[ticker]
                return None
            return price

    def set_price(self, ticker: str, price: float) -> None:
        """Store a price with the current timestamp."""
        with self._lock:
            self._prices[ticker] = (price, time.monotonic())

    # ── History cache ─────────────────────────────────────────

    def get_history(self, ticker: str) -> Optional[List[float]]:
        """Return cached history if still fresh, else None."""
        with self._lock:
            entry = self._histories.get(ticker)
            if entry is None:
                return None
            history, timestamp = entry
            if time.monotonic() - timestamp > HISTORY_TTL_SECONDS:
                del self._histories[ticker]
                return None
            return list(history)   # copy — caller cannot mutate cache

    def set_history(self, ticker: str, history: List[float]) -> None:
        """Store a copy of the history list with the current timestamp."""
        with self._lock:
            self._histories[ticker] = (list(history), time.monotonic())

    # ── Maintenance ───────────────────────────────────────────

    def clear(self) -> None:
        """Clear all cached data."""
        with self._lock:
            self._prices.clear()
            self._histories.clear()
        logger.debug("PriceCache: cleared all entries")

    def invalidate(self, ticker: str) -> None:
        """Remove all cached data for a specific ticker."""
        with self._lock:
            self._prices.pop(ticker, None)
            self._histories.pop(ticker, None)

    def invalidate_stale(self) -> int:
        """
        Proactively remove all expired entries.
        Returns the number of entries removed.
        Useful for long-running processes to prevent unbounded memory growth
        if large numbers of tickers are cached over time.
        """
        now     = time.monotonic()
        removed = 0
        with self._lock:
            stale_prices = [
                t for t, (_, ts) in self._prices.items()
                if now - ts > PRICE_TTL_SECONDS
            ]
            for t in stale_prices:
                del self._prices[t]
                removed += 1

            stale_histories = [
                t for t, (_, ts) in self._histories.items()
                if now - ts > HISTORY_TTL_SECONDS
            ]
            for t in stale_histories:
                del self._histories[t]
                removed += 1

        if removed:
            logger.debug("PriceCache: removed %d stale entries", removed)
        return removed

    def stats(self) -> dict:
        """
        Return a snapshot of cache occupancy for debugging and monitoring.
        """
        now = time.monotonic()
        with self._lock:
            fresh_prices = sum(
                1 for _, ts in self._prices.values()
                if now - ts <= PRICE_TTL_SECONDS
            )
            fresh_histories = sum(
                1 for _, ts in self._histories.values()
                if now - ts <= HISTORY_TTL_SECONDS
            )
            return {
                "price_entries":      len(self._prices),
                "history_entries":    len(self._histories),
                "fresh_prices":       fresh_prices,
                "fresh_histories":    fresh_histories,
            }