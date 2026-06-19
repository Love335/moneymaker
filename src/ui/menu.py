"""
menu.py — Mode button menu system.

Presents options on the display when the mode button is pressed.
YES confirms, NO cancels. All actions are confirmed before executing.
Emits events rather than acting directly.
"""

import logging
import threading
import time
from enum import Enum, auto
from typing import Callable, List, Optional

from core.events import EventBus, EventType, Event
from core.state import StateManager, TradingMode
from hardware.display import DisplayManager
from hardware.led import LEDManager

logger = logging.getLogger(__name__)


class MenuOption(Enum):
    SWITCH_MODE  = "  MODE  "
    SWITCH_ALGO  = "  ALGO  "
    RESET_PAPER  = "  RESET "
    VIEW_STATS   = "  STATS "


MENU_OPTIONS = [
    MenuOption.SWITCH_MODE,
    MenuOption.SWITCH_ALGO,
    MenuOption.RESET_PAPER,
    MenuOption.VIEW_STATS,
]


class MenuManager:
    """
    State machine for the mode button menu.

    States:
      CLOSED  → normal operation
      OPEN    → cycling through menu options
      CONFIRM → waiting for YES/NO on selected option
    """

    def __init__(
        self,
        bus:     EventBus,
        state:   StateManager,
        display: DisplayManager,
        led:     LEDManager,
    ) -> None:
        self._bus     = bus
        self._state   = state
        self._display = display
        self._led     = led

        self._menu_open      = False
        self._option_index   = 0
        self._confirming     = False
        self._lock           = threading.Lock()

        # Subscribe to hardware events
        bus.subscribe(EventType.BUTTON_MODE_PRESSED, self._on_mode)
        bus.subscribe(EventType.BUTTON_YES_PRESSED,  self._on_yes)
        bus.subscribe(EventType.BUTTON_NO_PRESSED,   self._on_no)

    # ── Event handlers ────────────────────────────────────────

    def _on_mode(self, event: Event) -> None:
        with self._lock:
            if not self._menu_open:
                self._open_menu()
            elif self._confirming:
                # Mode button in confirm state → go back to option selection
                self._confirming = False
                self._show_current_option()
            else:
                # Cycle to next option
                self._option_index = (self._option_index + 1) % len(MENU_OPTIONS)
                self._show_current_option()

    def _on_yes(self, event: Event) -> None:
        with self._lock:
            if not self._menu_open:
                return
            if not self._confirming:
                # Show confirmation prompt
                self._confirming = True
                option = MENU_OPTIONS[self._option_index]
                self._display.show_text("CONFIRM?")
            else:
                # Execute the selected option
                self._execute_option(MENU_OPTIONS[self._option_index])
                self._close_menu()

    def _on_no(self, event: Event) -> None:
        with self._lock:
            if not self._menu_open:
                return
            if self._confirming:
                # Cancel confirmation, return to option display
                self._confirming = False
                self._show_current_option()
            else:
                # Close menu entirely
                self._close_menu()

    # ── Menu state ────────────────────────────────────────────

    def _open_menu(self) -> None:
        self._menu_open    = True
        self._option_index = 0
        self._confirming   = False
        self._bus.publish(Event(type=EventType.MENU_OPENED, source="MenuManager"))
        self._show_current_option()  # LED follows display automatically
        logger.info("Menu opened")

    def _close_menu(self) -> None:
        self._menu_open  = False
        self._confirming = False
        self._bus.publish(Event(type=EventType.MENU_CLOSED, source="MenuManager"))
        # Displaying idle P&L will set LED to off (idle), pulse loop handles the rest
        logger.info("Menu closed")

    def _show_current_option(self) -> None:
        option = MENU_OPTIONS[self._option_index]
        self._display.show_text(option.value)

    # ── Option execution ──────────────────────────────────────

    def _execute_option(self, option: MenuOption) -> None:
        logger.info("Menu: executing option %s", option.name)

        if option == MenuOption.SWITCH_MODE:
            self._bus.publish(Event(
                type=EventType.MODE_SWITCHED,
                source="MenuManager"
            ))

        elif option == MenuOption.SWITCH_ALGO:
            self._bus.publish(Event(
                type=EventType.ALGORITHM_SWITCHED,
                source="MenuManager"
            ))

        elif option == MenuOption.RESET_PAPER:
            snap = self._state.snapshot()
            if snap.trading_mode != TradingMode.PAPER:
                self._display.show_text("REAL MOD")
            else:
                self._bus.publish(Event(
                    type=EventType.PAPER_PORTFOLIO_RESET,
                    source="MenuManager"
                ))

        elif option == MenuOption.VIEW_STATS:
            snap    = self._state.snapshot()
            pnl     = snap.current_pnl
            sign    = "+" if pnl >= 0 else "-"
            # Show on display as scrolling message for stats only
            self._display.show_message(
                f"PNL {sign}{abs(pnl):.0f} SEK"
            )