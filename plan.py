"""Trade planning: stop/target candidate generation, comparison table (spec 6-9)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
from .data import Candle
from .structure import Swing


@dataclass
class StopOption:
    method: str
    price: float
    distance: float
    atr_mult: float
    valid: bool
    note: str


@dataclass
class TargetSet:
    method: str
    tp1: float
    tp2: float
    tp3: float
    notes: str


@dataclass
class SetupPlan:
    direction: str
    entry: float
    stop: StopOption
    stops: List[StopOption]
    targets: TargetSet
    gross_rr: List[float]
    net_rr: List[float]
    comparison: List[dict]
    rejected: Optional[str] = None


def stop_candidates(direction, entry, cs: List[Candle], sw: List[Swing], atr_v, buffer, cfg, spec) -> List[StopOption]:
    long = direction == "BUY"
    opts = []
    def add(method, raw, note):
        p = spec.round_price(raw - buffer if long else raw + buffer) if method != "ATR" else spec.round_price(raw)
        d = (entry - p) if long else (p - entry)
        m = d / atr_v if atr_v else 0
        ok = d > 0 and cfg.min_stop_atr <= m <= cfg.max_stop_atr
        why = note + ("" if ok else (" | REJECTED: wrong side" if d <= 0 else
                      " | REJECTED: inside normal volatility noise" if m < cfg.min_stop_atr else " | REJECTED: exceeds max stop distance"))
        opts.append(StopOption(method, p, d, m, ok, why))
    kind = "L" if long else "H"
    below = [s for s in sw if s.kind == kind and ((s.price < entry) if long else (s.price > entry))]
    if below: add("Swing invalidation", below[-1].price, "last confirmed swing beyond entry + buffer")
    rng = cs[-12:]
    add("Range/zone boundary", min(c.l for c in rng) if long else max(c.h for c in rng), "12-bar structural boundary + buffer")
    add("ATR", entry - 2 * atr_v if long else entry + 2 * atr_v, "2.0 x ATR volatility stop")
    return opts


def target_candidates(direction, entry, risk, cs, sw, atr_v, cost, spec, eq_levels=(), min_net_rr=1.5):
    """Staged structural targets: TP1 = first level >=1R, TP2 = first level whose NET R:R clears the minimum,
    TP3 = next level beyond TP2. Projections are used only where NO opposing structure lies ahead."""
    long = direction == "BUY"
    kind = "H" if long else "L"
    cand = [s.price for s in sw if s.kind == kind and ((s.price > entry) if long else (s.price < entry))]
    cand += [p for p in eq_levels if (p > entry if long else p < entry)]
    cand = sorted(set(cand), key=lambda p: abs(p - entry))
    rr = lambda p: abs(p - entry) / risk
    net = lambda p: (abs(p - entry) - cost) / (risk + cost)
    proj = lambda m: spec.round_price(entry + m * risk if long else entry - m * risk)
    if not cand:  # open space: projection only, never assumed reachable
        return TargetSet("R-projection (no structure ahead)", proj(1.5), proj(max(2.0, min_net_rr + 0.25)), proj(3.0),
                         "open space ahead; projection only, price is not assumed to reach it")
    t1 = next((p for p in cand if rr(p) >= 1.0), None)
    t2 = next((p for p in cand if net(p) >= min_net_rr), None)
    if t1 is None or t2 is None:
        far = cand[-1]
        return TargetSet("none", t1 or far, t2 or far, far, "opposing structure leaves insufficient reward; no target forced")
    t3 = next((p for p in cand if abs(p - entry) > abs(t2 - entry) + 0.25 * atr_v), t2)
    return TargetSet("Staged structure/liquidity levels", t1, t2, t3, "targets are prior swing/equal-level liquidity; TP2 is the first level clearing min net R:R")


def build_plan(direction, entry, cs, sw, atr_v, spread, cost_price, cfg, spec, eq_levels=()) -> SetupPlan:
    buffer = max(1.5 * spread, 0.1 * atr_v)
    opts = stop_candidates(direction, entry, cs, sw, atr_v, buffer, cfg, spec)
    valid = [o for o in opts if o.valid]
    structural = [o for o in valid if o.method != "ATR"]
    chosen = (structural[0] if structural else (valid[0] if valid else opts[-1]))
    if not valid:
        return SetupPlan(direction, entry, chosen, opts, TargetSet("n/a", 0, 0, 0, ""), [], [], [], "no valid stop (all candidates rejected)")
    risk = chosen.distance
    comp = []
    best = None
    for o in valid:
        t = target_candidates(direction, entry, o.distance, cs, sw, atr_v, cost_price, spec, eq_levels, cfg.min_net_rr)
        g = [abs(p - entry) / o.distance for p in (t.tp1, t.tp2, t.tp3)]
        row = {"Method": o.method, "Entry": entry, "Stop-loss": o.price, "TP1": t.tp1, "TP2": t.tp2, "TP3": t.tp3,
               "Gross R:R (TP2)": round(g[1], 2), "Main weakness": o.note.split(" | ")[0] + "; " + t.notes}
        comp.append(row)
        if o is chosen: best = (t, g)
    t, g = best
    net = [(abs(p - entry) - cost_price) / (risk + cost_price) for p in (t.tp1, t.tp2, t.tp3)]
    rej = None
    if t.method == "none": rej = "opposing structure leaves insufficient reward (no target forced)"
    elif net[1] < cfg.min_net_rr: rej = f"net R:R on TP2 {net[1]:.2f} < required {cfg.min_net_rr}"
    return SetupPlan(direction, entry, chosen, opts, t, [round(x, 2) for x in g], [round(x, 2) for x in net], comp, rej)
