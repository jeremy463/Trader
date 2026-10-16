"""Execution layer (spec 15-17): validated requests only, paper broker, reconciliation, mode gating, kill switch."""
from __future__ import annotations
import logging, threading, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set
from .config import Mode, Authorization, RiskConfig, ExecConfig, EventPolicy
from .data import Candle, Quote
from .instruments import InstrumentSpec, futures_executable, get_spec
from .journal import Journal
from .risk import RiskState, OpenPosition, SizeResult, risk_gate

log = logging.getLogger("apex.exec")


@dataclass(frozen=True)
class OrderRequest:
    client_id: str
    symbol: str
    side: str            # BUY | SELL
    order_type: str      # market | stop (stop: paper only)
    size: float
    stop_loss: float
    take_profit: float
    price: Optional[float] = None
    risk_amount: float = 0.0
    tp1: float = 0.0           # optional first target: partial exit + stop to breakeven (paper/backtest management)
    partial: float = 0.5

    def validate(self, spec: InstrumentSpec) -> List[str]:
        e = []
        if self.side not in ("BUY", "SELL"): e.append("bad side")
        if self.order_type not in ("market", "stop"): e.append("bad order type")
        if self.size < spec.min_lot or self.size > spec.max_lot: e.append("size out of broker bounds")
        if abs(self.size / spec.lot_step - round(self.size / spec.lot_step)) > 1e-6: e.append("size not on lot step")
        if not self.stop_loss: e.append("protective stop is mandatory")
        if not self.take_profit: e.append("take-profit policy missing")
        ref = self.price
        if ref:
            if self.side == "BUY" and not (self.stop_loss < ref < self.take_profit): e.append("BUY levels inconsistent")
            if self.side == "SELL" and not (self.take_profit < ref < self.stop_loss): e.append("SELL levels inconsistent")
        if self.order_type == "stop" and not self.price: e.append("stop order needs trigger price")
        x = futures_executable(spec)
        if x: e.append(x)
        return e


@dataclass
class OrderResult:
    status: str          # FILLED | PENDING | REJECTED | DUPLICATE | UNKNOWN
    client_id: str
    broker_id: str = ""
    fill_price: float = 0.0
    detail: str = ""


class Broker:
    name = "abstract"
    def submit(self, req: OrderRequest, quote: Quote) -> OrderResult: raise NotImplementedError
    def positions(self) -> List[dict]: raise NotImplementedError          # normalized: id,symbol,side,size,entry,sl,tp,risk
    def account(self) -> Optional[dict]: return None                      # equity,balance,free_margin,currency
    def close_all(self, quote: Optional[Quote] = None) -> None: raise NotImplementedError
    def lookup(self, client_id: str) -> Optional[OrderResult]: return None
    def closed_since(self, ts: datetime) -> List[dict]: return []         # realized trades: pnl, ts


