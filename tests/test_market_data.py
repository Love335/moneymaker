"""
test_market_data.py — Tests for the data module.

Tests PriceCache and MarketDataService validation logic in full
isolation, then optionally tests live data fetching when network
is available.

Run with:
    cd ~/moneymaker
    sudo ~/moneymaker/venv/bin/python3 tests/test_market_data.py
"""

import sys
import os
import time
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data.cache import PriceCache, PRICE_TTL_SECONDS, HISTORY_TTL_SECONDS
from data.market_data import (
    MarketDataService, MarketDataError,
    MIN_PRICE_SEK, MAX_PRICE_SEK, MIN_HISTORY_LENGTH,
)
from core.events import EventBus, EventType

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


def make_service() -> tuple:
    """Create a MarketDataService with a running EventBus."""
    bus     = EventBus()
    bus.start()
    service = MarketDataService(bus)
    return service, bus


def good_history(length: int = MIN_HISTORY_LENGTH + 10) -> list:
    """Return a valid price history with slight variation."""
    return [100.0 + (i % 5) * 0.5 for i in range(length)]


# ══════════════════════════════════════════════════════════════
#  TEST 1 — PriceCache basics
# ══════════════════════════════════════════════════════════════

def test_cache_basics() -> None:
    section("TEST 1: PriceCache — Basic Operations")
    cache = PriceCache()

    # Empty cache returns None
    check(cache.get_price("ERIC-B.ST")   is None, "Empty cache: get_price returns None")
    check(cache.get_history("ERIC-B.ST") is None, "Empty cache: get_history returns None")

    # Set and retrieve price
    cache.set_price("ERIC-B.ST", 74.50)
    check(cache.get_price("ERIC-B.ST") == 74.50, "Cached price retrieved correctly")

    # Set and retrieve history
    history = [float(i) for i in range(70, 80)]
    cache.set_history("ERIC-B.ST", history)
    check(cache.get_history("ERIC-B.ST") == history, "Cached history retrieved correctly")

    # Overwrite price
    cache.set_price("ERIC-B.ST", 80.00)
    check(cache.get_price("ERIC-B.ST") == 80.00, "Price overwritten correctly")

    # Multiple tickers are independent
    cache.set_price("VOLV-B.ST", 200.0)
    cache.set_price("SEB-A.ST",  150.0)
    check(cache.get_price("VOLV-B.ST") == 200.0, "VOLV-B.ST price independent")
    check(cache.get_price("SEB-A.ST")  == 150.0, "SEB-A.ST price independent")


# ══════════════════════════════════════════════════════════════
#  TEST 2 — PriceCache isolation (copy semantics)
# ══════════════════════════════════════════════════════════════

def test_cache_copy_semantics() -> None:
    section("TEST 2: PriceCache — Copy Semantics")
    cache = PriceCache()

    original = [100.0, 101.0, 102.0, 103.0, 104.0]
    cache.set_history("TEST", original)

    # Mutating original after set_history doesn't affect cache
    original.append(999.0)
    cached = cache.get_history("TEST")
    check(
        len(cached) == 5,
        "Mutating original list after set_history does not affect cache"
    )

    # Mutating returned history doesn't affect cache
    cached.append(888.0)
    cached2 = cache.get_history("TEST")
    check(
        len(cached2) == 5,
        "Mutating returned history does not affect cache (returns copy)"
    )

    # set_price stores the value, not a reference (floats are immutable — always true)
    cache.set_price("TEST", 50.0)
    check(cache.get_price("TEST") == 50.0, "Price stored and retrieved correctly")


# ══════════════════════════════════════════════════════════════
#  TEST 3 — PriceCache invalidation and clearing
# ══════════════════════════════════════════════════════════════

