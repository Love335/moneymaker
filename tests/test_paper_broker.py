"""
test_paper_broker.py — Tests for the paper trading broker.

Verifies portfolio persistence, order execution, P&L calculation,
and reset functionality. No hardware or network required.

Run with:
    cd ~/moneymaker
    sudo ~/moneymaker/venv/bin/python3 tests/test_paper_broker.py
"""

import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading.paper_broker import PaperBroker
from trading.broker import OrderStatus

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


def make_broker(balance: float = 10_000.0) -> PaperBroker:
    """Create a fresh broker backed by a temp file."""
    import trading.paper_broker as pb
    # Redirect portfolio file to a temp location for testing
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    Path(tmp.name).unlink()   # delete so broker starts fresh
    pb.PORTFOLIO_FILE = Path(tmp.name)
    return PaperBroker(starting_balance=balance)


# ══════════════════════════════════════════════════════════════
#  TEST 1 — Initialisation
# ══════════════════════════════════════════════════════════════

def test_init() -> None:
    section("TEST 1: Initialisation")
    broker = make_broker(10_000.0)

    overview = broker.get_account_overview()
    check(overview.liquid_sek == 10_000.0, "Starting balance is correct")
    check(len(overview.positions) == 0,    "No positions on fresh start")
    check(overview.account_id == "PAPER",  "Account ID is PAPER")
    check(broker.is_connected(),           "is_connected() returns True")
    check(broker.cancel_all_orders(),      "cancel_all_orders() returns True")


# ══════════════════════════════════════════════════════════════
#  TEST 2 — Buying
# ══════════════════════════════════════════════════════════════

def test_buying() -> None:
    section("TEST 2: Buying")
    broker = make_broker(10_000.0)

    # Seed a price so the broker can execute
    broker.update_price("ERIC-B.ST", 75.0)

    result = broker.place_order("ERIC-B.ST", "BUY", 1_000.0)
    check(result.status == OrderStatus.FILLED, "BUY order is filled")
    check(result.ticker == "ERIC-B.ST",        "Correct ticker in result")
    check(result.action == "BUY",              "Correct action in result")
    check(result.quantity > 0,                 "Quantity is positive")
    check(result.executed_price == 75.0,       "Executed at correct price")

    overview = broker.get_account_overview()
    check(overview.liquid_sek < 10_000.0,      "Cash reduced after buy")
    check(len(overview.positions) == 1,        "One position held")
    check(overview.positions[0].ticker == "ERIC-B.ST", "Correct ticker in positions")

    # Buy more of the same stock — should average in
    broker.update_price("ERIC-B.ST", 80.0)
    result2 = broker.place_order("ERIC-B.ST", "BUY", 500.0)
    check(result2.status == OrderStatus.FILLED, "Second BUY is filled")
    overview2 = broker.get_account_overview()
    check(len(overview2.positions) == 1,        "Still one position (averaged in)")
    check(overview2.positions[0].quantity > result.quantity, "Quantity increased")

    # Buy with insufficient funds
    broker2 = make_broker(100.0)
    broker2.update_price("VOLV-B.ST", 200.0)
    result3 = broker2.place_order("VOLV-B.ST", "BUY", 5_000.0)
    # Should partially fill or reject — either is acceptable
    check(
        result3.total_sek <= 100.0,
        f"Cannot spend more than available (spent {result3.total_sek:.2f})"
    )


# ══════════════════════════════════════════════════════════════
#  TEST 3 — Selling
# ══════════════════════════════════════════════════════════════

def test_selling() -> None:
    section("TEST 3: Selling")
    broker = make_broker(10_000.0)

    # Buy first
    broker.update_price("ERIC-B.ST", 75.0)
    broker.place_order("ERIC-B.ST", "BUY", 2_000.0)
    cash_after_buy = broker.get_account_overview().liquid_sek

    # Sell at a higher price → profit
    broker.update_price("ERIC-B.ST", 90.0)
    result = broker.place_order("ERIC-B.ST", "SELL", 2_000.0)
    check(result.status == OrderStatus.FILLED,  "SELL order is filled")
    check(result.executed_price == 90.0,        "Sold at correct price")

    overview = broker.get_account_overview()
    check(len(overview.positions) == 0,          "Position closed after SELL")
    check(overview.liquid_sek > cash_after_buy,  "Cash increased after profitable SELL")

    # Verify P&L is positive
    pnl = broker.get_all_time_pnl()
    check(pnl > 0, f"P&L is positive after profitable trade: {pnl:.2f} SEK")

    # Sell stock we don't own → rejected
    result2 = broker.place_order("VOLV-B.ST", "SELL", 1_000.0)
    check(result2.status == OrderStatus.REJECTED, "SELL rejected for unowned stock")


