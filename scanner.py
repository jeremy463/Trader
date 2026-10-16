"""Multi-method chart scanner: 27 analysis methods in 12 independence families (spec 5A-5I).
A method 'passes' when it gives a directional signal with strength >= PASS_STRENGTH.
Consensus counts INDEPENDENT FAMILIES, not raw indicator votes (correlated indicators are not independent confirmation)."""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from . import indicators as ind
from . import structure as st
from .data import Candle, resample

nan = float("nan")
isnan = math.isnan
PASS_STRENGTH = 0.5


@dataclass
class Signal:
    name: str
    family: str
    direction: int          # +1 long, -1 short, 0 neutral
    strength: float
    detail: str
    applicable: bool = True
    error: bool = False

    @property
    def passed(self) -> bool:
        return self.applicable and self.direction != 0 and self.strength >= PASS_STRENGTH


@dataclass
class ScanConfig:
    min_methods: int = 8
    min_families: int = 4
    dominance: float = 2.0
    max_opposing_families: int = 2
    require_htf_alignment: bool = True      # do not trade against a strongly opposed higher-timeframe bias
    max_extension_atr: float = 3.0           # do not chase: price already this far from EMA21 in the signal direction
    counter_regime_penalty: float = 0.75


@dataclass
class TFView:
    name: str
    trend: str
    ema_state: str
    structure_event: str


def tf_view(name: str, cs: List[Candle]):
    close = [c.c for c in cs]
    sw = st.find_swings(cs)
    tr = st.trend_state(sw)
    es = "bullish (EMA21>EMA50)" if ind.last(ind.ema(close, 21)) > ind.last(ind.ema(close, 50)) else "bearish (EMA21<EMA50)"
    return TFView(name, tr, es, st.bos_choch(cs, sw, tr)), sw


def view_bias(v: TFView) -> int:
    return (1 if v.trend == "up" else -1 if v.trend == "down" else 0) + (1 if v.ema_state.startswith("bullish") else -1)


def compute_htf_bias(cs: List[Candle], factors=(4, 16)):
    ex, _ = tf_view("execution", cs)
    views, notes = [ex], []
    for f, nm in zip(factors, ("intermediate", "higher")):
        h = resample(cs, f)
        if len(h) >= 60:
            views.append(tf_view(f"{nm} (x{f}, derived by resampling)", h)[0])
        else:
            notes.append(f"{nm}: insufficient resampled bars")
    vals = [view_bias(v) for v in views[1:]] or [view_bias(ex)]
    return sum(vals) / len(vals), views, notes


def _choppiness(cs, n=14):
    if len(cs) < n + 1: return 50.0
    tr = ind.true_range(cs)[-n:]; hh, ll = max(c.h for c in cs[-n:]), min(c.l for c in cs[-n:])
    return 100 * math.log10(max(sum(tr), 1e-12) / max(hh - ll, 1e-12)) / math.log10(n)


def _variance_ratio(c, q=4, n=200):
    r1 = [c[i] - c[i - 1] for i in range(max(1, len(c) - n), len(c))]
    rq = [c[i] - c[i - q] for i in range(max(q, len(c) - n), len(c))]
    if len(r1) < 30: return 1.0
    v1 = sum(v * v for v in r1) / len(r1); vq = sum(v * v for v in rq) / len(rq)
    return vq / (q * v1) if v1 else 1.0


class Ctx:
    def __init__(self, cs: List[Candle], htf_bias: float = 0.0):
        self.cs, self.n = cs, len(cs)
        self.o, self.h, self.l = [c.o for c in cs], [c.h for c in cs], [c.l for c in cs]
        self.c, self.v = [c.c for c in cs], [c.v for c in cs]
        self.atr_s = ind.atr(cs)
        self.a = max(ind.last(self.atr_s), 1e-12)
        self.e20, self.e21 = ind.ema(self.c, 20), ind.ema(self.c, 21)
        self.e50, self.e200 = ind.ema(self.c, 50), ind.ema(self.c, 200)
        self.rsi = ind.rsi(self.c)
        self.macd = ind.macd(self.c)
        self.bb = ind.bollinger(self.c)
        self.adx, self.pdi, self.mdi = ind.adx(cs)
        self.sw = st.find_swings(cs)
        self.trend = st.trend_state(self.sw)
        self.htf_bias = htf_bias
        self.regime = st.classify_regime(cs)
        self.hasvol = sum(self.v[-50:]) > 0
        self.adx_v = ind.last(self.adx)
        self.chop = _choppiness(cs)
        self.vr = _variance_ratio(self.c)
        self.trending = self.adx_v >= 20 and self.chop < 61.8 and self.vr >= 0.95
        self.ranging = (self.adx_v < 25 and self.chop >= 50) or self.vr < 0.9

    def bull(self, i=-1): return self.c[i] > self.o[i]
    def bear(self, i=-1): return self.c[i] < self.o[i]


def _S(name, fam, d, s, detail): return Signal(name, fam, d, round(max(0.0, min(1.0, s)), 3), detail)
def _N(name, fam="", detail="no signal"): return Signal(name, fam, 0, 0.0, detail)
def _NA(name, fam, detail): return Signal(name, fam, 0, 0.0, detail, applicable=False)


def near_support(x: Ctx, d: int) -> bool:
    kind = "L" if d > 0 else "H"
    price = x.l[-1] if d > 0 else x.h[-1]
    for s in x.sw:
        if s.kind == kind and s.idx >= x.n - 120 and abs(price - s.price) <= 1.0 * x.a: return True
    e = x.e50[-1]
    return not isnan(e) and abs(price - e) <= 0.5 * x.a


