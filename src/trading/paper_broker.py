"""
paper_broker.py — Paper trading broker with persistent portfolio state.

Simulates trades using real market prices. Persists portfolio to
data/paper_portfolio.json so paper trading continues across reboots.
Can be reset to a fresh state with a chosen starting balance.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from trading.broker import (
    AccountOverview,
    BaseBroker,
    BrokerError,
    OrderResult,
    OrderStatus,
    Position,
)

logger = logging.getLogger(__name__)

PORTFOLIO_FILE = Path(__file__).resolve().parents[2] / "data" / "paper_portfolio.json"

# Simulated commission per trade (realistic for Avanza)
COMMISSION_SEK = 1.0


class PaperBroker(BaseBroker):
    """
    Paper trading implementation. All trades are simulated.
    Portfolio state is persisted to JSON after every change.
    """

    def __init__(
        self,
        starting_balance: float = 10_000.0,
        portfolio_file:   Optional[Path] = None,
    ) -> None:
        self._lock             = threading.Lock()
        self._starting_balance = starting_balance
        self._portfolio_file   = portfolio_file or PORTFOLIO_FILE
        self._state            = self._load_or_init(starting_balance)
        self._price_store:     dict = {}
        logger.info(
            "PaperBroker initialised. Balance: %.2f SEK. Positions: %d",
            self._state["liquid_sek"],
            len(self._state["positions"])
        )

    # ── BaseBroker interface ──────────────────────────────────

    def get_account_overview(self) -> AccountOverview:
        with self._lock:
            positions = [
                Position(
                    ticker=p["ticker"],
                    quantity=p["quantity"],
                    average_price=p["average_price"],
                    current_price=p.get("current_price", p["average_price"]),
                    opened_at=datetime.fromisoformat(p["opened_at"]),
                )
                for p in self._state["positions"]
            ]
            total = self._state["liquid_sek"] + sum(
                p.market_value for p in positions
            )
            return AccountOverview(
                liquid_sek=self._state["liquid_sek"],
                total_value_sek=total,
                positions=positions,
                account_id="PAPER",
            )

    def get_price(self, ticker: str) -> float:
        """
        Return last known price for ticker.
        Checks price_store first (updated by engine via update_price()),
        then falls back to the position's stored price.
        Raises BrokerError if no price is available at all.
        """
        with self._lock:
            price = self._get_current_price_locked(ticker)
            if price is not None and price > 0:
                return price
        raise BrokerError(
            f"No price available for {ticker}. "
            f"Call update_price() before placing orders."
        )

    def place_order(
        self,
        ticker:     str,
        action:     str,
        amount_sek: float,
    ) -> OrderResult:
        with self._lock:
            return self._execute(ticker, action, amount_sek)

    def cancel_all_orders(self) -> bool:
        logger.info("PaperBroker: cancel_all_orders called (no-op)")
        return True

    def is_connected(self) -> bool:
        return True

    # ── Paper-specific methods ────────────────────────────────

    def update_price(self, ticker: str, price: float) -> None:
        """Update the current price for a ticker."""
        with self._lock:
            self._price_store[ticker] = price
            for p in self._state["positions"]:
                if p["ticker"] == ticker:
                    p["current_price"] = price
            self._save()

    def reset(self, starting_balance: float) -> None:
        """Wipe all positions and reset cash to starting_balance."""
        with self._lock:
            self._starting_balance = starting_balance
            self._price_store.clear()
            self._state = self._fresh_state(starting_balance)
            self._save()
        logger.info(
            "PaperBroker: portfolio reset to %.2f SEK", starting_balance
        )

    def get_all_time_pnl(self) -> float:
        """Return total realised + unrealised P&L since last reset."""
        with self._lock:
            return round(
                self._state.get("realised_pnl", 0.0) + self._unrealised(), 2
            )

    # ── Internal ──────────────────────────────────────────────

    def _execute(
        self, ticker: str, action: str, amount_sek: float
    ) -> OrderResult:
        """Core execution logic. Must be called within lock."""
        price = self._get_current_price_locked(ticker)
        if price is None or price <= 0:
            logger.error(
                "PaperBroker: cannot execute — no price for %s", ticker
            )
            return OrderResult(
                status=OrderStatus.REJECTED,
                ticker=ticker,
                action=action,
                quantity=0,
                executed_price=0,
                total_sek=0,
                error_message=f"No price available for {ticker}",
            )

        effective_amount = amount_sek - COMMISSION_SEK

        if action == "BUY":
            return self._execute_buy(ticker, effective_amount, price)
        elif action == "SELL":
            return self._execute_sell(ticker, effective_amount, price)
        else:
            raise BrokerError(f"Unknown action '{action}'")

    def _execute_buy(
        self, ticker: str, amount_sek: float, price: float
    ) -> OrderResult:
        if amount_sek > self._state["liquid_sek"]:
            logger.warning(
                "PaperBroker: insufficient funds. Need %.2f, have %.2f",
                amount_sek, self._state["liquid_sek"]
            )
            amount_sek = self._state["liquid_sek"]

        if amount_sek <= 0:
            return OrderResult(
                status=OrderStatus.REJECTED,
                ticker=ticker, action="BUY",
                quantity=0, executed_price=price,
                total_sek=0,
                error_message="Insufficient funds",
            )

        quantity = amount_sek / price
        self._state["liquid_sek"] -= amount_sek

        existing = next(
            (p for p in self._state["positions"] if p["ticker"] == ticker),
            None
        )
        if existing:
            old_qty   = existing["quantity"]
            old_avg   = existing["average_price"]
            new_qty   = old_qty + quantity
            # Correct weighted average:
            # (old_cost + new_cost) / total_quantity
            new_avg   = (old_qty * old_avg + quantity * price) / new_qty
            existing["quantity"]      = new_qty
            existing["average_price"] = new_avg
            existing["current_price"] = price
        else:
            self._state["positions"].append({
                "ticker":        ticker,
                "quantity":      quantity,
                "average_price": price,
                "current_price": price,
                "opened_at":     datetime.now().isoformat(),
            })

        self._save()
        logger.info(
            "PaperBroker: BUY %.4f units of %s @ %.2f SEK = %.2f SEK total",
            quantity, ticker, price, amount_sek
        )
        return OrderResult(
            status=OrderStatus.FILLED,
            ticker=ticker, action="BUY",
            quantity=quantity, executed_price=price,
            total_sek=amount_sek,
        )

    def _execute_sell(
        self, ticker: str, amount_sek: float, price: float
    ) -> OrderResult:
        existing = next(
            (p for p in self._state["positions"] if p["ticker"] == ticker),
            None
        )
        if not existing or existing["quantity"] <= 0:
            return OrderResult(
                status=OrderStatus.REJECTED,
                ticker=ticker, action="SELL",
                quantity=0, executed_price=price,
                total_sek=0,
                error_message=f"No position in {ticker} to sell",
            )

        quantity   = existing["quantity"]
        proceeds   = quantity * price
        cost_basis = quantity * existing["average_price"]
        trade_pnl  = proceeds - cost_basis

        self._state["liquid_sek"]   += proceeds
        self._state["realised_pnl"] += trade_pnl
        self._state["positions"]     = [
            p for p in self._state["positions"]
            if p["ticker"] != ticker
        ]

        self._save()
        logger.info(
            "PaperBroker: SELL %.4f units of %s @ %.2f SEK. "
            "Proceeds: %.2f. P&L: %.2f",
            quantity, ticker, price, proceeds, trade_pnl
        )
        return OrderResult(
            status=OrderStatus.FILLED,
            ticker=ticker, action="SELL",
            quantity=quantity, executed_price=price,
            total_sek=proceeds,
        )

    def _get_current_price_locked(self, ticker: str) -> Optional[float]:
        """Return best known price for ticker. Must be called within lock."""
        if ticker in self._price_store:
            return self._price_store[ticker]
        for p in self._state["positions"]:
            if p["ticker"] == ticker:
                return p.get("current_price", p["average_price"])
        return None

    def _unrealised(self) -> float:
        """Sum of unrealised P&L across all positions. Call within lock."""
        total = 0.0
        for p in self._state["positions"]:
            current = p.get("current_price", p["average_price"])
            total  += (current - p["average_price"]) * p["quantity"]
        return total

    def _fresh_state(self, balance: float) -> dict:
        return {
            "liquid_sek":   balance,
            "positions":    [],
            "realised_pnl": 0.0,
            "created_at":   datetime.now().isoformat(),
        }

    def _load_or_init(self, starting_balance: float) -> dict:
        self._portfolio_file.parent.mkdir(parents=True, exist_ok=True)
        if self._portfolio_file.exists():
            try:
                with open(self._portfolio_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                # Ensure realised_pnl key exists in older portfolio files
                state.setdefault("realised_pnl", 0.0)
                logger.info(
                    "PaperBroker: loaded existing portfolio from %s",
                    self._portfolio_file
                )
                return state
            except (json.JSONDecodeError, KeyError) as exc:
                logger.error(
                    "PaperBroker: corrupt portfolio file, starting fresh. "
                    "Error: %s", exc
                )
        state = self._fresh_state(starting_balance)
        self._save_state(state)
        return state

    def _save(self) -> None:
        self._save_state(self._state)

    def _save_state(self, state: dict) -> None:
        self._portfolio_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._portfolio_file.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            tmp.replace(self._portfolio_file)   # atomic rename
        except OSError as exc:
            logger.error(
                "PaperBroker: failed to save portfolio: %s", exc
            )