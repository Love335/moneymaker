"""
power.py — Power management for the moneymaker trading bot.

The physical power switch is now handled entirely at the kernel level
via dtoverlay=gpio-shutdown in /boot/config.txt. The switch is wired
to GPIO 3 (Pin 5) and GND (Pin 6).

Switch behaviour:
  - Pi is running: flip switch → kernel triggers graceful shutdown
  - Pi is halted:  flip switch → GPIO 3 pulse restarts the Pi

This class only provides the initiate_shutdown() method, which the
engine can call programmatically when needed (e.g. from a menu option).
No GPIO monitoring is done here — the kernel handles it.
"""

import logging
import subprocess

from core.events import EventBus

logger = logging.getLogger(__name__)


class PowerManager:
    """
    Provides programmatic shutdown capability.

    The physical switch on GPIO 3 is handled by the kernel overlay
    and does not require any Python code to monitor.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus                = bus
        self._shutdown_initiated = False

    def start(self) -> None:
        """
        Nothing to start — the kernel overlay handles the switch.
        """
        logger.info(
            "PowerManager started — "
            "physical switch handled by dtoverlay=gpio-shutdown on GPIO 3"
        )

    def stop(self) -> None:
        logger.info("PowerManager stopped")

    def initiate_shutdown(self) -> None:
        """
        Trigger a graceful OS shutdown programmatically.
        Called by the engine after cleanup is complete,
        or can be triggered from the menu in future.
        """
        if self._shutdown_initiated:
            return
        self._shutdown_initiated = True
        logger.info("PowerManager: initiating system shutdown")
        try:
            subprocess.run(["sudo", "shutdown", "now"], check=True)
        except Exception as exc:
            logger.error("PowerManager: shutdown command failed: %s", exc)