class PaperBroker(Broker):
    """Simulated broker: spread, slippage, commission; SL assumed first when SL and TP hit in one bar."""
    name = "paper"

    def __init__(self, spec: InstrumentSpec, equity: float, commission_per_lot=0.0, slippage=0.0, q2a=1.0):
        self.spec, self.equity, self.comm, self.slip, self.q2a = spec, equity, commission_per_lot, slippage, q2a
        self.open: Dict[str, dict] = {}
        self.pending: Dict[str, dict] = {}
        self.closed: List[dict] = []
        self.orders: Dict[str, OrderResult] = {}
        self._lock = threading.Lock()

    def _pnl(self, p, exit_px, size=None):
        d, sz = (1 if p["side"] == "BUY" else -1), (p["size"] if size is None else size)
        return (exit_px - p["entry"]) * d * sz * self.spec.value_per_price_unit * self.q2a - self.comm * sz

    def submit(self, req: OrderRequest, quote: Quote) -> OrderResult:
        with self._lock:
            if req.client_id in self.orders:
                return OrderResult("DUPLICATE", req.client_id, self.orders[req.client_id].broker_id, detail="idempotent replay")
            errs = req.validate(self.spec)
            if errs:
                r = OrderResult("REJECTED", req.client_id, detail="; ".join(errs)); self.orders[req.client_id] = r; return r
            bid = f"P-{uuid.uuid4().hex[:10]}"
            if req.order_type == "market":
                px = quote.ask + self.slip if req.side == "BUY" else quote.bid - self.slip
                self.open[bid] = dict(id=bid, side=req.side, size=req.size, entry=self.spec.round_price(px), sl=req.stop_loss,
                                      tp=req.take_profit, risk=req.risk_amount, ts=quote.ts, symbol=req.symbol,
                                      tp1=req.tp1, partial=req.partial, be=False, realized=0.0)
                r = OrderResult("FILLED", req.client_id, bid, self.open[bid]["entry"])
            else:
                self.pending[bid] = dict(req=req, id=bid); r = OrderResult("PENDING", req.client_id, bid)
            self.orders[req.client_id] = r
            return r

    def lookup(self, client_id): return self.orders.get(client_id)

    def on_bar(self, c: Candle, spread: float):
        with self._lock:
            for bid, pd in list(self.pending.items()):
                req = pd["req"]
                hit = c.h + spread / 2 >= req.price if req.side == "BUY" else c.l - spread / 2 <= req.price
                if hit:
                    gap = max(req.price, c.o + spread / 2) if req.side == "BUY" else min(req.price, c.o - spread / 2)
                    px = gap + self.slip if req.side == "BUY" else gap - self.slip
                    self.open[bid] = dict(id=bid, side=req.side, size=req.size, entry=self.spec.round_price(px), sl=req.stop_loss,
                                          tp=req.take_profit, risk=req.risk_amount, ts=c.ts, symbol=req.symbol,
                                          tp1=req.tp1, partial=req.partial, be=False, realized=0.0)
                    del self.pending[bid]
                    self.orders[req.client_id] = OrderResult("FILLED", req.client_id, bid, self.open[bid]["entry"])
            for bid, p in list(self.open.items()):
                buy = p["side"] == "BUY"
                lo, hi = (c.l - spread / 2, c.h - spread / 2) if buy else (c.l + spread / 2, c.h + spread / 2)
                op = c.o - spread / 2 if buy else c.o + spread / 2
                sl_hit = lo <= p["sl"] if buy else hi >= p["sl"]
                tp_hit = hi >= p["tp"] if buy else lo <= p["tp"]
                exit_px, why = None, ""
                if sl_hit:
                    exit_px = (min(op, p["sl"]) if buy else max(op, p["sl"])) - (self.slip if buy else -self.slip); why = "SL" if not p.get("be") else "BE"
                else:
                    if p.get("tp1") and not p.get("be") and (hi >= p["tp1"] if buy else lo <= p["tp1"]):
                        part = round(p["size"] * p["partial"] / self.spec.lot_step) * self.spec.lot_step
                        if self.spec.min_lot <= part < p["size"]:           # partial exit at TP1, stop to breakeven
                            pp = self._pnl(p, p["tp1"], part); self.equity += pp; p["realized"] += pp; p["size"] = round(p["size"] - part, 8)
                        p["sl"], p["be"] = p["entry"], True
                    if (hi >= p["tp"] if buy else lo <= p["tp"]): exit_px, why = p["tp"], "TP"
                if exit_px is not None:
                    pnl = self._pnl(p, exit_px); self.equity += pnl; total = pnl + p.get("realized", 0.0)
                    self.closed.append({**p, "exit": exit_px, "pnl": total, "reason": why, "exit_ts": c.ts, "r": total / p["risk"] if p["risk"] else 0.0})
                    del self.open[bid]

    def positions(self):
        return [dict(id=p["id"], symbol=p["symbol"], side=p["side"], size=p["size"], entry=p["entry"], sl=p["sl"], tp=p["tp"], risk=p["risk"])
                for p in self.open.values()]

    def account(self):
        margin = sum(self.spec.margin_required(p["size"], p["entry"], self.q2a) for p in self.open.values())
        return {"equity": self.equity, "balance": self.equity, "free_margin": self.equity - margin, "currency": "paper"}

    def close_all(self, quote=None):
        with self._lock:
            for bid, p in list(self.open.items()):
                px = (quote.bid if p["side"] == "BUY" else quote.ask) if quote else p["entry"]
                pnl = self._pnl(p, px); self.equity += pnl
                self.closed.append({**p, "exit": px, "pnl": pnl, "reason": "MANUAL/KILL", "exit_ts": quote.ts if quote else None,
                                    "r": pnl / p["risk"] if p["risk"] else 0})
                del self.open[bid]
            self.pending.clear()

    def closed_since(self, ts):
        return [{"pnl": t["pnl"], "ts": t.get("exit_ts")} for t in self.closed if t.get("exit_ts") and t["exit_ts"] > ts]