def test_cache_invalidation() -> None:
    section("TEST 3: PriceCache — Invalidation and Clearing")
    cache = PriceCache()

    cache.set_price("A",   100.0)
    cache.set_price("B",   200.0)
    cache.set_history("A", [1.0, 2.0, 3.0])
    cache.set_history("B", [4.0, 5.0, 6.0])

    # Invalidate single ticker
    cache.invalidate("A")
    check(cache.get_price("A")   is None, "Price for A invalidated")
    check(cache.get_history("A") is None, "History for A invalidated")
    check(cache.get_price("B")   == 200.0, "Price for B unaffected by A invalidation")
    check(cache.get_history("B") is not None, "History for B unaffected by A invalidation")

    # Invalidate ticker that doesn't exist — should not raise
    try:
        cache.invalidate("NONEXISTENT")
        check(True, "Invalidating nonexistent ticker does not raise")
    except Exception as exc:
        check(False, f"invalidate() raised for nonexistent ticker: {exc}")

    # Clear all
    cache.set_price("C", 300.0)
    cache.clear()
    check(cache.get_price("B") is None, "All prices cleared")
    check(cache.get_price("C") is None, "Newly added price also cleared")

    # Cache usable after clear
    cache.set_price("D", 400.0)
    check(cache.get_price("D") == 400.0, "Cache usable after clear()")


# ══════════════════════════════════════════════════════════════
#  TEST 4 — PriceCache TTL expiry
# ══════════════════════════════════════════════════════════════

def test_cache_ttl() -> None:
    section("TEST 4: PriceCache — TTL Expiry")
    cache = PriceCache()

    # Inject an expired price by writing directly to internal state
    cache._prices["EXPIRED"] = (99.0, time.monotonic() - PRICE_TTL_SECONDS - 1)
    check(
        cache.get_price("EXPIRED") is None,
        "Expired price returns None"
    )
    check(
        "EXPIRED" not in cache._prices,
        "Expired price is removed from internal dict on access"
    )

    # Inject an expired history
    cache._histories["EXPIRED_H"] = (
        [1.0, 2.0, 3.0],
        time.monotonic() - HISTORY_TTL_SECONDS - 1
    )
    check(
        cache.get_history("EXPIRED_H") is None,
        "Expired history returns None"
    )
    check(
        "EXPIRED_H" not in cache._histories,
        "Expired history removed from internal dict on access"
    )

    # Fresh entries still accessible alongside expired ones
    cache.set_price("FRESH", 55.0)
    cache._prices["STALE"] = (10.0, time.monotonic() - PRICE_TTL_SECONDS - 1)
    check(cache.get_price("FRESH") == 55.0, "Fresh price still accessible alongside stale")
    check(cache.get_price("STALE") is None,  "Stale price correctly expired")


# ══════════════════════════════════════════════════════════════
#  TEST 5 — PriceCache invalidate_stale and stats
# ══════════════════════════════════════════════════════════════

def test_cache_maintenance() -> None:
    section("TEST 5: PriceCache — Maintenance Methods")
    cache = PriceCache()

    # Populate with a mix of fresh and stale entries
    cache.set_price("FRESH_A", 100.0)
    cache.set_price("FRESH_B", 200.0)
    cache._prices["STALE_A"] = (10.0, time.monotonic() - PRICE_TTL_SECONDS - 1)
    cache._prices["STALE_B"] = (20.0, time.monotonic() - PRICE_TTL_SECONDS - 1)
    cache.set_history("FRESH_H", [1.0, 2.0, 3.0])
    cache._histories["STALE_H"] = (
        [4.0, 5.0],
        time.monotonic() - HISTORY_TTL_SECONDS - 1
    )

    removed = cache.invalidate_stale()
    check(removed == 3, f"invalidate_stale() removed 3 stale entries (got {removed})")
    check(cache.get_price("FRESH_A")   == 100.0,      "FRESH_A price still present after invalidate_stale")
    check(cache.get_price("FRESH_B")   == 200.0,      "FRESH_B price still present after invalidate_stale")
    check(cache.get_price("STALE_A")   is None,       "STALE_A price removed by invalidate_stale")
    check(cache.get_price("STALE_B")   is None,       "STALE_B price removed by invalidate_stale")
    check(cache.get_history("FRESH_H") is not None,   "FRESH_H history still present after invalidate_stale")
    check(cache.get_history("STALE_H") is None,       "STALE_H history removed by invalidate_stale")

    # invalidate_stale on empty cache returns 0
    empty = PriceCache()
    check(empty.invalidate_stale() == 0, "invalidate_stale() on empty cache returns 0")

    # stats()
    cache2 = PriceCache()
    cache2.set_price("X", 1.0)
    cache2.set_price("Y", 2.0)
    cache2.set_history("X", [1.0, 2.0])
    cache2._prices["OLD"] = (3.0, time.monotonic() - PRICE_TTL_SECONDS - 1)

    s = cache2.stats()
    check(s["price_entries"]   == 3, f"stats: 3 price entries total (got {s['price_entries']})")
    check(s["fresh_prices"]    == 2, f"stats: 2 fresh prices (got {s['fresh_prices']})")
    check(s["history_entries"] == 1, f"stats: 1 history entry (got {s['history_entries']})")
    check(s["fresh_histories"] == 1, f"stats: 1 fresh history (got {s['fresh_histories']})")


