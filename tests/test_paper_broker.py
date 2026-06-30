"""
test_paper_broker.py — Tests for the paper trading broker and trading dataclasses.

Verifies portfolio persistence, order execution, P&L calculation,
averaging, commission, reset, and the broker/position dataclasses.
No hardware or network required.

Run with:
    cd ~/moneymaker
    sudo ~/moneymaker/venv/bin/python3 tests/test_paper_broker.py
"""

import sys
import os
import json
import tempfile
import threading
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading.paper_broker import PaperBroker, COMMISSION_SEK
from trading.broker import (
    OrderStatus, Position, OrderResult, AccountOverview, BrokerError
)
from trading.tickers import (
    REGISTRY, get, avanza_id, is_avanza_tradeable,
    all_tickers, tradeable_tickers, AssetClass, Instrument,
)

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
    """Create a fresh isolated broker backed by a unique temp file."""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    return PaperBroker(starting_balance=balance, portfolio_file=tmp)


def make_broker_with_position(
    ticker: str = "ERIC-B.ST",
    price: float = 100.0,
    amount: float = 2_000.0,
    balance: float = 10_000.0,
) -> PaperBroker:
    """Create a broker with one pre-existing position."""
    broker = make_broker(balance)
    broker.update_price(ticker, price)
    broker.place_order(ticker, "BUY", amount)
    return broker


# ══════════════════════════════════════════════════════════════
#  TEST 1 — Position and OrderResult dataclasses
# ══════════════════════════════════════════════════════════════

def test_dataclasses() -> None:
    section("TEST 1: Trading Dataclasses")

    # Position properties
    pos = Position(
        ticker="ERIC-B.ST",
        quantity=10.0,
        average_price=80.0,
        current_price=100.0,
    )
    check(pos.market_value  == 1000.0, f"market_value = qty * current ({pos.market_value})")
    check(pos.cost_basis    == 800.0,  f"cost_basis = qty * avg ({pos.cost_basis})")
    check(pos.unrealised_pnl == 200.0, f"unrealised_pnl = market - cost ({pos.unrealised_pnl})")

    # Position at a loss
    pos_loss = Position(
        ticker="VOLV-B.ST",
        quantity=5.0,
        average_price=200.0,
        current_price=150.0,
    )
    check(pos_loss.unrealised_pnl == -250.0,
          f"Unrealised loss is negative ({pos_loss.unrealised_pnl})")

    # Position with zero gain
    pos_flat = Position(
        ticker="SEB-A.ST",
        quantity=10.0,
        average_price=100.0,
        current_price=100.0,
    )
    check(pos_flat.unrealised_pnl == 0.0, "Zero gain position has zero P&L")

    # OrderResult fields
    result = OrderResult(
        status=OrderStatus.FILLED,
        ticker="ERIC-B.ST",
        action="BUY",
        quantity=10.0,
        executed_price=80.0,
        total_sek=800.0,
        order_id="ABC123",
    )
    check(result.status         == OrderStatus.FILLED, "OrderResult status correct")
    check(result.order_id       == "ABC123",           "OrderResult order_id stored")
    check(result.error_message  is None,               "No error message on filled order")
    check(isinstance(result.timestamp, datetime),      "Timestamp is datetime")

    # Rejected order
    rejected = OrderResult(
        status=OrderStatus.REJECTED,
        ticker="FAKE",
        action="BUY",
        quantity=0,
        executed_price=0,
        total_sek=0,
        error_message="Insufficient funds",
    )
    check(rejected.status        == OrderStatus.REJECTED,    "Rejected status stored")
    check(rejected.error_message == "Insufficient funds",    "Error message stored")

    # AccountOverview
    overview = AccountOverview(
        liquid_sek=5_000.0,
        total_value_sek=7_000.0,
        positions=[pos],
        account_id="PAPER",
    )
    check(overview.liquid_sek      == 5_000.0,  "AccountOverview liquid_sek")
    check(overview.total_value_sek == 7_000.0,  "AccountOverview total_value_sek")
    check(len(overview.positions)  == 1,         "AccountOverview positions")


# ══════════════════════════════════════════════════════════════
#  TEST 2 — Initialisation
# ══════════════════════════════════════════════════════════════

