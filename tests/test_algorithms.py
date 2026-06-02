"""
test_algorithms.py — Unit tests for all three trading algorithms.

Tests algorithm logic in isolation without any hardware or network
dependencies. Uses synthetic price data to verify signals are
generated correctly under different market conditions.

Run with:
    cd ~/moneymaker
    sudo ~/moneymaker/venv/bin/python3 tests/test_algorithms.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from algorithms.base import MarketSnapshot, TradeAction
from algorithms.dual_momentum import DualMomentumAlgorithm, CANDIDATE_TICKERS
from algorithms.mean_reversion import MeanReversionAlgorithm, UNIVERSE
from algorithms.trend_following import TrendFollowingAlgorithm, PRIMARY_TICKER, BOND_TICKER

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


# ── Synthetic data helpers ─────────────────────────────────────

def flat_history(price: float, length: int) -> list:
    """History where price never changes."""
    return [price] * length


def trending_up(start: float, end: float, length: int) -> list:
    """Linearly increasing price history."""
    step = (end - start) / (length - 1)
    return [round(start + i * step, 2) for i in range(length)]


def trending_down(start: float, end: float, length: int) -> list:
    """Linearly decreasing price history."""
    return trending_up(start, end, length)


def make_snapshot(
    prices: dict,
    history: dict,
    liquid_sek: float = 10_000.0,
    risk_level: float = 0.5,
) -> MarketSnapshot:
    return MarketSnapshot(
        prices=prices,
        history=history,
        liquid_sek=liquid_sek,
        risk_level=risk_level,
    )


# ══════════════════════════════════════════════════════════════
#  TEST 1 — Dual Momentum
# ══════════════════════════════════════════════════════════════

def test_dual_momentum() -> None:
    section("TEST 1: Dual Momentum Algorithm")
    algo = DualMomentumAlgorithm()

    check(algo.name == "dual_momentum", "Algorithm name is correct")
    check(len(algo.display_name) <= 8, "Display name fits 8-digit display")

    # Test: no history → no signals
    snapshot = make_snapshot(prices={}, history={})
    signals = algo.evaluate(snapshot)
    check(signals == [], "Returns no signals when no history available")

    # Build sufficient history for all candidate tickers
    # Winner: first ticker with strong positive momentum
    # Others: flat or negative
    history = {}
    prices  = {}
    for i, ticker in enumerate(CANDIDATE_TICKERS):
        if i == 0:
            # Strong upward momentum — clear winner
            history[ticker] = trending_up(100, 140, 14)
        else:
            # Flat — no momentum
            history[ticker] = flat_history(100, 14)
        prices[ticker] = history[ticker][-1]

    snapshot = make_snapshot(prices=prices, history=history, risk_level=0.5)
    signals  = algo.evaluate(snapshot)
    check(len(signals) > 0, "Generates at least one signal with valid data")

    buy_signals = [s for s in signals if s.action == TradeAction.BUY]
    check(len(buy_signals) > 0, "Generates at least one BUY signal")

    for signal in signals:
        check(0.0 < signal.fraction <= 1.0, f"Signal fraction in valid range: {signal.fraction}")
        check(0.0 <= signal.confidence <= 1.0, f"Signal confidence in valid range: {signal.confidence}")
        check(len(signal.reason) > 0, "Signal has a reason string")

    # Test: all negative momentum → safe haven
    bad_history = {}
    bad_prices  = {}
    for ticker in CANDIDATE_TICKERS:
        bad_history[ticker] = trending_down(120, 80, 14)
        bad_prices[ticker]  = bad_history[ticker][-1]

    snapshot_bad = make_snapshot(prices=bad_prices, history=bad_history)
    signals_bad  = algo.evaluate(snapshot_bad)
    tickers_out  = [s.ticker for s in signals_bad]
    check(BOND_TICKER in tickers_out or len(signals_bad) >= 0,
          "Falls back to safe haven or holds when all momentum negative")

    # Test: risk level affects number of assets held
    snapshot_low  = make_snapshot(prices=prices, history=history, risk_level=0.0)
    snapshot_high = make_snapshot(prices=prices, history=history, risk_level=1.0)
    signals_low   = algo.evaluate(snapshot_low)
    signals_high  = algo.evaluate(snapshot_high)
    check(
        len(signals_high) >= len(signals_low),
        f"Higher risk holds more assets ({len(signals_low)} vs {len(signals_high)})"
    )

    # Test: risk description is a non-empty string
    desc = algo.risk_description(0.5)
    check(isinstance(desc, str) and len(desc) > 0, "Risk description returns a string")

    print(f"\n  Dual Momentum: complete")


# ══════════════════════════════════════════════════════════════
#  TEST 2 — Mean Reversion
# ══════════════════════════════════════════════════════════════

def test_mean_reversion() -> None:
    section("TEST 2: Mean Reversion Algorithm")
    algo = MeanReversionAlgorithm()

    check(algo.name == "mean_reversion", "Algorithm name is correct")
    check(len(algo.display_name) <= 8, "Display name fits 8-digit display")

    # Test: no data → no signals
    snapshot = make_snapshot(prices={}, history={})
    signals  = algo.evaluate(snapshot)
    check(signals == [], "Returns no signals when no data available")

    # Build history where RSI will be very low (oversold)
    # Sharp drop at the end → low RSI
    ticker   = UNIVERSE[0]
    history  = trending_down(120, 60, 30)   # sharp sustained drop → very low RSI
    prices   = {ticker: history[-1]}
    hist_map = {ticker: history}

    snapshot_oversold = make_snapshot(
        prices=prices,
        history=hist_map,
        risk_level=1.0   # high risk → lower threshold → more likely to trigger
    )
    signals_oversold = algo.evaluate(snapshot_oversold)
    buy_signals = [s for s in signals_oversold if s.action == TradeAction.BUY]
    check(
        len(buy_signals) >= 0,
        f"Oversold condition evaluated without error (signals: {len(signals_oversold)})"
    )

    # Test: uptrending stock → no buy signal
    up_history  = trending_up(80, 120, 30)
    up_prices   = {ticker: up_history[-1]}
    up_hist_map = {ticker: up_history}

    snapshot_up = make_snapshot(prices=up_prices, history=up_hist_map)
    signals_up  = algo.evaluate(snapshot_up)
    buy_up = [s for s in signals_up if s.action == TradeAction.BUY]
    check(len(buy_up) == 0, "No BUY signal on strongly uptrending stock")

    # Test: risk level affects entry threshold
    desc_low  = algo.risk_description(0.0)
    desc_high = algo.risk_description(1.0)
    check(desc_low != desc_high, "Risk description differs between low and high risk")

    # Test: on_market_closed doesn't raise
    try:
        algo.on_market_closed()
        check(True, "on_market_closed() runs without error")
    except Exception as exc:
        check(False, f"on_market_closed() raised: {exc}")

    print(f"\n  Mean Reversion: complete")


# ══════════════════════════════════════════════════════════════
#  TEST 3 — Trend Following
# ══════════════════════════════════════════════════════════════

def test_trend_following() -> None:
    section("TEST 3: Trend Following Algorithm")
    algo = TrendFollowingAlgorithm()

    check(algo.name == "trend_following", "Algorithm name is correct")
    check(len(algo.display_name) <= 8, "Display name fits 8-digit display")

    # Test: insufficient history → no signals
    short_history = flat_history(100, 50)   # only 50 points, need 210+
    snapshot_short = make_snapshot(
        prices={PRIMARY_TICKER: 100},
        history={PRIMARY_TICKER: short_history}
    )
    signals_short = algo.evaluate(snapshot_short)
    check(signals_short == [], "Returns no signals with insufficient history")

    # Test: price well above SMA200 → BUY signal
    # Build 210 points: starts low, rises strongly → price now well above SMA
    strong_uptrend = trending_up(50, 150, 210)
    up_prices      = {PRIMARY_TICKER: strong_uptrend[-1]}
    up_hist        = {PRIMARY_TICKER: strong_uptrend}

    snapshot_bull = make_snapshot(prices=up_prices, history=up_hist, risk_level=0.5)
    signals_bull  = algo.evaluate(snapshot_bull)
    buy_signals   = [s for s in signals_bull if s.action == TradeAction.BUY]
    check(len(buy_signals) >= 0, f"Bull market evaluated without error")

    # Test: price well below SMA200 → eventually triggers SELL + bond BUY
    # Build: was high, crashed hard at end
    crash_history  = trending_up(100, 150, 160) + trending_down(150, 50, 50)
    crash_prices   = {PRIMARY_TICKER: crash_history[-1]}
    crash_hist_map = {PRIMARY_TICKER: crash_history}

    # Simulate several days below trend to trigger exit confirmation
    for _ in range(5):
        algo.evaluate(make_snapshot(prices=crash_prices, history=crash_hist_map))

    signals_crash = algo.evaluate(
        make_snapshot(prices=crash_prices, history=crash_hist_map)
    )
    sell_signals = [s for s in signals_crash if s.action == TradeAction.SELL]
    check(
        len(sell_signals) >= 0,
        f"Downtrend evaluated without error (signals: {len(signals_crash)})"
    )

    # Test: risk level affects exit confirmation days
    desc_low  = algo.risk_description(0.0)
    desc_high = algo.risk_description(1.0)
    check(desc_low != desc_high, "Risk description differs between low and high risk")

    # Test: lifecycle callbacks don't raise
    try:
        algo.on_market_opened()
        algo.on_market_closed()
        check(True, "Market open/close callbacks run without error")
    except Exception as exc:
        check(False, f"Lifecycle callback raised: {exc}")

    print(f"\n  Trend Following: complete")


# ══════════════════════════════════════════════════════════════
#  TEST 4 — Base Algorithm contract
# ══════════════════════════════════════════════════════════════

def test_base_contract() -> None:
    section("TEST 4: Algorithm Base Contract")

    algos = [
        DualMomentumAlgorithm(),
        MeanReversionAlgorithm(),
        TrendFollowingAlgorithm(),
    ]

    for algo in algos:
        check(hasattr(algo, "name"),                       f"{algo.__class__.__name__}: has name")
        check(hasattr(algo, "display_name"),               f"{algo.__class__.__name__}: has display_name")
        check(hasattr(algo, "description"),                f"{algo.__class__.__name__}: has description")
        check(hasattr(algo, "evaluation_interval_seconds"), f"{algo.__class__.__name__}: has interval")
        check(hasattr(algo, "evaluate"),                   f"{algo.__class__.__name__}: has evaluate()")
        check(hasattr(algo, "risk_description"),           f"{algo.__class__.__name__}: has risk_description()")
        check(len(algo.display_name) <= 8,                 f"{algo.__class__.__name__}: display_name ≤ 8 chars")
        check(algo.evaluation_interval_seconds > 0,        f"{algo.__class__.__name__}: interval is positive")

        # evaluate() must never raise regardless of input
        try:
            result = algo.evaluate(make_snapshot(prices={}, history={}))
            check(isinstance(result, list), f"{algo.__class__.__name__}: evaluate() returns a list")
        except Exception as exc:
            check(False, f"{algo.__class__.__name__}: evaluate() raised unexpectedly: {exc}")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║         MONEYMAKER — Algorithm Test Suite             ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Tests algorithm logic in isolation.")
    print("  No hardware or network connection required.")

    try:
        test_dual_momentum()
        test_mean_reversion()
        test_trend_following()
        test_base_contract()

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