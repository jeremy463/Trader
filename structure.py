"""Market structure, liquidity, regime (spec 5A, 5C, 5I)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from .data import Candle
from . import indicators as ind


@dataclass(frozen=True)
class Swing:
    idx: int
    price: float
    kind: str  # "H" | "L"


def find_swings(cs: List[Candle], left: int = 3, right: int = 3) -> List[Swing]:
    """Only CONFIRMED swings (need `right` bars after) - no look-ahead."""
    out = []
    for i in range(left, len(cs) - right):
        h, l = cs[i].h, cs[i].l
        if all(h > cs[j].h for j in range(i - left, i)) and all(h >= cs[j].h for j in range(i + 1, i + right + 1)):
            out.append(Swing(i, h, "H"))
        if all(l < cs[j].l for j in range(i - left, i)) and all(l <= cs[j].l for j in range(i + 1, i + right + 1)):
            out.append(Swing(i, l, "L"))
    return sorted(out, key=lambda s: s.idx)


def trend_state(sw: List[Swing]) -> str:
    hs = [s for s in sw if s.kind == "H"][-2:]
    ls = [s for s in sw if s.kind == "L"][-2:]
    if len(hs) < 2 or len(ls) < 2: return "undefined"
    hh, hl = hs[1].price > hs[0].price, ls[1].price > ls[0].price
    lh, ll = hs[1].price < hs[0].price, ls[1].price < ls[0].price
    if hh and hl: return "up"
    if lh and ll: return "down"
    return "range"


def bos_choch(cs: List[Candle], sw: List[Swing], trend: str) -> str:
    if not sw or trend in ("undefined", "range"): return "none"
    c = cs[-1].c
    lh = next((s for s in reversed(sw) if s.kind == "H"), None)
    ll = next((s for s in reversed(sw) if s.kind == "L"), None)
    if trend == "up":
        if ll and c < ll.price: return "CHOCH_bearish"
        if lh and c > lh.price: return "BOS_bullish"
    if trend == "down":
        if lh and c > lh.price: return "CHOCH_bullish"
        if ll and c < ll.price: return "BOS_bearish"
    return "none"


def equal_levels(sw: List[Swing], tol: float) -> Tuple[List[float], List[float]]:
    def grp(kind):
        pts = [s.price for s in sw if s.kind == kind][-8:]
        return [round((a + b) / 2, 6) for i, a in enumerate(pts) for b in pts[i + 1:] if abs(a - b) <= tol]
    return grp("H"), grp("L")


def recent_sweep(cs: List[Candle], sw: List[Swing], lookback: int = 5) -> Optional[str]:
    """Wick through a prior swing then close back inside = liquidity-sweep HYPOTHESIS."""
    n = len(cs)
    for i in range(n - 1, max(n - 1 - lookback, 0), -1):
        for s in reversed(sw):
            if s.idx >= i - 1: continue
            if s.kind == "L" and cs[i].l < s.price < cs[i].c: return "sell_side_swept"
            if s.kind == "H" and cs[i].h > s.price > cs[i].c: return "buy_side_swept"
    return None


def fair_value_gaps(cs: List[Candle], lookback: int = 60):
    out = []
    n = len(cs)
    for i in range(max(2, n - lookback), n):
        a, c = cs[i - 2], cs[i]
        if c.l > a.h: out.append(("bull", a.h, c.l, i))
        if c.h < a.l: out.append(("bear", c.h, a.l, i))
    res = []
    for d, lo, hi, i in out:  # drop filled
        later = cs[i + 1:]
        if d == "bull" and any(x.l <= lo for x in later): continue
        if d == "bear" and any(x.h >= hi for x in later): continue
        res.append((d, lo, hi))
    return res


@dataclass
class Regime:
    labels: List[str] = field(default_factory=list)
    adx: float = float("nan")
    atr_pctile: float = 0.5
    bbw_pctile: float = 0.5
    suitable_for_trend: bool = False


def classify_regime(cs: List[Candle]) -> Regime:
    a, _, _ = ind.adx(cs)
    at = ind.atr(cs)
    close = [c.c for c in cs]
    _, _, _, bw = ind.bollinger(close)
    e50 = ind.ema(close, 50)
    i = len(cs) - 1
    r = Regime(adx=ind.last(a), atr_pctile=ind.percentile_rank(at, i), bbw_pctile=ind.percentile_rank(bw, i))
    slope = (e50[i] - e50[i - 10]) / max(ind.last(at), 1e-12) if not ind.isnan(e50[i - 10]) else 0
    if r.adx >= 28 and slope > 0.5: r.labels.append("strong uptrend")
    elif r.adx >= 28 and slope < -0.5: r.labels.append("strong downtrend")
    elif r.adx >= 20: r.labels.append("weak trend")
    else: r.labels.append("range-bound")
    if r.bbw_pctile < 0.15: r.labels.append("low-volatility compression")
    if r.atr_pctile > 0.9: r.labels.append("high-volatility breakout / disorderly risk")
    if r.atr_pctile > 0.97: r.labels.append("disorderly or illiquid market")
    r.suitable_for_trend = r.adx >= 20 and r.atr_pctile <= 0.9
    return r
