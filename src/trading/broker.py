"""
broker.py — Abstract broker interface and shared trading dataclasses.

Both AvanzaBroker and PaperBroker implement this interface.
The engine never knows which one it is using.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import List, Optional

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    FILLED    = auto()
    PARTIAL   = auto()
    REJECTED  = auto()
    CANCELLED = auto()


@dataclass
class Position:
    """A currently held position."""
    ticker:         str
    quantity:       float
    average_price:  float     # SEK per unit at time of purchase
    current_price:  float     # SEK per unit, updated from market data
    opened_at:      datetime  = field(default_factory=datetime.now)

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.average_price

    @property
    def unrealised_pnl(self) -> float:
        return self.market_value - self.cost_basis


@dataclass
class OrderResult:
    """Result returned by the broker after an order attempt."""
    status:         OrderStatus
    ticker:         str
    action:         str         # "BUY" or "SELL"
    quantity:       float
    executed_price: float
    total_sek:      float
    order_id:       Optional[str] = None
    error_message:  Optional[str] = None
    timestamp:      datetime      = field(default_factory=datetime.now)


@dataclass
class AccountOverview:
    """Current account summary."""
    liquid_sek:       float             # uninvested cash
    total_value_sek:  float             # cash + market value of positions
    positions:        List[Position]    = field(default_factory=list)
    account_id:       Optional[str]     = None


class BaseBroker(ABC):
    """
    Abstract interface all broker implementations must satisfy.

    The engine only ever calls these methods — never implementation details.
    """

    @abstractmethod
    def get_account_overview(self) -> AccountOverview:
        """
        Return current account state including cash and positions.
        Raises BrokerError on failure.
        """

    @abstractmethod
    def get_price(self, ticker: str) -> float:
        """
        Return the current price for the given ticker in SEK.
        Raises BrokerError if price cannot be retrieved.
        """

    @abstractmethod
    def place_order(
        self,
        ticker: str,
        action: str,          # "BUY" or "SELL"
        amount_sek: float,    # amount in SEK to spend or receive
    ) -> OrderResult:
        """
        Place a market order.
        Returns an OrderResult regardless of success or failure —
        never raises on a failed trade, but raises BrokerError on
        connectivity or system failures.
        """

    @abstractmethod
    def cancel_all_orders(self) -> bool:
        """
        Cancel all open orders. Returns True if successful.
        Called during emergency shutdown or connectivity loss.
        """

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the broker connection is healthy."""


class BrokerError(Exception):
    """
    Raised by broker implementations for connectivity or system failures.
    Distinct from a rejected order, which comes back in OrderResult.
    """
    pass