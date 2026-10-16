"""Instrument specifications (spec 2, 11, 12). Futures/forex specs are EXAMPLES and must be verified with the broker."""
from __future__ import annotations
from dataclasses import dataclass
import math
from typing import Dict, Optional


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    asset_class: str            # "forex" | "futures"
    tick_size: float            # smallest price increment
    contract_size: float = 1.0  # forex: units per 1.0 lot
    tick_value: float = 0.0     # futures: value of one tick in quote currency
    pip_size: float = 0.0001
    base_ccy: str = ""
    quote_ccy: str = "USD"
    lot_step: float = 0.01
    min_lot: float = 0.01
    max_lot: float = 100.0
    margin_per_lot: float = 0.0     # futures: exchange/broker initial margin in account ccy; forex: derived from leverage
    forex_leverage: float = 30.0
    expiry: str = ""                # futures: dated contract e.g. "ESZ6"
    verified: bool = False          # set True only after checking broker spec

    @property
    def value_per_price_unit(self) -> float:
        """Account-quote-currency P/L for a 1.0 price move on 1 lot/contract."""
        if self.tick_value > 0:      # futures, or broker-supplied (already account-currency) tick value
            return self.tick_value / self.tick_size
        return self.contract_size

    def pip_value_per_lot(self, quote_to_account: float = 1.0) -> float:
        if self.asset_class != "forex":
            raise ValueError("pip value is a forex concept")
        return self.pip_size * self.contract_size * quote_to_account

    def round_price(self, p: float) -> float:
        return round(round(p / self.tick_size) * self.tick_size, 10)

    def round_size_down(self, s: float) -> float:
        n = math.floor(s / self.lot_step + 1e-9)
        return round(n * self.lot_step, 8)

    def notional_per_lot(self, price: float, quote_to_account: float = 1.0) -> float:
        return price * self.value_per_price_unit * quote_to_account

    def margin_required(self, size: float, price: float, quote_to_account: float = 1.0) -> float:
        if self.margin_per_lot > 0:
            return size * self.margin_per_lot
        return self.notional_per_lot(price, quote_to_account) * size / self.forex_leverage


def _fx(sym, base, quote, pip=0.0001, tick=0.00001):
    return InstrumentSpec(sym, "forex", tick, 100_000, 0.0, pip, base, quote)


REGISTRY: Dict[str, InstrumentSpec] = {s.symbol: s for s in [
    _fx("EURUSD", "EUR", "USD"), _fx("GBPUSD", "GBP", "USD"), _fx("AUDUSD", "AUD", "USD"),
    _fx("NZDUSD", "NZD", "USD"), _fx("USDCHF", "USD", "CHF"), _fx("USDCAD", "USD", "CAD"),
    _fx("USDJPY", "USD", "JPY", 0.01, 0.001), _fx("EURJPY", "EUR", "JPY", 0.01, 0.001),
    _fx("GBPJPY", "GBP", "JPY", 0.01, 0.001),
    # Futures: tick values are CME examples - VERIFY contract month, margin and specs before use.
    InstrumentSpec("ES", "futures", 0.25, 50, 12.50, 0.25, "", "USD", 1, 1, 100, 13000.0, expiry="UNSET"),
    InstrumentSpec("MES", "futures", 0.25, 5, 1.25, 0.25, "", "USD", 1, 1, 1000, 1300.0, expiry="UNSET"),
    InstrumentSpec("NQ", "futures", 0.25, 20, 5.00, 0.25, "", "USD", 1, 1, 100, 18000.0, expiry="UNSET"),
    InstrumentSpec("MNQ", "futures", 0.25, 2, 0.50, 0.25, "", "USD", 1, 1, 1000, 1800.0, expiry="UNSET"),
    InstrumentSpec("CL", "futures", 0.01, 1000, 10.00, 0.01, "", "USD", 1, 1, 100, 6000.0, expiry="UNSET"),
    InstrumentSpec("GC", "futures", 0.10, 100, 10.00, 0.10, "", "USD", 1, 1, 100, 10000.0, expiry="UNSET"),
    InstrumentSpec("SI", "futures", 0.005, 5000, 25.00, 0.005, "", "USD", 1, 1, 100, 11000.0, expiry="UNSET"),
]}


def get_spec(symbol: str) -> InstrumentSpec:
    s = symbol.upper().replace("/", "")
    if s not in REGISTRY:
        raise KeyError(f"Instrument {symbol} not enabled/known. Add a verified InstrumentSpec first.")
    return REGISTRY[s]


def futures_executable(spec: InstrumentSpec) -> Optional[str]:
    """Never confuse continuous charts with a dated contract (spec 2/11)."""
    if spec.asset_class == "futures" and spec.expiry in ("", "UNSET"):
        return f"{spec.symbol}: exact dated contract/expiry not set - cannot execute on a continuous chart"
    return None