def test_init() -> None:
    section("TEST 2: Initialisation")
    broker = make_broker(10_000.0)

    overview = broker.get_account_overview()
    check(overview.liquid_sek      == 10_000.0, "Starting balance correct")
    check(overview.total_value_sek == 10_000.0, "Total value equals balance on fresh start")
    check(len(overview.positions)  == 0,        "No positions on fresh start")
    check(overview.account_id      == "PAPER",  "Account ID is PAPER")
    check(broker.is_connected(),                "is_connected() returns True")
    check(broker.cancel_all_orders(),           "cancel_all_orders() returns True")
    check(broker.get_all_time_pnl() == 0.0,    "P&L is zero on fresh start")

    # Custom starting balance
    broker2 = make_broker(25_000.0)
    check(
        broker2.get_account_overview().liquid_sek == 25_000.0,
        "Custom starting balance stored correctly"
    )


# ══════════════════════════════════════════════════════════════
#  TEST 3 — Buying
# ══════════════════════════════════════════════════════════════

def test_buying() -> None:
    section("TEST 3: Buying")
    broker = make_broker(10_000.0)
    broker.update_price("ERIC-B.ST", 100.0)

    result = broker.place_order("ERIC-B.ST", "BUY", 1_000.0)
    effective = 1_000.0 - COMMISSION_SEK

    check(result.status        == OrderStatus.FILLED, "BUY order filled")
    check(result.ticker        == "ERIC-B.ST",        "Correct ticker")
    check(result.action        == "BUY",              "Correct action")
    check(result.executed_price == 100.0,             "Correct execution price")
    check(abs(result.quantity - effective / 100.0) < 0.0001,
          f"Correct quantity after commission ({result.quantity:.4f})")

    overview = broker.get_account_overview()
    expected_cash = 10_000.0 - effective
    check(
        abs(overview.liquid_sek - expected_cash) < 0.01,
        f"Cash reduced by effective amount (got {overview.liquid_sek:.2f})"
    )
    check(len(overview.positions) == 1,                "One position held")
    check(overview.positions[0].ticker == "ERIC-B.ST", "Correct ticker in positions")

    # Buy a different ticker — should create second position
    broker.update_price("VOLV-B.ST", 200.0)
    broker.place_order("VOLV-B.ST", "BUY", 500.0)
    overview2 = broker.get_account_overview()
    check(len(overview2.positions) == 2, "Two positions after buying second ticker")

    # No price available → rejected
    result_no_price = broker.place_order("SEB-A.ST", "BUY", 500.0)
    check(
        result_no_price.status == OrderStatus.REJECTED,
        "BUY rejected when no price available"
    )

    # Unknown action → raises BrokerError
    broker.update_price("INVE-B.ST", 50.0)
    try:
        broker.place_order("INVE-B.ST", "HOLD", 500.0)
        check(False, "Unknown action should raise BrokerError")
    except BrokerError:
        check(True, "Unknown action raises BrokerError")


# ══════════════════════════════════════════════════════════════
#  TEST 4 — Averaging in (weighted average price)
# ══════════════════════════════════════════════════════════════

def test_averaging() -> None:
    section("TEST 4: Position Averaging")
    broker = make_broker(10_000.0)

    # Buy 10 units at 100 SEK
    broker.update_price("ERIC-B.ST", 100.0)
    r1 = broker.place_order("ERIC-B.ST", "BUY", 1_000.0)
    qty1 = r1.quantity   # slightly less than 10 due to commission

    # Buy again at 200 SEK
    broker.update_price("ERIC-B.ST", 200.0)
    r2 = broker.place_order("ERIC-B.ST", "BUY", 1_000.0)
    qty2 = r2.quantity

    overview = broker.get_account_overview()
    check(len(overview.positions) == 1, "Still one position after averaging in")

    pos = overview.positions[0]
    total_qty  = qty1 + qty2
    total_cost = (qty1 * 100.0) + (qty2 * 200.0)
    expected_avg = total_cost / total_qty

    check(
        abs(pos.quantity - total_qty) < 0.0001,
        f"Total quantity correct ({pos.quantity:.4f} vs {total_qty:.4f})"
    )
    check(
        abs(pos.average_price - expected_avg) < 0.01,
        f"Weighted average price correct ({pos.average_price:.2f} vs {expected_avg:.2f})"
    )
    check(
        pos.average_price > 100.0 and pos.average_price < 200.0,
        "Average price is between the two buy prices"
    )


