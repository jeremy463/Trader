"""Transparent setup checklist and grade. A grade is a count of satisfied, explicit criteria. It is NOT a probability of profit."""
from __future__ import annotations
from typing import List, Optional
from .risk import event_blocked
from .scanner import TREND_NAMES, MEANREV_NAMES

GRADE_RANK = {"A": 3, "B": 2, "C": 1}


def _item(key, label, ok, detail, critical=False): return {"key": key, "label": label, "ok": ok, "detail": detail, "critical": critical}


def grade_of(items: List[dict]) -> dict:
    app = [i for i in items if i["ok"] is not None]; fails = [i for i in app if not i["ok"]]
    if not app: return {"grade": None, "passed": 0, "total": 0, "items": items}
    g = "C" if any(i["critical"] for i in fails) else "A" if not fails else "B" if len(fails) <= 2 else "C"
    return {"grade": g, "passed": len(app) - len(fails), "total": len(app), "items": items}


def assess(scan, plan, regime, atr_v: float, spread: float, htf_bias: float, policy, now, spec, min_net_rr: float) -> Optional[dict]:
    d = scan.consensus
    if d is None: return None
    sg = 1 if d == "BUY" else -1
    fam, opp = (scan.fam_long, scan.fam_short) if sg > 0 else (scan.fam_short, scan.fam_long)
    ext = scan.regime.get("ext_atr", 0.0) * sg
    passed = [s for s in scan.signals if s.passed and (s.direction > 0) == (sg > 0)]
    tr = sum(1 for s in passed if s.name in TREND_NAMES); mr = sum(1 for s in passed if s.name in MEANREV_NAMES)
    trending, ranging = scan.regime.get("trending"), scan.regime.get("ranging")
    fit = not ((tr > mr and ranging and not trending) or (mr > tr and trending))
    it = [_item("consensus", "Multi-method consensus reached", True, f"{len(passed)} methods across {len(fam)} independent families", True),
          _item("families", "At least 5 independent families agree", len(fam) >= 5, f"{len(fam)} families"),
          _item("opposition", "Little opposing evidence", len(opp) <= 1, f"{len(opp)} opposing families"),
          _item("htf", "Higher-timeframe bias supports the trade", htf_bias * sg >= 0.5, f"bias {htf_bias:+.1f}", htf_bias * sg <= -1.0),
          _item("regime", "Method types fit the market regime", fit, f"{'trending' if trending else 'ranging' if ranging else 'mixed'}; {tr} trend-type vs {mr} reversal-type signals"),
          _item("extension", "Entry is not chasing an extended move", ext <= 2.0, f"{ext:+.1f} ATR from EMA21 in trade direction")]
    if plan is not None and not plan.rejected:
        it += [_item("rr", f"Net reward:risk at least {min_net_rr:g}", plan.net_rr[1] >= min_net_rr, f"{plan.net_rr[1]} after costs", True),
               _item("stop", "Stop distance is sensible (<= 3 ATR)", plan.stop.atr_mult <= 3.0, f"{plan.stop.atr_mult:.2f} ATR"),
               _item("cost", "Spread is small relative to the stop", (spread / plan.stop.distance) <= 0.15 if plan.stop.distance else False, f"{spread / plan.stop.distance:.0%} of stop" if plan.stop.distance else "n/a")]
    else:
        it.append(_item("rr", f"Net reward:risk at least {min_net_rr:g}", False, plan.rejected if plan is not None else "no valid plan", True))
    it.append(_item("volatility", "Volatility in a normal band", 0.1 <= regime.atr_pctile <= 0.9, f"ATR percentile {regime.atr_pctile:.0%}"))
    if spec.asset_class == "forex": it.append(_item("session", "Liquid session (06-20 UTC)", 6 <= now.hour < 20, f"{now.hour:02d}:00 UTC"))
    if policy is not None:
        b = event_blocked(now, policy, spec); it.append(_item("news", "No high-impact news nearby", b is None, b or "clear", True))
    else: it.append(_item("news", "No high-impact news nearby", None, "no calendar loaded - cannot check"))
    return grade_of(it)


def add_evidence(g: Optional[dict], evidence: Optional[dict]) -> Optional[dict]:
    """Fold historical evidence into the checklist: a proven-negative signal type is a critical failure; no evidence is unknown, never a pass."""
    if g is None: return None
    v = (evidence or {}).get("verdict")
    items = [i for i in g["items"] if i["key"] != "evidence"]
    items.append(_item("evidence", "Historical evidence does not contradict it", None if v in (None, "INSUFFICIENT DATA") else v != "NEGATIVE EDGE",
                       "no validation run yet" if v is None else v.lower(), v == "NEGATIVE EDGE"))
    return grade_of(items)


def add_item(g: Optional[dict], item: dict) -> Optional[dict]:
    if g is None: return None
    return grade_of([i for i in g["items"] if i["key"] != item["key"]] + [item])


def news_item(bias: Optional[float], sg: int) -> dict:
    """Headline tone is a weak keyword heuristic: it may support or contradict, but never decides alone (non-critical, unknown when neutral)."""
    if bias is None: return _item("news_tone", "Recent headline tone does not oppose it", None, "no recent tagged headlines")
    if abs(bias) < 0.35: return _item("news_tone", "Recent headline tone does not oppose it", None, f"neutral tone ({bias:+.2f}; keyword heuristic)")
    return _item("news_tone", "Recent headline tone does not oppose it", bias * sg > 0, f"tone {bias:+.2f} for the pair (keyword heuristic, low reliability)")
