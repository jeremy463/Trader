"""Analysis engine: scanner consensus -> plan -> sizing -> independent risk veto (spec 5-9, 13, 21)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence
from . import indicators as ind
from . import structure as st
from .config import RiskConfig, EventPolicy, ExecConfig
from .data import Candle, Quote, validate_candles, DataQuality
from .instruments import InstrumentSpec, get_spec
from .plan import build_plan, SetupPlan
import dataclasses
from .risk import RiskState, size_position, risk_gate, cost_in_price, SizeResult, RiskDecision, risk_multiplier
from .scanner import scan_chart, compute_htf_bias, ScanConfig, ScanResult, TFView
from .scoring import score, classify, probability, ProbabilityEstimate
from . import quality


@dataclass
class Analysis:
    symbol: str
    spec: InstrumentSpec
    timeframe_s: int
    data_ts: datetime
    price_text: str
    quality: DataQuality
    regime: st.Regime
    tfs: List[TFView]
    decision: str
    classification: str
    order_type: str
    plan: Optional[SetupPlan]
    size: Optional[SizeResult]
    gate: Optional[RiskDecision]
    components: Dict[str, float]
    score: float
    prob: ProbabilityEstimate
    evidence_for: List[str]
    evidence_against: List[str]
    missing: List[str]
    cancel_conditions: List[str]
    trigger: str
    trigger_met: bool
    expiry: str
    hypotheses: Dict[str, str]
    strat_notes: Dict[str, str]
    atr: float
    spread: float
    spread_assumed: bool
    scan: Optional[ScanResult] = None
    quote: Optional[Quote] = None
    mode: str = "ANALYSIS_ONLY"
    exec_status: str = "Analysis only - no order sent"
    equity: float = 0.0
    risk_cfg: Optional[RiskConfig] = None
    q2a: float = 1.0
    commission_per_lot: float = 0.0
    htf_bias: float = 0.0
    grade: Optional[dict] = None


def _insufficient(symbol, spec, tf, cs, q, rc, missing):
    return Analysis(symbol, spec, tf, cs[-1].ts if cs else datetime.now(timezone.utc), "n/a", q, st.Regime(["undefined"]), [],
                    "NO TRADE", "DATA INSUFFICIENT", "n/a", None, None, None, {}, 0.0, probability(None), [], [], missing, [],
                    "n/a", False, "n/a", {}, {}, float("nan"), float("nan"), True, risk_cfg=rc)


def analyze(symbol: str, candles: List[Candle], tf_seconds: int, risk_cfg: Optional[RiskConfig] = None,
            exec_cfg: Optional[ExecConfig] = None, state: Optional[RiskState] = None,
            quote: Optional[Quote] = None, now: Optional[datetime] = None,
            policy: Optional[EventPolicy] = None, q2a: float = 1.0, commission_per_lot: float = 0.0,
            backtest_stats: Optional[dict] = None, htf_factors=(4, 16), enforce_session: bool = True,
            min_bars: int = 250, scan_cfg: Optional[ScanConfig] = None, disabled_methods: Sequence[str] = (),
            allow_simulated_quote: bool = False, spec: Optional[InstrumentSpec] = None,
            method_weights: Optional[dict] = None, market_closed: bool = False) -> Analysis:
    risk_cfg = risk_cfg or RiskConfig()
    exec_cfg = exec_cfg or ExecConfig()
    spec = spec or get_spec(symbol)
    state = state or RiskState.fresh(risk_cfg.equity)
    q = validate_candles(candles, tf_seconds, now, stale_factor=(1e9 if market_closed else 3.0), min_bars=min_bars)
    cs = candles
    missing = list(q.blocking) + [f"warning: {w}" for w in q.warnings]
    if len(cs) < 60: return _insufficient(symbol, spec, tf_seconds, cs, q, risk_cfg, missing)

    ts = cs[-1].ts
    atr_v = ind.last(ind.atr(cs))
    regime = st.classify_regime(cs)
    htf_bias, views, notes = compute_htf_bias(cs, htf_factors)
    missing += notes
    scan = scan_chart(cs, htf_bias, scan_cfg, disabled_methods, method_weights)

    # --- quote handling: never treat a simulated/absent quote as executable unless explicitly allowed ---
    simulated = bool(quote and quote.source.startswith("simulated"))
    spread_assumed = quote is None
    spread = quote.spread if quote else spec.pip_size
    if quote is None: missing.append("no live bid/ask: spread ASSUMED = 1 pip; snapshot only, not executable")
    elif simulated and not allow_simulated_quote: missing.append("quote is SIMULATED: not executable"); spread_assumed = True
    elif now is not None and not market_closed and (now - quote.ts).total_seconds() > 2 * tf_seconds:
        missing.append("quote is stale"); q.blocking.append("stale quote"); q.ok = False
    price = quote.mid if quote else cs[-1].c
    price_text = (f"bid {quote.bid} / ask {quote.ask} ({quote.source})" if quote else f"last close {cs[-1].c} (no live quote)")

    # --- levels / liquidity context ---
    sw = st.find_swings(cs)
    eqh, eql = st.equal_levels(sw, 0.1 * atr_v)
    rng_hi, rng_lo = max(c.h for c in cs[-100:]), min(c.l for c in cs[-100:])
    prem_disc = (price - rng_lo) / (rng_hi - rng_lo) if rng_hi > rng_lo else 0.5

    direction = scan.consensus
    plan = size = gate = None
    decision, order_type, trigger_met, trigger_txt = "NO TRADE", "n/a", False, "no consensus: " + ("; ".join(scan.why_not[:2]) or "n/a")
    ev_for, ev_against = [], []
    cancel = ["thesis invalidated if price closes beyond the stop level", "consensus flips or higher-timeframe bias flips",
              "spread/volatility conditions breach limits", "proposal expires"]
    if direction:
        trigger_met = True
        pl, ps = scan.long_passed, scan.short_passed
        trigger_txt = (f"consensus {direction}: {pl if direction=='BUY' else ps} methods across "
                       f"{len(scan.fam_long if direction=='BUY' else scan.fam_short)} independent families (MET)")
        entry_raw = (quote.ask if direction == "BUY" else quote.bid) if quote else (price + spread / 2 if direction == "BUY" else price - spread / 2)
        entry = spec.round_price(entry_raw)
        order_type = "market"
        cost_p = cost_in_price(spec, spread, commission_per_lot, risk_cfg.assumed_slippage, q2a)
        plan = build_plan(direction, entry, cs, sw, atr_v, spread, cost_p, risk_cfg, spec, eql if direction == "BUY" else eqh)
        if plan.rejected:
            ev_against.append(f"plan rejected: {plan.rejected}")
        else:
            mult, throttle_note = risk_multiplier(risk_cfg, state)
            if throttle_note: ev_against.append(throttle_note)
            size = size_position(spec, dataclasses.replace(risk_cfg, risk_per_trade_pct=risk_cfg.risk_per_trade_pct * mult), state.equity, entry, plan.stop.price, q2a, commission_per_lot, risk_cfg.assumed_slippage)
            gate = risk_gate(spec, direction, size, risk_cfg, state, now or ts, spread, atr_v, policy,
                             exec_cfg.allowed_sessions_utc if enforce_session else None, exec_cfg.allowed_instruments,
                             q.ok, risk_cfg.assumed_slippage)
            decision = direction
            if not gate.approved: ev_against += [f"risk veto: {r}" for r in gate.reasons]
        fam = scan.fam_long if direction == "BUY" else scan.fam_short
        opp = scan.fam_short if direction == "BUY" else scan.fam_long
        ev_for += [f"{s.name}: {s.detail}" for s in scan.signals if s.passed and (s.direction > 0) == (direction == "BUY")]
        ev_against += [f"opposing {s.name}: {s.detail}" for s in scan.signals if s.passed and (s.direction > 0) != (direction == "BUY")]
        ev_against.append("methods within one family are correlated and counted once; confluence is not a probability")
    else:
        near = max(scan.long_passed, scan.short_passed)
        if near >= max(2, scan.n_applicable // 6): decision = "WAIT"
        ev_against += scan.why_not
    if "disorderly" in " ".join(regime.labels): decision = "NO TRADE"; ev_against.append("disorderly/illiquid market: new entries blocked")
    if decision in ("BUY", "SELL") and (spread_assumed or (quote and quote.source.startswith("simulated") and not allow_simulated_quote)):
        decision = "WAIT"; ev_against.append("no executable live quote: cannot issue an executable BUY/SELL")
    if gate and not gate.approved and decision in ("BUY", "SELL"):
        decision = "NO TRADE" if any(w in " ".join(gate.reasons) for w in ("loss limit", "drawdown", "KILL", "consecutive")) else "WAIT"

    # --- score (components 0..1; correlated indicators already collapsed to families) ---
    sg = 1 if direction == "BUY" else -1 if direction == "SELL" else 0
    fam_map = scan.fam_long if direction == "BUY" else scan.fam_short
    n_fam_app = len({s.family for s in scan.signals if s.applicable}) or 1
    comp = {
        "htf_alignment": max(0.0, min(1.0, htf_bias * sg / 2)) if sg else 0.0,
        "structure_quality": fam_map.get("structure", 0.0) if sg else 0.0,
        "trigger_quality": min(1.0, len(fam_map) / 6) if sg else 0.0,
        "location": max(0.0, min(1.0, (1 - prem_disc) if sg > 0 else prem_disc)) if sg else 0.0,
        "liquidity": max(fam_map.get("liquidity", 0.0), fam_map.get("zones", 0.0)) if sg else 0.0,
        "momentum": fam_map.get("momentum", 0.0) if sg else 0.0,
        "volatility_suitability": 1.0 if regime.suitable_for_trend else 0.4,
        "fundamental_context": 0.5,
        "net_reward_risk": min(1.0, max(0.0, plan.net_rr[1] / 3.0)) if plan and plan.net_rr else 0.0,
        "data_and_costs": 1.0 if q.ok and not spread_assumed else 0.5 if q.ok else 0.0,
    }
    sc = score(comp)
    cls = classify(sc, q.ok, bool(plan and not plan.rejected), trigger_met, bool(gate and gate.approved))
    if cls in ("NO TRADE", "DATA INSUFFICIENT") and decision in ("BUY", "SELL", "WAIT") and cls == "DATA INSUFFICIENT": decision = "NO TRADE"
    if cls == "NO TRADE" and decision in ("BUY", "SELL"): decision = "NO TRADE"

    hyp = {"long": f"{scan.long_passed} methods / {len(scan.fam_long)} families", "short": f"{scan.short_passed} methods / {len(scan.fam_short)} families",
           "no-trade": "selected" if not direction else ""}
    notes_d = {"price_action": f"last close {cs[-1].c}; swing trend {views[0].trend}; structure {views[0].structure_event}",
               "liquidity": f"equal highs {eqh[-3:]} equal lows {eql[-3:]} premium/discount={prem_disc:.0%}",
               "indicators": f"ATR {atr_v:.6f} ADX {regime.adx:.1f} regime {', '.join(regime.labels)}",
               "volume": "tick volume only (not consolidated FX volume); no order-flow data",
               "fundamental": "no verified calendar/macro feed connected" if not (policy and policy.events) else f"{len(policy.events)} configured events checked",
               "quant": "no validated statistics: UNVALIDATED" if not backtest_stats else f"backtest status {backtest_stats.get('status')}"}
    if not spec.verified: missing.append("instrument spec not verified against broker (margin, tick value, contract size)")
    missing += ["order-book depth / order flow", "verified economic calendar & macro data"] if not (policy and policy.events) else []
    grade = quality.assess(scan, plan, regime, atr_v, spread, htf_bias, policy, now or ts, spec, risk_cfg.min_net_rr)
    return Analysis(symbol, spec, tf_seconds, ts, price_text, q, regime, views, decision, cls, order_type, plan, size, gate,
                    comp, sc, probability(backtest_stats), ev_for, ev_against, missing, cancel, trigger_txt, trigger_met,
                    "proposal expires in 3 bars or on >0.25 ATR price drift", hyp, notes_d, atr_v, spread, spread_assumed, scan, quote,
                    exec_cfg.mode.value, "Analysis only - no order sent", state.equity, risk_cfg, q2a, commission_per_lot, htf_bias, grade)