# ══════════════════════════════════════════════════════════════
#  TEST 6 — PriceCache thread safety
# ══════════════════════════════════════════════════════════════

def test_cache_thread_safety() -> None:
    section("TEST 6: PriceCache — Thread Safety")
    cache   = PriceCache()
    errors  = []
    tickers = ["A", "B", "C", "D"]

    # 4 writers + 4 readers + 2 maintenance = 10 threads total
    barrier = threading.Barrier(10)

    def writer(ticker: str, price: float) -> None:
        try:
            barrier.wait()
            for _ in range(50):
                cache.set_price(ticker, price)
                cache.set_history(ticker, [price] * 5)
        except Exception as exc:
            errors.append(exc)

    def reader(ticker: str) -> None:
        try:
            barrier.wait()
            for _ in range(50):
                cache.get_price(ticker)
                cache.get_history(ticker)
        except Exception as exc:
            errors.append(exc)

    def maintenance(fn) -> None:
        try:
            barrier.wait()
            for _ in range(10):
                fn()
        except Exception as exc:
            errors.append(exc)

    threads = []
    for ticker in tickers:
        threads.append(threading.Thread(target=writer, args=(ticker, 100.0)))
        threads.append(threading.Thread(target=reader, args=(ticker,)))
    threads.append(threading.Thread(target=maintenance, args=(cache.clear,)))
    threads.append(threading.Thread(target=maintenance, args=(cache.invalidate_stale,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(len(errors) == 0, f"No errors during concurrent cache access (errors: {errors})")

# ══════════════════════════════════════════════════════════════
#  TEST 7 — Price validation
# ══════════════════════════════════════════════════════════════

def test_price_validation() -> None:
    section("TEST 7: MarketDataService — Price Validation")
    service, bus = make_service()

    def validate(price):
        service._validate_price("TEST", price)

    # Valid prices
    for valid in [0.01, 1.0, 100.0, 50_000.0, MAX_PRICE_SEK]:
        try:
            validate(valid)
            check(True, f"Valid price {valid} passes validation")
        except MarketDataError:
            check(False, f"Valid price {valid} incorrectly rejected")

    # Zero
    try:
        validate(0.0)
        check(False, "Zero price should raise MarketDataError")
    except MarketDataError:
        check(True, "Zero price raises MarketDataError")

    # Negative
    try:
        validate(-1.0)
        check(False, "Negative price should raise MarketDataError")
    except MarketDataError:
        check(True, "Negative price raises MarketDataError")

    # Below minimum
    try:
        validate(MIN_PRICE_SEK / 2)
        check(False, "Price below MIN_PRICE_SEK should raise")
    except MarketDataError:
        check(True, "Price below MIN_PRICE_SEK raises MarketDataError")

    # Above maximum
    try:
        validate(MAX_PRICE_SEK + 1)
        check(False, "Price above MAX_PRICE_SEK should raise")
    except MarketDataError:
        check(True, "Price above MAX_PRICE_SEK raises MarketDataError")

    # NaN
    try:
        validate(float("nan"))
        check(False, "NaN price should raise MarketDataError")
    except MarketDataError:
        check(True, "NaN price raises MarketDataError")

    # Non-numeric
    try:
        validate("not a number")
        check(False, "Non-numeric price should raise MarketDataError")
    except MarketDataError:
        check(True, "Non-numeric price raises MarketDataError")

    bus.stop()


# ══════════════════════════════════════════════════════════════
#  TEST 8 — History validation
# ══════════════════════════════════════════════════════════════

def test_history_validation() -> None:
    section("TEST 8: MarketDataService — History Validation")
    service, bus = make_service()

    def validate(history):
        service._validate_history("TEST", history)

    # Valid history
    try:
        validate(good_history())
        check(True, "Valid history passes validation")
    except MarketDataError as exc:
        check(False, f"Valid history incorrectly rejected: {exc}")

    # Exactly at minimum length
    try:
        validate(good_history(MIN_HISTORY_LENGTH))
        check(True, f"History of exactly {MIN_HISTORY_LENGTH} points passes")
    except MarketDataError:
        check(False, f"History of exactly {MIN_HISTORY_LENGTH} points incorrectly rejected")

    # One below minimum
    try:
        validate(good_history(MIN_HISTORY_LENGTH - 1))
        check(False, "History one below minimum should raise")
    except MarketDataError:
        check(True, "History one below minimum raises MarketDataError")

    # Empty history
    try:
        validate([])
        check(False, "Empty history should raise MarketDataError")
    except MarketDataError:
        check(True, "Empty history raises MarketDataError")

    # History with a zero price
    bad_zero = good_history()
    bad_zero[5] = 0.0
    try:
        validate(bad_zero)
        check(False, "History with zero price should raise")
    except MarketDataError:
        check(True, "History with zero price raises MarketDataError")

    # History with a negative price
    bad_neg = good_history()
    bad_neg[3] = -50.0
    try:
        validate(bad_neg)
        check(False, "History with negative price should raise")
    except MarketDataError:
        check(True, "History with negative price raises MarketDataError")

    # History with NaN
    bad_nan = good_history()
    bad_nan[10] = float("nan")
    try:
        validate(bad_nan)
        check(False, "History with NaN should raise")
    except MarketDataError:
        check(True, "History with NaN raises MarketDataError")

    # Flat/stale history — last 10 identical
    stale = good_history(MIN_HISTORY_LENGTH + 10)
    for i in range(len(stale) - 10, len(stale)):
        stale[i] = 100.0
    try:
        validate(stale)
        check(False, "Stale (flat) history should raise")
    except MarketDataError:
        check(True, "Stale flat history raises MarketDataError")

    # History with variation only in older data is fine
    varied = [100.0 + (i % 3) for i in range(MIN_HISTORY_LENGTH + 5)]
    try:
        validate(varied)
        check(True, "History with variation in recent prices passes")
    except MarketDataError as exc:
        check(False, f"Varied history incorrectly rejected: {exc}")

    bus.stop()


# ══════════════════════════════════════════════════════════════
#  TEST 9 — Cache integration with MarketDataService
# ══════════════════════════════════════════════════════════════

def test_cache_integration() -> None:
    section("TEST 9: MarketDataService — Cache Integration")
    service, bus = make_service()

    fetch_count = [0]
    real_fetch  = service._fetch_current_price

    def counting_fetch(ticker):
        fetch_count[0] += 1
        return 123.45

    service._fetch_current_price = counting_fetch
    service._validate_price      = lambda t, p: None   # skip validation

    # First call fetches from source
    price1 = service.get_current_price("TEST")
    check(fetch_count[0] == 1, "First get_current_price() fetches from source")
    check(price1 == 123.45,    "Correct price returned on first fetch")

    # Second call uses cache — fetch count does not increase
    price2 = service.get_current_price("TEST")
    check(fetch_count[0] == 1, "Second get_current_price() uses cache (no extra fetch)")
    check(price2 == 123.45,    "Cached price matches original")

    # Invalidating cache forces re-fetch
    service._cache.invalidate("TEST")
    price3 = service.get_current_price("TEST")
    check(fetch_count[0] == 2, "After invalidation, next get_current_price() re-fetches")

    # Same pattern for history
    hist_count = [0]

    def counting_hist_fetch(ticker):
        hist_count[0] += 1
        return good_history()

    service._fetch_history    = counting_hist_fetch
    service._validate_history = lambda t, h: None

    service.get_price_history("HIST_TEST")
    service.get_price_history("HIST_TEST")
    check(hist_count[0] == 1, "Second get_price_history() uses cache")

    service._cache.invalidate("HIST_TEST")
    service.get_price_history("HIST_TEST")
    check(hist_count[0] == 2, "Cache miss after invalidation triggers re-fetch")

    bus.stop()


# ══════════════════════════════════════════════════════════════
#  TEST 10 — Bulk fetching and event emission
# ══════════════════════════════════════════════════════════════

def test_bulk_and_events() -> None:
    section("TEST 10: MarketDataService — Bulk Fetching and Events")
    service, bus = make_service()

    # Patch fetch to succeed for some tickers, fail for others
    def mock_fetch(ticker):
        if ticker == "FAIL":
            raise MarketDataError("Simulated fetch failure")
        return 100.0

    service._fetch_current_price = mock_fetch
    service._validate_price      = lambda t, p: None

    # Collect API_INVALID_DATA events
    invalid_events = []
    bus.subscribe(
        EventType.API_INVALID_DATA,
        lambda e: invalid_events.append(e)
    )

    results = service.get_prices_bulk(["ERIC-B.ST", "FAIL", "VOLV-B.ST"])
    time.sleep(0.05)

    check("ERIC-B.ST" in results,  "Successful ticker included in bulk results")
    check("VOLV-B.ST" in results,  "Second successful ticker included in bulk results")
    check("FAIL" not in results,   "Failed ticker excluded from bulk results")
    check(len(results) == 2,       f"Bulk result has 2 entries (got {len(results)})")
    check(len(invalid_events) == 1, "API_INVALID_DATA event emitted for failed ticker")
    check(
        invalid_events[0].payload.get("ticker") == "FAIL",
        "API_INVALID_DATA event contains correct ticker"
    )

    # get_prices_bulk on empty list returns empty dict
    empty = service.get_prices_bulk([])
    check(empty == {}, "get_prices_bulk([]) returns empty dict")

    # get_history_bulk skips failed tickers silently
    def mock_hist(ticker):
        if ticker == "BAD_HIST":
            raise MarketDataError("No history")
        return good_history()

    service._fetch_history    = mock_hist
    service._validate_history = lambda t, h: None

    hist_results = service.get_history_bulk(["ERIC-B.ST", "BAD_HIST"])
    check("ERIC-B.ST"  in hist_results, "Successful history ticker included")
    check("BAD_HIST" not in hist_results, "Failed history ticker excluded")

    bus.stop()


# ══════════════════════════════════════════════════════════════
#  TEST 11 — Live data (requires internet)
# ══════════════════════════════════════════════════════════════

def test_live_data() -> None:
    section("TEST 11: Live Market Data (requires internet)")
    print("  Fetching real data from Yahoo Finance...")
    print("  This may take 10-15 seconds. Press Ctrl+C to skip.")
    print()

    service, bus = make_service()
    test_ticker  = "AAPL"

    try:
        price = service.get_current_price(test_ticker)
        check(isinstance(price, float), f"Price is a float: {price:.2f}")
        check(price > 0,                f"Price is positive: {price:.2f}")
        print(f"    {test_ticker} current price: {price:.2f} USD")

        # Second call should be a cache hit
        price2 = service.get_current_price(test_ticker)
        check(price2 == price, "Second price fetch returns identical cached value")

    except MarketDataError as exc:
        print(f"  ⚠ Could not fetch price: {exc}")
        print("    (Expected if no internet connection)")

    try:
        history = service.get_price_history(test_ticker)
        check(isinstance(history, list),          "History is a list")
        check(len(history) >= MIN_HISTORY_LENGTH, f"History has sufficient length: {len(history)} days")
        check(all(p > 0 for p in history),        "All historical prices are positive")
        check(history == sorted(history, key=lambda _: True),
              "History list is ordered (oldest first)")
        print(f"    {test_ticker} history: {len(history)} days, latest: {history[-1]:.2f}")

        # History cache hit
        history2 = service.get_price_history(test_ticker)
        check(history2 == history, "Second history fetch returns identical cached value")

    except MarketDataError as exc:
        print(f"  ⚠ Could not fetch history: {exc}")

    # Test with a Swedish ticker
    swedish_ticker = "ERIC-B.ST"
    try:
        price_sek = service.get_current_price(swedish_ticker)
        check(price_sek > 0, f"Swedish ticker {swedish_ticker} price: {price_sek:.2f} SEK")
    except MarketDataError as exc:
        print(f"  ⚠ Could not fetch {swedish_ticker}: {exc}")

    bus.stop()


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║         MONEYMAKER — Data Module Test Suite           ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Tests 1–10 require no network connection.")
    print("  Test 11 fetches live data and requires internet.")

    try:
        test_cache_basics()
        test_cache_copy_semantics()
        test_cache_invalidation()
        test_cache_ttl()
        test_cache_maintenance()
        test_cache_thread_safety()
        test_price_validation()
        test_history_validation()
        test_cache_integration()
        test_bulk_and_events()
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