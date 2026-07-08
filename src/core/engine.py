"""
engine.py — Central orchestrator for the moneymaker trading bot.

Wires all subsystems together and coordinates responses to events.
Runs the main trading loop, handles crash recovery, and manages
graceful shutdown.
"""

import logging
import time
import threading
import signal
from typing import Dict, Optional

from algorithms.base import BaseAlgorithm, MarketSnapshot, TradeAction
from algorithms.dual_momentum import DualMomentumAlgorithm
from algorithms.mean_reversion import MeanReversionAlgorithm
from algorithms.trend_following import TrendFollowingAlgorithm
from core.events import EventBus, EventType, Event
from core.scheduler import MarketScheduler
from core.state import (
    AppState, ConnectivityStatus, StateManager,
    SystemStatus, TradingMode
)
from data.market_data import MarketDataService, MarketDataError
from data.settings import Settings
from hardware.display import DisplayManager
from hardware.led import LEDManager
from hardware.buttons import ButtonManager
from hardware.power import PowerManager
from applogging.logger import classify_crash, log_trade, setup_trade_logger
from trading.broker import BaseBroker, BrokerError, OrderStatus
from trading.paper_broker import PaperBroker
from ui.menu import MenuManager

logger = logging.getLogger(__name__)


ALGORITHM_REGISTRY: Dict[str, type] = {
    "dual_momentum":   DualMomentumAlgorithm,
    "mean_reversion":  MeanReversionAlgorithm,
    "trend_following": TrendFollowingAlgorithm,
}

RECOVERABLE_RESTART_DELAY = 30
MAX_AUTO_RECOVERIES       = 3
MARKET_CLOSED_SLEEP       = 60


