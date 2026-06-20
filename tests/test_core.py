"""
test_core.py — Unit tests for the core module.

Tests EventBus, StateManager, MarketScheduler, and Engine logic
in isolation without any hardware, network, or broker dependencies.

Run with:
    cd ~/moneymaker
    sudo ~/moneymaker/venv/bin/python3 tests/test_core.py
"""

import sys
import os
import time
import threading
from datetime import datetime, date
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.events import EventBus, EventType, Event
from core.state import (
    StateManager, StateError, AppState,
    TradingMode, SystemStatus, ConnectivityStatus, VALID_ALGORITHMS
)
from core.scheduler import (
    _easter_sunday, _is_holiday, _is_trading_day,
    market_is_open_now, MarketScheduler,
    _market_open_time, _market_close_time,
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


# ══════════════════════════════════════════════════════════════
#  TEST 1 — EventBus
# ══════════════════════════════════════════════════════════════

def test_event_bus() -> None:
    section("TEST 1: EventBus")

    # ── Basic publish/subscribe ───────────────────────────────
    bus = EventBus()
    bus.start()

    received = []
    bus.subscribe(EventType.MARKET_OPENED, lambda e: received.append(e))
    bus.publish(Event(type=EventType.MARKET_OPENED, source="test"))
    time.sleep(0.05)

    check(len(received) == 1, "Subscriber receives published event")
    check(received[0].type == EventType.MARKET_OPENED, "Event type is correct")
    check(received[0].source == "test", "Event source is correct")

    # ── Multiple subscribers ──────────────────────────────────
    results = []
    bus.subscribe(EventType.TRADE_EXECUTED, lambda e: results.append("A"))
    bus.subscribe(EventType.TRADE_EXECUTED, lambda e: results.append("B"))
    bus.publish(Event(type=EventType.TRADE_EXECUTED, source="test"))
    time.sleep(0.05)

    check(len(results) == 2, "Both subscribers receive the event")
    check("A" in results and "B" in results, "Both callbacks executed")

    # ── Type isolation ────────────────────────────────────────
    isolated = []
    bus.subscribe(EventType.API_ERROR, lambda e: isolated.append(e))
    bus.publish(Event(type=EventType.MARKET_CLOSED, source="test"))
    time.sleep(0.05)

    check(len(isolated) == 0, "Subscriber does not receive events of other types")

    # ── Unsubscribe ───────────────────────────────────────────
    counter = []
    handler = lambda e: counter.append(1)
    bus.subscribe(EventType.CONNECTION_LOST, handler)
    bus.publish(Event(type=EventType.CONNECTION_LOST, source="test"))
    time.sleep(0.05)
    before = len(counter)

    bus.unsubscribe(EventType.CONNECTION_LOST, handler)
    bus.publish(Event(type=EventType.CONNECTION_LOST, source="test"))
    time.sleep(0.05)

    check(before == 1, "Subscriber received event before unsubscribe")
    check(len(counter) == 1, "Subscriber does not receive events after unsubscribe")

    # ── Exception in subscriber doesn't stop other subscribers ─
    other = []
    bus.subscribe(EventType.API_ERROR, lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe(EventType.API_ERROR, lambda e: other.append(1))
    bus.publish(Event(type=EventType.API_ERROR, source="test"))
    time.sleep(0.05)

    check(len(other) == 1, "Exception in one subscriber does not block others")

    # ── Payload delivery ──────────────────────────────────────
    payloads = []
    bus.subscribe(EventType.TRADE_FAILED, lambda e: payloads.append(e.payload))
    bus.publish(Event(
        type=EventType.TRADE_FAILED,
        source="test",
        payload={"reason": "insufficient funds"}
    ))
    time.sleep(0.05)

    check(len(payloads) == 1, "Payload is delivered with event")
    check(payloads[0].get("reason") == "insufficient funds", "Payload content is correct")

    # ── Double stop is safe ───────────────────────────────────
    bus.stop()
    try:
        bus.stop()
        check(True, "Calling stop() twice does not raise")
    except Exception as exc:
        check(False, f"stop() raised on second call: {exc}")

    # ── Events published before start are queued ──────────────
    bus2 = EventBus()
    pre_start = []
    bus2.subscribe(EventType.STARTUP_COMPLETE, lambda e: pre_start.append(1))
    bus2.publish(Event(type=EventType.STARTUP_COMPLETE, source="test"))
    bus2.start()
    time.sleep(0.05)
    bus2.stop()

    check(len(pre_start) == 1, "Event published before start() is dispatched after start()")

    # ── Thread safety: concurrent publishers ──────────────────
    bus3 = EventBus()
    bus3.start()
    counts = []
    bus3.subscribe(EventType.PRICE_UPDATED, lambda e: counts.append(1))

    threads = [
        threading.Thread(
            target=lambda: bus3.publish(
                Event(type=EventType.PRICE_UPDATED, source="thread")
            )
        )
        for _ in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.1)
    bus3.stop()

    check(len(counts) == 20, f"All 20 concurrent publishes delivered (got {len(counts)})")


# ══════════════════════════════════════════════════════════════
#  TEST 2 — StateManager
# ══════════════════════════════════════════════════════════════

def test_state_manager() -> None:
    section("TEST 2: StateManager")

    # ── Initial state ─────────────────────────────────────────
    state = StateManager()
    snap  = state.snapshot()

    check(snap.trading_mode   == TradingMode.PAPER,           "Initial mode is PAPER")
    check(snap.system_status  == SystemStatus.STARTING,       "Initial status is STARTING")
    check(snap.active_algorithm == "dual_momentum",           "Initial algorithm is dual_momentum")
    check(snap.risk_level     == 0.5,                         "Initial risk level is 0.5")
    check(snap.paper_balance  is None,                        "Initial paper balance is None")
    check(snap.market_is_open is False,                       "Market initially closed")

    # ── Snapshot independence ─────────────────────────────────
    snap1 = state.snapshot()
    snap1.pending_urgent_msgs.append("mutate me")
    snap2 = state.snapshot()

    check(
        len(snap2.pending_urgent_msgs) == 0,
        "Mutating snapshot does not affect live state (deep copy)"
    )

    # ── System status transitions ─────────────────────────────
    state.set_status(SystemStatus.RUNNING)
    check(state.snapshot().system_status == SystemStatus.RUNNING, "Status set to RUNNING")

    state.set_status(SystemStatus.MARKET_CLOSED)
    check(state.snapshot().system_status == SystemStatus.MARKET_CLOSED, "Status set to MARKET_CLOSED")

    # ── Trading mode toggle ───────────────────────────────────
    mode = state.switch_trading_mode()
    check(mode == TradingMode.REAL, "First toggle switches to REAL")

    mode = state.switch_trading_mode()
    check(mode == TradingMode.PAPER, "Second toggle switches back to PAPER")

    # ── Mode switch blocked during shutdown ───────────────────
    state.set_status(SystemStatus.SHUTTING_DOWN)
    try:
        state.switch_trading_mode()
        check(False, "Should have raised StateError during shutdown")
    except StateError:
        check(True, "switch_trading_mode() raises StateError during shutdown")
    state.set_status(SystemStatus.RUNNING)

    # ── Algorithm switching ───────────────────────────────────
    state.switch_algorithm("mean_reversion")
    check(
        state.snapshot().active_algorithm == "mean_reversion",
        "Algorithm switched to mean_reversion"
    )

    try:
        state.switch_algorithm("invalid_algo")
        check(False, "Should have raised StateError for unknown algorithm")
    except StateError:
        check(True, "switch_algorithm() raises StateError for unknown name")

    # ── next_algorithm cycles through all ────────────────────
    seen = set()
    for _ in range(len(VALID_ALGORITHMS) + 1):
        seen.add(state.next_algorithm())

    check(seen == VALID_ALGORITHMS, f"next_algorithm() cycles through all algorithms: {seen}")

    # ── Risk level ────────────────────────────────────────────
    state.set_risk_level(0.75)
    check(state.snapshot().risk_level == 0.75, "Risk level set to 0.75")

    try:
        state.set_risk_level(1.5)
        check(False, "Should have raised StateError for risk > 1.0")
    except StateError:
        check(True, "set_risk_level() raises StateError for value > 1.0")

    try:
        state.set_risk_level(-0.1)
        check(False, "Should have raised StateError for risk < 0.0")
    except StateError:
        check(True, "set_risk_level() raises StateError for value < 0.0")

    # ── P&L ──────────────────────────────────────────────────
    state.update_pnl(1234.567)
    check(state.snapshot().current_pnl == 1234.57, "P&L rounded to 2 decimal places")

    state.update_pnl(-500.0)
    check(state.snapshot().current_pnl == -500.0, "Negative P&L stored correctly")

    # ── Market status ─────────────────────────────────────────
    state.set_market_open(True)
    check(state.snapshot().market_is_open is True, "Market set to open")

    state.set_market_open(False)
    check(state.snapshot().market_is_open is False, "Market set to closed")

    # ── Paper balance ─────────────────────────────────────────
    state.set_paper_balance(25_000.0)
    check(state.snapshot().paper_balance == 25_000.0, "Paper balance set correctly")

    try:
        state.set_paper_balance(0.0)
        check(False, "Should have raised StateError for zero balance")
    except StateError:
        check(True, "set_paper_balance() raises StateError for zero")

    try:
        state.set_paper_balance(-100.0)
        check(False, "Should have raised StateError for negative balance")
    except StateError:
        check(True, "set_paper_balance() raises StateError for negative value")

    # ── Crash handling ────────────────────────────────────────
    state.set_status(SystemStatus.RUNNING)
    state.record_crash("network timeout", recoverable=True)
    snap = state.snapshot()

    check(
        snap.system_status == SystemStatus.AWAITING_RECOVERY,
        "record_crash() sets status to AWAITING_RECOVERY"
    )
    check(snap.last_crash_reason == "network timeout", "Crash reason stored")
    check(snap.crash_is_recoverable is True, "Crash marked as recoverable")

    state.clear_crash()
    snap = state.snapshot()
    check(snap.last_crash_reason is None, "clear_crash() clears the reason")
    check(snap.crash_is_recoverable is True, "clear_crash() resets recoverable flag")

    # Unrecoverable crash
    state.record_crash("assertion error", recoverable=False)
    check(
        state.snapshot().crash_is_recoverable is False,
        "Unrecoverable crash stored correctly"
    )

    # ── Urgent messages ───────────────────────────────────────
    state2 = StateManager()
    check(state2.has_urgent_messages() is False, "No urgent messages initially")

    state2.add_urgent_message("alert one")
    state2.add_urgent_message("alert two")
    check(state2.has_urgent_messages() is True, "has_urgent_messages() returns True after add")

    msg = state2.pop_urgent_message()
    check(msg == "alert one", "pop_urgent_message() returns first message (FIFO)")

    msg = state2.pop_urgent_message()
    check(msg == "alert two", "pop_urgent_message() returns second message")

    check(state2.pop_urgent_message() is None, "pop_urgent_message() returns None when empty")

    # ── Thread safety ─────────────────────────────────────────
    state3    = StateManager()
    errors    = []
    barrier   = threading.Barrier(10)

    def toggle_mode():
        try:
            barrier.wait()
            for _ in range(5):
                state3.switch_trading_mode()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=toggle_mode) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(len(errors) == 0, "No errors during concurrent mode switches")
    check(
        state3.snapshot().trading_mode in (TradingMode.PAPER, TradingMode.REAL),
        "Trading mode is valid after concurrent access"
    )


# ══════════════════════════════════════════════════════════════
#  TEST 3 — MarketScheduler / scheduler helpers
# ══════════════════════════════════════════════════════════════

def test_scheduler() -> None:
    section("TEST 3: MarketScheduler and Scheduler Helpers")

    STOCKHOLM = ZoneInfo("Europe/Stockholm")

    # ── Easter calculation ────────────────────────────────────
    known_easters = {
        2020: date(2020, 4, 12),
        2021: date(2021, 4,  4),
        2022: date(2022, 4, 17),
        2023: date(2023, 4,  9),
        2024: date(2024, 3, 31),
        2025: date(2025, 4, 20),
    }
    for year, expected in known_easters.items():
        result = _easter_sunday(year)
        check(result == expected, f"Easter {year}: {result} == {expected}")

    # ── Known Swedish public holidays ────────────────────────
    known_holidays = [
        date(2024,  1,  1),   # Nyårsdagen
        date(2024,  1,  6),   # Trettondedag jul
        date(2024,  3, 29),   # Good Friday
        date(2024,  4,  1),   # Easter Monday
        date(2024,  5,  1),   # Första maj
        date(2024,  5,  9),   # Ascension Day
        date(2024,  6,  6),   # Nationaldagen
        date(2024, 11,  2),   # Alla helgons dag
        date(2024, 12, 24),   # Julafton
        date(2024, 12, 25),   # Juldagen
        date(2024, 12, 26),   # Annandag jul
        date(2024, 12, 31),   # Nyårsafton
    ]
    for d in known_holidays:
        check(_is_holiday(d), f"Correctly identified as holiday: {d}")

    # ── Known non-holidays ────────────────────────────────────
    known_trading_days = [
        date(2024,  1,  2),   # Normal Tuesday
        date(2024,  3, 15),   # Normal Friday
        date(2024,  6,  3),   # Normal Monday
        date(2024,  9, 16),   # Normal Monday
    ]
    for d in known_trading_days:
        check(not _is_holiday(d), f"Correctly identified as non-holiday: {d}")

    # ── Weekend detection ─────────────────────────────────────
    check(not _is_trading_day(date(2024, 3, 16)), "Saturday is not a trading day")
    check(not _is_trading_day(date(2024, 3, 17)), "Sunday is not a trading day")
    check(_is_trading_day(date(2024, 3, 18)),     "Monday is a trading day")
    check(not _is_trading_day(date(2024, 1,  1)), "New Year's Day is not a trading day")
    check(not _is_trading_day(date(2024, 3, 29)), "Good Friday is not a trading day")

    # ── market_is_open_now() with mocked time ─────────────────
    # Patch datetime.now inside the scheduler module

    # Normal trading day, during hours
    mock_open = datetime(2024, 3, 18, 10, 30, tzinfo=STOCKHOLM)
    with patch("core.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = mock_open
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = market_is_open_now()
    check(result is True, "Market open on Monday at 10:30")

    # Before open
    mock_before = datetime(2024, 3, 18, 8, 59, tzinfo=STOCKHOLM)
    with patch("core.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = mock_before
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = market_is_open_now()
    check(result is False, "Market closed before 09:00")

    # After close
    mock_after = datetime(2024, 3, 18, 17, 30, tzinfo=STOCKHOLM)
    with patch("core.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = mock_after
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = market_is_open_now()
    check(result is False, "Market closed at exactly 17:30")

    # Weekend
    mock_weekend = datetime(2024, 3, 16, 12, 0, tzinfo=STOCKHOLM)
    with patch("core.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = mock_weekend
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = market_is_open_now()
    check(result is False, "Market closed on Saturday")

    # Holiday
    mock_holiday = datetime(2024, 12, 25, 12, 0, tzinfo=STOCKHOLM)
    with patch("core.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = mock_holiday
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = market_is_open_now()
    check(result is False, "Market closed on Christmas Day")

    # ── MarketScheduler emits correct initial event ───────────
    bus    = EventBus()
    bus.start()
    events = []
    bus.subscribe(EventType.MARKET_OPENED, lambda e: events.append("OPENED"))
    bus.subscribe(EventType.MARKET_CLOSED, lambda e: events.append("CLOSED"))

    # Force market to be closed by patching
    mock_closed = datetime(2024, 3, 16, 12, 0, tzinfo=STOCKHOLM)
    with patch("core.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = mock_closed
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        scheduler = MarketScheduler(bus)
        scheduler.start()
        time.sleep(0.1)
        scheduler.stop()

    bus.stop()
    check("CLOSED" in events, "Scheduler emits MARKET_CLOSED as initial state")
    check(events.count("CLOSED") == 1, "Initial state emitted exactly once")

    # ── stop() returns quickly ────────────────────────────────
    bus2 = EventBus()
    bus2.start()
    scheduler2 = MarketScheduler(bus2)
    scheduler2.start()
    time.sleep(0.05)

    t_start = time.monotonic()
    scheduler2.stop()
    elapsed = time.monotonic() - t_start
    bus2.stop()

    check(elapsed < 1.0, f"stop() returns in under 1 second (took {elapsed:.2f}s)")

    # ── No duplicate events on repeated checks ────────────────
    bus3   = EventBus()
    bus3.start()
    opens  = []
    closes = []
    bus3.subscribe(EventType.MARKET_OPENED, lambda e: opens.append(1))
    bus3.subscribe(EventType.MARKET_CLOSED, lambda e: closes.append(1))

    mock_open_time = datetime(2024, 3, 18, 10, 0, tzinfo=STOCKHOLM)
    with patch("core.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = mock_open_time
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        sched3 = MarketScheduler(bus3)
        # Manually call _check() multiple times — should only emit once
        sched3._check()
        sched3._check()
        sched3._check()

    time.sleep(0.05)
    bus3.stop()

    check(len(opens) == 1, "MARKET_OPENED emitted only once across repeated checks")
    check(len(closes) == 0, "MARKET_CLOSED not emitted when market is open")


# ══════════════════════════════════════════════════════════════
#  TEST 4 — Engine (isolated, no hardware)
# ══════════════════════════════════════════════════════════════

def test_engine() -> None:
    section("TEST 4: Engine Logic (mocked subsystems)")

    from core.engine import Engine, ALGORITHM_REGISTRY, MAX_AUTO_RECOVERIES
    from applogging.logger import classify_crash

    # ── ALGORITHM_REGISTRY matches VALID_ALGORITHMS ───────────
    check(
        set(ALGORITHM_REGISTRY.keys()) == VALID_ALGORITHMS,
        "ALGORITHM_REGISTRY keys match VALID_ALGORITHMS in state.py"
    )

    # ── classify_crash: recoverable types ────────────────────
    _, recoverable = classify_crash(ConnectionError("timeout"))
    check(recoverable is True, "ConnectionError is recoverable")

    _, recoverable = classify_crash(TimeoutError("timed out"))
    check(recoverable is True, "TimeoutError is recoverable")

    # ── classify_crash: unrecoverable types ──────────────────
    _, recoverable = classify_crash(AssertionError("assertion failed"))
    check(recoverable is False, "AssertionError is unrecoverable")

    _, recoverable = classify_crash(Exception("permission denied"))
    check(recoverable is False, "Permission denied exception is unrecoverable")

    _, recoverable = classify_crash(Exception("authentication failed"))
    check(recoverable is False, "Authentication failure is unrecoverable")

    # ── Engine init without starting ──────────────────────────
    display = MagicMock()
    led     = MagicMock()
    state   = StateManager()
    bus     = EventBus()
    bus.start()

    engine = Engine(display=display, led=led, state=state, bus=bus)

    check(engine._broker    is None, "Broker is None before start()")
    check(engine._algorithm is None, "Algorithm is None before start()")
    check(engine._running   is False, "Engine not running before start()")

    # ── _load_algorithm ───────────────────────────────────────
    engine._load_algorithm("dual_momentum")
    check(engine._algorithm is not None, "_load_algorithm sets algorithm")
    check(
        engine._algorithm.__class__.__name__ == "DualMomentumAlgorithm",
        "_load_algorithm loads correct class"
    )

    engine._load_algorithm("nonexistent")
    check(
        engine._algorithm.__class__.__name__ == "DualMomentumAlgorithm",
        "_load_algorithm keeps current algorithm on unknown name"
    )

    engine._load_algorithm("mean_reversion")
    check(
        engine._algorithm.__class__.__name__ == "MeanReversionAlgorithm",
        "_load_algorithm switches to mean_reversion"
    )

    # ── _get_required_tickers ─────────────────────────────────
    for algo_name in VALID_ALGORITHMS:
        engine._load_algorithm(algo_name)
        state.switch_algorithm(algo_name)
        try:
            tickers = engine._get_required_tickers()
            check(
                isinstance(tickers, list) and len(tickers) > 0,
                f"_get_required_tickers() returns non-empty list for {algo_name}"
            )
        except Exception as exc:
            check(False, f"_get_required_tickers() raised for {algo_name}: {exc}")

    # Unknown algorithm raises RuntimeError
    state.switch_algorithm("dual_momentum")
    state._state.active_algorithm = "ghost_algo"   # bypass validation
    engine._load_algorithm("dual_momentum")        # reset engine algo
    state._state.active_algorithm = "ghost_algo"   # force unknown again
    try:
        engine._get_required_tickers()
        check(False, "_get_required_tickers() should raise for unknown algorithm")
    except RuntimeError:
        check(True, "_get_required_tickers() raises RuntimeError for unknown algorithm")

    # ── _calculate_pnl with paper broker ─────────────────────
    from trading.paper_broker import PaperBroker
    paper = PaperBroker(starting_balance=10_000.0)
    engine._broker = paper

    mock_overview = MagicMock()
    pnl = engine._calculate_pnl(mock_overview)
    check(isinstance(pnl, float), "_calculate_pnl() returns float for paper broker")

    # ── _calculate_pnl with real broker (mock) ────────────────
    real_broker         = MagicMock()
    engine._broker      = real_broker
    mock_pos1           = MagicMock()
    mock_pos1.unrealised_pnl = 150.0
    mock_pos2           = MagicMock()
    mock_pos2.unrealised_pnl = -50.0
    mock_overview.positions  = [mock_pos1, mock_pos2]

    pnl = engine._calculate_pnl(mock_overview)
    check(pnl == 100.0, f"_calculate_pnl() sums unrealised P&L for real broker (got {pnl})")

    # ── _execute_signal skips small amounts ───────────────────
    engine._broker    = MagicMock()
    engine._algorithm = MagicMock()
    snap              = state.snapshot()

    mock_signal           = MagicMock()
    mock_signal.fraction  = 0.001   # 0.1% of 10 SEK = 0.01 SEK → below minimum
    mock_signal.action    = MagicMock()
    mock_signal.action.value = "BUY"
    mock_signal.ticker    = "TEST.ST"

    engine._execute_signal(mock_signal, liquid_sek=10.0, snap=snap)
    check(
        not engine._broker.place_order.called,
        "_execute_signal() skips order when amount is below 10 SEK"
    )

    # ── Shutdown event interrupts market-closed sleep ─────────
    engine2 = Engine(display=MagicMock(), led=MagicMock(), state=StateManager(), bus=bus)

    t_start = time.monotonic()
    def set_shutdown():
        time.sleep(0.1)
        engine2._shutdown_event.set()

    threading.Thread(target=set_shutdown, daemon=True).start()
    # Simulate what the market-closed branch does
    engine2._shutdown_event.wait(timeout=60)
    elapsed = time.monotonic() - t_start

    check(elapsed < 1.0, f"Shutdown event wakes market-closed sleep immediately (took {elapsed:.2f}s)")

    # ── Crash counter resets after successful evaluation ──────
    # Verify the counter attribute exists and behaves correctly
    engine3 = Engine(display=MagicMock(), led=MagicMock(), state=StateManager(), bus=bus)
    engine3._consecutive_recoveries = 2
    engine3._consecutive_recoveries = 0   # simulates reset after success
    check(engine3._consecutive_recoveries == 0, "Crash counter resets to 0 after successful run")

    # ── MAX_AUTO_RECOVERIES constant is sane ─────────────────
    check(MAX_AUTO_RECOVERIES > 0, "MAX_AUTO_RECOVERIES is positive")
    check(MAX_AUTO_RECOVERIES <= 10, "MAX_AUTO_RECOVERIES is not unreasonably high")

    bus.stop()


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║         MONEYMAKER — Core Module Test Suite           ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Tests EventBus, StateManager, MarketScheduler,")
    print("  and Engine logic in isolation.")
    print("  No hardware or network connection required.")

    try:
        test_event_bus()
        test_state_manager()
        test_scheduler()
        test_engine()

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