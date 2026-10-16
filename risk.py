"""Deterministic position sizing and independent risk controller with veto (spec 10, 13-I, 18)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import json
from .config import RiskConfig, EventPolicy
from .instruments import InstrumentSpec, futures_executable


@dataclass
class OpenPosition:
    symbol: str
    direction: str          # "BUY" | "SELL"
    size: float
    entry: float
    stop: float
    risk_amount: float
    base: str = ""
    quote: str = "USD"
    notional: float = 0.0


@dataclass
class RiskState:
    equity: float
    day_start_equity: float
    week_start_equity: float
    peak_equity: float
    free_margin: float
    positions: List[OpenPosition] = field(default_factory=list)
    consecutive_losses: int = 0
    order_times: List[datetime] = field(default_factory=list)
    kill_switch: bool = False
    last_day: str = ""
    last_week: str = ""

    @classmethod
    def fresh(cls, equity: float) -> "RiskState":
        return cls(equity, equity, equity, equity, equity)

    def roll(self, now: datetime) -> None:
        """Roll daily/weekly loss baselines at UTC midnight / ISO-week change."""
        d, w = now.date().isoformat(), "%d-W%02d" % now.isocalendar()[:2]
        if self.last_day and d != self.last_day: self.day_start_equity = self.equity
        if self.last_week and w != self.last_week: self.week_start_equity = self.equity
        self.last_day, self.last_week = d, w

    def save(self, path: str) -> None:
        import os, tempfile
        d = {k: getattr(self, k) for k in ("equity", "day_start_equity", "week_start_equity", "peak_equity", "free_margin",
                                           "consecutive_losses", "kill_switch", "last_day", "last_week")}
        d["order_times"] = [t.isoformat() for t in self.order_times]
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
        with os.fdopen(fd, "w") as f: json.dump(d, f)
        os.replace(tmp, path)   # atomic

    @classmethod
    def load(cls, path: str, default_equity: float) -> "RiskState":
        import os
        if not os.path.exists(path): return cls.fresh(default_equity)
        with open(path) as f: d = json.load(f)
        st = cls(d["equity"], d["day_start_equity"], d["week_start_equity"], d["peak_equity"], d["free_margin"])
        st.consecutive_losses, st.kill_switch = d.get("consecutive_losses", 0), d.get("kill_switch", False)
        st.last_day, st.last_week = d.get("last_day", ""), d.get("last_week", "")
        st.order_times = [datetime.fromisoformat(t) for t in d.get("order_times", [])]
        return st


@dataclass
class SizeResult:
    size: float = 0.0
    risk_budget: float = 0.0
    risk_per_lot: float = 0.0
    stop_distance: float = 0.0
    stop_ticks: float = 0.0
    est_costs: float = 0.0
    margin: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0
    formula: str = ""
    rejected: Optional[str] = None


@dataclass
class RiskDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)


def cost_in_price(spec: InstrumentSpec, spread: float, commission_per_lot: float,
                  slippage: float, q2a: float = 1.0) -> float:
    """Round-trip friction expressed in price units: spread + entry/exit slippage + commission."""
    vpu = spec.value_per_price_unit * q2a
    return spread + 2 * slippage + (commission_per_lot / vpu if vpu else 0.0)


def size_position(spec: InstrumentSpec, cfg: RiskConfig, equity: float, entry: float, stop: float,
                  q2a: float = 1.0, commission_per_lot: float = 0.0, slippage: float = 0.0) -> SizeResult:
    r = SizeResult()
    r.stop_distance = abs(entry - stop)
    r.stop_ticks = r.stop_distance / spec.tick_size
    r.risk_budget = equity * cfg.risk_per_trade_pct / 100.0
    if r.stop_distance <= 0:
        r.rejected = "zero stop distance"; return r
    vpu = spec.value_per_price_unit * q2a
    r.risk_per_lot = r.stop_distance * vpu + commission_per_lot + slippage * vpu
    raw = r.risk_budget / r.risk_per_lot
    size = min(spec.round_size_down(raw), spec.max_lot, cfg.max_position_size)
    r.formula = (f"budget {r.risk_budget:.2f} / (stop {r.stop_distance:.6g} x {vpu:.6g}/unit + comm {commission_per_lot:.2f}"
                 f" + slip) = {raw:.4f} -> floor to step {spec.lot_step} = {size}")
    if size < spec.min_lot:
        r.rejected = f"minimum size {spec.min_lot} would exceed the risk budget ({r.risk_budget:.2f})"
        return r
    r.size = size
    r.risk_amount = size * r.risk_per_lot
    r.est_costs = size * (commission_per_lot + 2 * slippage * vpu)
    r.notional = spec.notional_per_lot(entry, q2a) * size
    r.margin = spec.margin_required(size, entry, q2a)
    assert r.risk_amount <= r.risk_budget + 1e-9, "sizing invariant violated"
    return r


def event_blocked(now: datetime, pol: EventPolicy, spec: InstrumentSpec) -> Optional[str]:
    ccys = {c for c in (spec.base_ccy, spec.quote_ccy) if c}
    for t, ccy, name in pol.events:
        if ccy not in ccys: continue
        et = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if et - timedelta(minutes=pol.blackout_minutes_before) <= now <= et + timedelta(minutes=pol.blackout_minutes_after):
            return f"news blackout: {ccy} {name} at {t}"
    if pol.block_weekend and (now.weekday() == 5 or (now.weekday() == 4 and now.hour >= 21)):
        return "weekend exposure blocked"
    return None


def risk_gate(spec: InstrumentSpec, direction: str, sz: SizeResult, cfg: RiskConfig, st: RiskState,
              now: datetime, spread: float, atr_value: float, policy: Optional[EventPolicy] = None,
              allowed_sessions=None, allowed_instruments=None, data_ok: bool = True,
              est_slippage: float = 0.0) -> RiskDecision:
    """Independent veto layer. Analysis agents cannot override a rejection."""
    why: List[str] = []
    if st.kill_switch: why.append("KILL SWITCH active")
    if not data_ok: why.append("data quality check failed")
    if sz.rejected: why.append(f"sizing rejected: {sz.rejected}")
    if allowed_instruments and spec.symbol not in allowed_instruments: why.append("instrument not enabled")
    e = futures_executable(spec)
    if e: why.append(e)
    if allowed_sessions and not any(a <= now.hour < b for a, b in allowed_sessions): why.append("outside allowed trading session")
    if policy:
        b = event_blocked(now, policy, spec)
        if b: why.append(b)
    if st.day_start_equity and (st.day_start_equity - st.equity) / st.day_start_equity * 100 >= cfg.max_daily_loss_pct:
        why.append("daily loss limit reached")
    if st.week_start_equity and (st.week_start_equity - st.equity) / st.week_start_equity * 100 >= cfg.max_weekly_loss_pct:
        why.append("weekly loss limit reached")
    if st.peak_equity and (st.peak_equity - st.equity) / st.peak_equity * 100 >= cfg.max_drawdown_pct:
        why.append("max drawdown reached")
    if st.consecutive_losses >= cfg.max_consecutive_losses: why.append("max consecutive losses reached")
    if len(st.positions) >= cfg.max_simultaneous_positions: why.append("max simultaneous positions")
    open_risk = sum(p.risk_amount for p in st.positions)
    if (open_risk + sz.risk_amount) / st.equity * 100 > cfg.max_total_open_risk_pct + 1e-9:
        why.append("aggregate open risk would exceed cap")
    # correlated exposure: net direction per currency
    def expo(base, quote, d):
        s = 1 if d == "BUY" else -1
        return {base: s, quote: -s} if base else {quote: -s}
    new = expo(spec.base_ccy, spec.quote_ccy, direction)
    for ccy, sgn in new.items():
        same = sum(1 for p in st.positions if expo(p.base, p.quote, p.direction).get(ccy) == sgn)
        if same >= cfg.max_correlated_positions: why.append(f"correlated {ccy} exposure limit"); break
    if sz.size > cfg.max_position_size: why.append("position size cap")
    if st.equity > 0 and (sum(p.notional for p in st.positions) + sz.notional) / st.equity > cfg.max_leverage:
        why.append("leverage cap")
    if st.free_margin - sz.margin < st.equity * cfg.min_free_margin_pct / 100: why.append("free-margin reserve would be breached")
    if atr_value and spread / atr_value > cfg.max_spread_atr_frac: why.append(f"spread too wide ({spread/atr_value:.0%} of ATR)")
    if cfg.max_spread_abs is not None and spread > cfg.max_spread_abs: why.append("spread above absolute maximum")
    if cfg.max_slippage and est_slippage > cfg.max_slippage: why.append("expected slippage above maximum")
    recent = [t for t in st.order_times if (now - t).total_seconds() <= cfg.order_window_seconds]
    if len(recent) >= cfg.max_orders_per_window: why.append("order rate limit")
    return RiskDecision(not why, why)


def risk_multiplier(cfg: RiskConfig, st: "RiskState"):
    """Cut risk after losing streaks / drawdown. Only ever reduces size (spec 10: never increase to recover)."""
    m, why = 1.0, []
    if st.consecutive_losses >= 3: m, why = 0.25, [f"{st.consecutive_losses} consecutive losses"]
    elif st.consecutive_losses >= 2: m, why = 0.5, ["2 consecutive losses"]
    if st.peak_equity:
        dd = (st.peak_equity - st.equity) / st.peak_equity * 100
        if dd >= 0.5 * cfg.max_drawdown_pct and m > 0.5: m, why = 0.5, why + [f"drawdown {dd:.1f}%"]
    return m, ("risk throttled x%.2f: %s" % (m, ", ".join(why))) if m < 1.0 else ""
