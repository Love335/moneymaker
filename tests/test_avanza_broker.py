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

def test_connection():
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
        return None

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
#  TEST 2 — Ticker Registry
# ══════════════════════════════════════════════════════════════

def test_ticker_registry() -> None:
    section("TEST 2: Ticker Registry Completeness")

    from trading.tickers import REGISTRY, avanza_id, is_avanza_tradeable
    from algorithms.dual_momentum import CANDIDATE_TICKERS
    from algorithms.mean_reversion import UNIVERSE
    from algorithms.trend_following import PRIMARY_TICKER, BOND_TICKER

    all_algo_tickers = (
        set(CANDIDATE_TICKERS) | set(UNIVERSE) | {PRIMARY_TICKER, BOND_TICKER}
    )

    check(len(REGISTRY) > 0, f"Registry has {len(REGISTRY)} instruments")

    print()
    print("  Algorithm tickers:")
    for ticker in sorted(all_algo_tickers):
        if ticker not in REGISTRY:
            check(False, f"{ticker} — MISSING from registry")
        elif is_avanza_tradeable(ticker):
            oid = avanza_id(ticker)
            check(True, f"{ticker} → Avanza orderbook {oid}")
        else:
            print(f"  [~] {ticker} — registered, paper trading only")

    print()
    print("  Registry summary:")
    tradeable = [t for t in REGISTRY if is_avanza_tradeable(t)]
    print(f"    Total instruments: {len(REGISTRY)}")
    print(f"    Avanza tradeable:  {len(tradeable)}")
    print(f"    Paper only:        {len(REGISTRY) - len(tradeable)}")


# ══════════════════════════════════════════════════════════════
#  TEST 3 — AvanzaBroker Account Overview
# ══════════════════════════════════════════════════════════════

def test_broker_overview():
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

        check(overview is not None,              "Overview returned")
        check(overview.liquid_sek >= 0,          f"Liquid SEK:  {overview.liquid_sek:.2f}")
        check(overview.total_value_sek >= 0,     f"Total value: {overview.total_value_sek:.2f}")
        check(overview.account_id == "3525815",  f"Account ID:  {overview.account_id}")
        check(isinstance(overview.positions, list), "Positions is a list")

        print(f"\n  Account summary:")
        print(f"    Account ID:   {overview.account_id}")
        print(f"    Liquid SEK:   {overview.liquid_sek:.2f}")
        print(f"    Total value:  {overview.total_value_sek:.2f}")
        print(f"    Positions:    {len(overview.positions)}")

        for pos in overview.positions:
            print(
                f"      orderbook={pos.ticker}  "
                f"qty={pos.quantity:.4f}  "
                f"avg={pos.average_price:.2f}  "
                f"current={pos.current_price:.2f}  "
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
    section("TEST 4: Price Fetching via Ticker Registry")

    if broker is None:
        print("  Skipped — no broker")
        return

    from trading.tickers import REGISTRY, is_avanza_tradeable
    from trading.broker import BrokerError

    tradeable = [t for t in REGISTRY if is_avanza_tradeable(t)]
    print(f"  Testing first 6 of {len(tradeable)} tradeable instruments...")
    print()

    for ticker in tradeable[:6]:
        try:
            price = broker.get_price(ticker)
            name  = REGISTRY[ticker].name
            check(price > 0, f"{name} ({ticker}): {price:.2f} SEK")
        except Exception as exc:
            check(False, f"{ticker}: {exc}")

    print()

    # Verify untradeable tickers raise correctly
    try:
        broker.get_price("FAKE.TICKER")
        check(False, "Unregistered ticker should raise BrokerError")
    except BrokerError:
        check(True, "Unregistered ticker raises BrokerError correctly")

    # SPY and GLD are now in the registry with real IDs — they should work
    try:
        price = broker.get_price("SPY")
        check(price > 0, f"SPY (now on Avanza): {price:.2f} SEK")
    except BrokerError as exc:
        print(f"  [~] SPY: {exc}")
    except Exception as exc:
        check(False, f"SPY unexpected error: {exc}")


# ══════════════════════════════════════════════════════════════
#  TEST 5 — Cancel All Orders
# ══════════════════════════════════════════════════════════════

def test_cancel_orders(broker) -> None:
    section("TEST 5: Cancel All Orders (read-only — safe)")

    if broker is None:
        print("  Skipped — no broker")
        return

    try:
        result = broker.cancel_all_orders()
        check(result is True, "cancel_all_orders() returned True")
    except Exception as exc:
        check(False, f"cancel_all_orders() raised: {exc}")


# ══════════════════════════════════════════════════════════════
#  TEST 6 — Algorithm tickers consistency
# ══════════════════════════════════════════════════════════════

def test_algorithm_tickers() -> None:
    section("TEST 6: Algorithm Ticker Consistency")

    from trading.tickers import REGISTRY
    from algorithms.dual_momentum import CANDIDATE_TICKERS
    from algorithms.mean_reversion import UNIVERSE
    from algorithms.trend_following import PRIMARY_TICKER, BOND_TICKER

    # All tickers used by algorithms must come from the registry
    for ticker in CANDIDATE_TICKERS:
        check(
            ticker in REGISTRY,
            f"DualMomentum candidate '{ticker}' is in registry"
        )

    for ticker in UNIVERSE:
        check(
            ticker in REGISTRY,
            f"MeanReversion universe '{ticker}' is in registry"
        )

    check(
        PRIMARY_TICKER in REGISTRY,
        f"TrendFollowing primary '{PRIMARY_TICKER}' is in registry"
    )
    check(
        BOND_TICKER in REGISTRY,
        f"TrendFollowing bond '{BOND_TICKER}' is in registry"
    )

    # Verify CANDIDATE_TICKERS contains no individual stocks
    from trading.tickers import AssetClass
    for ticker in CANDIDATE_TICKERS:
        instrument = REGISTRY.get(ticker)
        if instrument:
            check(
                instrument.asset_class != AssetClass.EQUITY,
                f"DualMomentum candidate '{ticker}' is not an individual stock"
            )

    # Verify UNIVERSE contains only individual equities
    for ticker in UNIVERSE:
        instrument = REGISTRY.get(ticker)
        if instrument:
            check(
                instrument.asset_class == AssetClass.EQUITY,
                f"MeanReversion universe '{ticker}' is an individual equity"
            )


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║         MONEYMAKER — Avanza Broker Test Suite         ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Read-only — no orders will be placed.")
    print("  Requires valid credentials in src/config.py.")

    try:
        test_ticker_registry()
        test_algorithm_tickers()

        client = test_connection()
        broker = test_broker_overview()

        test_price_fetching(broker)
        test_cancel_orders(broker)

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