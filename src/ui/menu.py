"""
menu.py — Mode button menu system.

Presents options on the display when the mode button is pressed.
YES confirms, NO cancels. All actions are confirmed before executing.
Emits events rather than acting directly.
"""

import logging
import threading
from enum import Enum
from typing import Optional

from core.events import EventBus, EventType, Event
from core.state import StateManager, TradingMode
from data.settings import Settings
from hardware.display import DisplayManager
from hardware.led import LEDManager

logger = logging.getLogger(__name__)


class MenuOption(Enum):
    SWITCH_MODE        = "  MODE  "
    SWITCH_ALGO        = "  ALGO  "
    RESET_PAPER        = "  RESET "
    VIEW_STATS         = "  STATS "
    DISPLAY_BRIGHTNESS = "  DISP  "
    LED_BRIGHTNESS     = "  LED   "


MENU_OPTIONS = [
    MenuOption.SWITCH_MODE,
    MenuOption.SWITCH_ALGO,
    MenuOption.RESET_PAPER,
    MenuOption.VIEW_STATS,
    MenuOption.DISPLAY_BRIGHTNESS,
    MenuOption.LED_BRIGHTNESS,
]

# Brightness levels to cycle through
DISPLAY_BRIGHTNESS_LEVELS = [32, 64, 128, 192, 255]
LED_BRIGHTNESS_LEVELS     = [0.1, 0.2, 0.3, 0.5, 0.8, 1.0]


