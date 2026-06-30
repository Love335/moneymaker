"""
main.py — Entry point for the moneymaker trading bot.

Wires all components together and starts the engine.
No business logic lives here — only initialisation and wiring.
"""

import logging
import sys
import time
from pathlib import Path

# ── Path and logging must be set up before any project imports ─
sys.path.insert(0, str(Path(__file__).parent))
from applogging.logger import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from core.events import EventBus
from core.state import StateManager, TradingMode
from core.engine import Engine
from data.settings import Settings
from hardware.display import DisplayManager
from hardware.led import LEDManager
from trading.paper_broker import PaperBroker, PORTFOLIO_FILE
from ui.paper_setup import PaperBalanceSelector
from security.secrets import validate_config_file_permissions


def main() -> None:
    logger.info("=" * 60)
    logger.info("moneymaker starting up")
    logger.info("=" * 60)

    validate_config_file_permissions()

    # ── Load persistent settings ──────────────────────────────
    # Settings are read here so hardware starts at the right
    # brightness on the very first frame, before the engine runs.
    settings = Settings()

    # ── Core infrastructure ───────────────────────────────────
    bus     = EventBus()
    state   = StateManager()
    display = DisplayManager()
    led     = LEDManager()

    bus.start()
    display.start(brightness=settings.get("display_brightness"))
    led.start(brightness=settings.get("led_brightness"))
    display.set_led_callback(led.on_display_update)

    try:
        # ── Paper portfolio setup ─────────────────────────────
        # Always create a PaperBroker — it loads the existing portfolio
        # if one exists, or waits for the user to set a balance if fresh.
        is_fresh = not PORTFOLIO_FILE.exists()

        if is_fresh:
            selector     = PaperBalanceSelector(display, led, bus)
            balance      = selector.run()
            state.set_paper_balance(balance)
            paper_broker = PaperBroker(starting_balance=balance)
            logger.info("Paper balance selected: %.2f SEK", balance)
        else:
            paper_broker = PaperBroker()
            logger.info("Existing paper portfolio found — resuming")

        # ── Determine starting mode and broker ────────────────
        # Check whether the user was in real mode when the bot last ran.
        # If so, attempt to reconnect to Avanza. Fall back to paper safely
        # if authentication fails — better to be visibly wrong than silently
        # live with a broken real broker.
        saved_mode = settings.get("trading_mode")
        saved_algo = settings.get("active_algorithm")

        if saved_mode == TradingMode.REAL.value:
            logger.info("Saved mode is REAL — attempting to reconnect to Avanza")
            try:
                from trading.avanza_broker import AvanzaBroker
                from security.secrets import load_avanza_credentials
                creds  = load_avanza_credentials()
                broker = AvanzaBroker(
                    username=creds.username,
                    password=creds.password,
                    totp_secret=creds.totp_secret,
                    account_id=creds.account_id,
                )
                # Advance StateManager from PAPER to REAL so it reflects
                # the actual mode we're starting in
                state.switch_trading_mode()
                display.set_mode_char("R")
                logger.info("Resuming in REAL mode")
            except Exception:
                logger.exception(
                    "Failed to reconnect to Avanza after restart — "
                    "falling back to PAPER for safety"
                )
                settings.set("trading_mode", TradingMode.PAPER.value)
                broker = paper_broker
                display.set_mode_char("P")
        else:
            broker = paper_broker
            display.set_mode_char("P")

        # ── Engine ────────────────────────────────────────────
        engine = Engine(
            display=display,
            led=led,
            state=state,
            bus=bus,
        )

        # engine.start() blocks until shutdown is requested
        engine.start(
            broker=broker,
            starting_algo=saved_algo,
        )

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — shutting down")

    except Exception:
        logger.exception("Fatal error in main()")
        display.show_text("FATAL ER")
        time.sleep(2)
        raise

    finally:
        logger.info("Cleaning up")
        bus.stop()
        display.stop()
        led.stop()
        logger.info("moneymaker shutdown complete")
        logger.info("=" * 60)


if __name__ == "__main__":
    main()