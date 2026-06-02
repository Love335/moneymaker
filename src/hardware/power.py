"""
power.py — Power switch monitor with suspend/resume support.

Switch ON  → bot runs normally
Switch OFF → bot suspends (deep sleep, all connections closed)
Switch ON  → bot resumes automatically

This avoids true shutdown since the Pi cannot wake itself after
a full poweroff without a hardware power switch on the supply.
"""

import logging
import subprocess
import threading
import time

import RPi.GPIO as GPIO

from core.events import EventBus, EventType, Event

logger = logging.getLogger(__name__)

PIN_POWER_SWITCH = 26
POLL_INTERVAL    = 0.1

class PowerManager:

    PIN_ACTIVE_STATE = GPIO.HIGH   # HIGH = switch is OFF

    def __init__(self, bus: EventBus) -> None:
        self._bus                 = bus
        self._running             = False
        self._thread              = None
        self._suspended           = False
        self._shutdown_initiated  = False

    def start(self) -> None:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(PIN_POWER_SWITCH, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            name="PowerManager",
            daemon=False
        )
        self._thread.start()
        logger.info("PowerManager started on GPIO %d", PIN_POWER_SWITCH)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("PowerManager stopped")

    def initiate_shutdown(self) -> None:
        """Called by engine for full OS shutdown (e.g. future hardware switch)."""
        if self._shutdown_initiated:
            return
        self._shutdown_initiated = True
        logger.info("PowerManager: initiating system shutdown")
        try:
            subprocess.run(["sudo", "shutdown", "now"], check=True)
        except Exception as exc:
            logger.error("PowerManager: shutdown command failed: %s", exc)

    def _loop(self) -> None:
        while self._running:
            try:
                state = GPIO.input(PIN_POWER_SWITCH)

                if state == self.PIN_ACTIVE_STATE and not self._suspended:
                    logger.info("PowerManager: switch OFF — suspending")
                    self._suspended = True
                    self._bus.publish(Event(
                        type=EventType.SHUTDOWN_REQUESTED,
                        source="PowerManager",
                        payload={"reason": "suspend"}
                    ))
                    time.sleep(3)

                elif state != self.PIN_ACTIVE_STATE and self._suspended:
                    logger.info("PowerManager: switch ON — resuming")
                    self._suspended = False
                    self._bus.publish(Event(
                        type=EventType.STARTUP_COMPLETE,
                        source="PowerManager",
                        payload={"reason": "resume"}
                    ))

                time.sleep(POLL_INTERVAL)

            except RuntimeError as exc:
                if "pin numbering" in str(exc):
                    # GPIO was cleaned up by another component — reinitialise
                    try:
                        GPIO.setmode(GPIO.BCM)
                        GPIO.setup(PIN_POWER_SWITCH, GPIO.IN,
                                pull_up_down=GPIO.PUD_UP)
                        logger.debug("PowerManager: GPIO reinitialised after cleanup")
                    except Exception:
                        pass
                time.sleep(0.5)
            except Exception:
                logger.exception("PowerManager: error in poll loop")
                time.sleep(1)