class Engine:
    """
    The central orchestrator.

    Owns the event bus, state manager, and all subsystems.
    Coordinates the full lifecycle from startup through shutdown.
    """

    def __init__(
        self,
        display:  DisplayManager,
        led:      LEDManager,
        state:    StateManager,
        bus:      EventBus,
        settings: Optional[Settings] = None,
    ) -> None:
        self._display      = display
        self._led          = led
        self._state        = state
        self._bus          = bus
        self._settings     = settings
        self._trade_logger = setup_trade_logger()
        self._running      = False
        self._shutdown_event          = threading.Event()
        self._recovery_confirmed      = threading.Event()
        self._force_evaluation_event  = threading.Event()
        self._consecutive_recoveries  = 0
        self._lock                    = threading.Lock()
        self._last_heartbeat: float   = time.monotonic()
        self._heartbeat_interval: int = 300

        self._broker:      Optional[BaseBroker]        = None
        self._algorithm:   Optional[BaseAlgorithm]     = None
        self._market_data: Optional[MarketDataService] = None
        self._scheduler:   Optional[MarketScheduler]   = None
        self._buttons:     Optional[ButtonManager]     = None
        self._power:       Optional[PowerManager]      = None
        self._menu:        Optional[MenuManager]       = None

        self._subscribe_all()

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self, broker, starting_algo="dual_momentum"):
        import config

        logger.info("Engine starting")
        self._display.show_message("STARTING")

        try:
            self._broker      = broker
            self._market_data = MarketDataService(self._bus)
            self._scheduler   = MarketScheduler(self._bus)
            self._buttons     = ButtonManager(self._bus)
            self._power       = PowerManager(self._bus)

            if self._settings is None:
                self._settings = Settings()
            self._display.set_brightness(self._settings.get("display_brightness"))
            self._led.set_brightness(self._settings.get("led_brightness"))

            self._menu = MenuManager(
                self._bus, self._state, self._display, self._led,
                settings=self._settings,
            )

            self._load_algorithm(starting_algo)
            self._state.set_risk_level(config.RISK_LEVEL)
            logger.info("Risk level set to %.2f from config", config.RISK_LEVEL)

            self._buttons.start()
            self._power.start()
            self._scheduler.start()

            self._state.set_status(SystemStatus.RUNNING)
            self._display.show_message("READY")

            logger.info("Engine startup complete")
            self._bus.publish(Event(
                type=EventType.STARTUP_COMPLETE,
                source="Engine"
            ))

            self._setup_os_signal_handlers()
            self._main_loop()

        except Exception as exc:
            reason, recoverable = classify_crash(exc)
            logger.critical("Engine failed to start: %s", reason)
            self._display.show_message("ERR START")
            self._perform_shutdown()
            raise

    def _setup_os_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_os_signal)
        signal.signal(signal.SIGINT,  self._handle_os_signal)

    def _handle_os_signal(self, signum, frame) -> None:
        logger.info(
            "OS signal %d received — intercepting for clean hardware termination.",
            signum
        )
        self._shutdown_event.set()

    def _main_loop(self) -> None:
        self._running    = True
        last_evaluation: float = float('-inf')

        while self._running:
            try:
                snap = self._state.snapshot()

                if self._shutdown_event.is_set():
                    self._perform_shutdown()
                    break

                now = time.monotonic()

                if self._force_evaluation_event.is_set():
                    self._force_evaluation_event.clear()
                    last_evaluation = float('-inf')

                if snap.system_status == SystemStatus.AWAITING_RECOVERY:
                    self._display.flash_message("CONFIRM?")
                    self._recovery_confirmed.wait(timeout=5)
                    if self._recovery_confirmed.is_set():
                        self._recovery_confirmed.clear()
                        self._state.clear_crash()
                        self._state.set_status(SystemStatus.RUNNING)
                        self._display.show_message("RESUMED")
                        self._last_heartbeat         = time.monotonic()
                        self._consecutive_recoveries = 0
                    continue

                if snap.system_status == SystemStatus.RUNNING and (
                    now - self._last_heartbeat
                ) >= self._heartbeat_interval:
                    logger.info("Heartbeat: Engine is healthy and running")
                    self._last_heartbeat = now

                if not snap.market_is_open:
                    self._state.set_status(SystemStatus.MARKET_CLOSED)
                    if self._shutdown_event.wait(timeout=MARKET_CLOSED_SLEEP):
                        continue
                    continue

                self._state.set_status(SystemStatus.RUNNING)
                algo     = self._algorithm
                interval = algo.evaluation_interval_seconds if algo else 3600

                if now - last_evaluation >= interval:
                    success = self._run_evaluation()
                    last_evaluation = now
                    if success:
                        self._consecutive_recoveries = 0

                if self._shutdown_event.wait(timeout=1):
                    continue

            except Exception as exc:
                self._handle_crash(exc)

    def _run_evaluation(self) -> bool:
        if not self._algorithm or not self._broker:
            return False

        snap = self._state.snapshot()
        logger.info(
            "Running evaluation: algo=%s (loaded=%s) mode=%s risk=%.2f",
            snap.active_algorithm,
            self._algorithm.__class__.__name__,
            snap.trading_mode.value,
            snap.risk_level,
        )
        self._display.show_message("EVALUATING")

        try:
            if not self._broker.is_connected():
                raise BrokerError("Broker not connected")

            overview = self._broker.get_account_overview()
            tickers  = self._get_required_tickers()
            prices   = self._market_data.get_prices_bulk(tickers)
            history  = self._market_data.get_history_bulk(tickers)

            for ticker, price in prices.items():
                if hasattr(self._broker, 'update_price'):
                    self._broker.update_price(ticker, price)

            if not prices:
                logger.warning("Evaluation: no price data available, skipping")
                self._display.show_message("NO DATA")
                return False

            market_snap = MarketSnapshot(
                prices=prices,
                history=history,
                liquid_sek=overview.liquid_sek,
                risk_level=snap.risk_level,
                total_value_sek=overview.total_value_sek,
            )

            signals = self._algorithm.evaluate(market_snap)

            if not signals:
                logger.info("Evaluation: no trade signals generated")
                self._display.show_message("NO SIGNAL")
            else:
                sell_signals = [s for s in signals if s.action == TradeAction.SELL]
                buy_signals  = [s for s in signals if s.action == TradeAction.BUY]

                for sig in sell_signals:
                    self._execute_signal(sig, 0.0, snap)

                # Re-fetch account state so buy sizing uses post-sell balance
                if sell_signals:
                    try:
                        overview = self._broker.get_account_overview()
                        logger.info(
                            "Evaluation: post-sell liquid balance: %.2f SEK",
                            overview.liquid_sek
                        )
                    except Exception as exc:
                        logger.error(
                            "Evaluation: failed to refresh overview "
                            "after sells: %s", exc
                        )

                # Guard: never buy a ticker we already hold. Protects
                # against tracking/reality divergence (the triple-BOL bug).
                # Real broker may report no positions if get_positions()
                # is unavailable — then this guard is a no-op.
                held_tickers = {p.ticker for p in overview.positions}

                # Track remaining cash locally so several buys in one
                # evaluation can never oversubscribe the balance.
                remaining_cash = overview.liquid_sek

                # Size positions against TOTAL account value (equity),
                # not remaining cash. A "15% position" means 15% of the
                # portfolio — cash only constrains what we can still buy.
                # This keeps position sizes consistent regardless of how
                # many positions are already open. Falls back to liquid
                # cash if total value is missing or zero.
                sizing_base = max(overview.total_value_sek, overview.liquid_sek)

                for sig in buy_signals:
                    if sig.ticker in held_tickers:
                        logger.warning(
                            "Evaluation: skipping BUY %s — position "
                            "already held according to broker", sig.ticker
                        )
                        continue

                    amount = round(sizing_base * sig.fraction, 2)
                    if amount > remaining_cash:
                        amount = round(remaining_cash, 2)
                    if amount < 10.0:
                        logger.info(
                            "Evaluation: skipping BUY %s — remaining "
                            "cash %.2f SEK too small", sig.ticker, remaining_cash
                        )
                        continue

                    result_total = self._execute_signal(sig, amount, snap)
                    remaining_cash -= result_total

            # Update P&L after all trades are complete
            try:
                updated_overview = self._broker.get_account_overview()
                pnl = self._calculate_pnl(updated_overview)
                self._state.update_pnl(pnl)
                self._display.update_pnl(pnl)
            except Exception as exc:
                logger.error(
                    "Evaluation: failed to update P&L after trading: %s", exc
                )

            return True

        except MarketDataError as exc:
            logger.error("Evaluation: market data error — %s", exc)
            self._display.show_message("DATA ERR")
            self._bus.publish(Event(
                type=EventType.API_ERROR,
                source="Engine",
                payload={"error": str(exc)}
            ))
            return False

        except BrokerError as exc:
            logger.error("Evaluation: broker error — %s", exc)
            self._display.show_message("BROKER ERR")
            self._bus.publish(Event(
                type=EventType.CONNECTION_LOST,
                source="Engine",
                payload={"error": str(exc)}
            ))
            return False

    def _execute_signal(
        self,
        sig,
        amount_sek: float,
        snap:       AppState,
    ) -> float:
        """
        Execute a single trade signal through the broker.
        For SELL signals amount_sek is ignored (broker sells the full
        position). Returns the SEK actually spent on a BUY (0.0 for
        sells, rejections, and errors) so the caller can track
        remaining cash across multiple buys.
        """
        action_str = sig.action.value
        logger.info(
            "Executing: %s %s (algo=%s confidence=%.2f)",
            action_str, sig.ticker,
            sig.algorithm, sig.confidence
        )

        if sig.action == TradeAction.SELL:
            self._display.show_message(f"SELL {sig.ticker[:5]}")
        else:
            self._display.show_message(
                f"{action_str} {sig.ticker[:5]} {amount_sek:.0f}SEK"
            )

        try:
            result = self._broker.place_order(
                ticker=sig.ticker,
                action=action_str,
                amount_sek=amount_sek,
            )

            success = result.status == OrderStatus.FILLED

            log_trade(
                trade_logger=self._trade_logger,
                action=action_str,
                ticker=sig.ticker,
                amount=result.total_sek,
                price=result.executed_price,
                mode=snap.trading_mode.value,
                algorithm=sig.algorithm,
                result=result.status.name,
                notes=sig.reason,
            )

            if success:
                self._display.show_message(
                    f"{action_str} OK {result.executed_price:.2f}"
                )
                self._bus.publish(Event(
                    type=EventType.TRADE_EXECUTED,
                    source="Engine",
                    payload={
                        "ticker": sig.ticker,
                        "action": action_str,
                        "amount": result.total_sek,
                        "price":  result.executed_price,
                    }
                ))
            else:
                self._display.show_message(f"ERR {sig.ticker[:4]}")
                self._bus.publish(Event(
                    type=EventType.TRADE_FAILED,
                    source="Engine",
                    payload={"reason": result.error_message}
                ))

            self._algorithm.on_trade_executed(sig, success)

            if success and sig.action == TradeAction.BUY:
                return result.total_sek
            return 0.0

        except BrokerError as exc:
            logger.error(
                "Trade execution error for %s: %s", sig.ticker, exc
            )
            self._display.show_message("EXEC ERR")
            return 0.0

    def _perform_shutdown(self) -> None:
        logger.info("Performing graceful hardware and subsystem shutdown...")
        self._running = False

        if self._scheduler:
            try: self._scheduler.stop()
            except Exception as e: logger.error("Error stopping scheduler: %s", e)
        if self._buttons:
            try: self._buttons.stop()
            except Exception as e: logger.error("Error stopping buttons: %s", e)
        if self._power:
            try: self._power.stop()
            except Exception as e: logger.error("Error stopping power manager: %s", e)

        if self._display:
            try:
                self._display.stop_flashing()
                if hasattr(self._display, 'stop'):
                    self._display.stop()
                self._display.show_text("GOODBYE")
                logger.info("DisplayManager: showing GOODBYE")
            except Exception as e:
                logger.error("Error writing goodbye to display: %s", e)

        if self._led:
            try:
                self._led.stop()
                logger.info("LEDManager cleared successfully.")
            except Exception as e:
                logger.error("Error clearing LEDs during shutdown: %s", e)

        logger.info("Graceful shutdown sequence complete.")

    # ── Event handlers ────────────────────────────────────────

    def _subscribe_all(self) -> None:
        self._bus.subscribe(EventType.MARKET_OPENED,         self._on_market_opened)
        self._bus.subscribe(EventType.MARKET_CLOSED,         self._on_market_closed)
        self._bus.subscribe(EventType.SHUTDOWN_REQUESTED,    self._on_shutdown_requested)
        self._bus.subscribe(EventType.MODE_SWITCHED,         self._on_mode_switched)
        self._bus.subscribe(EventType.ALGORITHM_SWITCHED,    self._on_algorithm_switched)
        self._bus.subscribe(EventType.PAPER_PORTFOLIO_RESET, self._on_paper_reset)
        self._bus.subscribe(EventType.CONNECTION_LOST,       self._on_connection_lost)
        self._bus.subscribe(EventType.API_ERROR,             self._on_api_error)
        self._bus.subscribe(EventType.BUTTON_YES_PRESSED,    self._on_yes_for_recovery)

    def _on_market_opened(self, event: Event) -> None:
        logger.info("Market opened")
        self._state.set_market_open(True)
        self._display.show_message("MKT OPEN")
        if self._algorithm:
            self._algorithm.on_market_opened()

    def _on_market_closed(self, event: Event) -> None:
        logger.info("Market closed")
        self._state.set_market_open(False)
        self._display.show_message("MKT CLOSE")

        # Cancel any unfilled limit orders. place_order() reports FILLED
        # optimistically when the limit order is accepted, but a limit at
        # the quoted price is not guaranteed to fill. Cancelling at close
        # bounds the exposure of any unfilled order to a single session
        # instead of letting it fill days later at a stale price.
        if self._broker:
            try:
                if self._broker.cancel_all_orders():
                    logger.info("Market close: open orders cancelled")
                else:
                    logger.warning(
                        "Market close: cancel_all_orders reported failure — "
                        "check Avanza manually for stale open orders"
                    )
            except Exception as exc:
                logger.error(
                    "Market close: error cancelling orders: %s", exc
                )

        if self._algorithm:
            self._algorithm.on_market_closed()

    def _on_shutdown_requested(self, event: Event) -> None:
        reason = event.payload.get("reason", "")
        logger.info("Shutdown requested: %s", reason)
        self._shutdown_event.set()

    def _on_mode_switched(self, event: Event) -> None:
        new_mode = self._state.switch_trading_mode()
        char     = "P" if new_mode == TradingMode.PAPER else "R"
        self._display.set_mode_char(char)
        self._display.show_message(f"MODE {new_mode.value}")
        logger.info("Mode switched to %s", new_mode.value)

        if new_mode == TradingMode.PAPER:
            snap = self._state.snapshot()
            self._broker = PaperBroker(snap.paper_balance or 10_000.0)
            self._state.update_pnl(self._broker.get_all_time_pnl())
            self._display.update_pnl(self._state.snapshot().current_pnl)
            if self._settings:
                self._settings.set("trading_mode", new_mode.value)

        else:
            try:
                from trading.avanza_broker import AvanzaBroker
                from security.secrets import load_avanza_credentials
                creds = load_avanza_credentials()
                self._broker = AvanzaBroker(
                    username=creds.username,
                    password=creds.password,
                    totp_secret=creds.totp_secret,
                    account_id=creds.account_id,
                )
                self._display.show_message("REAL OK")
                logger.info("Switched to real Avanza broker")
                if self._settings:
                    self._settings.set("trading_mode", new_mode.value)

            except Exception as exc:
                logger.error("Failed to init real broker: %s", exc)
                self._display.show_message("AUTH ERR")
                self._state.switch_trading_mode()
                self._display.set_mode_char("P")
                if self._settings:
                    self._settings.set("trading_mode", TradingMode.PAPER.value)
                return

        self._force_evaluation_event.set()

    def _on_algorithm_switched(self, event: Event) -> None:
        new_algo = self._state.next_algorithm()
        self._load_algorithm(new_algo)
        self._display.show_message(f"ALGO {new_algo[:8].upper()}")
        logger.info("Algorithm switched to %s", new_algo)
        if self._settings:
            self._settings.set("active_algorithm", new_algo)
        self._force_evaluation_event.set()

    def _on_paper_reset(self, event: Event) -> None:
        if isinstance(self._broker, PaperBroker):
            snap = self._state.snapshot()
            self._broker.reset(snap.paper_balance or 10_000.0)
            self._state.update_pnl(0.0)
            self._display.update_pnl(0.0)
            self._display.show_message("RESET OK")
            logger.info("Paper portfolio reset")

    def _on_connection_lost(self, event: Event) -> None:
        logger.warning("Connection lost: %s", event.payload.get("error"))
        self._state.set_connectivity(ConnectivityStatus.DISCONNECTED)
        self._display.show_message("NO CONNECT")

    def _on_api_error(self, event: Event) -> None:
        logger.error("API error: %s", event.payload.get("error"))
        self._state.set_connectivity(ConnectivityStatus.DEGRADED)
        self._display.show_message("API ERROR")

    def _on_yes_for_recovery(self, event: Event) -> None:
        logger.info("YES pressed — setting recovery confirmation")
        self._recovery_confirmed.set()
        self._display.stop_flashing()

    # ── Crash handling ────────────────────────────────────────

    def _handle_crash(self, exc: BaseException) -> None:
        reason, recoverable = classify_crash(exc)
        logger.critical("Crash in main loop: %s", reason)
        self._state.record_crash(reason, recoverable)

        if recoverable and self._consecutive_recoveries < MAX_AUTO_RECOVERIES:
            self._consecutive_recoveries += 1
            logger.info(
                "Recoverable crash #%d — resuming in %ds",
                self._consecutive_recoveries, RECOVERABLE_RESTART_DELAY
            )
            self._display.show_message(
                f"ERR RETRY {self._consecutive_recoveries}"
            )
            self._shutdown_event.wait(timeout=RECOVERABLE_RESTART_DELAY)
            if not self._shutdown_event.is_set():
                self._state.clear_crash()
                self._state.set_status(SystemStatus.RUNNING)
        else:
            logger.critical(
                "Unrecoverable crash or too many retries — "
                "waiting for manual YES confirmation"
            )
            self._display.flash_message("PRESS YES")
            self._state.set_status(SystemStatus.AWAITING_RECOVERY)
            self._consecutive_recoveries = 0

    # ── Helpers ───────────────────────────────────────────────

    def _load_algorithm(self, name: str) -> None:
        cls = ALGORITHM_REGISTRY.get(name)
        if cls is None:
            logger.error("Unknown algorithm '%s', keeping current", name)
            return
        self._algorithm = cls()
        logger.info("Algorithm loaded: %s", name)

        try:
            self._state.switch_algorithm(name)
        except Exception as exc:
            logger.error(
                "Failed to sync StateManager algorithm to '%s': %s",
                name, exc
            )

    def _get_required_tickers(self) -> list:
        if self._algorithm is None:
            raise RuntimeError("No algorithm loaded")
        
        if self._algorithm and hasattr(self._algorithm, 'required_tickers'):
            return self._algorithm.required_tickers

        from algorithms.dual_momentum  import CANDIDATE_TICKERS as DM_TICKERS
        from algorithms.mean_reversion import UNIVERSE as MR_UNIVERSE
        from algorithms.trend_following import PRIMARY_TICKER, BOND_TICKER

        algo = self._algorithm.name

        if algo == "dual_momentum":
            return DM_TICKERS
        elif algo == "mean_reversion":
            return MR_UNIVERSE
        elif algo == "trend_following":
            return [PRIMARY_TICKER, BOND_TICKER]

        raise RuntimeError(
            f"Unknown algorithm '{algo}' — cannot determine required tickers. "
            f"Add it to _get_required_tickers() or implement required_tickers "
            f"on the algorithm class."
        )

    def _calculate_pnl(self, overview) -> float:
        if isinstance(self._broker, PaperBroker):
            return self._broker.get_all_time_pnl()

        baseline = self._settings.get("real_pnl_baseline") if self._settings else None

        if baseline is None:
            baseline = overview.total_value_sek
            if self._settings:
                self._settings.set("real_pnl_baseline", baseline)
            logger.info(
                "Real P&L baseline set: %.2f SEK — all-time P&L will be "
                "measured from this point", baseline
            )
            return 0.0

        return round(overview.total_value_sek - baseline, 2)