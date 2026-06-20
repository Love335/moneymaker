"""
main.py — Entry point for the moneymaker trading bot.

Wires all components together and starts the engine.
No business logic lives here — only initialisation and wiring.
"""

import logging
import sys
from pathlib import Path
from trading.paper_broker import PORTFOLIO_FILE

# ── Logging must be set up before any other import ────────────
sys.path.insert(0, str(Path(__file__).parent))

from applogging.logger import setup_logging
setup_logging()

logger = logging.getLogger(__name__)

from core.events import EventBus
from core.state import StateManager, TradingMode
from core.engine import Engine
from hardware.display import DisplayManager
from hardware.led import LEDManager
from trading.paper_broker import PaperBroker
from ui.paper_setup import PaperBalanceSelector
from security.secrets import validate_config_file_permissions


def main() -> None:
    logger.info("=" * 60)
    logger.info("moneymaker starting up")
    logger.info("=" * 60)

    # Security check
    validate_config_file_permissions()

    # ── Core infrastructure ───────────────────────────────────
    bus     = EventBus()
    state   = StateManager()
    display = DisplayManager()
    led     = LEDManager()

    bus.start()
    display.start()
    led.start()
    display.set_led_callback(led.on_display_update) z

    try:
        # ── Paper trading setup ───────────────────────────────
        # Check if this is a fresh paper portfolio
        paper_broker = PaperBroker()

        is_fresh = not PORTFOLIO_FILE.exists()

        if is_fresh:
            selector = PaperBalanceSelector(display, led, bus)
            balance  = selector.run()
            state.set_paper_balance(balance)
            paper_broker = PaperBroker(starting_balance=balance)
            logger.info("Paper balance selected: %.2f SEK", balance)
        else:
            paper_broker = PaperBroker()
            logger.info("Existing paper portfolio found — resuming")
        # Set initial display mode character
        display.set_mode_char("P")

        # ── Engine ────────────────────────────────────────────
        engine = Engine(
            display=display,
            led=led,
            state=state,
            bus=bus,
        )

        # Engine.start() blocks until shutdown
        engine.start(
            broker=paper_broker,
            starting_algo="dual_momentum",
        )

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — shutting down")

    except Exception:
        logger.exception("Fatal error in main()")
        display.flash_message("FATAL ERR")
        time.sleep(2)
        raise

    finally:
        logger.info("Cleaning up")
        bus.stop()
        display.stop()
        led.stop()
        logger.info("moneymaker shutdown complete")

if __name__ == "__main__":
    main()