"""
tickers.py — Central registry of all tradeable instruments.

Single source of truth for:
  - Yahoo Finance ticker symbols (used by yfinance for data)
  - Avanza orderbook IDs (used by AvanzaBroker for trading)
  - Human-readable names
  - Asset class classification

Adding a new instrument requires only one entry here.
All algorithms and the broker read from this registry.

To find an Avanza orderbook ID: visit the instrument page on
avanza.se and read the number from the URL:
  e.g. avanza.se/aktier/om-aktien.html/5240/ericsson-b → ID 5240
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class AssetClass(Enum):
    EQUITY      = auto()   # Individual stocks
    ETF_EQUITY  = auto()   # Equity ETFs and index funds
    ETF_BOND    = auto()   # Bond ETFs
    ETF_GOLD    = auto()   # Commodity ETFs
    ETF_BULL    = auto()   # Leveraged ETFs (higher risk)


@dataclass(frozen=True)
class Instrument:
    """
    Represents a single tradeable instrument.

    yf_ticker:       Yahoo Finance symbol — used for price data
    avanza_id:       Avanza orderbook ID — used for real trading
                     None means paper trading only
    name:            Human-readable name for logs and display
    asset_class:     Classification for risk and portfolio management
    avanza_tradeable: True if this instrument can be traded on Avanza ISK
    """
    yf_ticker:        str
    avanza_id:        Optional[str]
    name:             str
    asset_class:      AssetClass
    avanza_tradeable: bool = True

    def __post_init__(self) -> None:
        if self.avanza_tradeable and self.avanza_id is None:
            # Use object.__setattr__ because dataclass is frozen
            object.__setattr__(self, "avanza_tradeable", False)


# ── Instrument registry ────────────────────────────────────────
# All instruments the bot may ever trade or monitor.
# Add new instruments here — nowhere else needs to change.

REGISTRY: dict[str, Instrument] = {

    # ── Swedish large-cap stocks (OMXS30 components) ──────────
    "ERIC-B.ST": Instrument(
        yf_ticker="ERIC-B.ST", avanza_id="5240",
        name="Ericsson B", asset_class=AssetClass.EQUITY,
    ),
    "VOLV-B.ST": Instrument(
        yf_ticker="VOLV-B.ST", avanza_id="5269",
        name="Volvo B", asset_class=AssetClass.EQUITY,
    ),
    "SEB-A.ST": Instrument(
        yf_ticker="SEB-A.ST", avanza_id="5255",
        name="SEB A", asset_class=AssetClass.EQUITY,
    ),
    "INVE-B.ST": Instrument(
        yf_ticker="INVE-B.ST", avanza_id="5247",
        name="Investor B", asset_class=AssetClass.EQUITY,
    ),
    "SAND.ST": Instrument(
        yf_ticker="SAND.ST", avanza_id="5471",
        name="Sandvik", asset_class=AssetClass.EQUITY,
    ),
    "ATCO-A.ST": Instrument(
        yf_ticker="ATCO-A.ST", avanza_id="5234",
        name="Atlas Copco A", asset_class=AssetClass.EQUITY,
    ),
    "SWED-A.ST": Instrument(
        yf_ticker="SWED-A.ST", avanza_id="5241",
        name="Swedbank A", asset_class=AssetClass.EQUITY,
    ),
    "HM-B.ST": Instrument(
        yf_ticker="HM-B.ST", avanza_id="5364",
        name="H&M B", asset_class=AssetClass.EQUITY,
    ),

    # ── Swedish ETFs ──────────────────────────────────────────
    "XACT-OMXS30.ST": Instrument(
        yf_ticker="XACT-OMXS30.ST", avanza_id="5510",
        name="XACT OMXS30 ESG", asset_class=AssetClass.ETF_EQUITY,
    ),
    "XACT-BULL.ST": Instrument(
        yf_ticker="XACT-BULL.ST", avanza_id="12252",
        name="XACT Bull ETF", asset_class=AssetClass.ETF_BULL,
    ),
    "XACT-OBLIGATION.ST": Instrument(
        yf_ticker="XACT-OBLIGATION.ST", avanza_id="636979",
        name="XACT Obligation (bonds)", asset_class=AssetClass.ETF_BOND,
    ),

    # ── Global ETFs (available on Avanza) ─────────────────────
    "GLD": Instrument(
        yf_ticker="GLD", avanza_id="34427",
        name="SPDR Gold Shares", asset_class=AssetClass.ETF_GOLD,
    ),
    "SPY": Instrument(
        yf_ticker="SPY", avanza_id="159932",
        name="SPDR S&P 500 ETF", asset_class=AssetClass.ETF_EQUITY,
    ),
}


# ── Convenience lookup functions ───────────────────────────────

def get(yf_ticker: str) -> Instrument:
    """
    Return an Instrument by its Yahoo Finance ticker.
    Raises KeyError if the ticker is not in the registry.
    Always check here before adding a ticker to an algorithm.
    """
    if yf_ticker not in REGISTRY:
        raise KeyError(
            f"Ticker '{yf_ticker}' is not in the instrument registry. "
            f"Add it to src/trading/tickers.py before using it."
        )
    return REGISTRY[yf_ticker]


def avanza_id(yf_ticker: str) -> str:
    """
    Return the Avanza orderbook ID for a Yahoo Finance ticker.
    Raises KeyError if not in registry.
    Raises ValueError if the instrument is not tradeable on Avanza.
    """
    instrument = get(yf_ticker)
    if not instrument.avanza_tradeable or instrument.avanza_id is None:
        raise ValueError(
            f"'{yf_ticker}' ({instrument.name}) is not tradeable on Avanza ISK. "
            f"It can be used for paper trading via yfinance only."
        )
    return instrument.avanza_id


def is_avanza_tradeable(yf_ticker: str) -> bool:
    """Return True if this ticker can be traded on Avanza ISK."""
    try:
        instrument = get(yf_ticker)
        return instrument.avanza_tradeable and instrument.avanza_id is not None
    except KeyError:
        return False


def all_tickers() -> list[str]:
    """Return all registered Yahoo Finance ticker symbols."""
    return list(REGISTRY.keys())


def tradeable_tickers() -> list[str]:
    """Return only tickers that can be traded on Avanza ISK."""
    return [t for t, i in REGISTRY.items() if i.avanza_tradeable]