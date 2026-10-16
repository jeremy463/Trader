"""Configuration (spec sections 10, 17, 18, 23). Conservative defaults; nothing aggressive is inferred."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class Mode(str, Enum):
    ANALYSIS_ONLY = "ANALYSIS_ONLY"
    PAPER = "PAPER"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    AUTONOMOUS = "AUTONOMOUS"


@dataclass
class RiskConfig:
    account_currency: str = "USD"
    equity: float = 10_000.0
    risk_per_trade_pct: float = 0.25          # % of equity (conservative research default)
    max_daily_loss_pct: float = 1.0
    max_weekly_loss_pct: float = 3.0
    max_drawdown_pct: float = 8.0
    max_total_open_risk_pct: float = 1.0
    max_correlated_positions: int = 1          # same-currency same-direction exposure count
    max_leverage: float = 10.0                 # notional / equity
    max_position_size: float = 5.0             # lots/contracts
    max_simultaneous_positions: int = 3
    max_consecutive_losses: int = 4
    max_orders_per_window: int = 5
    order_window_seconds: int = 3600
    min_free_margin_pct: float = 50.0          # free margin reserve as % of equity
    min_net_rr: float = 1.5                    # on TP2
    max_spread_atr_frac: float = 0.15          # spread / ATR
    max_spread_abs: Optional[float] = None     # price units
    max_slippage: float = 0.0                  # price units, 0 = use assumed
    assumed_slippage: float = 0.0              # price units used for sizing/costs
    max_stop_atr: float = 4.0
    min_stop_atr: float = 0.75


@dataclass
class EventPolicy:
    blackout_minutes_before: int = 30
    blackout_minutes_after: int = 15
    block_overnight: bool = False
    block_weekend: bool = True
    events: List[Tuple[str, str, str]] = field(default_factory=list)  # (iso_utc_time, currency, name) high impact


@dataclass
class ExecConfig:
    mode: Mode = Mode.ANALYSIS_ONLY
    allowed_instruments: List[str] = field(default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY"])
    allowed_sessions_utc: List[Tuple[int, int]] = field(default_factory=lambda: [(7, 20)])  # London+NY by default
    permitted_strategies: List[str] = field(default_factory=lambda: ["trend_pullback"])
    broker: str = "paper"
    account_type: str = "demo"


@dataclass
class Authorization:
    """Autonomous mode cannot be enabled by prompt alone (spec 17)."""
    verified_integration: bool = False
    owner_authorized: bool = False
    risk_engine_tested: bool = False
    kill_switch_ready: bool = False
    live_parameters_confirmed: bool = False

    def autonomous_ok(self) -> Tuple[bool, List[str]]:
        miss = [k for k, v in self.__dict__.items() if not v]
        return (not miss, miss)


def validate_config(r: RiskConfig) -> List[str]:
    p = []
    if r.equity <= 0: p.append("equity must be > 0")
    if not 0 < r.risk_per_trade_pct <= 2.0: p.append("risk_per_trade_pct must be in (0, 2]")
    if r.max_daily_loss_pct < r.risk_per_trade_pct: p.append("daily loss limit below per-trade risk")
    if r.max_total_open_risk_pct < r.risk_per_trade_pct: p.append("open-risk cap below per-trade risk")
    if r.min_net_rr < 1.0: p.append("min_net_rr < 1 is not permitted")
    if r.max_leverage <= 0: p.append("max_leverage must be > 0")
    return p
