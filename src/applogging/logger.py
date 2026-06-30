"""
logger.py — Structured logging with rotation and crash classification.

Sets up two log streams:
  - app.log:   all application events, INFO and above, rotating
  - trades.log: every trade signal and execution, rotating

Crash classification determines whether the engine should
auto-recover or wait for manual confirmation.
"""

import logging
import logging.handlers
import traceback
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────

LOG_DIR   = Path(__file__).resolve().parents[2] / "data" / "logs"
APP_LOG   = LOG_DIR / "app.log"
TRADE_LOG = LOG_DIR / "trades.log"

# ── Log rotation settings ─────────────────────────────────────

MAX_BYTES    = 10 * 1024 * 1024   # 10 MB per file
BACKUP_COUNT = 5                   # keep last 5 rotated files


def setup_logging(level: int = logging.INFO) -> None:
    """
    Configure root logger with console and rotating file handlers.
    Call once at application startup before any other imports.
    Safe to call multiple times — subsequent calls are no-ops.
    """
    root = logging.getLogger()

    # Guard against duplicate handlers if called more than once
    # (e.g. during testing or after a soft restart)
    if root.handlers:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root.setLevel(logging.DEBUG)   # capture everything; handlers filter

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # ── Console handler ───────────────────────────────────────
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # ── Rotating app log ──────────────────────────────────────
    app_handler = logging.handlers.RotatingFileHandler(
        APP_LOG,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8"
    )
    app_handler.setLevel(logging.DEBUG)
    app_handler.setFormatter(formatter)
    root.addHandler(app_handler)

    logging.info("Logging initialised. Log dir: %s", LOG_DIR)


def setup_trade_logger() -> logging.Logger:
    """
    Return a dedicated logger for trade records.
    Writes to trades.log independently of the main log.
    Safe to call multiple times — handlers are only added once.
    """
    trade_logger = logging.getLogger("trades")

    # Guard against duplicate handlers if called more than once
    if trade_logger.handlers:
        return trade_logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    trade_logger.setLevel(logging.INFO)
    trade_logger.propagate = False   # don't also write to app.log

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    handler = logging.handlers.RotatingFileHandler(
        TRADE_LOG,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8"
    )
    handler.setFormatter(formatter)
    trade_logger.addHandler(handler)

    return trade_logger


# ── Crash classification ──────────────────────────────────────

# Exceptions that are safe to auto-recover from
RECOVERABLE_EXCEPTION_TYPES = (
    ConnectionError,
    TimeoutError,
    OSError,
)

# Substrings in exception messages that indicate a recoverable issue
RECOVERABLE_MESSAGE_FRAGMENTS = (
    "timeout",
    "connection",
    "network",
    "temporary",
    "rate limit",
    "503",
    "502",
    "504",
)

# Substrings that indicate an unrecoverable issue
UNRECOVERABLE_MESSAGE_FRAGMENTS = (
    "authentication",
    "credentials",
    "invalid token",
    "permission denied",
    "corrupt",
    "assertion",
)


def classify_crash(exc: BaseException) -> tuple[str, bool]:
    """
    Classify an exception as recoverable or not.

    Returns:
        (reason: str, recoverable: bool)

    Recoverable crashes auto-restart trading after a delay.
    Unrecoverable crashes require manual confirmation via YES button.

    Classification order:
      1. Unrecoverable message fragments (most specific, checked first)
      2. Known recoverable exception types
      3. Recoverable message fragments
      4. Unknown — treated as unrecoverable to be safe
    """
    exc_type = type(exc).__name__
    exc_msg  = str(exc).lower()
    tb       = traceback.format_exc()

    # Unrecoverable fragment check applies to all exception types
    # including BrokerError — checked first so it takes priority
    for fragment in UNRECOVERABLE_MESSAGE_FRAGMENTS:
        if fragment in exc_msg:
            reason = (
                f"{exc_type}: {exc} "
                f"[unrecoverable: matched '{fragment}']"
            )
            return reason, False

    # Known transient exception types
    if isinstance(exc, RECOVERABLE_EXCEPTION_TYPES):
        reason = f"{exc_type}: {exc} [recoverable: known transient type]"
        return reason, True

    # Recoverable message fragments
    for fragment in RECOVERABLE_MESSAGE_FRAGMENTS:
        if fragment in exc_msg:
            reason = f"{exc_type}: {exc} [recoverable: matched '{fragment}']"
            return reason, True

    # Unknown — unrecoverable by default
    reason = (
        f"{exc_type}: {exc} "
        f"[unrecoverable: unknown exception type]\n{tb}"
    )
    return reason, False


def log_trade(
    trade_logger: logging.Logger,
    action:    str,
    ticker:    str,
    amount:    float,
    price:     float,
    mode:      str,
    algorithm: str,
    result:    str,
    notes:     Optional[str] = None,
) -> None:
    """
    Write a structured trade record to trades.log.

    All fields are mandatory except notes.
    Falls back to root logger warning if trade_logger has no handlers,
    so trade records are never silently lost.
    """
    if not trade_logger.handlers:
        logging.warning(
            "log_trade() called but trade logger has no handlers — "
            "trade will be logged to root logger only. "
            "Call setup_trade_logger() at startup."
        )

    note_str = f" | {notes}" if notes else ""
    trade_logger.info(
        "ACTION=%s | TICKER=%s | AMOUNT=%.2f SEK | PRICE=%.4f"
        " | MODE=%s | ALGO=%s | RESULT=%s%s",
        action, ticker, amount, price, mode, algorithm, result, note_str
    )