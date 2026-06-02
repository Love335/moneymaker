"""
buttons.py — Button polling with debouncing and event emission.

Polls all buttons on a dedicated thread. Emits events on the bus
when presses are detected. Never calls business logic directly.
"""

import logging
import threading
import time
from typing import Dict

import RPi.GPIO as GPIO

from core.events import EventBus, EventType, Event

logger = logging.getLogger(__name__)

# GPIO pin assignments (BCM numbering)
PIN_YES    = 16   # Physical pin 36
PIN_NO     = 20   # Physical pin 38
PIN_MODE   = 21   # Physical pin 40

# Debounce: minimum milliseconds between registered presses
DEBOUNCE_MS    = 50
POLL_INTERVAL  = 0.02   # 20ms poll interval


class ButtonManager:
    """
    Monitors all buttons and emits events on press.

    Uses active-low logic — button press pulls GPIO pin LOW.
    Internal pull-up resistors are enabled so no external resistors needed.
    """

    BUTTON_MAP: Dict[int, EventType] = {
        PIN_YES:  EventType.BUTTON_YES_PRESSED,
        PIN_NO:   EventType.BUTTON_NO_PRESSED,
        PIN_MODE: EventType.BUTTON_MODE_PRESSED,
    }

    def __init__(self, bus: EventBus) -> None:
        self._bus       = bus
        self._running   = False
        self._thread    = None
        # Track last press time per pin for debouncing
        self._last_press: Dict[int, float] = {pin: 0.0 for pin in self.BUTTON_MAP}
        # Track last known state to detect edges (pressed vs held)
        self._last_state: Dict[int, bool]  = {pin: False for pin in self.BUTTON_MAP}

    def start(self) -> None:
        GPIO.setmode(GPIO.BCM)
        for pin in self.BUTTON_MAP:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            logger.debug("ButtonManager: configured GPIO %d", pin)

        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            name="ButtonManager",
            daemon=True
        )
        self._thread.start()
        logger.info("ButtonManager started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        GPIO.cleanup()
        logger.info("ButtonManager stopped")

    # ── Poll loop ─────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                now = time.monotonic()
                for pin, event_type in self.BUTTON_MAP.items():
                    is_pressed = GPIO.input(pin) == GPIO.LOW
                    was_pressed = self._last_state[pin]

                    # Detect falling edge (not-pressed → pressed)
                    if is_pressed and not was_pressed:
                        elapsed_ms = (now - self._last_press[pin]) * 1000
                        if elapsed_ms >= DEBOUNCE_MS:
                            self._last_press[pin] = now
                            self._emit(pin, event_type)

                    self._last_state[pin] = is_pressed

                time.sleep(POLL_INTERVAL)

            except Exception:
                logger.exception("ButtonManager: error in poll loop")
                time.sleep(0.5)

    def _emit(self, pin: int, event_type: EventType) -> None:
        logger.debug("Button pressed: GPIO %d → %s", pin, event_type.name)
        self._bus.publish(Event(
            type=event_type,
            payload={"pin": pin},
            source="ButtonManager"
        ))