# ══════════════════════════════════════════════════════════════
#  TEST 5 — Insufficient funds
# ══════════════════════════════════════════════════════════════

def test_insufficient_funds() -> None:
    section("TEST 5: Insufficient Funds")

    # Exact balance available
    broker = make_broker(500.0)
    broker.update_price("ERIC-B.ST", 100.0)

    # Try to spend more than available — should cap at balance
    result = broker.place_order("ERIC-B.ST", "BUY", 10_000.0)
    check(
        result.total_sek <= 500.0,
        f"Cannot spend more than balance (spent {result.total_sek:.2f})"
    )

    # After spending all cash, another buy should be rejected
    broker2 = make_broker(COMMISSION_SEK * 0.5)  # less than commission
    broker2.update_price("ERIC-B.ST", 100.0)
    result2 = broker2.place_order("ERIC-B.ST", "BUY", 1.0)
    check(
        result2.status == OrderStatus.REJECTED,
        "BUY rejected when balance is less than commission"
    )


# ══════════════════════════════════════════════════════════════
#  TEST 6 — Selling
# ══════════════════════════════════════════════════════════════

def test_selling() -> None:
    section("TEST 6: Selling")
    broker = make_broker_with_position(price=100.0, amount=2_000.0)
    cash_after_buy = broker.get_account_overview().liquid_sek

    # Sell at profit
    broker.update_price("ERIC-B.ST", 150.0)
    result = broker.place_order("ERIC-B.ST", "SELL", 2_000.0)

    check(result.status         == OrderStatus.FILLED, "SELL order filled")
    check(result.executed_price == 150.0,              "Sold at updated price")
    check(result.quantity       >  0,                  "Positive quantity sold")

    overview = broker.get_account_overview()
    check(len(overview.positions)  == 0,              "Position closed after SELL")
    check(overview.liquid_sek > cash_after_buy,       "Cash increased after profitable SELL")

    # Sell at a loss
    broker2 = make_broker_with_position(price=100.0, amount=2_000.0)
    broker2.update_price("ERIC-B.ST", 50.0)
    result2 = broker2.place_order("ERIC-B.ST", "SELL", 2_000.0)
    check(result2.status == OrderStatus.FILLED, "SELL at loss still fills")

    # Sell stock not held → rejected
    broker3 = make_broker()
    broker3.update_price("VOLV-B.ST", 200.0)
    result3 = broker3.place_order("VOLV-B.ST", "SELL", 1_000.0)
    check(result3.status == OrderStatus.REJECTED, "SELL rejected for unowned stock")
    check("No position" in (result3.error_message or ""), "Rejection message mentions position")

    # Sell clears position from list
    broker4 = make_broker(10_000.0)
    broker4.update_price("ERIC-B.ST",  100.0)
    broker4.update_price("VOLV-B.ST",  200.0)
    broker4.place_order("ERIC-B.ST",  "BUY", 1_000.0)
    broker4.place_order("VOLV-B.ST",  "BUY", 1_000.0)
    broker4.place_order("ERIC-B.ST",  "SELL", 1_000.0)
    overview4 = broker4.get_account_overview()
    tickers = [p.ticker for p in overview4.positions]
    check("ERIC-B.ST"  not in tickers, "Sold position removed from list")
    check("VOLV-B.ST"  in tickers,     "Unsold position remains in list")


# ══════════════════════════════════════════════════════════════
#  TEST 7 — P&L calculation
# ══════════════════════════════════════════════════════════════

