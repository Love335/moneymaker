"""
led.py — WS2812D RGB LED manager, display-driven.

The LED colour is derived entirely from what is shown on the display.
Register this manager as a display callback via DisplayManager.set_led_callback().
The LED always matches what the user sees — no independent state machine.
"""

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# GRB colour order (WS2812)
COLOUR_OFF    = (0,   0,   0)
COLOUR_GREEN  = (255, 0,   0)
COLOUR_RED    = (0,   255, 0)
COLOUR_YELLOW = (180, 255, 0)
COLOUR_BLUE   = (0,   0,   150)
COLOUR_WHITE  = (80,  80,  80)
COLOUR_PINK   = (0,   100, 50)

# Map display message prefixes/exact strings to LED colours.
# Checked in order — first match wins.
# Tuples of (match_string, is_prefix, colour)
DISPLAY_COLOUR_MAP = [
    # Exact matches
    ("GOODBYE",    False, COLOUR_OFF),
    ("READY",      False, COLOUR_GREEN),
    ("STARTING",   False, COLOUR_YELLOW),
    ("MKT OPEN",   False, COLOUR_GREEN),
    ("MKT CLOSE",  False, COLOUR_OFF),
    ("NO SIGNAL",  False, COLOUR_WHITE),
    ("NO DATA",    False, COLOUR_RED),
    ("RESUMED",    False, COLOUR_GREEN),
    ("RESET OK",   False, COLOUR_GREEN),
    ("REAL OK",    False, COLOUR_GREEN),
    ("CONFIRM?",   False, COLOUR_YELLOW),

    # Balance selector
    ("SET BAL",    False, COLOUR_BLUE),
    ("BAL ",       True,  COLOUR_BLUE),
    ("SET ",       True,  COLOUR_GREEN),

    # Menu navigation
    ("  MODE  ",   False, COLOUR_BLUE),
    ("  ALGO  ",   False, COLOUR_BLUE),
    ("  RESET ",   False, COLOUR_BLUE),
    ("  STATS ",   False, COLOUR_BLUE),

    # Mode display
    ("MODE ",      True,  COLOUR_BLUE),
    ("ALGO ",      True,  COLOUR_BLUE),

    # Trading activity — OK variants must come before plain BUY/SELL
    ("EVALUATING", True,  COLOUR_YELLOW),
    ("BUY OK",     True,  COLOUR_GREEN),
    ("SELL OK",    True,  COLOUR_GREEN),
    ("BUY ",       True,  COLOUR_YELLOW),
    ("SELL ",      True,  COLOUR_YELLOW),

    # Errors
    ("ERR",        True,  COLOUR_RED),
    ("AUTH",       True,  COLOUR_RED),
    ("DATA ERR",   True,  COLOUR_RED),
    ("BROKER ERR", True,  COLOUR_RED),
    ("EXEC ERR",   True,  COLOUR_RED),
    ("PRESS YES",  True,  COLOUR_RED),
    ("NO CONNECT", True,  COLOUR_RED),
    ("API ERROR",  True,  COLOUR_RED),
]


class LEDManager:
    """
    Controls the WS2812D LED based on what is shown on the display.
    LED is off when the display is showing the idle P&L screen.
    """

    def __init__(self) -> None:
        self._pixel = None
        self._lock  = threading.Lock()

    def start(self, brightness: float = 0.3) -> None:
        """Initialise hardware."""
        try:
            import board
            import neopixel
            self._pixel = neopixel.NeoPixel(
                board.D18, 1, brightness=brightness, auto_write=True
            )
            logger.info(
                "LEDManager: WS2812D initialised on GPIO 18 (brightness=%.2f)",
                brightness
            )
        except Exception as exc:
            logger.error("LEDManager: failed to init LED: %s", exc)
            self._pixel = None

    def set_brightness(self, value: float) -> None:
        """
        Set LED brightness (0.0–1.0).
        Takes effect immediately — the next colour write will use
        the new brightness.
        """
        value = max(0.0, min(1.0, float(value)))
        if self._pixel is not None:
            try:
                self._pixel.brightness = value
                logger.info("LEDManager: brightness set to %.2f", value)
            except Exception as exc:
                logger.error("LEDManager: failed to set brightness: %s", exc)
        else:
            logger.warning(
                "LEDManager: set_brightness called but pixel not initialised"
            )

    def stop(self) -> None:
        """Turn off LED."""
        self._write(COLOUR_OFF)
        logger.info("LEDManager stopped")

    # ── Public API ─────────────────────────────────────────────

    def on_display_update(self, text: str) -> None:
        text_upper = text.upper()

        # Blank screen (between scrolls or after shutdown) — LED off
        if text_upper.strip() == "":
            self._write(COLOUR_OFF)
            return

        # Idle P&L screen: stripped content starts with "P " or "R "
        stripped = text_upper.strip()
        if (
            len(stripped) >= 2
            and stripped[0] in ("P", "R")
            and stripped[1] == " "
        ):
            self._write(COLOUR_OFF)
            return

        # Match against full unstripped text (preserves menu entry spaces)
        self._write(self._colour_for(text_upper))        
        
    # ── Colour resolution ──────────────────────────────────────

    def _colour_for(self, text: str) -> tuple:
        """Return the LED colour for a given display string."""
        for match, is_prefix, colour in DISPLAY_COLOUR_MAP:
            if is_prefix:
                if text.startswith(match):
                    return colour
            else:
                if text == match or text.startswith(match):
                    return colour
        return COLOUR_WHITE

    # ── Hardware write ─────────────────────────────────────────

    def _write(self, colour: tuple) -> None:
        if self._pixel is None:
            return
        try:
            self._pixel[0] = colour
        except Exception as exc:
            logger.error("LEDManager: write error: %s", exc)