# ══════════════════════════════════════════════════════════════
#  TEST 4 — P&L calculation
# ══════════════════════════════════════════════════════════════

def test_pnl() -> None:
    section("TEST 4: P&L Calculation")
    broker = make_broker(10_000.0)

    # Initial P&L should be zero
    check(broker.get_all_time_pnl() == 0.0, "Initial P&L is zero")

    # Buy then sell at loss
    broker.update_price("ERIC-B.ST", 100.0)
    broker.place_order("ERIC-B.ST", "BUY", 2_000.0)

    broker.update_price("ERIC-B.ST", 80.0)
    broker.place_order("ERIC-B.ST", "SELL", 2_000.0)

    pnl = broker.get_all_time_pnl()
    check(pnl < 0, f"P&L is negative after loss trade: {pnl:.2f} SEK")

    # Unrealised P&L — buy and hold, price rises
    broker2 = make_broker(10_000.0)
    broker2.update_price("VOLV-B.ST", 100.0)
    broker2.place_order("VOLV-B.ST", "BUY", 2_000.0)
    broker2.update_price("VOLV-B.ST", 120.0)

    unrealised = broker2.get_all_time_pnl()
    check(unrealised > 0, f"Unrealised P&L positive when price rises: {unrealised:.2f}")


# ══════════════════════════════════════════════════════════════
#  TEST 5 — Persistence
# ══════════════════════════════════════════════════════════════

def test_persistence() -> None:
    section("TEST 5: Portfolio Persistence")
    import trading.paper_broker as pb

    # Use a specific temp file we control
    tmp_path = Path(tempfile.mktemp(suffix=".json"))
    pb.PORTFOLIO_FILE = tmp_path

    # Create broker, make a trade
    broker1 = PaperBroker(starting_balance=10_000.0)
    broker1.update_price("ERIC-B.ST", 75.0)
    broker1.place_order("ERIC-B.ST", "BUY", 2_000.0)
    balance_before = broker1.get_account_overview().liquid_sek

    check(tmp_path.exists(), "Portfolio file created after trade")

    # Create a second broker instance pointing to same file
    broker2 = PaperBroker(starting_balance=10_000.0)
    balance_after = broker2.get_account_overview().liquid_sek

    check(
        abs(balance_after - balance_before) < 0.01,
        f"Balance persisted correctly ({balance_after:.2f} SEK)"
    )
    check(
        len(broker2.get_account_overview().positions) == 1,
        "Position persisted correctly"
    )

    # Cleanup
    tmp_path.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════
#  TEST 6 — Reset
# ══════════════════════════════════════════════════════════════

def test_reset() -> None:
    section("TEST 6: Portfolio Reset")
    broker = make_broker(10_000.0)

    # Make some trades
    broker.update_price("ERIC-B.ST", 75.0)
    broker.place_order("ERIC-B.ST", "BUY", 3_000.0)
    broker.update_price("ERIC-B.ST", 90.0)
    broker.place_order("ERIC-B.ST", "SELL", 3_000.0)

    pnl_before = broker.get_all_time_pnl()
    check(pnl_before != 0.0, "P&L non-zero before reset")

    # Reset with a different balance
    broker.reset(5_000.0)

    overview = broker.get_account_overview()
    check(overview.liquid_sek == 5_000.0,  "Balance reset to new amount")
    check(len(overview.positions) == 0,    "All positions cleared on reset")
    check(broker.get_all_time_pnl() == 0.0, "P&L reset to zero")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║         MONEYMAKER — Paper Broker Test Suite          ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Tests paper trading logic in isolation.")
    print("  No hardware or network connection required.")

    try:
        test_init()
        test_buying()
        test_selling()
        test_pnl()
        test_persistence()
        test_reset()

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