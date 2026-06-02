"""
Tracks Swedish market hours (09:00–17:30 Stockholm time, weekdays,
excluding Swedish public holidays). Emits MARKET_OPENED and
MARKET_CLOSED events at the right moments and handles the
sleep/wake cycle between sessions.
"""

import time
import threading
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from core.events import EventBus, EventType, Event

logger = logging.getLogger(__name__)

STOCKHOLM_TZ = ZoneInfo("Europe/Stockholm")

# Nasdaq Stockholm trading hours
MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MINUTE = 0
MARKET_CLOSE_HOUR  = 17
MARKET_CLOSE_MINUTE = 30

# Swedish public holidays (updated annually — add new years as needed)
# Format: (month, day)
SWEDISH_HOLIDAYS: set[tuple[int, int]] = {
    (1,  1),   # Nyårsdagen
    (1,  6),   # Trettondedag jul
    (5,  1),   # Första maj
    (6,  6),   # Nationaldagen
    (12, 24),  # Julafton
    (12, 25),  # Juldagen
    (12, 26),  # Annandag jul
    (12, 31),  # Nyårsafton
    # Note: Easter, Midsommar, Ascension Day are computed separately
    # and added dynamically in _is_holiday()
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


def _get_moveable_holidays(year: int) -> set[date]:
    """Compute moveable Swedish holidays for a given year."""
    easter    = _easter_sunday(year)
    holidays  = set()

    # Good Friday (Långfredagen) — 2 days before Easter
    holidays.add(easter - timedelta(days=2))
    # Easter Monday (Annandag påsk) — 1 day after Easter
    holidays.add(easter + timedelta(days=1))
    # Ascension Day (Kristi himmelsfärdsdag) — 39 days after Easter
    holidays.add(easter + timedelta(days=39))
    # Whit Monday (Annandag pingst) — 50 days after Easter
    # Note: Sweden removed this as a public holiday in 2005
    # Midsommarafton — Friday between June 19–25
    midsummer_eve = date(year, 6, 19)
    while midsummer_eve.weekday() != 4:  # 4 = Friday
        midsummer_eve += timedelta(days=1)
    holidays.add(midsummer_eve)
    # Alla helgons dag — Saturday between Oct 31–Nov 6
    allhelgon = date(year, 10, 31)
    while allhelgon.weekday() != 5:  # 5 = Saturday
        allhelgon += timedelta(days=1)
    holidays.add(allhelgon)

    return holidays


def _is_holiday(d: date) -> bool:
    """Return True if the given date is a Swedish public holiday."""
    if (d.month, d.day) in SWEDISH_HOLIDAYS:
        return True
    moveable = _get_moveable_holidays(d.year)
    return d in moveable


def _is_trading_day(d: date) -> bool:
    """Return True if the market trades on the given date."""
    if d.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    if _is_holiday(d):
        return False
    return True


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
    now  = datetime.now(STOCKHOLM_TZ)
    d    = now.date()

    # Check if today's open is still in the future
    if _is_trading_day(d) and now < _market_open_time(d):
        return (_market_open_time(d) - now).total_seconds()

    # Find the next trading day
    d += timedelta(days=1)
    while not _is_trading_day(d):
        d += timedelta(days=1)

    return (_market_open_time(d) - now).total_seconds()


class MarketScheduler:
    """
    Monitors market hours and emits MARKET_OPENED / MARKET_CLOSED
    events at the appropriate times.

    Runs on its own daemon thread. Checks every 30 seconds — this
    is frequent enough to catch open/close within half a minute,
    while being negligible on CPU.
    """

    POLL_INTERVAL_SECONDS = 30

    def __init__(self, bus: EventBus) -> None:
        self._bus          = bus
        self._running      = False
        self._thread:  threading.Thread | None = None
        self._market_was_open: bool | None     = None   # None = unknown at start

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            name="MarketScheduler",
            daemon=True
        )
        self._thread.start()
        logger.info("MarketScheduler started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("MarketScheduler stopped")

    def _loop(self) -> None:
        while self._running:
            try:
                self._check()
            except Exception:
                logger.exception("Error in MarketScheduler loop")

            time.sleep(self.POLL_INTERVAL_SECONDS)

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
            # First check — emit the current state unconditionally
            event_type = EventType.MARKET_OPENED if is_open else EventType.MARKET_CLOSED
            self._bus.publish(Event(type=event_type, source="MarketScheduler"))
            self._market_was_open = is_open
            logger.info(
                "Initial market state: %s", "OPEN" if is_open else "CLOSED"
            )