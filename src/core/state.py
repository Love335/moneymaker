"""
state.py — Single source of truth for all runtime application state.

All state mutations go through defined methods that validate the
transition is legal before applying it. No component mutates state
directly. Thread-safe via a reentrant lock.
"""

import threading
import logging
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


class TradingMode(Enum):
    PAPER = "PAPER"
    REAL  = "REAL"


class SystemStatus(Enum):
    STARTING          = auto()
    RUNNING           = auto()
    MARKET_CLOSED     = auto()
    MENU_OPEN         = auto()
    AWAITING_RECOVERY = auto()
    SHUTTING_DOWN     = auto()
    SUSPENDED         = auto()

class ConnectivityStatus(Enum):
    CONNECTED    = auto()
    DISCONNECTED = auto()
    DEGRADED     = auto()   # Connected but API returning bad data


@dataclass
class AppState:
    """
    All mutable runtime state in one place.

    Only StateManager should modify these fields.
    External components should call StateManager methods,
    not access this object directly.
    """
    trading_mode:        TradingMode         = TradingMode.PAPER
    system_status:       SystemStatus        = SystemStatus.STARTING
    connectivity:        ConnectivityStatus  = ConnectivityStatus.DISCONNECTED
    active_algorithm:    str                 = "dual_momentum"
    risk_level:          float               = 0.5     # 0.0 = lowest, 1.0 = highest
    current_pnl:         float               = 0.0     # total all-time P&L in SEK
    paper_balance:       float               = 0.0     # starting balance for paper mode
    market_is_open:      bool                = False
    pending_urgent_msgs: list                = field(default_factory=list)
    last_crash_reason:   Optional[str]       = None
    crash_is_recoverable: bool               = True


class StateManager:
    """
    Controls all transitions to AppState.

    Validates that transitions are legal and logs every change.
    Uses a reentrant lock so methods can safely call each other.
    """

    VALID_ALGORITHMS = {"dual_momentum", "mean_reversion", "trend_following"}

    def __init__(self) -> None:
        self._state = AppState()
        self._lock  = threading.RLock()

    # ── Read access ───────────────────────────────────────────

    @property
    def state(self) -> AppState:
        """Read-only snapshot. Do not mutate the returned object."""
        return self._state

    def snapshot(self) -> AppState:
        """Return a shallow copy for safe reading outside the lock."""
        with self._lock:
            from copy import copy
            return copy(self._state)

    # ── System status ─────────────────────────────────────────

    def set_status(self, status: SystemStatus) -> None:
        with self._lock:
            old = self._state.system_status
            self._state.system_status = status
            
            if old != status:
                logger.info("System status: %s → %s", old.name, status.name)

    # ── Trading mode ──────────────────────────────────────────

    def switch_trading_mode(self) -> TradingMode:
        """Toggle between PAPER and REAL. Returns the new mode."""
        with self._lock:
            if self._state.system_status == SystemStatus.SHUTTING_DOWN:
                raise StateError("Cannot switch mode during shutdown")

            if self._state.trading_mode == TradingMode.PAPER:
                self._state.trading_mode = TradingMode.REAL
            else:
                self._state.trading_mode = TradingMode.PAPER

            new_mode = self._state.trading_mode
            logger.info("Trading mode switched to %s", new_mode.name)
            return new_mode

    # ── Algorithm ─────────────────────────────────────────────

    def switch_algorithm(self, algorithm_name: str) -> None:
        with self._lock:
            if algorithm_name not in self.VALID_ALGORITHMS:
                raise StateError(
                    f"Unknown algorithm '{algorithm_name}'. "
                    f"Valid options: {self.VALID_ALGORITHMS}"
                )
            old = self._state.active_algorithm
            self._state.active_algorithm = algorithm_name
            logger.info("Algorithm switched: %s → %s", old, algorithm_name)

    def next_algorithm(self) -> str:
        """Cycle to the next algorithm and return its name."""
        with self._lock:
            algorithms = sorted(self.VALID_ALGORITHMS)
            current_idx = algorithms.index(self._state.active_algorithm)
            next_idx = (current_idx + 1) % len(algorithms)
            next_algo = algorithms[next_idx]
            self.switch_algorithm(next_algo)
            return next_algo

    # ── Risk level ────────────────────────────────────────────

    def set_risk_level(self, level: float) -> None:
        """Set risk level. Must be between 0.0 and 1.0."""
        with self._lock:
            if not 0.0 <= level <= 1.0:
                raise StateError(
                    f"Risk level must be between 0.0 and 1.0, got {level}"
                )
            self._state.risk_level = round(level, 2)
            logger.debug("Risk level set to %.2f", self._state.risk_level)

    # ── P&L ───────────────────────────────────────────────────

    def update_pnl(self, pnl: float) -> None:
        with self._lock:
            self._state.current_pnl = round(pnl, 2)
            logger.debug("P&L updated to %.2f SEK", self._state.current_pnl)

    # ── Market status ─────────────────────────────────────────

    def set_market_open(self, is_open: bool) -> None:
        with self._lock:
            self._state.market_is_open = is_open
            logger.info("Market is now %s", "OPEN" if is_open else "CLOSED")

    # ── Connectivity ──────────────────────────────────────────

    def set_connectivity(self, status: ConnectivityStatus) -> None:
        with self._lock:
            old = self._state.connectivity
            self._state.connectivity = status
            logger.info(
                "Connectivity: %s → %s", old.name, status.name
            )

    # ── Paper trading ─────────────────────────────────────────

    def set_paper_balance(self, balance: float) -> None:
        with self._lock:
            if balance <= 0:
                raise StateError(
                    f"Paper balance must be positive, got {balance}"
                )
            self._state.paper_balance = balance
            logger.info("Paper balance set to %.2f SEK", balance)

    # ── Crash handling ────────────────────────────────────────

    def record_crash(self, reason: str, recoverable: bool) -> None:
        with self._lock:
            self._state.last_crash_reason   = reason
            self._state.crash_is_recoverable = recoverable
            self._state.system_status       = SystemStatus.AWAITING_RECOVERY
            logger.critical(
                "Crash recorded. Recoverable: %s. Reason: %s",
                recoverable, reason
            )

    def clear_crash(self) -> None:
        with self._lock:
            self._state.last_crash_reason    = None
            self._state.crash_is_recoverable = True
            logger.info("Crash state cleared")

    # ── Urgent messages ───────────────────────────────────────

    def add_urgent_message(self, message: str) -> None:
        with self._lock:
            self._state.pending_urgent_msgs.append(message)
            logger.warning("Urgent message queued: %s", message)

    def pop_urgent_message(self) -> Optional[str]:
        with self._lock:
            if self._state.pending_urgent_msgs:
                return self._state.pending_urgent_msgs.pop(0)
            return None

    def has_urgent_messages(self) -> bool:
        with self._lock:
            return len(self._state.pending_urgent_msgs) > 0


class StateError(Exception):
    """Raised when an illegal state transition is attempted."""
    pass