# ---------------- TREND ----------------
def m_ema_trend(x):
    e21, e50, e200, p = x.e21[-1], x.e50[-1], x.e200[-1], x.c[-1]
    slope = (x.e50[-1] - x.e50[-6]) / x.a
    if isnan(e200): up, dn = e21 > e50 and p > e50, e21 < e50 and p < e50
    else: up, dn = e21 > e50 > e200 and p > e50, e21 < e50 < e200 and p < e50
    if up and slope > 0: return _S("EMA trend stack", "trend", 1, 0.5 + min(0.5, abs(slope)), f"EMA21>50{'>200' if not isnan(e200) else ''}, slope {slope:+.2f}ATR")
    if dn and slope < 0: return _S("EMA trend stack", "trend", -1, 0.5 + min(0.5, abs(slope)), f"EMA21<50{'<200' if not isnan(e200) else ''}, slope {slope:+.2f}ATR")
    return _N("EMA trend stack", "trend", "EMAs not aligned")


def m_supertrend(x):
    atr10 = ind.atr(x.cs, 10); n = x.n
    fu, fl, d, dirs = [nan] * n, [nan] * n, 1, [0] * n
    for i in range(10, n):
        hl2 = (x.h[i] + x.l[i]) / 2; ub, lb = hl2 + 3 * atr10[i], hl2 - 3 * atr10[i]
        if i == 10: fu[i], fl[i] = ub, lb
        else:
            fu[i] = ub if ub < fu[i - 1] or x.c[i - 1] > fu[i - 1] else fu[i - 1]
            fl[i] = lb if lb > fl[i - 1] or x.c[i - 1] < fl[i - 1] else fl[i - 1]
            if d == 1 and x.c[i] < fl[i]: d = -1
            elif d == -1 and x.c[i] > fu[i]: d = 1
        dirs[i] = d
    flip = next((k for k in range(1, 8) if dirs[-1 - k] != dirs[-1]), None)
    s = 0.55 + (0.2 if flip else 0) + (0.15 if x.adx_v > 20 else 0)
    return _S("Supertrend(10,3)", "trend", dirs[-1], s, f"direction {'up' if dirs[-1]>0 else 'down'}{', fresh flip' if flip else ''}")


def m_ichimoku(x):
    if x.n < 80: return _NA("Ichimoku cloud", "trend", "needs 80 bars")
    def mid(a, b, i): return (max(x.h[i - a + 1:i + 1]) + min(x.l[i - a + 1:i + 1])) / 2
    t, k = mid(9, 0, x.n - 1), mid(26, 0, x.n - 1)
    j = x.n - 27
    sa, sb = (mid(9, 0, j) + mid(26, 0, j)) / 2, mid(52, 0, j)
    top, bot, p = max(sa, sb), min(sa, sb), x.c[-1]
    if p > top and t > k and p > k: return _S("Ichimoku cloud", "trend", 1, 0.6 + (0.2 if sa > sb else 0), "price above cloud, Tenkan>Kijun")
    if p < bot and t < k and p < k: return _S("Ichimoku cloud", "trend", -1, 0.6 + (0.2 if sa < sb else 0), "price below cloud, Tenkan<Kijun")
    return _N("Ichimoku cloud", "trend", "price inside cloud or mixed")


def m_adx_di(x):
    a, p, m = x.adx, x.pdi[-1], x.mdi[-1]
    if isnan(x.adx_v) or isnan(a[-4]): return _NA("ADX/DI", "trend", "warm-up")
    if x.adx_v >= 22 and x.adx_v > a[-4]:
        s = 0.5 + min(0.5, (x.adx_v - 22) / 30)
        if p > m: return _S("ADX/DI", "trend", 1, s, f"ADX {x.adx_v:.1f} rising, +DI>-DI")
        if m > p: return _S("ADX/DI", "trend", -1, s, f"ADX {x.adx_v:.1f} rising, -DI>+DI")
    return _N("ADX/DI", "trend", f"ADX {x.adx_v:.1f} weak/falling")