class MenuManager:
    """
    State machine for the mode button menu.

    States:
      CLOSED     → normal operation
      OPEN       → cycling through menu options
      CONFIRM    → waiting for YES/NO on selected option
      BRIGHTNESS → adjusting a brightness level with MODE/YES/NO

    Threading note: event handlers hold self._lock while calling display
    methods and bus.publish(). This is safe because bus.publish() is
    non-blocking and DisplayManager uses its own independent lock and
    never calls back into MenuManager.
    """

    def __init__(
        self,
        bus:      EventBus,
        state:    StateManager,
        display:  DisplayManager,
        led:      LEDManager,
        settings: Settings,
    ) -> None:
        self._bus          = bus
        self._state        = state
        self._display      = display
        self._led          = led
        self._settings     = settings
        self._menu_open    = False
        self._option_index = 0
        self._confirming   = False
        self._lock         = threading.Lock()

        # Brightness adjustment state
        self._adjusting_brightness: Optional[str] = None   # "display" or "led"
        self._display_brightness_index = self._find_display_index()
        self._led_brightness_index     = self._find_led_index()

        bus.subscribe(EventType.BUTTON_MODE_PRESSED, self._on_mode)
        bus.subscribe(EventType.BUTTON_YES_PRESSED,  self._on_yes)
        bus.subscribe(EventType.BUTTON_NO_PRESSED,   self._on_no)

    # ── Event handlers ────────────────────────────────────────

    def _on_mode(self, event: Event) -> None:
        with self._lock:
            if self._adjusting_brightness:
                # MODE cycles through brightness levels
                self._cycle_brightness()
            elif not self._menu_open:
                self._open_menu()
            elif self._confirming:
                self._confirming = False
                self._show_current_option()
            else:
                self._option_index = (
                    self._option_index + 1
                ) % len(MENU_OPTIONS)
                self._show_current_option()

    def _on_yes(self, event: Event) -> None:
        with self._lock:
            if self._adjusting_brightness:
                # YES confirms brightness and saves it
                self._confirm_brightness()
                return
            if not self._menu_open:
                return
            if not self._confirming:
                option = MENU_OPTIONS[self._option_index]
                if option in (
                    MenuOption.DISPLAY_BRIGHTNESS,
                    MenuOption.LED_BRIGHTNESS,
                ):
                    # Enter brightness adjustment mode directly
                    self._enter_brightness(option)
                else:
                    self._confirming = True
                    self._display.show_text("CONFIRM?")
            else:
                self._execute_option(MENU_OPTIONS[self._option_index])
                self._close_menu()

    def _on_no(self, event: Event) -> None:
        with self._lock:
            if self._adjusting_brightness:
                # NO cancels brightness adjustment — restore previous value
                self._cancel_brightness()
                return
            if not self._menu_open:
                return
            if self._confirming:
                self._confirming = False
                self._show_current_option()
            else:
                self._close_menu()

    # ── Menu state ────────────────────────────────────────────

    def _open_menu(self) -> None:
        self._menu_open    = True
        self._option_index = 0
        self._confirming   = False
        self._bus.publish(Event(
            type=EventType.MENU_OPENED,
            source="MenuManager"
        ))
        self._show_current_option()
        logger.info("Menu opened")

    def _close_menu(self) -> None:
        self._menu_open           = False
        self._confirming          = False
        self._adjusting_brightness = None
        self._bus.publish(Event(
            type=EventType.MENU_CLOSED,
            source="MenuManager"
        ))
        logger.info("Menu closed")

    def _show_current_option(self) -> None:
        self._display.show_text(MENU_OPTIONS[self._option_index].value)

    # ── Brightness adjustment ─────────────────────────────────

    def _enter_brightness(self, option: MenuOption) -> None:
        """Enter brightness adjustment mode for display or LED."""
        if option == MenuOption.DISPLAY_BRIGHTNESS:
            self._adjusting_brightness = "display"
            self._brightness_index_before = self._display_brightness_index
            self._show_display_brightness()
            logger.info("Entering display brightness adjustment")
        else:
            self._adjusting_brightness = "led"
            self._brightness_index_before = self._led_brightness_index
            self._show_led_brightness()
            logger.info("Entering LED brightness adjustment")

    def _cycle_brightness(self) -> None:
        """Advance to next brightness level and apply it live."""
        if self._adjusting_brightness == "display":
            self._display_brightness_index = (
                self._display_brightness_index + 1
            ) % len(DISPLAY_BRIGHTNESS_LEVELS)
            level = DISPLAY_BRIGHTNESS_LEVELS[self._display_brightness_index]
            self._display.set_brightness(level)
            self._show_display_brightness()
        else:
            self._led_brightness_index = (
                self._led_brightness_index + 1
            ) % len(LED_BRIGHTNESS_LEVELS)
            level = LED_BRIGHTNESS_LEVELS[self._led_brightness_index]
            self._led.set_brightness(level)
            self._show_led_brightness()

    def _confirm_brightness(self) -> None:
        """Save current brightness and return to menu."""
        if self._adjusting_brightness == "display":
            level = DISPLAY_BRIGHTNESS_LEVELS[self._display_brightness_index]
            self._settings.set("display_brightness", level)
            logger.info("Display brightness saved: %d", level)
        else:
            level = LED_BRIGHTNESS_LEVELS[self._led_brightness_index]
            self._settings.set("led_brightness", level)
            logger.info("LED brightness saved: %.2f", level)
        self._adjusting_brightness = None
        self._close_menu()

    def _cancel_brightness(self) -> None:
        """Restore the previous brightness without saving."""
        if self._adjusting_brightness == "display":
            self._display_brightness_index = self._brightness_index_before
            level = DISPLAY_BRIGHTNESS_LEVELS[self._display_brightness_index]
            self._display.set_brightness(level)
            logger.info("Display brightness cancelled, restored to %d", level)
        else:
            self._led_brightness_index = self._brightness_index_before
            level = LED_BRIGHTNESS_LEVELS[self._led_brightness_index]
            self._led.set_brightness(level)
            logger.info("LED brightness cancelled, restored to %.2f", level)
        self._adjusting_brightness = None
        self._close_menu()

    def _show_display_brightness(self) -> None:
        level = DISPLAY_BRIGHTNESS_LEVELS[self._display_brightness_index]
        # Show as percentage for clarity e.g. "DISP 50%"
        pct = round(level / 255 * 100)
        self._display.show_text(f"DISP{pct:3d}%")

    def _show_led_brightness(self) -> None:
        level = LED_BRIGHTNESS_LEVELS[self._led_brightness_index]
        pct   = round(level * 100)
        self._display.show_text(f"LED {pct:3d}%")

    def _find_display_index(self) -> int:
        """Find the index in DISPLAY_BRIGHTNESS_LEVELS closest to saved value."""
        saved = self._settings.get("display_brightness")
        return min(
            range(len(DISPLAY_BRIGHTNESS_LEVELS)),
            key=lambda i: abs(DISPLAY_BRIGHTNESS_LEVELS[i] - saved)
        )

    def _find_led_index(self) -> int:
        """Find the index in LED_BRIGHTNESS_LEVELS closest to saved value."""
        saved = self._settings.get("led_brightness")
        return min(
            range(len(LED_BRIGHTNESS_LEVELS)),
            key=lambda i: abs(LED_BRIGHTNESS_LEVELS[i] - saved)
        )

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
            snap = self._state.snapshot()
            pnl  = snap.current_pnl
            sign = "+" if pnl >= 0 else "-"
            self._display.show_message(f"PNL {sign}{abs(pnl):.0f} SEK")

        # DISPLAY_BRIGHTNESS and LED_BRIGHTNESS are handled in _on_yes
        # before reaching _execute_option so they never arrive here