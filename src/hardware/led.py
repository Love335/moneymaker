"""
led.py — WS2812D RGB LED manager with named states.

No component outside this file ever deals with raw RGB values.
All LED behaviour is defined as named states here.
"""

import logging
import threading
import time
from enum import Enum, auto
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# GRB colour definitions
COLOUR_OFF    = (0,   0,   0)
COLOUR_GREEN  = (255, 0,   0)
COLOUR_RED    = (0,   255, 0)
COLOUR_YELLOW = (180, 255, 0)
COLOUR_BLUE   = (0,   0,   150)
COLOUR_WHITE  = (80,  80,  80)
COLOUR_PINK   = (0,   100, 50)

FLASH_COUNT   = 3
FLASH_ON_SEC  = 0.2
FLASH_OFF_SEC = 0.2
PULSE_DIM     = (5, 5, 5)        # very dim pulse for passive indicator

STATE_HOLD_SECONDS = 6.0


class LEDState(Enum):
    OFF            = auto()
    IDLE_PAPER     = auto()   # very dim pink pulse every few minutes
    IDLE_REAL      = auto()   # very dim white pulse every few minutes
    WORKING        = auto()   # steady yellow
    TRADE_PROFIT   = auto()   # green flash x3
    TRADE_LOSS     = auto()   # red flash x3
    ERROR          = auto()   # slow red pulse
    MARKET_CLOSED  = auto()   # off
    MENU_OPEN      = auto()   # steady blue


class LEDManager:
    """
    Controls the WS2812D LED with state-based behaviour.
    Transient states (flashes) execute and then return to the last
    persistent state automatically.
    """

    # States that persist until explicitly changed
    PERSISTENT_STATES = {
        LEDState.OFF,
        LEDState.IDLE_PAPER,
        LEDState.IDLE_REAL,
        LEDState.WORKING,
        LEDState.ERROR,
        LEDState.MARKET_CLOSED,
        LEDState.MENU_OPEN,
    }

    # States that are transient (flash and return)
    TRANSIENT_STATES = {
        LEDState.TRADE_PROFIT,
        LEDState.TRADE_LOSS,
    }

    # Passive pulse interval in seconds
    PASSIVE_PULSE_INTERVAL = 300   # every 5 minutes

    def __init__(self) -> None:
        self._pixel              = None
        self._current_state:     LEDState = LEDState.OFF
        self._persistent_state:  LEDState = LEDState.OFF
        self._lock               = threading.Lock()
        self._thread:            Optional[threading.Thread] = None
        self._running:           bool     = False
        self._pulse_thread:      Optional[threading.Thread] = None
        self._hold_active: bool = False

    def start(self) -> None:
        """Initialise hardware and start background thread."""
        try:
            import board
            import neopixel
            self._pixel = pixel = neopixel.NeoPixel(
                board.D18, 1, brightness=0.3, auto_write=True)
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
        self._running = False
        if self._pulse_thread:
            self._pulse_thread.join(timeout=5)
        self._set_colour(COLOUR_OFF)
        logger.info("LEDManager stopped")

    # ── Public API ────────────────────────────────────────────

    def set_state(self, state: LEDState, hold: bool = True) -> None:
        """Set the LED to the given state."""
        # Don't interrupt an active hold with a lower-priority idle state
        with self._lock:
            if self._hold_active and state in (
                LEDState.IDLE_PAPER, LEDState.IDLE_REAL,
                LEDState.OFF, LEDState.MARKET_CLOSED
            ):
                return

            self._current_state = state
            if state in self.PERSISTENT_STATES:
                self._persistent_state = state

        if state in self.TRANSIENT_STATES:
            t = threading.Thread(
                target=self._run_transient,
                args=(state,),
                daemon=True
            )
            t.start()
        elif hold and state not in (
            LEDState.OFF, LEDState.MARKET_CLOSED,
            LEDState.IDLE_PAPER, LEDState.IDLE_REAL
        ):
            t = threading.Thread(
                target=self._hold_then_restore,
                args=(state,),
                daemon=True
            )
            t.start()
        else:
            self._apply_persistent(state)
            
    # ── State rendering ───────────────────────────────────────

    def _apply_persistent(self, state: LEDState) -> None:
        colour_map = {
            LEDState.OFF:           COLOUR_OFF,
            LEDState.IDLE_PAPER:    COLOUR_OFF,   # handled by pulse loop
            LEDState.IDLE_REAL:     COLOUR_OFF,   # handled by pulse loop
            LEDState.WORKING:       COLOUR_YELLOW,
            LEDState.ERROR:         COLOUR_OFF,   # handled by pulse loop
            LEDState.MARKET_CLOSED: COLOUR_OFF,
            LEDState.MENU_OPEN:     COLOUR_BLUE,
        }
        self._set_colour(colour_map.get(state, COLOUR_OFF))

    def _run_transient(self, state: LEDState) -> None:
        """Execute a flash animation then restore persistent state."""
        colour = COLOUR_GREEN if state == LEDState.TRADE_PROFIT else COLOUR_RED

        for _ in range(FLASH_COUNT):
            self._set_colour(colour)
            time.sleep(FLASH_ON_SEC)
            self._set_colour(COLOUR_OFF)
            time.sleep(FLASH_OFF_SEC)

        # Restore persistent state
        with self._lock:
            restore = self._persistent_state
        self._apply_persistent(restore)

    def _hold_then_restore(self, state: LEDState) -> None:
        with self._lock:
            self._hold_active = True
        self._apply_persistent(state)
        time.sleep(STATE_HOLD_SECONDS)
        with self._lock:
            self._hold_active = False
            self._persistent_state = LEDState.IDLE_PAPER
        self._apply_persistent(LEDState.IDLE_PAPER)

    def _passive_pulse_loop(self) -> None:
        """Emit a brief dim pulse on a long interval for passive mode indication."""
        while self._running:
            time.sleep(self.PASSIVE_PULSE_INTERVAL)
            with self._lock:
                state = self._persistent_state

            if state == LEDState.IDLE_PAPER:
                self._set_colour(COLOUR_PINK)
                time.sleep(0.3)
                self._set_colour(COLOUR_OFF)
            elif state == LEDState.IDLE_REAL:
                self._set_colour(COLOUR_WHITE)
                time.sleep(0.3)
                self._set_colour(COLOUR_OFF)
            elif state == LEDState.ERROR:
                # Slow red pulse for error state
                self._set_colour(COLOUR_RED)
                time.sleep(0.5)
                self._set_colour(COLOUR_OFF)
                time.sleep(0.5)

    # ── Hardware write ────────────────────────────────────────

    def _set_colour(self, colour) -> None:
        if self._pixel is None:
            logger.warning("LEDManager: _set_colour called but pixel is None")
            return
        try:
            self._pixel[0] = colour
        except Exception as exc:
            logger.error("LEDManager: write error: %s", exc)