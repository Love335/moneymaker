"""
encoder.py — Rotary encoder reader for the risk dial.

Reads the RGB rotary encoder and updates the risk level in state.
The encoder's built-in RGB LED reflects the current risk level:
  Green → low risk
  Yellow → medium risk
  Red → high risk
"""

import logging
import threading
import time

import RPi.GPIO as GPIO

from core.state import StateManager

logger = logging.getLogger(__name__)

# GPIO pins for encoder (BCM numbering)
PIN_CLK = 23   # Physical pin 16
PIN_DT  = 24   # Physical pin 18
PIN_SW  = 25   # Physical pin 22 (encoder pushbutton — unused for now)

# Encoder RGB LED pins (on the encoder itself, not the WS2812D)
PIN_ENC_RED   = 17   # Physical pin 11
PIN_ENC_GREEN = 27   # Physical pin 13
PIN_ENC_BLUE  = 22   # Physical pin 15

# Number of encoder steps for full range (0.0 to 1.0)
STEPS_FULL_RANGE = 24   # 24 pulses per revolution


class EncoderManager:
    """
    Reads the rotary encoder and maps position to risk level 0.0–1.0.
    Updates StateManager directly (risk level is not event-driven
    as it changes continuously and doesn't need to trigger actions).
    """

    def __init__(self, state: StateManager) -> None:
        self._state    = state
        self._position = 12        # start at middle (0.5 risk)
        self._clk_last = None
        self._running  = False
        self._thread   = None
        self._lock     = threading.Lock()

    def start(self) -> None:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(PIN_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(PIN_DT,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(PIN_SW,  GPIO.IN, pull_up_down=GPIO.PUD_UP)

        # Encoder LED pins as outputs
        for pin in [PIN_ENC_RED, PIN_ENC_GREEN, PIN_ENC_BLUE]:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW)

        self._clk_last = GPIO.input(PIN_CLK)
        self._apply_risk()

        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            name="EncoderManager",
            daemon=True
        )
        self._thread.start()
        logger.info("EncoderManager started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        # Turn off encoder LED
        for pin in [PIN_ENC_RED, PIN_ENC_GREEN, PIN_ENC_BLUE]:
            try:
                GPIO.output(pin, GPIO.LOW)
            except Exception:
                pass
        logger.info("EncoderManager stopped")

    # ── Poll loop ─────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                clk_state = GPIO.input(PIN_CLK)
                dt_state  = GPIO.input(PIN_DT)

                if clk_state != self._clk_last:
                    # Rising edge detected — determine direction
                    if dt_state != clk_state:
                        # Clockwise → increase risk
                        with self._lock:
                            self._position = min(
                                self._position + 1, STEPS_FULL_RANGE
                            )
                    else:
                        # Counter-clockwise → decrease risk
                        with self._lock:
                            self._position = max(self._position - 1, 0)

                    self._apply_risk()

                self._clk_last = clk_state
                time.sleep(0.001)   # 1ms poll for smooth response

            except Exception:
                logger.exception("EncoderManager: error in poll loop")
                time.sleep(0.1)

    def _apply_risk(self) -> None:
        """Update state and encoder LED based on current position."""
        with self._lock:
            risk = round(self._position / STEPS_FULL_RANGE, 2)

        try:
            self._state.set_risk_level(risk)
        except Exception as exc:
            logger.error("EncoderManager: failed to set risk level: %s", exc)
            return

        # Update encoder LED colour
        self._set_encoder_led(risk)
        logger.debug("Risk level: %.2f", risk)

    def _set_encoder_led(self, risk: float) -> None:
        """
        Set encoder LED colour:
          0.0–0.33 → green (low risk)
          0.34–0.66 → yellow (medium risk)
          0.67–1.0 → red (high risk)
        """
        try:
            if risk < 0.34:
                GPIO.output(PIN_ENC_RED,   GPIO.LOW)
                GPIO.output(PIN_ENC_GREEN, GPIO.HIGH)
                GPIO.output(PIN_ENC_BLUE,  GPIO.LOW)
            elif risk < 0.67:
                GPIO.output(PIN_ENC_RED,   GPIO.HIGH)
                GPIO.output(PIN_ENC_GREEN, GPIO.HIGH)
                GPIO.output(PIN_ENC_BLUE,  GPIO.LOW)
            else:
                GPIO.output(PIN_ENC_RED,   GPIO.HIGH)
                GPIO.output(PIN_ENC_GREEN, GPIO.LOW)
                GPIO.output(PIN_ENC_BLUE,  GPIO.LOW)
        except Exception as exc:
            logger.error("EncoderManager: LED error: %s", exc)