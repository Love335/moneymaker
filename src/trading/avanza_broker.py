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
from trading import tickers as ticker_registry

logger = logging.getLogger(__name__)

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


def _extract_value(field) -> float:
    """
    Safely extract a numeric value from an Avanza API field.

    The API returns monetary values as dicts:
      {"value": 40013.28, "unit": "SEK", ...}
    or occasionally as plain floats/ints.
    Returns 0.0 if the field is None or unparseable.
    """
    if field is None:
        return 0.0
    if isinstance(field, dict):
        return float(field.get("value", 0.0))
    try:
        return float(field)
    except (TypeError, ValueError):
        return 0.0


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
        account_id:  str,
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

        API field notes (confirmed from live response):
          account["id"]          — account ID string
          account["type"]        — account type e.g. "INVESTERINGSSPARKONTO"
          account["buyingPower"] — dict {"value": float, "unit": "SEK", ...}
          account["totalValue"]  — dict {"value": float, ...}
        """
        with self._lock:
            self._ensure_connected()
            try:
                overview = self._client.get_overview()

                # Find our ISK account by "id" (not "accountId")
                account = next(
                    (a for a in overview.get("accounts", [])
                     if str(a.get("id")) == self._account_id),
                    None
                )
                if account is None:
                    available = [
                        f"{a.get('id')} ({a.get('type')})"
                        for a in overview.get("accounts", [])
                    ]
                    raise BrokerError(
                        f"Account {self._account_id} not found. "
                        f"Available: {available}"
                    )

                liquid_sek = _extract_value(account.get("buyingPower"))
                total_sek  = _extract_value(account.get("totalValue"))

                # Fetch positions separately
                held_positions = self._fetch_positions()

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

    def _fetch_positions(self) -> list:
        """
        Fetch current positions for our account.
        Returns an empty list if the API call fails or no positions exist.
        The avanza-api library uses get_positions() on newer versions
        and get_positions_by_account() on others — we try both.
        """
        held_positions = []
        try:
            # Try the account-specific endpoint first
            try:
                raw = self._client.get_positions()
            except AttributeError:
                logger.warning(
                    "AvanzaBroker: get_positions() not available — "
                    "positions will be empty"
                )
                return []

            # The response structure varies by library version.
            # Handle both list and dict responses.
            if isinstance(raw, list):
                position_list = raw
            elif isinstance(raw, dict):
                # Flatten instrumentPositions groups
                position_list = []
                for group in raw.get("instrumentPositions", []):
                    position_list.extend(group.get("positions", []))
            else:
                logger.warning(
                    "AvanzaBroker: unexpected positions response type: %s",
                    type(raw)
                )
                return []

            for pos in position_list:
                # Filter to our account only if accountId is present
                if "accountId" in pos:
                    if str(pos["accountId"]) != self._account_id:
                        continue

                try:
                    held_positions.append(Position(
                        ticker=str(pos.get("orderbookId", "")),
                        quantity=float(pos.get("volume", 0)),
                        average_price=_extract_value(
                            pos.get("averageAcquiredPrice")
                        ),
                        current_price=_extract_value(
                            pos.get("lastPrice") or pos.get("currentPrice")
                        ),
                    ))
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "AvanzaBroker: could not parse position %s: %s",
                        pos, exc
                    )

        except Exception as exc:
            logger.error("AvanzaBroker: failed to fetch positions: %s", exc)

        return held_positions

    def get_price(self, ticker: str) -> float:
        """
        Return the current price for a Yahoo Finance ticker in SEK.
        Translates to Avanza orderbook ID via the ticker registry.
        Raises BrokerError if unavailable or not on Avanza.
        """
        orderbook_id = self._to_orderbook_id(ticker)
        with self._lock:
            self._ensure_connected()
            try:
                data = self._client.get_stock_info(orderbook_id)

                price = None

                # Primary: live quote fields (populated during market hours)
                quote = data.get("quote") or {}
                for field in ("last", "buy", "sell", "highest", "lowest"):
                    raw = quote.get(field)
                    if raw is not None:
                        try:
                            candidate = float(raw)
                            if candidate > 0:
                                price = candidate
                                break
                        except (TypeError, ValueError):
                            continue

                # Fallback: most recent closing price (reliable after hours)
                if price is None or price <= 0:
                    hcp = data.get("historicalClosingPrices") or {}
                    raw = hcp.get("oneDay")
                    if raw is not None:
                        try:
                            candidate = float(raw)
                            if candidate > 0:
                                price = candidate
                        except (TypeError, ValueError):
                            pass

                if price is None or price <= 0:
                    raise BrokerError(
                        f"No valid price in response for {ticker} "
                        f"(orderbook {orderbook_id}). "
                        f"quote fields: {list(quote.keys())}"
                    )
                return price

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
        Place a limit order at current price on the ISK account.

        ticker:     Yahoo Finance ticker (translated to orderbook ID internally)
        action:     "BUY" or "SELL"
        amount_sek: amount in SEK to spend (BUY) or target proceeds (SELL)
        """
        orderbook_id = self._to_orderbook_id(ticker)

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
                    raise BrokerError(f"Invalid price {price} for {ticker}")

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

                order_type  = OrderType.BUY if action == "BUY" else OrderType.SELL
                valid_until = date.today() + timedelta(days=ORDER_VALID_DAYS)

                logger.info(
                    "AvanzaBroker: placing %s — "
                    "ticker=%s orderbook=%s qty=%.4f "
                    "price=%.2f total=%.2f SEK",
                    action, ticker, orderbook_id,
                    quantity, price, quantity * price,
                )

                result = self._client.place_order(
                    account_id=self._account_id,
                    order_book_id=orderbook_id,
                    order_type=order_type,
                    price=price,
                    valid_until=valid_until,
                    volume=quantity,
                )

                order_id = str(result.get("orderId", ""))
                logger.info(
                    "AvanzaBroker: order placed — id=%s", order_id
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
                orders = self._client.get_orders().get("orders", [])

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
                            order_id, exc,
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
                    attempt, MAX_CONNECTION_RETRIES,
                )
                self._client = Avanza({
                    "username":   self._username,
                    "password":   self._password,
                    "totpSecret": self._totp_secret,
                })
                self._connected = True
                self._last_auth = time.monotonic()
                logger.info(
                    "AvanzaBroker: connected to account %s",
                    self._account_id,
                )
                return

            except Exception as exc:
                logger.warning(
                    "AvanzaBroker: attempt %d failed: %s", attempt, exc
                )
                if attempt < MAX_CONNECTION_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)

        self._connected = False
        raise BrokerError(
            f"Failed to connect to Avanza after "
            f"{MAX_CONNECTION_RETRIES} attempts"
        )

    def _ensure_connected(self) -> None:
        """
        Reconnect if the session has expired.
        Must be called within self._lock.
        """
        age = time.monotonic() - self._last_auth
        if not self._connected or age > SESSION_REFRESH_INTERVAL:
            logger.info("AvanzaBroker: reconnecting (session age %.0fs)", age)
            self._connect()

    def _session_refresh_loop(self) -> None:
        """Proactively refresh the session before it expires."""
        while True:
            time.sleep(SESSION_REFRESH_INTERVAL - 60)
            with self._lock:
                if self._connected:
                    try:
                        logger.info("AvanzaBroker: proactive session refresh")
                        self._connect()
                    except Exception as exc:
                        logger.error(
                            "AvanzaBroker: session refresh failed: %s", exc
                        )
                        self._connected = False

    def _to_orderbook_id(self, yf_ticker: str) -> str:
        """
        Translate a Yahoo Finance ticker to an Avanza orderbook ID
        via the central ticker registry.
        Raises BrokerError if not found or not tradeable on Avanza.
        """
        try:
            return ticker_registry.avanza_id(yf_ticker)
        except KeyError:
            raise BrokerError(
                f"Ticker '{yf_ticker}' is not in the instrument registry. "
                f"Add it to src/trading/tickers.py."
            )
        except ValueError as exc:
            raise BrokerError(str(exc))