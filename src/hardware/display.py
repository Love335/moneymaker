"""
display.py — MAX7219 8-digit display manager.

Handles scrolling messages, urgent flashing, and idle P&L display.
Runs its own thread so scrolling is smooth and non-blocking.

Display format when idle:
  [P or R] [space] [P&L number right-aligned]
  e.g.  "P  +1234" or "R   -56"
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from luma.led_matrix.device import max7219
from luma.core.interface.serial import spi, noop
from luma.core.virtual import sevensegment

logger = logging.getLogger(__name__)

SCROLL_DELAY    = 0.3    # seconds per character during scroll
FLASH_INTERVAL  = 0.4    # seconds between flashes for urgent messages
IDLE_REFRESH    = 10.0   # seconds between idle P&L refreshes


class MessagePriority(Enum):
    NORMAL = auto()
    URGENT = auto()


@dataclass
class DisplayMessage:
    text:     str
    priority: MessagePriority = MessagePriority.NORMAL
    flash:    bool            = False   # flash instead of scroll if True
    static:   bool            = False   # show immediately, no scroll


class DisplayManager:
    """
    Manages the MAX7219 8-digit 7-segment display.

    Normal messages scroll left to right.
    Urgent messages flash statically until acknowledged externally.
    Idle state shows mode indicator and current P&L.
    """
    def __init__(self) -> None:
        self._seg:             Optional[sevensegment] = None
        self._queue:           queue.Queue            = queue.Queue()
        self._running:         bool                   = False
        self._thread:          Optional[threading.Thread] = None
        self._current_pnl:     float                  = 0.0
        self._mode_char:       str                    = "P"
        self._lock:            threading.Lock         = threading.Lock()
        self._flashing_urgent: bool                   = False
        self._led_callback                            = None

    def set_led_callback(self, callback) -> None:
        """Register a callable that receives the display text on every update."""
        self._led_callback = callback

    def _write_raw(self, text: str) -> None:
        """Write exactly 8 characters to the display hardware only."""
        if self._seg is None:
            return
        try:
            self._seg.text = text[:8]
        except Exception as exc:
            logger.error("DisplayManager: write error: %s", exc)

    def _notify_led(self, text: str) -> None:
        """Notify the LED of the current logical message."""
        if self._led_callback:
            try:
                self._led_callback(text)
            except Exception as exc:
                logger.error("DisplayManager: LED callback error: %s", exc)
            
    def start(self) -> None:
        """Initialise hardware and start display thread."""
        try:
            serial     = spi(port=0, device=0, gpio=noop())
            device     = max7219(serial, cascaded=1, block_orientation=0, rotate=0)
            self._seg  = sevensegment(device)
            logger.info("DisplayManager: MAX7219 initialised")
        except Exception as exc:
            logger.error("DisplayManager: failed to init display: %s", exc)
            self._seg = None

        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            name="DisplayManager",
            daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._write_raw("        ")
        logger.info("DisplayManager stopped")

    # ── Public API ────────────────────────────────────────────

    def show_message(self, text: str, urgent: bool = False) -> None:
        """Queue a message for display. Urgent messages interrupt normal flow."""
        priority = MessagePriority.URGENT if urgent else MessagePriority.NORMAL
        self._queue.put(DisplayMessage(text=text, priority=priority))

    def flash_message(self, text: str) -> None:
        """Queue a message to flash (not scroll) — for urgent alerts."""
        self._queue.put(DisplayMessage(
            text=text,
            priority=MessagePriority.URGENT,
            flash=True,
        ))

    def update_pnl(self, pnl: float) -> None:
        """Update the P&L value shown in idle state."""
        with self._lock:
            self._current_pnl = pnl

    def set_mode_char(self, char: str) -> None:
        """Set the mode indicator character ('P' for paper, 'R' for real)."""
        with self._lock:
            self._mode_char = char[0].upper()

    def stop_flashing(self) -> None:
        """Signal that the urgent flash has been acknowledged."""
        self._flashing_urgent = False

    # ── Display thread ────────────────────────────────────────

    def _loop(self) -> None:
        last_idle_refresh = 0.0

        while self._running:
            try:
                # Check for queued messages (non-blocking)
                try:
                    msg = self._queue.get_nowait()
                    if msg.priority == MessagePriority.URGENT and msg.flash:
                        self._do_flash(msg.text)
                    elif msg.static:
                        self._write_raw(msg.text[:8].ljust(8))
                    else:
                        self._do_scroll(msg.text)                    
                    continue
                except queue.Empty:
                    pass

                # Idle: refresh P&L display periodically
                now = time.monotonic()
                if now - last_idle_refresh >= IDLE_REFRESH:
                    self._show_idle()
                    last_idle_refresh = now

                time.sleep(0.1)

            except Exception:
                logger.exception("DisplayManager: error in display loop")
                time.sleep(1)

    def show_text(self, text: str) -> None:
        """Write a static message directly to hardware, bypassing the queue."""
        full = text[:8].upper().ljust(8)
        self._write_raw(full)
        self._notify_led(full)

    def _do_scroll(self, text: str) -> None:
        """Scroll text across all 8 digits, left to right."""
        text = text.upper()
        # Notify LED once with the full message at the start of the scroll
        self._notify_led(text)

        padded = "        " + text + "        "
        for i in range(len(padded) - 7):
            if not self._running:
                break
            self._write_raw(padded[i:i + 8])
            time.sleep(SCROLL_DELAY)

        # LED goes off when scroll finishes — idle screen will follow shortly
        # and _show_idle will notify correctly
        self._notify_led("        ")

    def _do_flash(self, text: str) -> None:
        """Flash text on and off until stop_flashing() is called."""
        text = text[:8].upper().center(8)
        # Notify LED once with the message being flashed
        self._notify_led(text)
        self._flashing_urgent = True
        while self._flashing_urgent and self._running:
            self._write_raw(text)
            time.sleep(FLASH_INTERVAL)
            self._write_raw("        ")
            time.sleep(FLASH_INTERVAL)
        # Notify LED that flashing ended
        self._notify_led("        ")

    def _show_idle(self) -> None:
        """Show mode char and P&L on the display."""
        with self._lock:
            pnl  = self._current_pnl
            mode = self._mode_char

        sign      = "+" if pnl >= 0 else "-"
        amount    = f"{abs(pnl):.1f}"
        text      = f"{mode} {sign}{amount}"
        formatted = text[:8].rjust(8)
        self._write_raw(formatted)
        self._notify_led(formatted)