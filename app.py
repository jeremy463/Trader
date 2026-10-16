"""Application core: scan -> proposal -> user clicks ALLOW -> fresh revalidation -> execution.
The human click is the approval. Nothing is ever sent without it, and every click re-runs all safety checks on a FRESH quote."""
from __future__ import annotations
import logging, os, threading, uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set
from .config import Mode
from .data import Quote
from .edge import load as load_edge, disabled_from
from .engine import analyze, Analysis
from .events import load_events
from .config import EventPolicy
from .execution import ExecutionManager, OrderRequest, PaperBroker
from .feeds import CSVFeed, OandaFeed
from .instruments import get_spec, InstrumentSpec
from .journal import Journal
from .logsetup import setup_logging
from .plan import SetupPlan
from .report import format_report
import dataclasses
from .risk import RiskState, size_position, cost_in_price, risk_multiplier
from .settings import Settings

log = logging.getLogger("apex.app")


def q2a_for(spec: InstrumentSpec, account_ccy: str, price: float) -> float:
    if spec.tick_value > 0 and spec.expiry == "BROKER": return 1.0          # broker tick value is already account currency
    if spec.asset_class == "futures" or spec.quote_ccy == account_ccy: return 1.0
    if spec.base_ccy == account_ccy: return 1.0 / price
    raise RuntimeError(f"no conversion {spec.quote_ccy}->{account_ccy} for {spec.symbol}; use a broker-sourced spec")


@dataclass
class Proposal:
    id: str
    a: Analysis
    created: datetime
    expires: datetime
    status: str = "pending"      # pending | executed | rejected | expired | failed
    detail: str = ""


