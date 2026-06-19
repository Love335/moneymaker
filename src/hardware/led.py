"""
led.py — WS2812D RGB LED manager, display-driven.

The LED colour is derived entirely from what is shown on the display.
Register this manager as a display callback via DisplayManager.set_led_callback().
The LED always matches what the user sees — no independent state machine.

Passive idle pulse (every 5 minutes) indicates paper vs real mode
since the display shows P&L in idle state and the mode char alone
is not visually distinct enough at a glance.
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
    # Exact matches first
    ("GOODBYE",   False, COLOUR_OFF),
    ("READY",     False, COLOUR_GREEN),
    ("STARTING",  False, COLOUR_YELLOW),
    ("MKT OPEN",  False, COLOUR_GREEN),
    ("MKT CLOSE", False, COLOUR_OFF),
    ("NO SIGNAL", False, COLOUR_WHITE),
    ("NO DATA",   False, COLOUR_RED),
    ("RESUMED",   False, COLOUR_GREEN),
    ("RESET OK",  False, COLOUR_GREEN),
    ("REAL OK",   False, COLOUR_GREEN),
    ("MODE",      False, COLOUR_BLUE),
    ("  MODE  ", False, COLOUR_BLUE),
    ("  ALGO  ", False, COLOUR_BLUE),
    ("  RESET ", False, COLOUR_BLUE),
    ("  STATS ", False, COLOUR_BLUE),
    ("CONFIRM?", False, COLOUR_YELLOW),

    # Prefix matches
    ("BUY",       True,  COLOUR_YELLOW),
    ("SELL",      True,  COLOUR_YELLOW),
    ("BUY OK",    True,  COLOUR_GREEN),
    ("SELL OK",   True,  COLOUR_GREEN),
    ("ALGO",      True,  COLOUR_BLUE),
    ("ERR",       True,  COLOUR_RED),
    ("AUTH",      True,  COLOUR_RED),
    ("DATA ERR",  True,  COLOUR_RED),
    ("BROKER ERR",True,  COLOUR_RED),
    ("EXEC ERR",  True,  COLOUR_RED),
    ("CONFIRM",   True,  COLOUR_RED),
    ("PRESS YES", True,  COLOUR_RED),
    ("EVALUATING",True,  COLOUR_YELLOW),
]

PASSIVE_PULSE_INTERVAL = 300   # seconds between idle pulses


class LEDManager:
    """
    Controls the WS2812D LED based on what is shown on the display.

    Call set_mode() when trading mode changes so the idle pulse
    colour reflects paper (pink) vs real (white).
    """

    def __init__(self) -> None:
        self._pixel         = None
        self._lock          = threading.Lock()
        self._running       = False
        self._pulse_thread: Optional[threading.Thread] = None
        self._is_idle       = True    # True when display is showing P&L idle screen
        self._paper_mode    = True    # True = paper, False = real

    def start(self) -> None:
        """Initialise hardware and start passive pulse thread."""
        try:
            import board
            import neopixel
            self._pixel = neopixel.NeoPixel(
                board.D18, 1, brightness=0.3, auto_write=True
            )
            logger.info("LEDManager: WS2812D initialised on GPIO 18")
        except Exception as exc:
            logger.error("LEDManager: failed to init LED: %s", exc)
            self._pixel = None

        self._running = True
        self._pulse_thread = threading.Thread(
            target=self._passive_pulse_loop,
            name="LEDPulse",
            daemon=True
        )
        self._pulse_thread.start()

    def stop(self) -> None:
        """Turn off LED and stop background thread."""
        self._running = False
        if self._pulse_thread:
            self._pulse_thread.join(timeout=5)
        self._write(COLOUR_OFF)
        logger.info("LEDManager stopped")

    # ── Public API ─────────────────────────────────────────────

    def set_mode(self, paper: bool) -> None:
        """Call when trading mode changes to update idle pulse colour."""
        with self._lock:
            self._paper_mode = paper

    def on_display_update(self, text: str) -> None:
        """
        Callback invoked by DisplayManager whenever the display changes.
        Derives LED colour from the displayed text and applies it.
        """
        text_upper = text.strip().upper()

        # Idle screen: starts with "P " or "R " followed by P&L
        # e.g. "P  +1234" — show no colour, let pulse thread handle it
        if len(text_upper) >= 2 and text_upper[0] in ("P", "R") and text_upper[1] == " ":
            with self._lock:
                self._is_idle = True
            self._write(COLOUR_OFF)
            return

        with self._lock:
            self._is_idle = False

        colour = self._colour_for(text_upper)
        self._write(colour)

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
        # Default: white for any unrecognised message
        return COLOUR_WHITE

    # ── Passive idle pulse ─────────────────────────────────────

    def _passive_pulse_loop(self) -> None:
        """Brief colour pulse every 5 minutes when display is idle."""
        while self._running:
            time.sleep(PASSIVE_PULSE_INTERVAL)
            with self._lock:
                idle = self._is_idle
                paper = self._paper_mode

            if not idle:
                continue

            colour = COLOUR_PINK if paper else COLOUR_WHITE
            self._write(colour)
            time.sleep(0.3)
            self._write(COLOUR_OFF)

    # ── Hardware write ─────────────────────────────────────────

    def _write(self, colour: tuple) -> None:
        if self._pixel is None:
            return
        try:
            self._pixel[0] = colour
        except Exception as exc:
            logger.error("LEDManager: write error: %s", exc)