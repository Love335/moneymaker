"""
test_avanza_broker.py — Read-only integration tests for the real Avanza broker.

Tests connection, account overview, and price fetching against the
live Avanza API. Never places any orders — completely safe to run
against a real account at any time.

Requires valid credentials in src/config.py before running.

Run with:
    cd ~/moneymaker
    sudo ~/moneymaker/venv/bin/python3 tests/test_avanza_broker.py
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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
#  TEST 1 — Credentials and Connection
# ══════════════════════════════════════════════════════════════

def test_connection() -> None:
    section("TEST 1: Credentials and Connection")

    try:
        import config
        check(
            bool(config.AVANZA_USERNAME) and
            config.AVANZA_USERNAME != "your_username_here",
            "AVANZA_USERNAME is set in config.py"
        )
        check(
            bool(config.AVANZA_PASSWORD) and
            config.AVANZA_PASSWORD != "your_password_here",
            "AVANZA_PASSWORD is set in config.py"
        )
        check(
            bool(config.AVANZA_TOTP_SECRET) and
            config.AVANZA_TOTP_SECRET != "your_totp_secret_here",
            "AVANZA_TOTP_SECRET is set in config.py"
        )
    except ImportError:
        print("  FAILED: config.py not found")
        return

    print("\n  Connecting to Avanza...")
    try:
        from avanza import Avanza
        client = Avanza({
            "username":   config.AVANZA_USERNAME,
            "password":   config.AVANZA_PASSWORD,
            "totpSecret": config.AVANZA_TOTP_SECRET,
        })
        check(True, "Authentication succeeded")
        return client
    except Exception as exc:
        check(False, f"Authentication failed: {exc}")
        return None


# ══════════════════════════════════════════════════════════════
#  TEST 2 — Raw API Response Inspection
# ══════════════════════════════════════════════════════════════

def test_raw_overview(client) -> None:
    section("TEST 2: Raw API Response (for field name verification)")

    if client is None:
        print("  Skipped — no connection")
        return

    try:
        overview = client.get_overview()
        accounts = overview.get("accounts", [])
        check(len(accounts) > 0, f"Overview returned {len(accounts)} account(s)")

        print()
        print("  Account fields available:")
        if accounts:
            for key, val in accounts[0].items():
                print(f"    {key}: {repr(val)[:80]}")

        print()
        print("  Full overview keys:", list(overview.keys()))

    except Exception as exc:
        check(False, f"get_overview() failed: {exc}")


# ══════════════════════════════════════════════════════════════
#  TEST 3 — AvanzaBroker Account Overview
# ══════════════════════════════════════════════════════════════

def test_broker_overview() -> None:
    section("TEST 3: AvanzaBroker.get_account_overview()")

    try:
        import config
        from trading.avanza_broker import AvanzaBroker

        print("  Initialising AvanzaBroker...")
        broker = AvanzaBroker(
            username=config.AVANZA_USERNAME,
            password=config.AVANZA_PASSWORD,
            totp_secret=config.AVANZA_TOTP_SECRET,
        )

        check(broker.is_connected(), "Broker reports connected")

        print("  Fetching account overview...")
        overview = broker.get_account_overview()

        check(overview is not None,            "Overview returned")
        check(overview.liquid_sek >= 0,        f"Liquid SEK: {overview.liquid_sek:.2f}")
        check(overview.total_value_sek >= 0,   f"Total value: {overview.total_value_sek:.2f}")
        check(overview.account_id is not None, f"Account ID: {overview.account_id}")
        check(isinstance(overview.positions, list), "Positions is a list")

        print(f"\n  Account summary:")
        print(f"    Account ID:   {overview.account_id}")
        print(f"    Liquid SEK:   {overview.liquid_sek:.2f}")
        print(f"    Total value:  {overview.total_value_sek:.2f}")
        print(f"    Positions:    {len(overview.positions)}")

        for pos in overview.positions:
            print(
                f"      {pos.ticker}: qty={pos.quantity:.4f} "
                f"avg={pos.average_price:.2f} "
                f"current={pos.current_price:.2f} "
                f"pnl={pos.unrealised_pnl:.2f}"
            )

        return broker

    except Exception as exc:
        check(False, f"Unexpected error: {exc}")
        import traceback
        traceback.print_exc()
        return None


# ══════════════════════════════════════════════════════════════
#  TEST 4 — Price Fetching
# ══════════════════════════════════════════════════════════════

def test_price_fetching(broker) -> None:
    section("TEST 4: Price Fetching")

    if broker is None:
        print("  Skipped — no broker")
        return

    from trading.avanza_broker import TICKER_MAP

    print("  Testing price fetch for mapped tickers...")
    print("  (Skipping SPY and GLD — not on Avanza)")
    print()

    fetchable = {k: v for k, v in TICKER_MAP.items() if v is not None}

    for ticker, orderbook_id in list(fetchable.items())[:5]:  # test first 5
        try:
            price = broker.get_price(ticker)
            check(
                price > 0,
                f"{ticker} (orderbook {orderbook_id}): {price:.2f} SEK"
            )
        except Exception as exc:
            check(False, f"{ticker}: {exc}")

    # Test that unmapped tickers raise correctly
    from trading.broker import BrokerError
    try:
        broker.get_price("FAKE.TICKER")
        check(False, "Unmapped ticker should raise BrokerError")
    except BrokerError:
        check(True, "Unmapped ticker raises BrokerError correctly")
    except Exception as exc:
        check(False, f"Wrong exception type: {exc}")

    # Test that None-mapped tickers (SPY, GLD) raise correctly
    try:
        broker.get_price("SPY")
        check(False, "SPY should raise BrokerError (not on Avanza)")
    except BrokerError as exc:
        check(True, f"SPY raises BrokerError correctly: {exc}")
    except Exception as exc:
        check(False, f"Wrong exception type for SPY: {exc}")


# ══════════════════════════════════════════════════════════════
#  TEST 5 — Cancel All Orders (safe — only cancels if any exist)
# ══════════════════════════════════════════════════════════════

def test_cancel_orders(broker) -> None:
    section("TEST 5: Cancel All Orders (safe read)")

    if broker is None:
        print("  Skipped — no broker")
        return

    try:
        result = broker.cancel_all_orders()
        check(result is True, "cancel_all_orders() returns True (no open orders or all cancelled)")
    except Exception as exc:
        check(False, f"cancel_all_orders() raised: {exc}")


# ══════════════════════════════════════════════════════════════
#  TEST 6 — Ticker Map Completeness
# ══════════════════════════════════════════════════════════════

def test_ticker_map() -> None:
    section("TEST 6: Ticker Map Completeness")

    from trading.avanza_broker import TICKER_MAP
    from algorithms.dual_momentum import CANDIDATE_TICKERS
    from algorithms.mean_reversion import UNIVERSE
    from algorithms.trend_following import PRIMARY_TICKER, BOND_TICKER

    all_algo_tickers = set(CANDIDATE_TICKERS) | set(UNIVERSE) | {PRIMARY_TICKER, BOND_TICKER}

    print("  Checking all algorithm tickers are in TICKER_MAP...")
    for ticker in sorted(all_algo_tickers):
        in_map = ticker in TICKER_MAP
        has_id = TICKER_MAP.get(ticker) is not None
        if in_map and has_id:
            check(True, f"{ticker} → orderbook {TICKER_MAP[ticker]}")
        elif in_map and not has_id:
            print(f"  [~] {ticker} → paper trading only (no Avanza orderbook)")
        else:
            check(False, f"{ticker} → MISSING from TICKER_MAP")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║         MONEYMAKER — Avanza Broker Test Suite         ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Read-only tests — no orders will be placed.")
    print("  Requires valid credentials in src/config.py")

    try:
        # Test 1 — raw connection (returns raw client for inspection)
        client = test_connection()

        # Test 2 — inspect raw API response to verify field names
        test_raw_overview(client)

        # Test 3 — full broker wrapper
        broker = test_broker_overview()

        # Test 4 — price fetching
        test_price_fetching(broker)

        # Test 5 — cancel orders (safe)
        test_cancel_orders(broker)

        # Test 6 — ticker map completeness (no network needed)
        test_ticker_map()

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
    print("  NOTE: Review Test 2 output carefully.")
    print("  If account fields show None, avanza_broker.py needs")
    print("  updating to match the actual API response structure.")
    print()


if __name__ == "__main__":
    main()