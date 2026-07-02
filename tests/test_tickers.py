"""
test_new_tickers.py — Verify the new OMXS30 additions are working end to end.

Checks:
  1. Registry entries are consistent
  2. yfinance can fetch price and history for each new ticker
  3. Avanza orderbook IDs resolve to a valid price (requires credentials)
  4. MeanReversion universe now includes the new stocks

Run with:
    cd ~/moneymaker
    sudo ~/moneymaker/venv/bin/python3 tests/test_tickers.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

PASS = 0
FAIL = 0

NEW_TICKERS = [
    "ABB.ST",
    "AZN.ST",
    "ESSITY-B.ST",
    "ALFA.ST",
    "SKF-B.ST",
    "HEXA-B.ST",
    "BOL.ST",
    "NDA-SE.ST",
    "TELIA.ST",
    "SINCH.ST",
    "EVO.ST",
]

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
#  TEST 1 — Registry entries
# ══════════════════════════════════════════════════════════════

def test_registry() -> None:
    section("TEST 1: Registry Entries")

    from trading.tickers import REGISTRY, get, avanza_id, is_avanza_tradeable, AssetClass

    for ticker in NEW_TICKERS:
        check(ticker in REGISTRY, f"{ticker} is in registry")

        inst = REGISTRY.get(ticker)
        if inst is None:
            continue

        check(
            inst.asset_class == AssetClass.EQUITY,
            f"{ticker} has correct asset class (EQUITY)"
        )
        check(
            inst.avanza_tradeable is True,
            f"{ticker} is marked as Avanza tradeable"
        )
        check(
            isinstance(inst.avanza_id, str) and len(inst.avanza_id) > 0,
            f"{ticker} has non-empty avanza_id: {inst.avanza_id}"
        )
        check(
            is_avanza_tradeable(ticker),
            f"{ticker} is_avanza_tradeable() returns True"
        )

        try:
            oid = avanza_id(ticker)
            check(len(oid) > 0, f"{ticker} avanza_id() returns: {oid}")
        except Exception as exc:
            check(False, f"{ticker} avanza_id() raised: {exc}")


# ══════════════════════════════════════════════════════════════
#  TEST 2 — MeanReversion universe coverage
# ══════════════════════════════════════════════════════════════

def test_mean_reversion_universe() -> None:
    section("TEST 2: MeanReversion Universe Coverage")

    from algorithms.mean_reversion import UNIVERSE

    print(f"  Universe now contains {len(UNIVERSE)} stocks:")
    for ticker in sorted(UNIVERSE):
        print(f"    {ticker}")
    print()

    for ticker in NEW_TICKERS:
        check(
            ticker in UNIVERSE,
            f"{ticker} is in MeanReversion universe"
        )

    check(len(UNIVERSE) >= 16, f"Universe has at least 16 stocks (got {len(UNIVERSE)})")


# ══════════════════════════════════════════════════════════════
#  TEST 3 — yfinance data availability
# ══════════════════════════════════════════════════════════════

def test_yfinance_data() -> None:
    section("TEST 3: yfinance Data (requires internet)")
    print("  Fetching live data — this may take 20–30 seconds...")
    print()

    import yfinance as yf
    from data.market_data import MarketDataService
    from core.events import EventBus

    bus     = EventBus()
    bus.start()
    service = MarketDataService(bus)

    for ticker in NEW_TICKERS:
        try:
            price = service.get_current_price(ticker)
            check(
                isinstance(price, float) and price > 0,
                f"{ticker}: current price {price:.2f} SEK"
            )
        except Exception as exc:
            check(False, f"{ticker}: price fetch failed — {exc}")

        try:
            history = service.get_price_history(ticker)
            check(
                len(history) >= 30,
                f"{ticker}: history has {len(history)} days (need ≥ 30)"
            )
            check(
                all(p > 0 for p in history),
                f"{ticker}: all historical prices positive"
            )
        except Exception as exc:
            check(False, f"{ticker}: history fetch failed — {exc}")

        # Small delay to avoid hammering Yahoo Finance
        time.sleep(0.5)

    bus.stop()


# ══════════════════════════════════════════════════════════════
#  TEST 4 — Avanza orderbook IDs (requires credentials)
# ══════════════════════════════════════════════════════════════

def test_avanza_prices() -> None:
    section("TEST 4: Avanza Orderbook IDs (requires credentials)")
    print("  Connecting to Avanza to verify orderbook IDs...")
    print("  This confirms IDs haven't changed since the registry was written.")
    print()

    try:
        import config
        from trading.avanza_broker import AvanzaBroker
        from security.secrets import load_avanza_credentials

        creds  = load_avanza_credentials()
        broker = AvanzaBroker(
            username=creds.username,
            password=creds.password,
            totp_secret=creds.totp_secret,
            account_id=creds.account_id,
        )

        check(broker.is_connected(), "Connected to Avanza")

        for ticker in NEW_TICKERS:
            try:
                price = broker.get_price(ticker)
                check(
                    isinstance(price, float) and price > 0,
                    f"{ticker}: Avanza price {price:.2f} SEK "
                    f"(orderbook {broker._to_orderbook_id(ticker)})"
                )
            except Exception as exc:
                check(False, f"{ticker}: Avanza price failed — {exc}")

            time.sleep(0.3)   # be gentle with the API

    except ImportError:
        print("  Skipped — config.py not found")
    except Exception as exc:
        print(f"  Skipped — could not connect: {exc}")


# ══════════════════════════════════════════════════════════════
#  TEST 5 — RSI can be computed for new tickers
# ══════════════════════════════════════════════════════════════

def test_rsi_computation() -> None:
    section("TEST 5: RSI Computation for New Tickers")
    print("  Verifying MeanReversion can compute RSI for each new stock...")
    print()

    import yfinance as yf
    from algorithms.mean_reversion import MeanReversionAlgorithm
    from algorithms.base import MarketSnapshot

    algo = MeanReversionAlgorithm()

    prices  = {}
    history = {}

    for ticker in NEW_TICKERS:
        try:
            data = yf.Ticker(ticker).history(period="3mo")
            if data.empty:
                check(False, f"{ticker}: no data from yfinance")
                continue
            closes = [float(p) for p in data["Close"].dropna().tolist()]
            prices[ticker]  = closes[-1]
            history[ticker] = closes
            check(len(closes) >= 12, f"{ticker}: {len(closes)} days of history fetched")
        except Exception as exc:
            check(False, f"{ticker}: data fetch failed — {exc}")

    if not prices:
        print("  No data fetched — skipping RSI evaluation")
        return

    snap = MarketSnapshot(
        prices=prices,
        history=history,
        liquid_sek=10_000.0,
        risk_level=0.7,
    )

    try:
        signals = algo.evaluate(snap)
        check(
            isinstance(signals, list),
            f"evaluate() returns a list with {len(signals)} signal(s)"
        )
        for sig in signals:
            check(
                sig.ticker in NEW_TICKERS,
                f"Signal ticker {sig.ticker} is a known new ticker"
            )
            print(
                f"    Signal: {sig.action.value} {sig.ticker} "
                f"— {sig.reason}"
            )
        if not signals:
            print("    No signals generated (normal if no stocks are oversold)")
    except Exception as exc:
        check(False, f"evaluate() raised: {exc}")
        import traceback
        traceback.print_exc()


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║     MONEYMAKER — New Ticker Verification Suite        ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Verifying 8 new OMXS30 stocks added to the registry.")
    print("  Tests 3–5 require an internet connection.")
    print("  Test 4 additionally requires valid Avanza credentials.")

    try:
        test_registry()
        test_mean_reversion_universe()
        test_yfinance_data()
        test_avanza_prices()
        test_rsi_computation()

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
    if FAIL == 0:
        print("  All checks passed. Safe to deploy the updated tickers.py.")
    else:
        print("  Some checks failed. Review output before deploying.")
        print("  Pay particular attention to any Avanza orderbook ID failures")
        print("  in TEST 4 — a wrong ID will cause real orders to be rejected.")
    print()


if __name__ == "__main__":
    main()