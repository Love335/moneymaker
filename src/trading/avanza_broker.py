"""
avanza_broker.py — Real Avanza broker implementation.

Implements the BaseBroker interface using the unofficial avanza-api
Python library. All trades are placed against a real ISK account.

Authentication requires:
  - Avanza username (6-digit number)
  - Avanza password
  - TOTP secret (from Avanza two-factor authentication settings)

These are loaded from config.py which is gitignored and never committed.

IMPORTANT: This broker places real trades with real money.
Only activate by switching to REAL mode via the menu.
The bot defaults to paper trading on every startup.
"""

import logging
import threading
import time
from datetime import date, timedelta
from typing import Optional

from avanza import Avanza, OrderType

from trading.broker import (
    AccountOverview,
    BaseBroker,
    BrokerError,
    OrderResult,
    OrderStatus,
    Position,
)

logger = logging.getLogger(__name__)

# ISK account ID
ACCOUNT_ID = "3525815"

# How long a placed order remains valid (days)
ORDER_VALID_DAYS = 1

# Minimum order size in SEK — Avanza rejects orders below this
MIN_ORDER_SEK = 1.0

# How many times to retry a failed connection before giving up
MAX_CONNECTION_RETRIES = 3

# Seconds to wait between connection retries
RETRY_DELAY_SECONDS = 10

# Session refresh interval — re-authenticate before the session expires
# Avanza sessions expire after ~30 minutes of inactivity
SESSION_REFRESH_INTERVAL = 1500   # 25 minutes