def test_pnl() -> None:
    section("TEST 7: P&L Calculation")

    # Fresh broker has zero P&L
    broker = make_broker()
    check(broker.get_all_time_pnl() == 0.0, "Initial P&L is zero")

    # Realised profit
    broker2 = make_broker_with_position(price=100.0, amount=2_000.0)
    qty     = broker2.get_account_overview().positions[0].quantity
    broker2.update_price("ERIC-B.ST", 200.0)
    broker2.place_order("ERIC-B.ST", "SELL", 2_000.0)
    pnl = broker2.get_all_time_pnl()
    check(pnl > 0, f"Realised P&L positive after profitable trade: {pnl:.2f}")

    # Realised loss
    broker3 = make_broker_with_position(price=100.0, amount=2_000.0)
    broker3.update_price("ERIC-B.ST", 50.0)
    broker3.place_order("ERIC-B.ST", "SELL", 2_000.0)
    pnl3 = broker3.get_all_time_pnl()
    check(pnl3 < 0, f"Realised P&L negative after loss trade: {pnl3:.2f}")

    # Unrealised P&L — hold while price rises
    broker4 = make_broker_with_position(price=100.0, amount=2_000.0)
    broker4.update_price("ERIC-B.ST", 150.0)
    pnl4 = broker4.get_all_time_pnl()
    check(pnl4 > 0, f"Unrealised P&L positive when price rises: {pnl4:.2f}")

    # Unrealised P&L — hold while price falls
    broker5 = make_broker_with_position(price=100.0, amount=2_000.0)
    broker5.update_price("ERIC-B.ST", 70.0)
    pnl5 = broker5.get_all_time_pnl()
    check(pnl5 < 0, f"Unrealised P&L negative when price falls: {pnl5:.2f}")

    # P&L accumulates across multiple trades
    broker6 = make_broker(10_000.0)
    broker6.update_price("ERIC-B.ST",  100.0)
    broker6.update_price("VOLV-B.ST",  200.0)
    broker6.place_order("ERIC-B.ST",  "BUY",  1_000.0)
    broker6.place_order("VOLV-B.ST",  "BUY",  1_000.0)
    broker6.update_price("ERIC-B.ST",  150.0)
    broker6.update_price("VOLV-B.ST",  100.0)
    broker6.place_order("ERIC-B.ST",  "SELL", 1_000.0)
    broker6.place_order("VOLV-B.ST",  "SELL", 1_000.0)
    pnl6 = broker6.get_all_time_pnl()
    # ERIC profit + VOLV loss — net could be either sign
    check(isinstance(pnl6, float), f"P&L is float after multiple trades: {pnl6:.2f}")


# ══════════════════════════════════════════════════════════════
#  TEST 8 — Commission
# ══════════════════════════════════════════════════════════════

def test_commission() -> None:
    section("TEST 8: Commission")
    broker = make_broker(10_000.0)
    broker.update_price("ERIC-B.ST", 100.0)

    result = broker.place_order("ERIC-B.ST", "BUY", 1_000.0)
    effective = 1_000.0 - COMMISSION_SEK
    expected_qty = effective / 100.0

    check(
        abs(result.quantity - expected_qty) < 0.0001,
        f"Commission deducted from buy amount "
        f"(qty={result.quantity:.4f}, expected={expected_qty:.4f})"
    )
    check(
        abs(result.total_sek - effective) < 0.01,
        f"Total SEK reflects commission deduction ({result.total_sek:.2f})"
    )


# ══════════════════════════════════════════════════════════════
#  TEST 9 — update_price
# ══════════════════════════════════════════════════════════════

def test_update_price() -> None:
    section("TEST 9: update_price()")
    broker = make_broker_with_position(price=100.0, amount=2_000.0)

    # Price update reflected in position's current_price
    broker.update_price("ERIC-B.ST", 150.0)
    overview = broker.get_account_overview()
    check(
        overview.positions[0].current_price == 150.0,
        "Position current_price updated correctly"
    )

    # Price update allows buy for ticker not yet in positions
    broker.update_price("VOLV-B.ST", 200.0)
    result = broker.place_order("VOLV-B.ST", "BUY", 500.0)
    check(result.status == OrderStatus.FILLED, "Can buy ticker after update_price")

    # get_price() returns updated price
    price = broker.get_price("ERIC-B.ST")
    check(price == 150.0, f"get_price() returns updated price ({price})")

    # get_price() raises for ticker with no price
    try:
        broker.get_price("SAND.ST")
        check(False, "get_price() should raise for unknown ticker")
    except BrokerError:
        check(True, "get_price() raises BrokerError for unknown ticker")


# ══════════════════════════════════════════════════════════════
#  TEST 10 — Persistence
# ══════════════════════════════════════════════════════════════

