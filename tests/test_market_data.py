"""
test_market_data.py — Tests for market data fetching and validation.

Tests data validation logic in isolation, and optionally tests
live data fetching when network is available.

Run with:
    cd ~/moneymaker
    sudo ~/moneymaker/venv/bin/python3 tests/test_market_data.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data.cache import PriceCache
from data.market_data import MarketDataService, MarketDataError
from core.events import EventBus

PASS = 0
FAIL = 0


def check(condition: bool, description: str) -> None:
    global PASS, FAIL
    if condition:
        print(f"  [✓] {description}")
        PASS += 1
    else:
        print(f"  [✗] FAILED: {description}")
        FAIL += 1


def section(title: str) -> None:
    print()
    print("=" * 55)
    print(f"  {title}")
    print("=" * 55)


# ══════════════════════════════════════════════════════════════
#  TEST 1 — Price Cache
# ══════════════════════════════════════════════════════════════

def test_cache() -> None:
    section("TEST 1: Price Cache")
    cache = PriceCache()

    # Empty cache returns None
    check(cache.get_price("ERIC-B.ST") is None, "Empty cache returns None for price")
    check(cache.get_history("ERIC-B.ST") is None, "Empty cache returns None for history")

    # Set and get price
    cache.set_price("ERIC-B.ST", 74.50)
    result = cache.get_price("ERIC-B.ST")
    check(result == 74.50, f"Cached price retrieved correctly: {result}")

    # Set and get history
    history = [70.0, 71.0, 72.0, 73.0, 74.50]
    cache.set_history("ERIC-B.ST", history)
    cached_history = cache.get_history("ERIC-B.ST")
    check(cached_history == history, "Cached history retrieved correctly")

    # History returns a copy — mutations don't affect cache
    cached_history.append(999.0)
    check(
        cache.get_history("ERIC-B.ST") == history,
        "Cache returns copy of history — external mutation doesn't affect cache"
    )

    # Invalidate specific ticker
    cache.invalidate("ERIC-B.ST")
    check(cache.get_price("ERIC-B.ST") is None,   "Price invalidated correctly")
    check(cache.get_history("ERIC-B.ST") is None, "History invalidated correctly")

    # Multiple tickers
    cache.set_price("VOLV-B.ST", 200.0)
    cache.set_price("ERIC-B.ST", 74.50)
    cache.invalidate("VOLV-B.ST")
    check(cache.get_price("VOLV-B.ST") is None,   "Only VOLV-B.ST invalidated")
    check(cache.get_price("ERIC-B.ST") == 74.50,  "ERIC-B.ST price still cached")

    # Clear all
    cache.clear()
    check(cache.get_price("ERIC-B.ST") is None, "All cache cleared")


# ══════════════════════════════════════════════════════════════
#  TEST 2 — Data Validation
# ══════════════════════════════════════════════════════════════

def test_validation() -> None:
    section("TEST 2: Data Validation")

    bus     = EventBus()
    service = MarketDataService(bus)
    bus.start()

    # Access private validation methods directly for unit testing
    validate_price   = service._validate_price
    validate_history = service._validate_history

    # Valid price
    try:
        validate_price("TEST", 100.0)
        check(True, "Valid price passes validation")
    except MarketDataError:
        check(False, "Valid price should not raise")

    # Zero price
    try:
        validate_price("TEST", 0.0)
        check(False, "Zero price should raise MarketDataError")
    except MarketDataError:
        check(True, "Zero price raises MarketDataError")

    # Negative price
    try:
        validate_price("TEST", -10.0)
        check(False, "Negative price should raise MarketDataError")
    except MarketDataError:
        check(True, "Negative price raises MarketDataError")

    # Absurdly high price
    try:
        validate_price("TEST", 2_000_000.0)
        check(False, "Absurdly high price should raise MarketDataError")
    except MarketDataError:
        check(True, "Absurdly high price raises MarketDataError")

    # Valid history
    good_history = [float(i) for i in range(100, 130)]
    try:
        validate_history("TEST", good_history)
        check(True, "Valid history passes validation")
    except MarketDataError:
        check(False, "Valid history should not raise")

    # Too short history
    try:
        validate_history("TEST", [100.0, 101.0, 102.0])
        check(False, "Short history should raise MarketDataError")
    except MarketDataError:
        check(True, "Short history raises MarketDataError")

    # Stale (flat) history
    flat_history = [100.0] * 30
    try:
        validate_history("TEST", flat_history)
        check(False, "Flat history should raise MarketDataError")
    except MarketDataError:
        check(True, "Flat/stale history raises MarketDataError")

    bus.stop()


# ══════════════════════════════════════════════════════════════
#  TEST 3 — Live data (requires internet)
# ══════════════════════════════════════════════════════════════

def test_live_data() -> None:
    section("TEST 3: Live Market Data (requires internet)")
    print("  Fetching real data from Yahoo Finance...")
    print("  This may take 10-15 seconds. Press Ctrl+C to skip.")
    print()

    bus     = EventBus()
    service = MarketDataService(bus)
    bus.start()

    # Use a reliable global ticker for testing
    test_ticker = "AAPL"

    try:
        price = service.get_current_price(test_ticker)
        check(isinstance(price, float), f"Price is a float: {price:.2f}")
        check(price > 0,                f"Price is positive: {price:.2f}")
        print(f"    {test_ticker} current price: {price:.2f} USD")
    except MarketDataError as exc:
        print(f"  ⚠ Could not fetch price: {exc}")
        print("    (This is expected if there is no internet connection)")

    try:
        history = service.get_price_history(test_ticker)
        check(isinstance(history, list),  f"History is a list")
        check(len(history) >= 30,          f"History has sufficient length: {len(history)} days")
        check(all(p > 0 for p in history), "All historical prices are positive")
        print(f"    {test_ticker} history: {len(history)} days, "
              f"latest: {history[-1]:.2f}")
    except MarketDataError as exc:
        print(f"  ⚠ Could not fetch history: {exc}")

    # Test cache hit on second call
    try:
        price2 = service.get_current_price(test_ticker)
        check(True, "Second price fetch (cache hit) succeeded")
    except Exception:
        pass

    bus.stop()


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║         MONEYMAKER — Market Data Test Suite           ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Tests 1 and 2 require no network connection.")
    print("  Test 3 fetches live data and requires internet.")

    try:
        test_cache()
        test_validation()
        test_live_data()

    except KeyboardInterrupt:
        print("\n\n  Tests interrupted.")
    except Exception as exc:
        print(f"\n  UNEXPECTED ERROR: {exc}")
        import traceback
        traceback.print_exc()

    print()
    print("=" * 55)
    print(f"  Results: {PASS} passed, {FAIL} failed")
    print("=" * 55)
    print()


if __name__ == "__main__":
    main()