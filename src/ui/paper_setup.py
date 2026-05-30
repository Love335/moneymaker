"""
paper_setup.py — Balance selector shown on first fresh paper trading start.

Cycles through balance options using the mode button.
YES confirms, NO cancels and uses the default.
"""

import logging
import threading
from typing import Optional

from core.events import EventBus, EventType, Event
from hardware.display import DisplayManager
from hardware.led import LEDManager, LEDState

logger = logging.getLogger(__name__)

DEFAULT_BALANCE     = 10_000.0
BALANCE_STEP        =  1_000.0
BALANCE_MIN         =  1_000.0
BALANCE_MAX         = 100_000.0


class PaperBalanceSelector:
    """
    Interactive balance selector using physical buttons.

    Mode button → increment balance by 1,000 SEK
    YES button  → confirm selection
    NO button   → cancel (use default)
    """

    def __init__(
        self,
        display: DisplayManager,
        led:     LEDManager,
        bus:     EventBus,
    ) -> None:
        self._display   = display
        self._led       = led
        self._bus       = bus
        self._balance   = DEFAULT_BALANCE
        self._confirmed = threading.Event()
        self._cancelled = threading.Event()
        self._result:   Optional[float] = None

    def run(self) -> float:
        """
        Block until user confirms or cancels.
        Returns the selected balance in SEK.
        """
        self._display.show_message("SET BALANCE")
        self._led.set_state(LEDState.WORKING)
        self._show_balance()

        # Subscribe to button events
        self._bus.subscribe(EventType.BUTTON_MODE_PRESSED, self._on_mode)
        self._bus.subscribe(EventType.BUTTON_YES_PRESSED,  self._on_yes)
        self._bus.subscribe(EventType.BUTTON_NO_PRESSED,   self._on_no)

        # Wait for user input (no timeout — user must make a choice)
        while not self._confirmed.is_set() and not self._cancelled.is_set():
            import time
            time.sleep(0.1)

        # Unsubscribe
        self._bus.unsubscribe(EventType.BUTTON_MODE_PRESSED, self._on_mode)
        self._bus.unsubscribe(EventType.BUTTON_YES_PRESSED,  self._on_yes)
        self._bus.unsubscribe(EventType.BUTTON_NO_PRESSED,   self._on_no)

        if self._cancelled.is_set():
            logger.info(
                "Balance selection cancelled — using default %.2f SEK",
                DEFAULT_BALANCE
            )
            return DEFAULT_BALANCE

        logger.info("Balance confirmed: %.2f SEK", self._balance)
        return self._balance

    def _on_mode(self, event: Event) -> None:
        self._balance = min(self._balance + BALANCE_STEP, BALANCE_MAX)
        if self._balance > BALANCE_MAX:
            self._balance = BALANCE_MIN   # wrap around
        self._show_balance()

    def _on_yes(self, event: Event) -> None:
        self._result = self._balance
        self._display.show_message(f"SET {self._balance:.0f}SEK")
        self._confirmed.set()

    def _on_no(self, event: Event) -> None:
        self._cancelled.set()

    def _show_balance(self) -> None:
        self._display.show_message(f"BAL {self._balance:.0f}SEK")