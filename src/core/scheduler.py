"""
Tracks Swedish market hours (09:00–17:30 Stockholm time, weekdays,
excluding Swedish public holidays). Emits MARKET_OPENED and
MARKET_CLOSED events at the right moments.
"""

import time
import threading
import logging
from datetime import datetime, date, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from core.events import EventBus, EventType, Event

logger = logging.getLogger(__name__)

STOCKHOLM_TZ = ZoneInfo("Europe/Stockholm")

MARKET_OPEN_HOUR    = 9
MARKET_OPEN_MINUTE  = 0
MARKET_CLOSE_HOUR   = 17
MARKET_CLOSE_MINUTE = 30

# Fixed Swedish public holidays (month, day)
SWEDISH_HOLIDAYS: set[tuple[int, int]] = {
    (1,  1),   # Nyårsdagen
    (1,  6),   # Trettondedag jul
    (5,  1),   # Första maj
    (6,  6),   # Nationaldagen
    (12, 24),  # Julafton
    (12, 25),  # Juldagen
    (12, 26),  # Annandag jul
    (12, 31),  # Nyårsafton
}


def _easter_sunday(year: int) -> date:
    """Compute Easter Sunday using the Anonymous Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day   = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=10)
def _get_moveable_holidays(year: int) -> frozenset:
    """
    Compute moveable Swedish holidays for a given year.
    Result is cached per year — called thousands of times per day
    during market hours so recomputing Easter every call is wasteful.
    Returns a frozenset so it is hashable and safe to cache.

    Note: Midsommarafton (Midsummer Eve) is intentionally excluded.
    Nasdaq Stockholm closes early at 13:00 on that day rather than
    being fully closed — the scheduler does not model early closes,
    so including it would incorrectly block all trading that morning.
    """
    easter   = _easter_sunday(year)
    holidays = set()

    # Good Friday — 2 days before Easter
    holidays.add(easter - timedelta(days=2))
    # Easter Monday — 1 day after Easter
    holidays.add(easter + timedelta(days=1))
    # Ascension Day — 39 days after Easter
    holidays.add(easter + timedelta(days=39))
    # Alla helgons dag — Saturday between Oct 31–Nov 6
    allhelgon = date(year, 10, 31)
    while allhelgon.weekday() != 5:   # 5 = Saturday
        allhelgon += timedelta(days=1)
    holidays.add(allhelgon)

    return frozenset(holidays)


def _is_holiday(d: date) -> bool:
    """Return True if the given date is a Swedish public holiday."""
    if (d.month, d.day) in SWEDISH_HOLIDAYS:
        return True
    return d in _get_moveable_holidays(d.year)


def _is_trading_day(d: date) -> bool:
    """Return True if the market trades on the given date."""
    if d.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return not _is_holiday(d)


def _market_open_time(d: date) -> datetime:
    return datetime(
        d.year, d.month, d.day,
        MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE,
        tzinfo=STOCKHOLM_TZ
    )


def _market_close_time(d: date) -> datetime:
    return datetime(
        d.year, d.month, d.day,
        MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE,
        tzinfo=STOCKHOLM_TZ
    )


def market_is_open_now() -> bool:
    """Return True if the market is currently open."""
    now = datetime.now(STOCKHOLM_TZ)
    if not _is_trading_day(now.date()):
        return False
    return _market_open_time(now.date()) <= now < _market_close_time(now.date())


def seconds_until_next_open() -> float:
    """Return seconds until the next market open."""
    now = datetime.now(STOCKHOLM_TZ)
    d   = now.date()

    if _is_trading_day(d) and now < _market_open_time(d):
        return (_market_open_time(d) - now).total_seconds()

    d += timedelta(days=1)
    while not _is_trading_day(d):
        d += timedelta(days=1)

    return (_market_open_time(d) - now).total_seconds()


class MarketScheduler:
    """
    Monitors market hours and emits MARKET_OPENED / MARKET_CLOSED
    events at the appropriate times.

    Runs on its own daemon thread. Checks every 30 seconds.
    Uses a threading.Event for the sleep so stop() wakes it
    immediately rather than waiting up to 30 seconds to join.
    """

    POLL_INTERVAL_SECONDS = 30

    def __init__(self, bus: EventBus) -> None:
        self._bus              = bus
        self._running          = False
        self._stop_event       = threading.Event()
        self._thread:          threading.Thread | None = None
        self._market_was_open: bool | None             = None

    def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="MarketScheduler",
            daemon=True
        )
        self._thread.start()
        logger.info("MarketScheduler started")

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()   # wake the sleeping thread immediately
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("MarketScheduler stopped")

    def _loop(self) -> None:
        while self._running:
            try:
                self._check()
            except Exception:
                logger.exception("Error in MarketScheduler loop")

            # Wait up to POLL_INTERVAL_SECONDS, but wake immediately on stop
            self._stop_event.wait(timeout=self.POLL_INTERVAL_SECONDS)
            self._stop_event.clear()

    def _check(self) -> None:
        is_open = market_is_open_now()

        if is_open and self._market_was_open is not True:
            logger.info("Market has opened")
            self._bus.publish(Event(
                type=EventType.MARKET_OPENED,
                source="MarketScheduler"
            ))
            self._market_was_open = True

        elif not is_open and self._market_was_open is True:
            logger.info("Market has closed")
            self._bus.publish(Event(
                type=EventType.MARKET_CLOSED,
                source="MarketScheduler"
            ))
            self._market_was_open = False

        elif self._market_was_open is None:
            # First check — emit current state unconditionally
            event_type = (
                EventType.MARKET_OPENED if is_open
                else EventType.MARKET_CLOSED
            )
            self._bus.publish(Event(
                type=event_type,
                source="MarketScheduler"
            ))
            self._market_was_open = is_open
            logger.info(
                "Initial market state: %s", "OPEN" if is_open else "CLOSED"
            )