def test_persistence() -> None:
    section("TEST 10: Portfolio Persistence")
    tmp = Path(tempfile.mktemp(suffix=".json"))

    try:
        # Create broker, make a trade
        broker1 = PaperBroker(starting_balance=10_000.0, portfolio_file=tmp)
        broker1.update_price("ERIC-B.ST", 75.0)
        broker1.place_order("ERIC-B.ST", "BUY", 2_000.0)
        balance1 = broker1.get_account_overview().liquid_sek
        qty1     = broker1.get_account_overview().positions[0].quantity

        check(tmp.exists(), "Portfolio file created after trade")

        # Load from same file in a new instance
        broker2 = PaperBroker(starting_balance=10_000.0, portfolio_file=tmp)
        overview2 = broker2.get_account_overview()

        check(
            abs(overview2.liquid_sek - balance1) < 0.01,
            f"Balance persisted correctly ({overview2.liquid_sek:.2f})"
        )
        check(
            len(overview2.positions) == 1,
            "Position count persisted correctly"
        )
        check(
            abs(overview2.positions[0].quantity - qty1) < 0.0001,
            f"Position quantity persisted ({overview2.positions[0].quantity:.4f})"
        )
        check(
            overview2.positions[0].ticker == "ERIC-B.ST",
            "Position ticker persisted"
        )

        # Corrupt portfolio file — should fall back to fresh state
        tmp.write_text("{ invalid json }")
        broker3 = PaperBroker(starting_balance=5_000.0, portfolio_file=tmp)
        check(
            broker3.get_account_overview().liquid_sek == 5_000.0,
            "Corrupt portfolio falls back to fresh state"
        )
        check(
            len(broker3.get_account_overview().positions) == 0,
            "No positions after corrupt portfolio recovery"
        )

        # Atomic write — tmp file should not remain after save
        broker4 = PaperBroker(starting_balance=1_000.0, portfolio_file=tmp)
        broker4.update_price("VOLV-B.ST", 100.0)
        broker4.place_order("VOLV-B.ST", "BUY", 200.0)
        tmp_file = tmp.with_suffix(".tmp")
        check(
            not tmp_file.exists(),
            "Temporary .tmp file does not remain after atomic save"
        )

    finally:
        tmp.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════
#  TEST 11 — Reset
# ══════════════════════════════════════════════════════════════

def test_reset() -> None:
    section("TEST 11: Portfolio Reset")
    broker = make_broker(10_000.0)

    broker.update_price("ERIC-B.ST", 100.0)
    broker.place_order("ERIC-B.ST", "BUY", 3_000.0)
    broker.update_price("ERIC-B.ST", 150.0)
    broker.place_order("ERIC-B.ST", "SELL", 3_000.0)

    check(broker.get_all_time_pnl() != 0.0, "P&L non-zero before reset")

    broker.reset(5_000.0)
    overview = broker.get_account_overview()

    check(overview.liquid_sek       == 5_000.0, "Balance reset to new amount")
    check(overview.total_value_sek  == 5_000.0, "Total value equals balance after reset")
    check(len(overview.positions)   == 0,        "All positions cleared on reset")
    check(broker.get_all_time_pnl() == 0.0,     "P&L reset to zero")

    # Price store is cleared on reset
    broker.update_price("ERIC-B.ST", 100.0)
    broker.reset(1_000.0)
    try:
        broker.get_price("ERIC-B.ST")
        check(False, "get_price() should raise after reset clears price store")
    except BrokerError:
        check(True, "Price store cleared on reset")

    # Can trade normally after reset
    broker.update_price("ERIC-B.ST", 50.0)
    result = broker.place_order("ERIC-B.ST", "BUY", 200.0)
    check(result.status == OrderStatus.FILLED, "Can trade normally after reset")


# ══════════════════════════════════════════════════════════════
#  TEST 12 — Thread safety
# ══════════════════════════════════════════════════════════════

def test_thread_safety() -> None:
    section("TEST 12: Thread Safety")
    broker  = make_broker(100_000.0)
    errors  = []
    barrier = threading.Barrier(6)

    def buy_thread():
        try:
            barrier.wait()
            for _ in range(20):
                broker.update_price("ERIC-B.ST", 100.0)
                broker.place_order("ERIC-B.ST", "BUY", 100.0)
        except Exception as exc:
            errors.append(exc)

    def sell_thread():
        try:
            barrier.wait()
            for _ in range(20):
                broker.update_price("ERIC-B.ST", 100.0)
                broker.place_order("ERIC-B.ST", "SELL", 100.0)
        except Exception as exc:
            errors.append(exc)

    def overview_thread():
        try:
            barrier.wait()
            for _ in range(20):
                broker.get_account_overview()
                broker.get_all_time_pnl()
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=buy_thread),
        threading.Thread(target=buy_thread),
        threading.Thread(target=sell_thread),
        threading.Thread(target=sell_thread),
        threading.Thread(target=overview_thread),
        threading.Thread(target=overview_thread),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(len(errors) == 0, f"No errors during concurrent trading (errors: {errors})")

    # Balance should never go negative
    overview = broker.get_account_overview()
    check(
        overview.liquid_sek >= 0,
        f"Balance never went negative ({overview.liquid_sek:.2f})"
    )