class ExecutionManager:
    """Agent J. Re-runs the risk gate at submit time; kill switch is absolute; unknown order state halts trading."""

    def __init__(self, broker: Broker, exec_cfg: ExecConfig, risk_cfg: RiskConfig, state: RiskState,
                 journal: Optional[Journal] = None, auth: Optional[Authorization] = None,
                 approve: Optional[Callable[[OrderRequest], bool]] = None, policy: Optional[EventPolicy] = None,
                 spec_lookup: Callable[[str], InstrumentSpec] = get_spec, state_path: Optional[str] = None):
        self.broker, self.cfg, self.rcfg, self.state = broker, exec_cfg, risk_cfg, state
        self.journal, self.auth, self.approve, self.policy = journal, auth or Authorization(), approve, policy
        self.spec_lookup, self.state_path = spec_lookup, state_path
        self._lock = threading.RLock()
        self._last_reconcile = datetime.fromtimestamp(0, tz=timezone.utc)

    def _log(self, ev, **k):
        log.info("%s %s", ev, k)
        if self.journal: self.journal.log(ev, **k)

    def _persist(self):
        if self.state_path:
            try: self.state.save(self.state_path)
            except OSError as e: log.error("state save failed: %s", e)

    def kill(self, quote: Optional[Quote] = None, reason="manual"):
        with self._lock:
            self.state.kill_switch = True
            try: self.broker.close_all(quote)
            except NotImplementedError: log.error("broker cannot close_all")
            except Exception as e: log.exception("close_all failed during kill: %s", e)
            self._log("KILL_SWITCH", reason=reason); self._persist()

    def reconcile(self, now: Optional[datetime] = None) -> List[str]:
        """Broker is the source of truth. Unprotected positions or lost visibility trigger the kill switch."""
        now = now or datetime.now(timezone.utc)
        issues: List[str] = []
        with self._lock:
            try:
                acct, bpos = self.broker.account(), self.broker.positions()
            except Exception as e:
                self._log("RECONCILE_FAILED", error=str(e)); self.state.kill_switch = True; self._persist()
                return [f"cannot read broker state: {e}"]
            if acct:
                self.state.equity, self.state.free_margin = acct["equity"], acct["free_margin"]
                self.state.peak_equity = max(self.state.peak_equity, self.state.equity)
            self.state.roll(now)
            for t in self.broker.closed_since(self._last_reconcile):
                self.state.consecutive_losses = self.state.consecutive_losses + 1 if t["pnl"] < 0 else 0
            self._last_reconcile = now
            new = []
            for p in bpos:
                try: sp = self.spec_lookup(p["symbol"])
                except KeyError: issues.append(f"unknown instrument {p['symbol']}"); continue
                if not p.get("sl"):
                    issues.append(f"UNPROTECTED position {p['id']} ({p['symbol']}): no stop-loss"); self.state.kill_switch = True
                vpu = sp.value_per_price_unit
                risk = p.get("risk") or (abs(p["entry"] - p["sl"]) * vpu * p["size"] if p.get("sl") else 0.0)
                new.append(OpenPosition(p["symbol"], p["side"], p["size"], p["entry"], p.get("sl") or 0.0, risk,
                                        sp.base_ccy, sp.quote_ccy, sp.notional_per_lot(p["entry"]) * p["size"]))
            self.state.positions = new
            if issues: self._log("RECONCILE_ISSUES", issues=issues)
            self._persist()
        return issues

    def build_request(self, a, client_id: Optional[str] = None) -> Optional[OrderRequest]:
        if a.decision not in ("BUY", "SELL") or not a.plan or a.plan.rejected or not a.size or a.size.rejected: return None
        if not a.gate or not a.gate.approved: return None
        return OrderRequest(client_id or uuid.uuid4().hex, a.symbol, a.decision, "market", a.size.size,
                            a.plan.stop.price, a.plan.targets.tp2, None, a.size.risk_amount)

    def execute_request(self, req: OrderRequest, size: SizeResult, spec: InstrumentSpec, quote: Quote,
                        atr: float, now: Optional[datetime] = None, data_ok: bool = True) -> OrderResult:
        now = now or datetime.now(timezone.utc)
        mode = self.cfg.mode
        with self._lock:
            if mode == Mode.ANALYSIS_ONLY:
                self._log("NO_ORDER", mode=mode.value); return OrderResult("REJECTED", req.client_id, detail="analysis-only mode")
            if mode == Mode.AUTONOMOUS:
                ok, miss = self.auth.autonomous_ok()
                if not ok:
                    self._log("AUTONOMOUS_BLOCKED", missing=miss)
                    return OrderResult("REJECTED", req.client_id, detail=f"autonomy not authorized: {miss}")
            if mode == Mode.APPROVAL_REQUIRED and not (self.approve and self.approve(req)):
                self._log("APPROVAL_DENIED", client_id=req.client_id)
                return OrderResult("REJECTED", req.client_id, detail="owner approval not given")
            errs = req.validate(spec)
            if errs: return OrderResult("REJECTED", req.client_id, detail="; ".join(errs))
            gate = risk_gate(spec, req.side, size, self.rcfg, self.state, now, quote.spread, atr, self.policy,
                             self.cfg.allowed_sessions_utc, self.cfg.allowed_instruments, data_ok, self.rcfg.assumed_slippage)
            if not gate.approved:
                self._log("GATE_VETO_AT_SUBMIT", reasons=gate.reasons)
                return OrderResult("REJECTED", req.client_id, detail="; ".join(gate.reasons))
            self._log("ORDER_SUBMIT", req=req.__dict__, mode=mode.value)
            try: res = self.broker.submit(req, quote)
            except Exception as e:
                log.exception("broker submit raised")
                res = OrderResult("UNKNOWN", req.client_id, detail=f"exception: {e}")
            if res.status == "UNKNOWN":      # never retry blindly: look it up, else halt and reconcile
                look = None
                try: look = self.broker.lookup(req.client_id)
                except Exception: pass
                if look: res = look
                else:
                    self.state.kill_switch = True; self._log("UNKNOWN_STATE_HALT", client_id=req.client_id)
            self._log("ORDER_RESULT", status=res.status, broker_id=res.broker_id, fill=res.fill_price, detail=res.detail)
            if res.status in ("FILLED", "PENDING"): self.state.order_times.append(now)
            self._persist()
            if res.status == "FILLED": self.reconcile(now)   # broker truth replaces local bookkeeping
            return res

    def execute(self, a, quote: Quote, now: Optional[datetime] = None) -> OrderResult:
        req = self.build_request(a)
        if req is None:
            self._log("NO_ORDER", decision=a.decision, cls=a.classification)
            return OrderResult("REJECTED", "-", detail="no approved setup")
        res = self.execute_request(req, a.size, a.spec, quote, a.atr, now, a.quality.ok)
        a.exec_status = f"{self.cfg.mode.value}: {res.status} {res.detail}".strip()
        return res
