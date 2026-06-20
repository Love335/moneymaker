"""
market_data.py — Market data fetching with validation and caching.

Fetches price history from yfinance as primary source.
Validates all responses before passing to algorithms.
Emits API_ERROR or API_INVALID_DATA events on failure.
Never passes bad data downstream — fails loudly instead.
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import yfinance as yf

from core.events import EventBus, EventType, Event
from data.cache import PriceCache

logger = logging.getLogger(__name__)

# How many months of daily history to fetch
HISTORY_MONTHS = 14

# Sanity limits for price validation
MIN_PRICE_SEK = 0.001
MAX_PRICE_SEK = 1_000_000.0

# Minimum number of data points required for a valid history
MIN_HISTORY_LENGTH = 30


class MarketDataError(Exception):
    """Raised when market data cannot be fetched or fails validation."""
    pass


class MarketDataService:
    """
    Fetches and validates market data for the algorithm engine.

    All public methods either return clean validated data
    or raise MarketDataError — they never return partial
    or suspect data silently.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus   = bus
        self._cache = PriceCache()
        self._lock  = threading.Lock()

    # ── Public API ────────────────────────────────────────────

    def get_current_price(self, ticker: str) -> float:
        """
        Return current price for ticker in SEK.
        Raises MarketDataError if unavailable or invalid.
        """
        cached = self._cache.get_price(ticker)
        if cached is not None:
            return cached

        price = self._fetch_current_price(ticker)
        self._validate_price(ticker, price)
        self._cache.set_price(ticker, price)
        return price

    def get_price_history(self, ticker: str) -> List[float]:
        """
        Return list of daily closing prices (oldest first).
        Raises MarketDataError if insufficient or invalid history.
        """
        cached = self._cache.get_history(ticker)
        if cached is not None:
            return cached

        history = self._fetch_history(ticker)
        self._validate_history(ticker, history)
        self._cache.set_history(ticker, history)
        return history

    def get_prices_bulk(self, tickers: List[str]) -> Dict[str, float]:
        """
        Fetch current prices for multiple tickers.
        Returns only tickers for which valid data was obtained.
        Logs but does not raise on individual ticker failures.
        """
        results = {}
        for ticker in tickers:
            try:
                results[ticker] = self.get_current_price(ticker)
            except MarketDataError as exc:
                logger.warning(
                    "MarketDataService: skipping %s — %s", ticker, exc
                )
                self._bus.publish(Event(
                    type=EventType.API_INVALID_DATA,
                    source="MarketDataService",
                    payload={"ticker": ticker, "error": str(exc)}
                ))
        return results

    def get_history_bulk(self, tickers: List[str]) -> Dict[str, List[float]]:
        """
        Fetch price history for multiple tickers.
        Returns only tickers with sufficient valid history.
        """
        results = {}
        for ticker in tickers:
            try:
                results[ticker] = self.get_price_history(ticker)
            except MarketDataError as exc:
                logger.warning(
                    "MarketDataService: no history for %s — %s", ticker, exc
                )
        return results

    # ── Fetching ──────────────────────────────────────────────

    def _fetch_current_price(self, ticker: str) -> float:
        """Fetch latest price from yfinance."""
        try:
            data  = yf.Ticker(ticker)
            info  = data.fast_info
            price = getattr(info, "last_price", None)

            if price is None:
                # Fallback: use last close from recent history
                hist = data.history(period="2d")
                if hist.empty:
                    raise MarketDataError(
                        f"No price data returned for {ticker}"
                    )
                price = float(hist["Close"].iloc[-1])

            return float(price)

        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataError(
                f"Failed to fetch price for {ticker}: {exc}"
            ) from exc

    def _fetch_history(self, ticker: str) -> List[float]:
        """Fetch daily closing price history from yfinance."""
        try:
            end   = datetime.today()
            start = end - timedelta(days=HISTORY_MONTHS * 31)

            data = yf.download(
                ticker,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                interval="1d",
                progress=False,
                auto_adjust=True,
            )

            if data.empty:
                raise MarketDataError(
                    f"Empty history returned for {ticker}"
                )

            close_data = data["Close"]

            # yfinance may return a DataFrame instead of a Series
            # when multiple tickers are requested — normalise to Series
            if hasattr(close_data, "columns"):
                close_data = close_data.iloc[:, 0]

            closes = close_data.dropna().tolist()
            return [float(p) for p in closes]

        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataError(
                f"Failed to fetch history for {ticker}: {exc}"
            ) from exc

    # ── Validation ────────────────────────────────────────────

    def _validate_price(self, ticker: str, price: float) -> None:
        """Raise MarketDataError if price is outside sane bounds."""
        if not isinstance(price, (int, float)):
            raise MarketDataError(
                f"Non-numeric price for {ticker}: {price!r}"
            )
        if price != price:   # NaN check — NaN is never equal to itself
            raise MarketDataError(
                f"NaN price received for {ticker}"
            )
        if price < MIN_PRICE_SEK:
            raise MarketDataError(
                f"Price for {ticker} is suspiciously low: {price}"
            )
        if price > MAX_PRICE_SEK:
            raise MarketDataError(
                f"Price for {ticker} is suspiciously high: {price}"
            )

    def _validate_history(self, ticker: str, history: List[float]) -> None:
        """Raise MarketDataError if history is insufficient or corrupt."""
        if len(history) < MIN_HISTORY_LENGTH:
            raise MarketDataError(
                f"Insufficient history for {ticker}: "
                f"only {len(history)} data points (need {MIN_HISTORY_LENGTH})"
            )
        for i, price in enumerate(history):
            if price != price:
                raise MarketDataError(
                    f"NaN at index {i} in {ticker} history"
                )
            if price < MIN_PRICE_SEK or price > MAX_PRICE_SEK:
                raise MarketDataError(
                    f"Invalid price at index {i} in {ticker} history: {price}"
                )
        # Check for suspiciously flat data (all prices identical)
        if len(set(history[-10:])) == 1:
            raise MarketDataError(
                f"History for {ticker} appears stale — last 10 prices identical"
            )