# ══════════════════════════════════════════════════════════════
#  TEST 13 — Ticker registry
# ══════════════════════════════════════════════════════════════

def test_ticker_registry() -> None:
    section("TEST 13: Ticker Registry")

    # Registry is non-empty
    check(len(REGISTRY) > 0, f"Registry has {len(REGISTRY)} instruments")

    # all_tickers and tradeable_tickers are consistent
    all_t       = all_tickers()
    tradeable_t = tradeable_tickers()
    check(len(all_t) == len(REGISTRY),         "all_tickers() length matches REGISTRY")
    check(len(tradeable_t) <= len(all_t),       "tradeable_tickers() is subset of all_tickers()")
    check(set(tradeable_t).issubset(set(all_t)),"tradeable_tickers() are all in all_tickers()")

    # get() returns correct instrument
    eric = get("ERIC-B.ST")
    check(eric.yf_ticker    == "ERIC-B.ST",      "get() returns correct yf_ticker")
    check(eric.asset_class  == AssetClass.EQUITY, "get() returns correct asset class")
    check(eric.avanza_id    == "5240",            "get() returns correct avanza_id")
    check(eric.avanza_tradeable is True,          "ERIC-B.ST is avanza_tradeable")

    # get() raises KeyError for unknown ticker
    try:
        get("FAKE.TICKER")
        check(False, "get() should raise KeyError for unknown ticker")
    except KeyError:
        check(True, "get() raises KeyError for unknown ticker")

    # avanza_id() returns string ID
    oid = avanza_id("ERIC-B.ST")
    check(isinstance(oid, str) and len(oid) > 0, f"avanza_id() returns string: {oid}")

    # avanza_id() raises KeyError for unknown
    try:
        avanza_id("FAKE.TICKER")
        check(False, "avanza_id() should raise KeyError for unknown ticker")
    except KeyError:
        check(True, "avanza_id() raises KeyError for unknown ticker")

    # is_avanza_tradeable() for known and unknown
    check(is_avanza_tradeable("ERIC-B.ST"),    "ERIC-B.ST is tradeable on Avanza")
    check(not is_avanza_tradeable("FAKE.ST"),  "Unknown ticker returns False")

    # Instrument with avanza_id=None is not tradeable
    paper_only = Instrument(
        yf_ticker="PAPER.ST",
        avanza_id=None,
        name="Paper Only",
        asset_class=AssetClass.EQUITY,
        avanza_tradeable=True,   # should be coerced to False
    )
    check(
        paper_only.avanza_tradeable is False,
        "__post_init__ coerces avanza_tradeable to False when avanza_id is None"
    )

    # All registry entries have consistent avanza_tradeable flags
    for ticker, instrument in REGISTRY.items():
        if instrument.avanza_id is None:
            check(
                instrument.avanza_tradeable is False,
                f"{ticker}: avanza_id=None implies avanza_tradeable=False"
            )
        else:
            check(
                isinstance(instrument.avanza_id, str) and len(instrument.avanza_id) > 0,
                f"{ticker}: avanza_id is non-empty string"
            )

    # Asset classes cover all expected types
    asset_classes = {i.asset_class for i in REGISTRY.values()}
    check(AssetClass.EQUITY     in asset_classes, "Registry contains EQUITY instruments")
    check(AssetClass.ETF_EQUITY in asset_classes, "Registry contains ETF_EQUITY instruments")
    check(AssetClass.ETF_BOND   in asset_classes, "Registry contains ETF_BOND instruments")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║     MONEYMAKER — Trading Module Test Suite            ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Tests paper broker, broker dataclasses, and ticker")
    print("  registry in isolation.")
    print("  No hardware or network connection required.")

    try:
        test_dataclasses()
        test_init()
        test_buying()
        test_averaging()
        test_insufficient_funds()
        test_selling()
        test_pnl()
        test_commission()
        test_update_price()
        test_persistence()
        test_reset()
        test_thread_safety()
        test_ticker_registry()

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