class App:
    def __init__(self, s: Settings, feed=None, broker=None, spec: Optional[InstrumentSpec] = None, mt5=None):
        self.s = s
        setup_logging(s.server.log_path)
        self.journal = Journal(s.server.journal_path)
        self.lock = threading.RLock()
        self.symbol, self.tf = s.feed.symbol, s.feed.tf_minutes * 60
        self.mt5 = mt5
        if s.feed.type == "mt5" or s.broker.type == "mt5":
            from .mt5_adapter import load_mt5, MT5Feed, spec_from_mt5, MT5Broker
            self.mt5 = self.mt5 or load_mt5(s.broker.mt5_login, s.broker.mt5_server, path=s.broker.mt5_path)
        if spec is None:
            if self.mt5:
                from .mt5_adapter import spec_from_mt5
                spec = spec_from_mt5(self.mt5, self.symbol)
            else: spec = get_spec(self.symbol)
        self.spec = spec
        if feed is None:
            if s.feed.type == "csv": feed = CSVFeed(s.feed.path, s.feed.spread)
            elif s.feed.type == "oanda": feed = OandaFeed(s.feed.oanda_account, practice=s.feed.oanda_practice)
            else:
                from .mt5_adapter import MT5Feed
                feed = MT5Feed(self.mt5, s.feed.server_utc_offset_hours)
                if feed.offset is None: feed.detect_offset(self.symbol)
        self.feed = feed
        if broker is None:
            if s.broker.type == "mt5":
                from .mt5_adapter import MT5Broker
                broker = MT5Broker(self.mt5, spec, allow_live=s.broker.allow_live)
            else: broker = PaperBroker(spec, s.risk.equity, s.broker.commission_per_lot, s.broker.slippage)
        self.broker = broker
        if s.broker.type == "mt5" and s.exec.mode == Mode.PAPER: raise RuntimeError("mode PAPER cannot be used with a live broker")
        self.state = RiskState.load(s.server.state_path, s.risk.equity)
        s.risk.assumed_slippage = s.broker.slippage
        self.policy = EventPolicy(events=load_events(s.server.events_path)) if s.server.events_path else None
        if self.policy is None: log.warning("no economic calendar configured: news-blackout protection INACTIVE")
        self.approved: Set[str] = set()
        mode = Mode.APPROVAL_REQUIRED if s.exec.mode in (Mode.PAPER, Mode.APPROVAL_REQUIRED) else Mode.ANALYSIS_ONLY
        s.exec.mode = mode
        self.mgr = ExecutionManager(self.broker, s.exec, s.risk, self.state, self.journal, None,
                                    lambda r: r.client_id in self.approved, self.policy, lambda sym: self.spec,
                                    s.server.state_path)
        self.proposals: Dict[str, Proposal] = {}
        self.is_paper = broker.name == "paper"
        issues = self.mgr.reconcile(self.feed.now())
        if issues: log.error("startup reconcile issues: %s", issues)

    # ---- helpers ----
    def _inputs(self):
        cs = self.feed.candles(self.symbol, self.tf, 600)
        q = self.feed.quote(self.symbol)
        return cs, q, self.feed.now()

    def _disabled(self):
        return disabled_from(load_edge(self.s.server.edge_path))

    def _weights(self):
        from .edge import weights_from
        return weights_from(load_edge(self.s.server.edge_path))

    def scan(self) -> dict:
        with self.lock:
            issues = self.mgr.reconcile(self.feed.now())
            try: cs, q, now = self._inputs()
            except Exception as e:
                log.exception("feed failure"); return {"error": f"data feed failure: {e}", "decision": "NO TRADE"}
            q2a = q2a_for(self.spec, self.s.risk.account_currency, cs[-1].c)
            a = analyze(self.symbol, cs, self.tf, self.s.risk, self.s.exec, self.state, q, now, self.policy, q2a,
                        self.s.broker.commission_per_lot, None, enforce_session=True, scan_cfg=self.s.scan,
                        disabled_methods=self._disabled(), allow_simulated_quote=self.is_paper, spec=self.spec, method_weights=self._weights())
            for p in self.proposals.values():
                if p.status == "pending" and p.expires < now: p.status = "expired"
            prop = None
            if self.state.kill_switch: a.evidence_against.append("KILL SWITCH active")
            elif self.mgr.cfg.mode == Mode.ANALYSIS_ONLY: a.evidence_against.append("analysis-only mode: no proposals")
            elif self.mgr.build_request(a) is not None:
                for p in self.proposals.values():
                    if p.status == "pending": p.status = "expired"; p.detail = "superseded by newer scan"
                pid = uuid.uuid4().hex
                prop = Proposal(pid, a, now, now + timedelta(seconds=self.tf * self.s.server.proposal_ttl_bars))
                self.proposals[pid] = prop
                self.journal.log("PROPOSAL", id=pid, symbol=self.symbol, side=a.decision, entry=a.plan.entry, sl=a.plan.stop.price,
                                 tp2=a.plan.targets.tp2, size=a.size.size, consensus=a.scan.summary())
            return self._view(a, prop, issues)

    def _view(self, a: Analysis, prop: Optional[Proposal], issues) -> dict:
        sc = a.scan
        d = {"symbol": a.symbol, "decision": a.decision, "classification": a.classification, "score": a.score,
             "data_ts": a.data_ts.isoformat(), "price": a.price_text, "regime": a.regime.labels,
             "issues": issues, "why": a.evidence_against[:8], "report": format_report(a), "proposal": None, "scan": None}
        if sc:
            d["scan"] = {"summary": sc.summary(), "consensus": sc.consensus, "n_total": sc.n_total, "long": sc.long_passed,
                         "short": sc.short_passed, "neutral": sc.neutral, "na": sc.not_applicable, "errors": sc.errors,
                         "fam_long": len(sc.fam_long), "fam_short": len(sc.fam_short), "why_not": sc.why_not[:4],
                         "methods": [{"name": s.name, "family": s.family, "dir": s.direction, "strength": s.strength,
                                      "passed": s.passed, "applicable": s.applicable, "detail": s.detail} for s in sc.signals]}
        if prop:
            p, z = a.plan, a.size
            d["proposal"] = {"id": prop.id, "side": a.decision, "entry": p.entry, "sl": p.stop.price, "tp1": p.targets.tp1,
                             "tp2": p.targets.tp2, "tp3": p.targets.tp3, "rr_net": p.net_rr[1], "size": z.size, "risk": round(z.risk_amount, 2),
                             "risk_pct": self.s.risk.risk_per_trade_pct, "margin": round(z.margin, 2), "expires": prop.expires.isoformat()}
        return d

    def allow(self, pid: str, who: str = "?") -> dict:
        """The user's click. Everything is re-validated against a FRESH quote before anything is sent."""
        with self.lock:
            p = self.proposals.get(pid)
            if p is None: return {"ok": False, "error": "unknown proposal"}
            if p.status != "pending": return {"ok": False, "error": f"proposal is {p.status}"}
            self.journal.log("USER_ALLOW", id=pid, who=who)
            try:
                now, q = self.feed.now(), self.feed.quote(self.symbol)
                issues = self.mgr.reconcile(now)
                a = p.a
                def fail(msg):
                    p.status, p.detail = "rejected", msg; self.journal.log("ALLOW_REJECTED", id=pid, reason=msg); return {"ok": False, "error": msg}
                if self.state.kill_switch: return fail("kill switch active")
                if p.expires < now: p.status = "expired"; return {"ok": False, "error": "proposal expired - rescan"}
                if q is None: return fail("no live quote")
                if (now - q.ts).total_seconds() > 2 * self.tf and not q.source.startswith("simulated"): return fail("quote stale")
                if q.source.startswith("simulated") and not self.is_paper: return fail("simulated quote on live broker")
                drift = abs(q.mid - a.quote.mid) / a.atr
                if drift > self.s.server.max_drift_atr: return fail(f"price moved {drift:.2f} ATR since proposal (max {self.s.server.max_drift_atr}) - rescan")
                buy = a.decision == "BUY"
                entry = self.spec.round_price(q.ask if buy else q.bid)
                sl, tp = a.plan.stop.price, a.plan.targets.tp2
                if (buy and not (sl < entry < tp)) or (not buy and not (tp < entry < sl)): return fail("price already beyond stop or target")
                q2a = a.q2a
                cost = cost_in_price(self.spec, q.spread, self.s.broker.commission_per_lot, self.s.risk.assumed_slippage, q2a)
                risk = abs(entry - sl)
                net = (abs(tp - entry) - cost) / (risk + cost)
                if net < self.s.risk.min_net_rr: return fail(f"net R:R now {net:.2f} < {self.s.risk.min_net_rr}")
                mult, _ = risk_multiplier(self.s.risk, self.state)
                sz = size_position(self.spec, dataclasses.replace(self.s.risk, risk_per_trade_pct=self.s.risk.risk_per_trade_pct * mult), self.state.equity, entry, sl, q2a, self.s.broker.commission_per_lot, self.s.risk.assumed_slippage)
                if sz.rejected: return fail(sz.rejected)
                req = OrderRequest(pid, self.symbol, a.decision, "market", sz.size, sl, tp, None, sz.risk_amount, a.plan.targets.tp1, 0.5)
                self.approved.add(pid)
                res = self.mgr.execute_request(req, sz, self.spec, q, a.atr, now, True)
                self.approved.discard(pid)
                p.status = "executed" if res.status == "FILLED" else "failed"; p.detail = f"{res.status} {res.detail}"
                return {"ok": res.status == "FILLED", "status": res.status, "detail": res.detail, "fill": res.fill_price,
                        "broker_id": res.broker_id, "size": sz.size, "sl": sl, "tp": tp}
            except Exception as e:
                log.exception("allow failed"); p.status, p.detail = "failed", str(e)
                return {"ok": False, "error": f"unexpected error (no order assumed): {e}"}

    def reject(self, pid: str) -> dict:
        with self.lock:
            p = self.proposals.get(pid)
            if p and p.status == "pending": p.status = "rejected"; self.journal.log("USER_REJECT", id=pid)
            return {"ok": True}

    def kill(self) -> dict:
        with self.lock:
            try: q = self.feed.quote(self.symbol)
            except Exception: q = None
            self.mgr.kill(q, "dashboard")
            for p in self.proposals.values():
                if p.status == "pending": p.status = "rejected"
            return {"ok": True, "kill_switch": True}

    def advance(self, n: int = 1) -> dict:
        if not isinstance(self.feed, CSVFeed): return {"ok": False, "error": "replay only"}
        with self.lock:
            for _ in range(n):
                self.feed.advance(1)
                if self.is_paper: self.broker.on_bar(self.feed.all[self.feed.cursor - 1], self.s.feed.spread)
            self.mgr.reconcile(self.feed.now())
            return {"ok": True, "cursor": self.feed.cursor, "now": self.feed.now().isoformat()}

    def status(self) -> dict:
        with self.lock:
            return {"equity": self.state.equity, "kill": self.state.kill_switch, "mode": self.s.exec.mode.value, "broker": self.broker.name,
                    "feed": self.s.feed.type, "symbol": self.symbol, "positions": self.broker.positions(),
                    "daily_loss_pct": round((self.state.day_start_equity - self.state.equity) / self.state.day_start_equity * 100, 2),
                    "calendar": bool(self.policy), "consecutive_losses": self.state.consecutive_losses,
                    "real_account": bool(getattr(self.broker, "real", False))}