class AvanzaBroker(BaseBroker):
    """
    Real Avanza broker. Places live orders on a real ISK account.

    Maintains a persistent authenticated session and refreshes it
    automatically before expiry. Thread-safe via a reentrant lock.
    """

    def __init__(
        self,
        username:    str,
        password:    str,
        totp_secret: str,
        account_id:  str = ACCOUNT_ID,
    ) -> None:
        self._username    = username
        self._password    = password
        self._totp_secret = totp_secret
        self._account_id  = account_id
        self._client:     Optional[Avanza] = None
        self._lock        = threading.RLock()
        self._connected   = False
        self._last_auth:  float = 0.0

        # Start session refresh thread
        self._refresh_thread = threading.Thread(
            target=self._session_refresh_loop,
            name="AvanzaSessionRefresh",
            daemon=True
        )
        self._refresh_thread.start()

        # Connect immediately on init
        self._connect()

    # ── BaseBroker interface ──────────────────────────────────

    def get_account_overview(self) -> AccountOverview:
        """
        Fetch current account state from Avanza.
        Raises BrokerError if the request fails.
        """
        with self._lock:
            self._ensure_connected()
            try:
                overview  = self._client.get_overview()
                positions = self._client.get_positions()

                # Find our ISK account
                account = next(
                    (a for a in overview.get("accounts", [])
                     if str(a.get("accountId")) == self._account_id),
                    None
                )
                if account is None:
                    raise BrokerError(
                        f"Account {self._account_id} not found in overview. "
                        f"Available: {[a.get('accountId') for a in overview.get('accounts', [])]}"
                    )

                liquid_sek = float(account.get("buyingPower", 0))
                total_sek  = float(account.get("totalValue", 0))

                # Parse positions for our account
                held_positions = []
                for pos_group in positions.get("instrumentPositions", []):
                    for pos in pos_group.get("positions", []):
                        if str(pos.get("accountId")) != self._account_id:
                            continue
                        try:
                            held_positions.append(Position(
                                ticker=str(pos.get("orderbookId", "")),
                                quantity=float(pos.get("volume", 0)),
                                average_price=float(pos.get("averageAcquiredPrice", 0)),
                                current_price=float(pos.get("lastPrice", 0)),
                            ))
                        except (TypeError, ValueError) as exc:
                            logger.warning(
                                "AvanzaBroker: could not parse position %s: %s",
                                pos, exc
                            )

                return AccountOverview(
                    liquid_sek=liquid_sek,
                    total_value_sek=total_sek,
                    positions=held_positions,
                    account_id=self._account_id,
                )

            except BrokerError:
                raise
            except Exception as exc:
                self._connected = False
                raise BrokerError(
                    f"Failed to fetch account overview: {exc}"
                ) from exc

    def get_price(self, ticker: str) -> float:
        """
        Return the current price for a ticker (orderbook ID) in SEK.
        Raises BrokerError if unavailable.
        """
        with self._lock:
            self._ensure_connected()
            try:
                data  = self._client.get_stock_info(ticker)
                price = data.get("lastPrice") or data.get("buyPrice")
                if price is None:
                    raise BrokerError(
                        f"No price available for orderbook {ticker}"
                    )
                return float(price)
            except BrokerError:
                raise
            except Exception as exc:
                raise BrokerError(
                    f"Failed to fetch price for {ticker}: {exc}"
                ) from exc

    def place_order(
        self,
        ticker:     str,
        action:     str,
        amount_sek: float,
    ) -> OrderResult:
        """
        Place a market-price order on the ISK account.

        ticker:     Avanza orderbook ID (e.g. "5479" for Ericsson B)
        action:     "BUY" or "SELL"
        amount_sek: amount in SEK to spend (BUY) or target proceeds (SELL)
        """
        with self._lock:
            self._ensure_connected()

            if amount_sek < MIN_ORDER_SEK:
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    ticker=ticker,
                    action=action,
                    quantity=0,
                    executed_price=0,
                    total_sek=0,
                    error_message=f"Amount {amount_sek:.2f} SEK below minimum",
                )

            try:
                price = self.get_price(ticker)
                if price <= 0:
                    raise BrokerError(
                        f"Invalid price {price} for {ticker}"
                    )

                quantity = round(amount_sek / price, 4)
                if quantity <= 0:
                    return OrderResult(
                        status=OrderStatus.REJECTED,
                        ticker=ticker,
                        action=action,
                        quantity=0,
                        executed_price=price,
                        total_sek=0,
                        error_message="Calculated quantity is zero",
                    )

                order_type = (
                    OrderType.BUY if action == "BUY" else OrderType.SELL
                )
                valid_until = date.today() + timedelta(days=ORDER_VALID_DAYS)

                logger.info(
                    "AvanzaBroker: placing %s order — "
                    "orderbook=%s qty=%.4f price=%.2f SEK total=%.2f SEK",
                    action, ticker, quantity, price, quantity * price
                )

                result = self._client.place_order(
                    account_id=self._account_id,
                    order_book_id=ticker,
                    order_type=order_type,
                    price=price,
                    valid_until=valid_until,
                    volume=quantity,
                )

                order_id = str(result.get("orderId", ""))
                logger.info(
                    "AvanzaBroker: order placed successfully — id=%s",
                    order_id
                )

                return OrderResult(
                    status=OrderStatus.FILLED,
                    ticker=ticker,
                    action=action,
                    quantity=quantity,
                    executed_price=price,
                    total_sek=round(quantity * price, 2),
                    order_id=order_id,
                )

            except BrokerError:
                raise
            except Exception as exc:
                logger.error(
                    "AvanzaBroker: order failed for %s: %s", ticker, exc
                )
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    ticker=ticker,
                    action=action,
                    quantity=0,
                    executed_price=0,
                    total_sek=0,
                    error_message=str(exc),
                )

    def cancel_all_orders(self) -> bool:
        """
        Cancel all open orders on the ISK account.
        Returns True if successful or no orders to cancel.
        """
        with self._lock:
            if not self._connected or self._client is None:
                logger.warning(
                    "AvanzaBroker: cancel_all_orders called but not connected"
                )
                return True

            try:
                deals = self._client.get_deals_and_orders()
                orders = deals.get("orders", [])

                account_orders = [
                    o for o in orders
                    if str(o.get("account", {}).get("id")) == self._account_id
                ]

                if not account_orders:
                    logger.info("AvanzaBroker: no open orders to cancel")
                    return True

                all_cancelled = True
                for order in account_orders:
                    order_id   = order.get("orderId")
                    book_id    = order.get("orderbook", {}).get("id")
                    account_id = order.get("account", {}).get("id")
                    try:
                        self._client.delete_order(
                            account_id=str(account_id),
                            order_id=str(order_id),
                        )
                        logger.info(
                            "AvanzaBroker: cancelled order %s", order_id
                        )
                    except Exception as exc:
                        logger.error(
                            "AvanzaBroker: failed to cancel order %s: %s",
                            order_id, exc
                        )
                        all_cancelled = False

                return all_cancelled

            except Exception as exc:
                logger.error(
                    "AvanzaBroker: cancel_all_orders failed: %s", exc
                )
                return False

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected and self._client is not None

    # ── Internal ──────────────────────────────────────────────

    def _connect(self) -> None:
        """Authenticate with Avanza. Retries on failure."""
        for attempt in range(1, MAX_CONNECTION_RETRIES + 1):
            try:
                logger.info(
                    "AvanzaBroker: connecting (attempt %d/%d)",
                    attempt, MAX_CONNECTION_RETRIES
                )
                self._client = Avanza({
                    "username":   self._username,
                    "password":   self._password,
                    "totpSecret": self._totp_secret,
                })
                self._connected = True
                self._last_auth = time.monotonic()
                logger.info(
                    "AvanzaBroker: connected successfully to account %s",
                    self._account_id
                )
                return

            except Exception as exc:
                logger.warning(
                    "AvanzaBroker: connection attempt %d failed: %s",
                    attempt, exc
                )
                if attempt < MAX_CONNECTION_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)

        self._connected = False
        raise BrokerError(
            f"Failed to connect to Avanza after {MAX_CONNECTION_RETRIES} attempts"
        )

    def _ensure_connected(self) -> None:
        """
        Verify connection is active. Reconnect if session has expired.
        Must be called within self._lock.
        """
        age = time.monotonic() - self._last_auth
        if not self._connected or age > SESSION_REFRESH_INTERVAL:
            logger.info(
                "AvanzaBroker: session expired or disconnected — reconnecting"
            )
            self._connect()

    def _session_refresh_loop(self) -> None:
        """
        Background thread that proactively refreshes the session
        before it expires, to avoid mid-trade authentication failures.
        """
        while True:
            time.sleep(SESSION_REFRESH_INTERVAL - 60)   # refresh 1 min early
            with self._lock:
                if self._connected:
                    try:
                        logger.info(
                            "AvanzaBroker: proactive session refresh"
                        )
                        self._connect()
                    except Exception as exc:
                        logger.error(
                            "AvanzaBroker: session refresh failed: %s", exc
                        )
                        self._connected = False