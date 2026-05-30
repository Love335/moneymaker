"""
power.py — Power switch monitor and graceful shutdown.

Monitors the on/off switch on a high-priority thread.
When the switch is flipped off, emits SHUTDOWN_REQUESTED
and initiates a graceful system shutdown.
"""

import logging
import os
import subprocess
import threading
import time

import RPi.GPIO as GPIO

from core.events import EventBus, EventType, Event

logger = logging.getLogger(__name__)

# GPIO pin for the on/off switch (BCM numbering)
PIN_POWER_SWITCH = 26   # Physical pin 37

POLL_INTERVAL = 0.1   # 100ms — fast response to switch


class PowerManager:
    """
    Monitors the power switch and triggers graceful shutdown.

    The switch is wired normally-open (NO):
    - Switch ON  → pin is pulled HIGH (via internal pull-up, no connection)
    - Switch OFF → pin is pulled LOW  (switch connects to GND)

    Adjust PIN_ACTIVE_STATE below if your switch is wired differently.
    """

    # What GPIO level means "switch is in OFF position"
    PIN_ACTIVE_STATE = GPIO.LOW

    def __init__(self, bus: EventBus) -> None:
        self._bus          = bus
        self._running      = False
        self._thread       = None
        self._shutdown_initiated = False

    def start(self) -> None:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(PIN_POWER_SWITCH, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        logger.debug("PowerManager: configured GPIO %d", PIN_POWER_SWITCH)

        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            name="PowerManager",
            daemon=False   # NOT a daemon — must complete shutdown cleanly
        )
        self._thread.start()
        logger.info("PowerManager started on GPIO %d", PIN_POWER_SWITCH)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("PowerManager stopped")

    def initiate_shutdown(self) -> None:
        """
        Called by the engine to perform the actual system shutdown
        after all cleanup is complete.
        """
        if self._shutdown_initiated:
            return
        self._shutdown_initiated = True
        logger.info("PowerManager: initiating system shutdown")
        try:
            subprocess.run(["sudo", "shutdown", "now"], check=True)
        except Exception as exc:
            logger.error("PowerManager: shutdown command failed: %s", exc)

    # ── Poll loop ─────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                state = GPIO.input(PIN_POWER_SWITCH)
                if state == self.PIN_ACTIVE_STATE and not self._shutdown_initiated:
                    logger.warning(
                        "PowerManager: switch set to OFF — requesting shutdown"
                    )
                    self._bus.publish(Event(
                        type=EventType.SHUTDOWN_REQUESTED,
                        source="PowerManager",
                        payload={"reason": "power_switch"}
                    ))
                    # Wait briefly before rechecking — avoids spamming events
                    time.sleep(5)
                else:
                    time.sleep(POLL_INTERVAL)

            except Exception:
                logger.exception("PowerManager: error in poll loop")
                time.sleep(1)