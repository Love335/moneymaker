"""
events.py — Event definitions and thread-safe publish-subscribe event bus.

All inter-component communication flows through here. No component
imports another directly; they only speak through events.
"""

import queue
import threading
import logging
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


class EventType(Enum):
    # ── Hardware ──────────────────────────────────────────────
    BUTTON_YES_PRESSED      = auto()
    BUTTON_NO_PRESSED       = auto()
    BUTTON_MODE_PRESSED     = auto()
    SWITCH_OFF_TRIGGERED    = auto()

    # ── Market ────────────────────────────────────────────────
    MARKET_OPENED           = auto()
    MARKET_CLOSED           = auto()
    PRICE_UPDATED           = auto()

    # ── Trading ───────────────────────────────────────────────
    TRADE_EXECUTED          = auto()
    TRADE_FAILED            = auto()
    TRADE_SIGNAL_GENERATED  = auto()
    PORTFOLIO_UPDATED       = auto()
    ORDER_CANCELLED         = auto()

    # ── Application ───────────────────────────────────────────
    ALGORITHM_SWITCHED      = auto()
    MODE_SWITCHED           = auto()
    PAPER_PORTFOLIO_RESET   = auto()
    PAPER_BALANCE_SELECTED  = auto()

    # ── UI ────────────────────────────────────────────────────
    URGENT_MESSAGE          = auto()
    MESSAGE_ACKNOWLEDGED    = auto()
    MESSAGE_DISMISSED       = auto()
    MENU_OPENED             = auto()
    MENU_CLOSED             = auto()
    DISPLAY_UPDATED         = auto()

    # ── System ────────────────────────────────────────────────
    STARTUP_COMPLETE        = auto()
    SHUTDOWN_REQUESTED      = auto()
    CRASH_DETECTED          = auto()
    RECOVERY_CONFIRMED      = auto()

    # ── Connectivity ──────────────────────────────────────────
    CONNECTION_LOST         = auto()
    CONNECTION_RESTORED     = auto()
    API_ERROR               = auto()
    API_INVALID_DATA        = auto()


@dataclass
class Event:
    """
    A single event travelling through the bus.

    type:    the EventType identifying what happened
    payload: optional dict of data relevant to this event
    source:  name of the component that emitted this event
    """
    type:    EventType
    payload: Dict[str, Any] = field(default_factory=dict)
    source:  str            = "unknown"


# Type alias for subscriber callbacks
Subscriber = Callable[[Event], None]


class EventBus:
    """
    Thread-safe publish-subscribe event bus.

    Components subscribe to one or more EventTypes with a callback.
    When an event is published it is placed on an internal queue and
    dispatched on the bus's own dedicated thread, so publishers are
    never blocked waiting for slow subscribers.
    """

    def __init__(self) -> None:
        self._subscribers: Dict[EventType, List[Subscriber]] = {}
        self._queue: queue.Queue[Event] = queue.Queue()
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self) -> None:
        """Start the dispatch thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name="EventBus",
            daemon=True
        )
        self._thread.start()
        logger.info("EventBus started")

    def stop(self) -> None:
        """Stop the dispatch thread gracefully."""
        self._running = False
        # Unblock the queue with a sentinel
        self._queue.put(None)  # type: ignore[arg-type]
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("EventBus stopped")

    # ── Public API ────────────────────────────────────────────

    def subscribe(self, event_type: EventType, callback: Subscriber) -> None:
        """Register a callback for a specific event type."""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)
        logger.debug("Subscribed %s to %s", callback.__qualname__, event_type.name)

    def unsubscribe(self, event_type: EventType, callback: Subscriber) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            if event_type in self._subscribers:
                self._subscribers[event_type] = [
                    cb for cb in self._subscribers[event_type]
                    if cb != callback
                ]

    def publish(self, event: Event) -> None:
        """
        Publish an event. Non-blocking — places event on queue
        and returns immediately.
        """
        logger.debug("Published: %s from %s", event.type.name, event.source)
        self._queue.put(event)

    def publish_simple(
        self,
        event_type: EventType,
        source: str = "unknown",
        **kwargs: Any
    ) -> None:
        """Convenience method for publishing without constructing an Event."""
        self.publish(Event(type=event_type, payload=kwargs, source=source))

    # ── Internal ──────────────────────────────────────────────

    def _dispatch_loop(self) -> None:
        """Continuously dispatch events from the queue to subscribers."""
        while self._running:
            try:
                event = self._queue.get(timeout=1)
                if event is None:
                    break
                self._dispatch(event)
            except queue.Empty:
                continue
            except Exception:
                logger.exception("Unexpected error in EventBus dispatch loop")

    def _dispatch(self, event: Event) -> None:
        """Deliver an event to all registered subscribers."""
        with self._lock:
            callbacks = list(self._subscribers.get(event.type, []))

        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                logger.exception(
                    "Subscriber %s raised an exception handling %s",
                    callback.__qualname__,
                    event.type.name
                )