def m_psar(x):
    h, l, n = x.h, x.l, x.n
    bull, sar, ep, af, dirs = True, l[0], h[0], 0.02, [1]
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if bull:
            sar = min(sar, l[i - 1], l[i - 2] if i > 1 else l[i - 1])
            if l[i] < sar: bull, sar, ep, af = False, ep, l[i], 0.02
            elif h[i] > ep: ep, af = h[i], min(af + 0.02, 0.2)
        else:
            sar = max(sar, h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > sar: bull, sar, ep, af = True, ep, h[i], 0.02
            elif l[i] < ep: ep, af = l[i], min(af + 0.02, 0.2)
        dirs.append(1 if bull else -1)
    fresh = any(dirs[-1 - k] != dirs[-1] for k in range(1, 4))
    ok = (x.c[-1] > x.e50[-1]) == (dirs[-1] > 0)
    return _S("Parabolic SAR", "trend", dirs[-1], 0.5 + (0.2 if fresh else 0) + (0.1 if ok else -0.2), f"SAR {'below' if dirs[-1]>0 else 'above'} price{', fresh flip' if fresh else ''}")


def m_trend_pullback(x):
    e21, e50, a, rs = x.e21[-1], x.e50[-1], x.a, x.rsi[-1]
    for d in (1, -1):
        near = abs(x.c[-1] - e21) <= a or any((x.l[-k] <= e21) if d > 0 else (x.h[-k] >= e21) for k in range(1, 6))
        trend_ok = (e21 > e50 and x.htf_bias >= 0.5 and rs < 68) if d > 0 else (e21 < e50 and x.htf_bias <= -0.5 and rs > 32)
        trig = (x.c[-1] > x.h[-2]) if d > 0 else (x.c[-1] < x.l[-2])
        if trend_ok and near and trig: return _S("Trend pullback + trigger", "trend", d, 0.65 + min(0.3, abs(x.htf_bias) * 0.1), "pullback to EMA21 with break of prior bar")
    return _N("Trend pullback + trigger", "trend", "no aligned pullback trigger")


# ---------------- MOMENTUM ----------------
def m_macd(x):
    line, sig, hist = x.macd
    if isnan(hist[-1]) or isnan(hist[-4]): return _NA("MACD", "momentum", "warm-up")
    cross_up = any(line[-k - 1] <= sig[-k - 1] and line[-k] > sig[-k] for k in range(1, 4))
    cross_dn = any(line[-k - 1] >= sig[-k - 1] and line[-k] < sig[-k] for k in range(1, 4))
    if line[-1] > sig[-1] and hist[-1] > hist[-2]: return _S("MACD", "momentum", 1, 0.55 + (0.25 if cross_up else 0) + (0.1 if line[-1] > 0 else 0), "line>signal, histogram rising")
    if line[-1] < sig[-1] and hist[-1] < hist[-2]: return _S("MACD", "momentum", -1, 0.55 + (0.25 if cross_dn else 0) + (0.1 if line[-1] < 0 else 0), "line<signal, histogram falling")
    return _N("MACD", "momentum")


def m_roc(x):
    if x.n < 110: return _NA("Time-series momentum", "momentum", "needs 110 bars")
    r20, r100 = (x.c[-1] - x.c[-21]) / x.a, (x.c[-1] - x.c[-101]) / x.a
    if r20 > 1.5 and r100 > 0 and x.rsi[-1] < 80: return _S("Time-series momentum", "momentum", 1, 0.5 + min(0.5, (r20 - 1.5) / 6), f"ROC20 {r20:+.1f}ATR, ROC100>0")
    if r20 < -1.5 and r100 < 0 and x.rsi[-1] > 20: return _S("Time-series momentum", "momentum", -1, 0.5 + min(0.5, (-r20 - 1.5) / 6), f"ROC20 {r20:+.1f}ATR, ROC100<0")
    return _N("Time-series momentum")


def m_stoch(x):
    k_, d_, sm = 14, 3, 3
    def raw(i):
        hh, ll = max(x.h[i - k_ + 1:i + 1]), min(x.l[i - k_ + 1:i + 1])
        return 100 * (x.c[i] - ll) / (hh - ll) if hh > ll else 50.0
    r = [raw(x.n - 1 - j) for j in range(12)][::-1]
    K = [sum(r[i - sm + 1:i + 1]) / sm for i in range(sm - 1, len(r))]
    D = [sum(K[i - d_ + 1:i + 1]) / d_ for i in range(d_ - 1, len(K))]
    K = K[-len(D):]
    up = K[-1] > D[-1] and any(K[-j - 1] <= D[-j - 1] for j in range(1, 3)) and min(K[-4:]) < 30
    dn = K[-1] < D[-1] and any(K[-j - 1] >= D[-j - 1] for j in range(1, 3)) and max(K[-4:]) > 70
    if up: return _S("Stochastic cross", "momentum", 1, 0.6 + (30 - min(K[-4:])) / 100, f"%K crossed up from oversold ({min(K[-4:]):.0f})")
    if dn: return _S("Stochastic cross", "momentum", -1, 0.6 + (max(K[-4:]) - 70) / 100, f"%K crossed down from overbought ({max(K[-4:]):.0f})")
    return _N("Stochastic cross")


def m_rsi_div(x):
    ls = [s for s in x.sw if s.kind == "L" and s.idx >= x.n - 70][-2:]
    hs = [s for s in x.sw if s.kind == "H" and s.idx >= x.n - 70][-2:]
    if len(ls) == 2 and ls[1].idx - ls[0].idx >= 5 and ls[1].price < ls[0].price and x.rsi[ls[1].idx] > x.rsi[ls[0].idx] + 2 and x.bull() and x.c[-1] > x.h[-2]:
        return _S("RSI divergence", "momentum", 1, 0.68, "bullish divergence + confirmation bar")
    if len(hs) == 2 and hs[1].idx - hs[0].idx >= 5 and hs[1].price > hs[0].price and x.rsi[hs[1].idx] < x.rsi[hs[0].idx] - 2 and x.bear() and x.c[-1] < x.l[-2]:
        return _S("RSI divergence", "momentum", -1, 0.68, "bearish divergence + confirmation bar")
    return _N("RSI divergence")


# ---------------- MEAN REVERSION / QUANT ----------------
def m_rsi_rev(x):
    if x.adx_v >= 30: return _N("RSI extreme reversal", "mean_reversion", f"ADX {x.adx_v:.0f}: trending, fade disabled")
    r = x.rsi
    if min(r[-4:-1]) < 30 and r[-1] > r[-2]: return _S("RSI extreme reversal", "mean_reversion", 1, 0.6 + (30 - min(r[-4:-1])) / 50, "oversold turning up in non-trending market")
    if max(r[-4:-1]) > 70 and r[-1] < r[-2]: return _S("RSI extreme reversal", "mean_reversion", -1, 0.6 + (max(r[-4:-1]) - 70) / 50, "overbought turning down in non-trending market")
    return _N("RSI extreme reversal")


def m_bb_rev(x):
    mid, up, lo, bw = x.bb
    if x.adx_v >= 25: return _N("Bollinger reversion", "mean_reversion", "trending: fade disabled")
    if x.c[-2] < lo[-2] and x.c[-1] > lo[-1]: return _S("Bollinger reversion", "mean_reversion", 1, 0.65 + (0.15 if x.rsi[-1] < 40 else 0), "re-entered lower band")
    if x.c[-2] > up[-2] and x.c[-1] < up[-1]: return _S("Bollinger reversion", "mean_reversion", -1, 0.65 + (0.15 if x.rsi[-1] > 60 else 0), "re-entered upper band")
    return _N("Bollinger reversion")


def m_zscore(x):
    w = x.c[-50:]; m = sum(w) / 50; sd = math.sqrt(sum((v - m) ** 2 for v in w) / 50) or 1e-12
    z, zp = (x.c[-1] - m) / sd, (x.c[-2] - m) / sd
    if x.adx_v >= 25: return _N("Z-score reversion", "quant", f"z={z:.2f}; trending: fade disabled")
    if z < -2 and z > zp: return _S("Z-score reversion", "quant", 1, 0.6 + min(0.3, (-z - 2) / 2), f"z={z:.2f} rising")
    if z > 2 and z < zp: return _S("Z-score reversion", "quant", -1, 0.6 + min(0.3, (z - 2) / 2), f"z={z:.2f} falling")
    return _N("Z-score reversion", "quant", f"z={z:.2f}")


def m_linreg(x):
    n = 60; y = x.c[-n:]; xs = list(range(n)); mx, my = (n - 1) / 2, sum(y) / n
    sxx = sum((i - mx) ** 2 for i in xs); sxy = sum((i - mx) * (v - my) for i, v in zip(xs, y))
    b = sxy / sxx; a0 = my - b * mx
    ssr = sum((v - (a0 + b * i)) ** 2 for i, v in zip(xs, y)); sst = sum((v - my) ** 2 for v in y) or 1e-12
    r2 = 1 - ssr / sst; sd = math.sqrt(ssr / n) or 1e-12; resid_z = (y[-1] - (a0 + b * (n - 1))) / sd
    sn = b * n / x.a
    if r2 >= 0.6 and abs(sn) >= 1.5:
        s = 0.5 + min(0.3, r2 - 0.6) + (0.1 if abs(resid_z) < 1.5 else -0.15)
        return _S("Regression channel trend", "quant", 1 if b > 0 else -1, s, f"R2 {r2:.2f}, slope {sn:+.1f}ATR/60bars, resid z {resid_z:+.1f}")
    return _N("Regression channel trend", "quant", f"R2 {r2:.2f} too low / flat")


# ---------------- BREAKOUT / VOLATILITY ----------------
def m_donchian(x):
    ub, lb = max(x.h[-21:-1]), min(x.l[-21:-1]); p = x.c[-1]
    if p > ub: return _S("Donchian 20 breakout", "breakout", 1, 0.55 + min(0.4, (p - ub) / x.a), f"close above 20-bar high {ub:.5g}")
    if p < lb: return _S("Donchian 20 breakout", "breakout", -1, 0.55 + min(0.4, (lb - p) / x.a), f"close below 20-bar low {lb:.5g}")
    return _N("Donchian 20 breakout")


def m_bb_squeeze(x):
    mid, up, lo, bw = x.bb
    if ind.percentile_rank(bw, x.n - 4) > 0.25: return _N("Bollinger squeeze breakout", "volatility", "no prior squeeze")
    if x.c[-1] > up[-1]: return _S("Bollinger squeeze breakout", "volatility", 1, 0.62 + (0.15 if x.v[-1] > 1.2 * (sum(x.v[-21:-1]) / 20 or 1e9) else 0), "upside break after squeeze")
    if x.c[-1] < lo[-1]: return _S("Bollinger squeeze breakout", "volatility", -1, 0.62 + (0.15 if x.v[-1] > 1.2 * (sum(x.v[-21:-1]) / 20 or 1e9) else 0), "downside break after squeeze")
    return _N("Bollinger squeeze breakout")


def m_keltner_squeeze(x):
    mid, up, lo, bw = x.bb
    def sq(i): return up[i] < x.e20[i] + 1.5 * x.atr_s[i] and lo[i] > x.e20[i] - 1.5 * x.atr_s[i]
    if sq(-1) or not any(sq(-k) for k in range(2, 7)): return _N("TTM squeeze release", "volatility", "no squeeze release")
    dm = (max(x.h[-20:]) + min(x.l[-20:])) / 2; mom = x.c[-1] - (dm + x.e20[-1]) / 2
    return _S("TTM squeeze release", "volatility", 1 if mom > 0 else -1, 0.65, f"squeeze released, momentum {'positive' if mom>0 else 'negative'}")


# ---------------- STRUCTURE / LIQUIDITY / ZONES ----------------
def m_structure(x):
    ev = st.bos_choch(x.cs, x.sw, x.trend)
    if ev.endswith("bullish"): return _S("Market structure BOS/CHOCH", "structure", 1, (0.7 if ev.startswith("CHOCH") else 0.6) + (0.1 if x.htf_bias > 0 else 0), ev)
    if ev.endswith("bearish"): return _S("Market structure BOS/CHOCH", "structure", -1, (0.7 if ev.startswith("CHOCH") else 0.6) + (0.1 if x.htf_bias < 0 else 0), ev)
    return _N("Market structure BOS/CHOCH", "structure", f"trend {x.trend}, no break")


def m_sweep(x):
    sw = st.recent_sweep(x.cs, x.sw, 3)
    if sw == "sell_side_swept" and x.bull() and x.c[-1] > x.c[-2]: return _S("Liquidity sweep reversal", "liquidity", 1, 0.7, "sell-side liquidity swept, bullish close (hypothesis)")
    if sw == "buy_side_swept" and x.bear() and x.c[-1] < x.c[-2]: return _S("Liquidity sweep reversal", "liquidity", -1, 0.7, "buy-side liquidity swept, bearish close (hypothesis)")
    return _N("Liquidity sweep reversal")


def m_fvg(x):
    for d, lo, hi in reversed(st.fair_value_gaps(x.cs)):
        inside = x.l[-1] <= hi and x.c[-1] >= lo if d == "bull" else x.h[-1] >= lo and x.c[-1] <= hi
        if d == "bull" and inside and x.bull() and x.htf_bias >= 0: return _S("Fair value gap retest", "zones", 1, 0.6, f"retest of bullish FVG {lo:.5g}-{hi:.5g}")
        if d == "bear" and inside and x.bear() and x.htf_bias <= 0: return _S("Fair value gap retest", "zones", -1, 0.6, f"retest of bearish FVG {lo:.5g}-{hi:.5g}")
    return _N("Fair value gap retest")


def m_order_block(x):
    for k in range(x.n - 2, max(x.n - 35, 5), -1):
        body = abs(x.c[k] - x.o[k])
        if body < 1.5 * x.a: continue
        d = 1 if x.c[k] > x.o[k] else -1
        ob = next((j for j in range(k - 1, k - 4, -1) if (x.c[j] < x.o[j]) == (d > 0)), None)
        if ob is None: continue
        lo, hi = x.l[ob], x.h[ob]
        broken = any((x.c[j] < lo) if d > 0 else (x.c[j] > hi) for j in range(k + 1, x.n))
        retest = x.l[-1] <= hi and x.c[-1] >= lo if d > 0 else x.h[-1] >= lo and x.c[-1] <= hi
        if not broken and retest and k < x.n - 2 and ((d > 0 and x.bull()) or (d < 0 and x.bear())):
            return _S("Order block retest", "zones", d, 0.6, f"retest of {'bullish' if d>0 else 'bearish'} OB {lo:.5g}-{hi:.5g} (hypothesis)")
        break
    return _N("Order block retest")


# ---------------- PATTERNS ----------------
def detect_candle(cs: List[Candle]) -> Tuple[int, str, float]:
    a, b, c = cs[-3], cs[-2], cs[-1]
    body = lambda k: abs(k.c - k.o); rng = lambda k: max(k.h - k.l, 1e-12)
    if b.c < b.o and c.c > c.o and c.o <= b.c and c.c >= b.o: return 1, "bullish engulfing", 0.6
    if b.c > b.o and c.c < c.o and c.o >= b.c and c.c <= b.o: return -1, "bearish engulfing", 0.6
    lw, uw = min(c.o, c.c) - c.l, c.h - max(c.o, c.c)
    if body(c) > 0 and lw >= 2 * body(c) and uw <= 0.6 * body(c) and (c.c - c.l) / rng(c) > 0.6: return 1, "hammer/bullish pin bar", 0.55
    if body(c) > 0 and uw >= 2 * body(c) and lw <= 0.6 * body(c) and (c.h - c.c) / rng(c) > 0.6: return -1, "shooting star/bearish pin bar", 0.55
    if a.c < a.o and body(a) > 0.5 * rng(a) and body(b) < 0.3 * rng(b) and c.c > c.o and c.c > (a.o + a.c) / 2: return 1, "morning star", 0.65
    if a.c > a.o and body(a) > 0.5 * rng(a) and body(b) < 0.3 * rng(b) and c.c < c.o and c.c < (a.o + a.c) / 2: return -1, "evening star", 0.65
    if b.h < a.h and b.l > a.l and c.c > a.h: return 1, "inside-bar breakout up", 0.58
    if b.h < a.h and b.l > a.l and c.c < a.l: return -1, "inside-bar breakout down", 0.58
    return 0, "", 0.0


def m_candles(x):
    d, nm, s = detect_candle(x.cs)
    if not d: return _N("Candlestick at level", "pattern_candle")
    loc = near_support(x, d)
    return _S("Candlestick at level", "pattern_candle", d, s + (0.1 if loc else -0.2), f"{nm}{' at support/resistance' if loc else ' (no key level nearby - downgraded)'}")


def m_chart_pattern(x):
    sw = [s for s in x.sw if s.idx >= x.n - 150]
    hs, ls, a, c = [s for s in sw if s.kind == "H"], [s for s in sw if s.kind == "L"], x.a, x.c
    up = lambda lv: c[-1] > lv and any(c[-k] <= lv for k in range(2, 6))
    dn = lambda lv: c[-1] < lv and any(c[-k] >= lv for k in range(2, 6))
    if len(ls) >= 2 and ls[-1].idx - ls[-2].idx >= 8 and abs(ls[-1].price - ls[-2].price) <= 0.4 * a:
        neck = max(x.h[ls[-2].idx:ls[-1].idx + 1])
        if up(neck): return _S("Chart pattern", "pattern", 1, 0.7, f"double bottom, neckline {neck:.5g} broken")
    if len(hs) >= 2 and hs[-1].idx - hs[-2].idx >= 8 and abs(hs[-1].price - hs[-2].price) <= 0.4 * a:
        neck = min(x.l[hs[-2].idx:hs[-1].idx + 1])
        if dn(neck): return _S("Chart pattern", "pattern", -1, 0.7, f"double top, neckline {neck:.5g} broken")
    if len(hs) >= 3:
        h1, h2, h3 = hs[-3:]
        if h2.price > h1.price and h2.price > h3.price and abs(h1.price - h3.price) <= 0.8 * a:
            neck = min(x.l[h1.idx:h3.idx + 1])
            if dn(neck): return _S("Chart pattern", "pattern", -1, 0.72, f"head & shoulders, neckline {neck:.5g} broken")
    if len(ls) >= 3:
        l1, l2, l3 = ls[-3:]
        if l2.price < l1.price and l2.price < l3.price and abs(l1.price - l3.price) <= 0.8 * a:
            neck = max(x.h[l1.idx:l3.idx + 1])
            if up(neck): return _S("Chart pattern", "pattern", 1, 0.72, f"inverse head & shoulders, neckline {neck:.5g} broken")
    if len(hs) >= 3 and len(ls) >= 3:
        H, L = [s.price for s in hs[-3:]], [s.price for s in ls[-3:]]
        if max(H) - min(H) <= 0.4 * a and L[1] > L[0] + 0.1 * a and L[2] > L[1] + 0.1 * a and up(sum(H) / 3):
            return _S("Chart pattern", "pattern", 1, 0.65, "ascending triangle breakout")
        if max(L) - min(L) <= 0.4 * a and H[1] < H[0] - 0.1 * a and H[2] < H[1] - 0.1 * a and dn(sum(L) / 3):
            return _S("Chart pattern", "pattern", -1, 0.65, "descending triangle breakdown")
    return _N("Chart pattern", "pattern", "no completed double top/bottom, H&S or triangle breakout")


# ---------------- LEVELS ----------------
def m_fib(x):
    s = x.sw
    if len(s) < 2: return _N("Fibonacci retracement", "levels")
    a, b = s[-2], s[-1]
    leg = abs(b.price - a.price)
    if leg < 2 * x.a or b.idx < x.n - 80: return _N("Fibonacci retracement", "levels", "no recent significant leg")
    if a.kind == "L" and b.kind == "H":
        r = (b.price - x.c[-1]) / leg
        if 0.5 <= r <= 0.786 and x.bull() and x.htf_bias >= 0: return _S("Fibonacci retracement", "levels", 1, 0.6 + (0.1 if abs(r - 0.618) < 0.05 else 0), f"retraced {r:.0%} of up-leg")
    if a.kind == "H" and b.kind == "L":
        r = (x.c[-1] - b.price) / leg
        if 0.5 <= r <= 0.786 and x.bear() and x.htf_bias <= 0: return _S("Fibonacci retracement", "levels", -1, 0.6 + (0.1 if abs(r - 0.618) < 0.05 else 0), f"retraced {r:.0%} of down-leg")
    return _N("Fibonacci retracement", "levels")


def m_pivots(x):
    cur = x.cs[-1].ts.date()
    if (x.cs[-1].ts - x.cs[-2].ts).total_seconds() >= 86400: return _NA("Daily pivot reaction", "levels", "needs intraday data")
    prev = None
    for i in range(x.n - 1, -1, -1):
        d = x.cs[i].ts.date()
        if d < cur:
            prev = d; break
    if prev is None: return _NA("Daily pivot reaction", "levels", "no prior day")
    day = [c for c in x.cs if c.ts.date() == prev]
    if len(day) < 8: return _NA("Daily pivot reaction", "levels", "prior day incomplete")
    H, L, C = max(c.h for c in day), min(c.l for c in day), day[-1].c
    pp = (H + L + C) / 3; lv = {"S1": 2 * pp - H, "S2": pp - (H - L), "R1": 2 * pp - L, "R2": pp + (H - L), "PP": pp}
    for nm in ("S1", "S2", "PP"):
        if abs(x.l[-1] - lv[nm]) <= 0.3 * x.a and x.bull() and x.c[-1] > lv[nm]: return _S("Daily pivot reaction", "levels", 1, 0.55 + (0.1 if nm == "S2" else 0), f"bullish rejection at {nm} {lv[nm]:.5g}")
    for nm in ("R1", "R2", "PP"):
        if abs(x.h[-1] - lv[nm]) <= 0.3 * x.a and x.bear() and x.c[-1] < lv[nm]: return _S("Daily pivot reaction", "levels", -1, 0.55 + (0.1 if nm == "R2" else 0), f"bearish rejection at {nm} {lv[nm]:.5g}")
    return _N("Daily pivot reaction", "levels")


# ---------------- VOLUME ----------------
def m_vwap(x):
    if not x.hasvol: return _NA("VWAP reclaim", "volume", "no volume data")
    vw = ind.rolling_vwap(x.cs, 48)
    if isnan(vw[-1]) or isnan(vw[-4]): return _NA("VWAP reclaim", "volume", "warm-up")
    rv = x.v[-1] / (sum(x.v[-21:-1]) / 20 or 1e9)
    if x.c[-1] > vw[-1] and any(x.c[-k] < vw[-k] for k in range(2, 5)): return _S("VWAP reclaim", "volume", 1, 0.55 + (0.15 if rv > 1.0 else 0), f"reclaimed rolling VWAP, rel.vol {rv:.2f} (tick volume)")
    if x.c[-1] < vw[-1] and any(x.c[-k] > vw[-k] for k in range(2, 5)): return _S("VWAP reclaim", "volume", -1, 0.55 + (0.15 if rv > 1.0 else 0), f"lost rolling VWAP, rel.vol {rv:.2f} (tick volume)")
    return _N("VWAP reclaim")


def m_obv(x):
    if not x.hasvol: return _NA("OBV/volume confirmation", "volume", "no volume data")
    obv, o = [0.0], 0.0
    for i in range(1, x.n):
        o += x.v[i] if x.c[i] > x.c[i - 1] else -x.v[i] if x.c[i] < x.c[i - 1] else 0
        obv.append(o)
    eo = ind.ema(obv, 20); rv = x.v[-1] / (sum(x.v[-21:-1]) / 20 or 1e9)
    if obv[-1] > eo[-1] and x.c[-1] > x.e21[-1] and rv > 1.1 and x.bull(): return _S("OBV/volume confirmation", "volume", 1, 0.55 + min(0.3, (rv - 1.1) / 2), f"OBV>EMA20, rel.vol {rv:.2f} (tick volume)")
    if obv[-1] < eo[-1] and x.c[-1] < x.e21[-1] and rv > 1.1 and x.bear(): return _S("OBV/volume confirmation", "volume", -1, 0.55 + min(0.3, (rv - 1.1) / 2), f"OBV<EMA20, rel.vol {rv:.2f} (tick volume)")
    return _N("OBV/volume confirmation")


# ---------------- SESSION / MTF ----------------
def m_session(x):
    dt = (x.cs[-1].ts - x.cs[-2].ts).total_seconds()
    if dt > 3600: return _NA("Asian-range breakout", "session", "needs <=1h data")
    t = x.cs[-1].ts
    if not 7 <= t.hour < 13: return _N("Asian-range breakout", "session", "outside London morning window")
    asia = [c for c in x.cs if c.ts.date() == t.date() and c.ts.hour < 7]
    if len(asia) < 0.7 * (7 * 3600 / dt): return _NA("Asian-range breakout", "session", "Asian range incomplete")
    hi, lo = max(c.h for c in asia), min(c.l for c in asia)
    if hi - lo < 0.3 * x.a: return _N("Asian-range breakout", "session", "range too tight")
    if x.c[-1] > hi and any(x.c[-k] <= hi for k in range(2, 5)): return _S("Asian-range breakout", "session", 1, 0.6, f"broke Asian high {hi:.5g}")
    if x.c[-1] < lo and any(x.c[-k] >= lo for k in range(2, 5)): return _S("Asian-range breakout", "session", -1, 0.6, f"broke Asian low {lo:.5g}")
    return _N("Asian-range breakout")


def m_mtf(x):
    b = x.htf_bias
    if abs(b) >= 1.0: return _S("Multi-timeframe confluence", "mtf", 1 if b > 0 else -1, 0.4 + abs(b) * 0.3, f"derived higher-timeframe bias {b:+.2f}")
    return _N("Multi-timeframe confluence", "mtf", f"bias {b:+.2f}")


METHODS: List[Tuple[str, str, Callable]] = [
    ("EMA trend stack", "trend", m_ema_trend), ("Supertrend(10,3)", "trend", m_supertrend),
    ("Ichimoku cloud", "trend", m_ichimoku), ("ADX/DI", "trend", m_adx_di), ("Parabolic SAR", "trend", m_psar),
    ("Trend pullback + trigger", "trend", m_trend_pullback),
    ("MACD", "momentum", m_macd), ("Time-series momentum", "momentum", m_roc),
    ("Stochastic cross", "momentum", m_stoch), ("RSI divergence", "momentum", m_rsi_div),
    ("RSI extreme reversal", "mean_reversion", m_rsi_rev), ("Bollinger reversion", "mean_reversion", m_bb_rev),
    ("Z-score reversion", "quant", m_zscore), ("Regression channel trend", "quant", m_linreg),
    ("Donchian 20 breakout", "breakout", m_donchian),
    ("Bollinger squeeze breakout", "volatility", m_bb_squeeze), ("TTM squeeze release", "volatility", m_keltner_squeeze),
    ("Market structure BOS/CHOCH", "structure", m_structure), ("Liquidity sweep reversal", "liquidity", m_sweep),
    ("Fair value gap retest", "zones", m_fvg), ("Order block retest", "zones", m_order_block),
    ("Candlestick at level", "pattern_candle", m_candles), ("Chart pattern", "pattern", m_chart_pattern),
    ("Fibonacci retracement", "levels", m_fib), ("Daily pivot reaction", "levels", m_pivots),
    ("VWAP reclaim", "volume", m_vwap), ("OBV/volume confirmation", "volume", m_obv),
    ("Asian-range breakout", "session", m_session), ("Multi-timeframe confluence", "mtf", m_mtf),
]


@dataclass
class ScanResult:
    signals: List[Signal]
    n_total: int
    n_applicable: int
    long_passed: int
    short_passed: int
    neutral: int
    not_applicable: int
    errors: int
    fam_long: Dict[str, float]
    fam_short: Dict[str, float]
    consensus: Optional[str]          # "BUY" | "SELL" | None
    consensus_strength: float
    why_not: List[str]
    disabled: List[str] = field(default_factory=list)
    regime: dict = field(default_factory=dict)

    @property
    def passed_total(self) -> int: return self.long_passed + self.short_passed

    def summary(self) -> str:
        return (f"{self.n_total} methods scanned | passed LONG {self.long_passed} | passed SHORT {self.short_passed} | "
                f"neutral {self.neutral} | not applicable {self.not_applicable} | errors {self.errors} | "
                f"independent families long/short {len(self.fam_long)}/{len(self.fam_short)}")


TREND_NAMES = {"EMA trend stack", "Supertrend(10,3)", "Ichimoku cloud", "ADX/DI", "Parabolic SAR", "Trend pullback + trigger", "Time-series momentum",
               "Donchian 20 breakout", "Regression channel trend", "Aroon(25)", "Vortex(14)", "Heikin-Ashi trend", "Hull MA trend", "KAMA adaptive trend",
               "Chandelier exit flip", "Keltner channel breakout", "Bollinger squeeze breakout", "TTM squeeze release"}
MEANREV_NAMES = {"RSI extreme reversal", "Bollinger reversion", "Z-score reversion", "Stochastic cross", "CCI(20) extreme reversal", "Williams %R reversal",
                 "RSI(2) pullback", "Williams VixFix bottom", "Fisher transform"}


def regime_adjust(x: Ctx, sg: Signal, pen: float) -> Signal:
    """Use the right tool for the market: penalise trend tools in ranges and reversal tools in strong trends."""
    if sg.direction == 0 or not sg.applicable: return sg
    wrong = (sg.name in TREND_NAMES and x.ranging and not x.trending) or (sg.name in MEANREV_NAMES and x.trending and x.adx_v >= 28)
    if wrong:
        sg.strength = round(sg.strength * pen, 3); sg.detail += f" [counter-regime x{pen}]"
    return sg


def run_methods(x: Ctx, disabled: Sequence[str] = (), pen: float = 0.75) -> List[Signal]:
    out = []
    for name, fam, fn in METHODS:
        if name in disabled:
            out.append(Signal(name, fam, 0, 0.0, "disabled: negative out-of-sample edge", applicable=False)); continue
        try:
            sg = fn(x); sg.name, sg.family = name, fam; out.append(regime_adjust(x, sg, pen))
        except Exception as e:  # a failing method must never take down the scan
            out.append(Signal(name, fam, 0, 0.0, f"error: {type(e).__name__}: {e}", applicable=False, error=True))
    return out


def decide(signals: List[Signal], cfg: ScanConfig, weights: Optional[Dict[str, float]] = None, htf_bias: float = 0.0, ext: float = 0.0) -> ScanResult:
    w = weights or {}
    L = [s for s in signals if s.passed and s.direction > 0 and w.get(s.name, 1.0) > 0]
    S = [s for s in signals if s.passed and s.direction < 0 and w.get(s.name, 1.0) > 0]
    fl, fs = {}, {}
    for s in L: fl[s.family] = max(fl.get(s.family, 0), s.strength * w.get(s.name, 1.0))
    for s in S: fs[s.family] = max(fs.get(s.family, 0), s.strength * w.get(s.name, 1.0))
    app = [s for s in signals if s.applicable]
    res = ScanResult(signals, len(signals), len(app), len(L), len(S), sum(1 for s in app if not s.passed),
                     sum(1 for s in signals if not s.applicable and not s.error), sum(1 for s in signals if s.error), fl, fs, None, 0.0, [],
                     [s.name for s in signals if "disabled" in s.detail])
    for side, mine, opp, passed in (("BUY", fl, fs, L), ("SELL", fs, fl, S)):
        if not mine: continue
        sm, so = sum(mine.values()), sum(opp.values())
        why = []
        if len(passed) < cfg.min_methods: why.append(f"{side}: only {len(passed)} methods passed (<{cfg.min_methods})")
        if len(mine) < cfg.min_families: why.append(f"{side}: only {len(mine)} independent families (<{cfg.min_families})")
        if len(opp) > cfg.max_opposing_families: why.append(f"{side}: {len(opp)} opposing families (>{cfg.max_opposing_families})")
        if so and sm < cfg.dominance * so: why.append(f"{side}: not dominant over opposite side ({sm:.2f} vs {so:.2f})")
        sg = 1 if side == "BUY" else -1
        if cfg.require_htf_alignment and htf_bias * sg <= -1.0: why.append(f"{side}: against the higher-timeframe bias ({htf_bias:+.1f})")
        if ext * sg > cfg.max_extension_atr: why.append(f"{side}: price already extended {abs(ext):.1f} ATR from EMA21 (no chasing)")
        if why: res.why_not += why
        elif res.consensus is None or sm > sum((fl if res.consensus == "BUY" else fs).values()):
            res.consensus, res.consensus_strength = side, min(1.0, sm / max(1, len({s.family for s in app})))
    if not fl and not fs: res.why_not.append("no method produced a passing directional signal")
    return res


def scan_chart(cs: List[Candle], htf_bias: float = 0.0, cfg: Optional[ScanConfig] = None, disabled: Sequence[str] = (),
               weights: Optional[Dict[str, float]] = None) -> ScanResult:
    if len(cs) < 60: raise ValueError("need >= 60 candles to scan")
    cfg = cfg or ScanConfig()
    x = Ctx(cs, htf_bias)
    ext = (x.c[-1] - x.e21[-1]) / x.a if not isnan(x.e21[-1]) else 0.0
    res = decide(run_methods(x, disabled, cfg.counter_regime_penalty), cfg, weights, htf_bias, ext)
    res.regime = {"adx": round(x.adx_v, 1), "chop": round(x.chop, 1), "vr": round(x.vr, 2), "trending": x.trending, "ranging": x.ranging, "ext_atr": round(ext, 2)}
    return res

from . import scanner_ext  # noqa: E402,F401  (registers the extended methods)
METHOD_COUNT = len(METHODS)
FAMILIES = sorted({f for _, f, _ in METHODS})
