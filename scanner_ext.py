"""Extended method library (22 more methods). Registered into scanner.METHODS on import.
Every method returns a Signal; none is a guarantee. Correlated methods share a family and count once in consensus."""
from __future__ import annotations
import math
from typing import List
from . import indicators as ind
from .scanner import METHODS, Ctx, _S, _N, _NA, near_support

nan = float("nan"); isnan = math.isnan


def _cache(x: Ctx, key, fn):
    d = x.__dict__.setdefault("cache", {})
    if key not in d: d[key] = fn()
    return d[key]


def wma(vals: List[float], n: int) -> List[float]:
    out, den = [nan] * len(vals), n * (n + 1) / 2
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1:i + 1]
        if any(isnan(v) for v in w): continue
        out[i] = sum((k + 1) * v for k, v in enumerate(w)) / den
    return out


def _fresh_up(c, lv): return c[-1] > lv and any(c[-k] <= lv for k in range(2, 6))
def _fresh_dn(c, lv): return c[-1] < lv and any(c[-k] >= lv for k in range(2, 6))


# ---------------- momentum / oscillators ----------------
def m_cci(x):
    n = 20; tp = [(x.h[i] + x.l[i] + x.c[i]) / 3 for i in range(x.n)]
    def cci(i):
        w = tp[i - n + 1:i + 1]; m = sum(w) / n; md = sum(abs(v - m) for v in w) / n or 1e-12
        return (tp[i] - m) / (0.015 * md)
    a, b = cci(x.n - 2), cci(x.n - 1)
    if a < -100 and b > -100: return _S("CCI(20) extreme reversal", "mean_reversion", 1, 0.6 + min(0.25, (-a - 100) / 200), f"CCI {b:.0f} leaving oversold")
    if a > 100 and b < 100: return _S("CCI(20) extreme reversal", "mean_reversion", -1, 0.6 + min(0.25, (a - 100) / 200), f"CCI {b:.0f} leaving overbought")
    return _N("CCI(20) extreme reversal", "mean_reversion", f"CCI {b:.0f}")


def m_willr(x):
    def wr(i):
        hh, ll = max(x.h[i - 13:i + 1]), min(x.l[i - 13:i + 1]); return -100 * (hh - x.c[i]) / (hh - ll) if hh > ll else -50
    a, b = wr(x.n - 2), wr(x.n - 1)
    if a < -80 and b > -80: return _S("Williams %R reversal", "mean_reversion", 1, 0.58 + (0.1 if x.bull() else 0), f"%R {b:.0f} leaving oversold")
    if a > -20 and b < -20: return _S("Williams %R reversal", "mean_reversion", -1, 0.58 + (0.1 if x.bear() else 0), f"%R {b:.0f} leaving overbought")
    return _N("Williams %R reversal", "mean_reversion", f"%R {b:.0f}")


def m_ao(x):
    mp = [(x.h[i] + x.l[i]) / 2 for i in range(x.n)]; s5, s34 = ind.sma(mp, 5), ind.sma(mp, 34)
    ao = [a - b if not (isnan(a) or isnan(b)) else nan for a, b in zip(s5, s34)]
    if isnan(ao[-4]): return _NA("Awesome Oscillator", "momentum", "warm-up")
    zero_up = ao[-1] > 0 and any(ao[-k] <= 0 for k in range(2, 5)); zero_dn = ao[-1] < 0 and any(ao[-k] >= 0 for k in range(2, 5))
    sau_up = ao[-1] > 0 and ao[-3] > ao[-2] < ao[-1] and ao[-3] > 0; sau_dn = ao[-1] < 0 and ao[-3] < ao[-2] > ao[-1] and ao[-3] < 0
    if zero_up or sau_up: return _S("Awesome Oscillator", "momentum", 1, 0.58 + (0.08 if zero_up else 0), "zero-line cross up" if zero_up else "bullish saucer")
    if zero_dn or sau_dn: return _S("Awesome Oscillator", "momentum", -1, 0.58 + (0.08 if zero_dn else 0), "zero-line cross down" if zero_dn else "bearish saucer")
    return _N("Awesome Oscillator", "momentum")


def m_elder(x):
    e13 = ind.ema(x.c, 13)
    if isnan(e13[-3]): return _NA("Elder Ray", "momentum", "warm-up")
    bear = [x.l[-k] - e13[-k] for k in (3, 2, 1)]; bull = [x.h[-k] - e13[-k] for k in (3, 2, 1)]
    if e13[-1] > e13[-3] and bear[-1] < 0 and bear[0] < bear[1] < bear[2]: return _S("Elder Ray", "momentum", 1, 0.58, "EMA13 rising, bear power negative but improving")
    if e13[-1] < e13[-3] and bull[-1] > 0 and bull[0] > bull[1] > bull[2]: return _S("Elder Ray", "momentum", -1, 0.58, "EMA13 falling, bull power positive but fading")
    return _N("Elder Ray", "momentum")


def m_fisher(x):
    if x.n < 100: return _NA("Fisher transform", "mean_reversion", "needs 100 bars")
    n = 9; v, f, fs = 0.0, 0.0, []
    for i in range(x.n - 60, x.n):
        hh, ll = max(x.h[i - n + 1:i + 1]), min(x.l[i - n + 1:i + 1]); mp = (x.h[i] + x.l[i]) / 2
        raw = 0.66 * ((mp - ll) / (hh - ll) - 0.5) + 0.67 * v if hh > ll else 0.0
        v = max(-0.999, min(0.999, raw)); f = 0.5 * math.log((1 + v) / (1 - v)) + 0.5 * f; fs.append(f)
    if fs[-2] <= fs[-3] and fs[-1] > fs[-2] and fs[-2] < -1: return _S("Fisher transform", "mean_reversion", 1, 0.6 + min(0.2, (-fs[-2] - 1) / 4), f"turn up from {fs[-2]:.2f}")
    if fs[-2] >= fs[-3] and fs[-1] < fs[-2] and fs[-2] > 1: return _S("Fisher transform", "mean_reversion", -1, 0.6 + min(0.2, (fs[-2] - 1) / 4), f"turn down from {fs[-2]:.2f}")
    return _N("Fisher transform", "mean_reversion", f"fisher {fs[-1]:.2f}")


def m_rsi2(x):
    r = ind.rsi(x.c, 2); s5 = sum(x.c[-5:]) / 5; base = x.e200[-1] if not isnan(x.e200[-1]) else x.e50[-1]
    if r[-1] < 10 and x.c[-1] > base and x.c[-1] < s5: return _S("RSI(2) pullback", "mean_reversion", 1, 0.62 + (10 - r[-1]) / 50, f"RSI2 {r[-1]:.1f} in uptrend (above long MA)")
    if r[-1] > 90 and x.c[-1] < base and x.c[-1] > s5: return _S("RSI(2) pullback", "mean_reversion", -1, 0.62 + (r[-1] - 90) / 50, f"RSI2 {r[-1]:.1f} in downtrend (below long MA)")
    return _N("RSI(2) pullback", "mean_reversion", f"RSI2 {r[-1]:.0f}")


def m_vixfix(x):
    if x.n < 100: return _NA("Williams VixFix bottom", "mean_reversion", "needs 100 bars")
    n = 22; wv = []
    for i in range(x.n - 60, x.n):
        hc = max(x.c[i - n + 1:i + 1]); wv.append((hc - x.l[i]) / hc * 100 if hc else 0)
    m = sum(wv[-20:]) / 20; sd = math.sqrt(sum((v - m) ** 2 for v in wv[-20:]) / 20)
    if wv[-2] > m + 2 * sd and wv[-1] < wv[-2] and x.bull(): return _S("Williams VixFix bottom", "mean_reversion", 1, 0.62, "capitulation spike fading with bullish close")
    return _N("Williams VixFix bottom", "mean_reversion", "no capitulation")


# ---------------- trend ----------------
def m_aroon(x):
    n = 25
    def ar(i):
        w_h, w_l = x.h[i - n:i + 1], x.l[i - n:i + 1]
        return 100 * w_h.index(max(w_h)) / n, 100 * w_l.index(min(w_l)) / n
    u, d = ar(x.n - 1); u0, d0 = ar(x.n - 4)
    if u > 70 and d < 30 and (u0 <= d0 or u - d > 40): return _S("Aroon(25)", "trend", 1, 0.52 + (u - d) / 250, f"up {u:.0f} / down {d:.0f}")
    if d > 70 and u < 30 and (d0 <= u0 or d - u > 40): return _S("Aroon(25)", "trend", -1, 0.52 + (d - u) / 250, f"down {d:.0f} / up {u:.0f}")
    return _N("Aroon(25)", "trend", f"up {u:.0f} / down {d:.0f}")


def m_vortex(x):
    n = 14
    def vi(i):
        vp = sum(abs(x.h[k] - x.l[k - 1]) for k in range(i - n + 1, i + 1)); vm = sum(abs(x.l[k] - x.h[k - 1]) for k in range(i - n + 1, i + 1))
        tr = sum(ind.true_range(x.cs[i - n:i + 1])[1:]) or 1e-12; return vp / tr, vm / tr
    p, m = vi(x.n - 1); p0, m0 = vi(x.n - 3)
    if p > m and p0 <= m0 * 1.02 and p > 1.0: return _S("Vortex(14)", "trend", 1, 0.58 + min(0.2, p - m), f"VI+ {p:.2f} crossed above VI- {m:.2f}")
    if m > p and m0 <= p0 * 1.02 and m > 1.0: return _S("Vortex(14)", "trend", -1, 0.58 + min(0.2, m - p), f"VI- {m:.2f} crossed above VI+ {p:.2f}")
    return _N("Vortex(14)", "trend")


def m_heikin(x):
    ha = _cache(x, "ha", lambda: _heikin(x)); o, h, l, c = ha
    bull = [c[-k] > o[-k] for k in (1, 2, 3)]; bear = [c[-k] < o[-k] for k in (1, 2, 3)]
    tol = 0.05 * x.a
    if all(bull): return _S("Heikin-Ashi trend", "trend", 1, 0.55 + (0.15 if abs(o[-1] - l[-1]) < tol else 0), "3 bullish HA candles" + (", no lower wick" if abs(o[-1] - l[-1]) < tol else ""))
    if all(bear): return _S("Heikin-Ashi trend", "trend", -1, 0.55 + (0.15 if abs(o[-1] - h[-1]) < tol else 0), "3 bearish HA candles" + (", no upper wick" if abs(o[-1] - h[-1]) < tol else ""))
    return _N("Heikin-Ashi trend", "trend", "mixed HA candles")


def _heikin(x):
    n = x.n; ho, hc, hh, hl = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
    for i in range(n):
        hc[i] = (x.o[i] + x.h[i] + x.l[i] + x.c[i]) / 4; ho[i] = (x.o[0] + x.c[0]) / 2 if i == 0 else (ho[i - 1] + hc[i - 1]) / 2
        hh[i], hl[i] = max(x.h[i], ho[i], hc[i]), min(x.l[i], ho[i], hc[i])
    return ho, hh, hl, hc


def m_hull(x):
    n = 21; w = wma(x.c, n // 2); w2 = wma(x.c, n); raw = [2 * a - b if not (isnan(a) or isnan(b)) else nan for a, b in zip(w, w2)]
    hm = wma(raw, int(math.sqrt(n)))
    if isnan(hm[-3]): return _NA("Hull MA trend", "trend", "warm-up")
    turn_up = hm[-1] > hm[-2] and hm[-2] <= hm[-3]; turn_dn = hm[-1] < hm[-2] and hm[-2] >= hm[-3]
    if hm[-1] > hm[-2] > hm[-3] and x.c[-1] > hm[-1]: return _S("Hull MA trend", "trend", 1, 0.55 + (0.1 if turn_up else 0), "HMA rising, price above")
    if hm[-1] < hm[-2] < hm[-3] and x.c[-1] < hm[-1]: return _S("Hull MA trend", "trend", -1, 0.55 + (0.1 if turn_dn else 0), "HMA falling, price below")
    return _N("Hull MA trend", "trend")


def m_kama(x):
    n, f, s = 10, 2 / 3, 2 / 31; k = [nan] * x.n; k[n] = x.c[n]; er = 0.0
    for i in range(n + 1, x.n):
        ch = abs(x.c[i] - x.c[i - n]); vol = sum(abs(x.c[j] - x.c[j - 1]) for j in range(i - n + 1, i + 1)) or 1e-12
        er = ch / vol; sc = (er * (f - s) + s) ** 2; k[i] = k[i - 1] + sc * (x.c[i] - k[i - 1])
    sl = (k[-1] - k[-4]) / x.a
    if x.c[-1] > k[-1] and sl > 0.15 and er > 0.3: return _S("KAMA adaptive trend", "trend", 1, 0.5 + min(0.4, er * 0.6), f"efficiency {er:.2f}, KAMA rising")
    if x.c[-1] < k[-1] and sl < -0.15 and er > 0.3: return _S("KAMA adaptive trend", "trend", -1, 0.5 + min(0.4, er * 0.6), f"efficiency {er:.2f}, KAMA falling")
    return _N("KAMA adaptive trend", "trend", f"efficiency {er:.2f}")


def m_chandelier(x):
    if x.n < 120: return _NA("Chandelier exit flip", "trend", "needs 120 bars")
    n, m = 22, 3.0; d, dirs = 1, []
    for i in range(x.n - 80, x.n):
        la = max(x.h[i - n + 1:i + 1]) - m * x.atr_s[i]; sa = min(x.l[i - n + 1:i + 1]) + m * x.atr_s[i]
        if d == 1 and x.c[i] < la: d = -1
        elif d == -1 and x.c[i] > sa: d = 1
        dirs.append(d)
    if dirs[-1] != dirs[-4] or dirs[-1] != dirs[-2]: return _S("Chandelier exit flip", "trend", dirs[-1], 0.62, "fresh trailing-stop regime flip")
    return _N("Chandelier exit flip", "trend", "no fresh flip")


def m_keltner_break(x):
    up, lo = x.e20[-1] + 2 * x.a, x.e20[-1] - 2 * x.a
    if x.c[-1] > up and x.adx_v > 20: return _S("Keltner channel breakout", "breakout", 1, 0.55 + min(0.3, (x.c[-1] - up) / x.a), f"close above EMA20+2ATR, ADX {x.adx_v:.0f}")
    if x.c[-1] < lo and x.adx_v > 20: return _S("Keltner channel breakout", "breakout", -1, 0.55 + min(0.3, (lo - x.c[-1]) / x.a), f"close below EMA20-2ATR, ADX {x.adx_v:.0f}")
    return _N("Keltner channel breakout", "breakout")


# ---------------- volume ----------------
def m_mfi(x):
    if not x.hasvol: return _NA("Money Flow Index", "volume", "no volume data")
    tp = [(x.h[i] + x.l[i] + x.c[i]) / 3 for i in range(x.n)]
    def mfi(i):
        pos = sum(tp[k] * x.v[k] for k in range(i - 13, i + 1) if tp[k] > tp[k - 1]); neg = sum(tp[k] * x.v[k] for k in range(i - 13, i + 1) if tp[k] < tp[k - 1])
        return 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)
    a, b = mfi(x.n - 2), mfi(x.n - 1)
    if a < 20 and b > a: return _S("Money Flow Index", "volume", 1, 0.58 + (20 - a) / 100, f"MFI {b:.0f} turning up from oversold (tick volume)")
    if a > 80 and b < a: return _S("Money Flow Index", "volume", -1, 0.58 + (a - 80) / 100, f"MFI {b:.0f} turning down from overbought (tick volume)")
    return _N("Money Flow Index", "volume", f"MFI {b:.0f}")


def m_cmf(x):
    if not x.hasvol: return _NA("Chaikin Money Flow", "volume", "no volume data")
    def cmf(i):
        num = sum(((x.c[k] - x.l[k]) - (x.h[k] - x.c[k])) / ((x.h[k] - x.l[k]) or 1e-12) * x.v[k] for k in range(i - 19, i + 1)); den = sum(x.v[i - 19:i + 1]) or 1e-12
        return num / den
    a, b = cmf(x.n - 3), cmf(x.n - 1)
    if b > 0.1 and b > a and x.c[-1] > x.e21[-1]: return _S("Chaikin Money Flow", "volume", 1, 0.55 + min(0.3, b), f"CMF {b:+.2f} rising, price above EMA21 (tick volume)")
    if b < -0.1 and b < a and x.c[-1] < x.e21[-1]: return _S("Chaikin Money Flow", "volume", -1, 0.55 + min(0.3, -b), f"CMF {b:+.2f} falling, price below EMA21 (tick volume)")
    return _N("Chaikin Money Flow", "volume", f"CMF {b:+.2f}")


# ---------------- levels ----------------
def m_prevday(x):
    if (x.cs[-1].ts - x.cs[-2].ts).total_seconds() >= 86400: return _NA("Previous-day high/low", "levels", "needs intraday data")
    cur = x.cs[-1].ts.date(); prev = next((x.cs[i].ts.date() for i in range(x.n - 1, -1, -1) if x.cs[i].ts.date() < cur), None)
    day = [c for c in x.cs if prev and c.ts.date() == prev]
    if len(day) < 8: return _NA("Previous-day high/low", "levels", "prior day incomplete")
    pdh, pdl = max(c.h for c in day), min(c.l for c in day)
    if _fresh_up(x.c, pdh) and x.bull(): return _S("Previous-day high/low", "levels", 1, 0.58, f"closed above prior-day high {pdh:.5g}")
    if _fresh_dn(x.c, pdl) and x.bear(): return _S("Previous-day high/low", "levels", -1, 0.58, f"closed below prior-day low {pdl:.5g}")
    if x.l[-1] < pdl < x.c[-1] and x.bull(): return _S("Previous-day high/low", "levels", 1, 0.6, f"swept prior-day low {pdl:.5g}, closed back above")
    if x.h[-1] > pdh > x.c[-1] and x.bear(): return _S("Previous-day high/low", "levels", -1, 0.6, f"swept prior-day high {pdh:.5g}, closed back below")
    return _N("Previous-day high/low", "levels")


def m_round(x):
    p = x.c[-1]
    if p <= 0: return _N("Round-number reaction", "levels")
    step = 10 ** (math.floor(math.log10(p)) - 2); lv = round(p / (step / 2)) * (step / 2)
    if abs(x.l[-1] - lv) <= 0.3 * x.a and x.c[-1] > lv and x.bull() and x.l[-1] <= lv + 0.05 * x.a: return _S("Round-number reaction", "levels", 1, 0.52 + (0.08 if lv % step < 1e-9 else 0), f"bounce off psychological level {lv:.5g}")
    if abs(x.h[-1] - lv) <= 0.3 * x.a and x.c[-1] < lv and x.bear() and x.h[-1] >= lv - 0.05 * x.a: return _S("Round-number reaction", "levels", -1, 0.52 + (0.08 if lv % step < 1e-9 else 0), f"rejection at psychological level {lv:.5g}")
    return _N("Round-number reaction", "levels")


# ---------------- liquidity / wyckoff ----------------
def m_wyckoff(x):
    rng = slice(-43, -3); hi, lo = max(x.h[rng]), min(x.l[rng])
    if hi - lo < 2 * x.a or x.adx_v >= 28: return _N("Wyckoff spring/upthrust", "liquidity", "no established range")
    if any(x.l[-k] < lo for k in (1, 2, 3)) and x.c[-1] > lo and x.bull() and x.c[-1] > x.o[-1]: return _S("Wyckoff spring/upthrust", "liquidity", 1, 0.68, f"spring below range low {lo:.5g}, reclaimed")
    if any(x.h[-k] > hi for k in (1, 2, 3)) and x.c[-1] < hi and x.bear(): return _S("Wyckoff spring/upthrust", "liquidity", -1, 0.68, f"upthrust above range high {hi:.5g}, rejected")
    return _N("Wyckoff spring/upthrust", "liquidity", "inside range")


# ---------------- patterns ----------------
def m_candles2(x):
    cs, a = x.cs, x.a
    A, B, C = cs[-3], cs[-2], cs[-1]; body = lambda k: abs(k.c - k.o); rng = lambda k: max(k.h - k.l, 1e-12)
    res = None
    if B.c < B.o and C.c > C.o and C.o > B.c and C.c < B.o and body(C) < body(B): res = (1, "bullish harami", 0.56)
    elif B.c > B.o and C.c < C.o and C.o < B.c and C.c > B.o and body(C) < body(B): res = (-1, "bearish harami", 0.56)
    elif B.c < B.o and C.c > C.o and C.o < B.l and C.c > (B.o + B.c) / 2 and C.c < B.o: res = (1, "piercing line", 0.6)
    elif B.c > B.o and C.c < C.o and C.o > B.h and C.c < (B.o + B.c) / 2 and C.c > B.o: res = (-1, "dark cloud cover", 0.6)
    elif all(k.c > k.o and body(k) > 0.5 * rng(k) for k in (A, B, C)) and A.c < B.c < C.c: res = (1, "three white soldiers", 0.66)
    elif all(k.c < k.o and body(k) > 0.5 * rng(k) for k in (A, B, C)) and A.c > B.c > C.c: res = (-1, "three black crows", 0.66)
    elif abs(B.l - C.l) <= 0.05 * a and B.c < B.o and C.c > C.o: res = (1, "tweezer bottom", 0.58)
    elif abs(B.h - C.h) <= 0.05 * a and B.c > B.o and C.c < C.o: res = (-1, "tweezer top", 0.58)
    elif A.c < A.o and B.c > B.o and body(B) < body(A) and C.c > A.o and C.c > C.o: res = (1, "three inside up", 0.62)
    elif A.c > A.o and B.c < B.o and body(B) < body(A) and C.c < A.o and C.c < C.o: res = (-1, "three inside down", 0.62)
    if not res: return _N("Multi-candle patterns", "pattern_candle")
    d, nm, s = res; loc = near_support(x, d)
    return _S("Multi-candle patterns", "pattern_candle", d, s + (0.1 if loc else -0.2), f"{nm}{' at key level' if loc else ' (no key level - downgraded)'}")


def _line(pts):
    n = len(pts); mx = sum(p[0] for p in pts) / n; my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts) or 1e-12; b = sum((p[0] - mx) * (p[1] - my) for p in pts) / den
    return b, my - b * mx


def m_geom(x):
    sw = [s for s in x.sw if s.idx >= x.n - 160]; hs = [s for s in sw if s.kind == "H"]; ls = [s for s in sw if s.kind == "L"]; a, c, n = x.a, x.c, x.n
    if len(hs) >= 3 and all(hs[-k].idx - hs[-k - 1].idx >= 6 for k in (1, 2)) and max(s.price for s in hs[-3:]) - min(s.price for s in hs[-3:]) <= 0.4 * a:
        neck = min(x.l[hs[-3].idx:hs[-1].idx + 1])
        if _fresh_dn(c, neck): return _S("Wedge/flag/rectangle/triple", "pattern", -1, 0.7, "triple top, neckline broken")
    if len(ls) >= 3 and all(ls[-k].idx - ls[-k - 1].idx >= 6 for k in (1, 2)) and max(s.price for s in ls[-3:]) - min(s.price for s in ls[-3:]) <= 0.4 * a:
        neck = max(x.h[ls[-3].idx:ls[-1].idx + 1])
        if _fresh_up(c, neck): return _S("Wedge/flag/rectangle/triple", "pattern", 1, 0.7, "triple bottom, neckline broken")
    if len(hs) >= 3 and len(ls) >= 3:
        H, L = hs[-3:], ls[-3:]
        if max(s.price for s in H) - min(s.price for s in H) <= 0.5 * a and max(s.price for s in L) - min(s.price for s in L) <= 0.5 * a:
            top, bot = sum(s.price for s in H) / 3, sum(s.price for s in L) / 3
            if _fresh_up(c, top): return _S("Wedge/flag/rectangle/triple", "pattern", 1, 0.62, "rectangle breakout up")
            if _fresh_dn(c, bot): return _S("Wedge/flag/rectangle/triple", "pattern", -1, 0.62, "rectangle breakdown")
        bh, ah = _line([(s.idx, s.price) for s in H]); bl, al = _line([(s.idx, s.price) for s in L])
        up_line, lo_line = bh * (n - 1) + ah, bl * (n - 1) + al
        if bh > 0 and bl > bh * 1.3 and _fresh_dn(c, lo_line) and up_line > lo_line: return _S("Wedge/flag/rectangle/triple", "pattern", -1, 0.62, "rising wedge breakdown")
        if bh < 0 and bl < 0 and abs(bh) > abs(bl) * 1.3 and _fresh_up(c, up_line) and up_line > lo_line: return _S("Wedge/flag/rectangle/triple", "pattern", 1, 0.62, "falling wedge breakout")
    for lookback in range(5, 16):   # flags: strong impulse then shallow counter-channel
        for off in range(6, 14):
            i0, i1 = n - 1 - lookback - off, n - 1 - lookback
            if i0 < 0: continue
            imp = x.c[i1] - x.c[i0]; seg = x.cs[i1:n - 1]
            if abs(imp) >= 3 * a and len(seg) >= 4:
                hh, ll = max(k.h for k in seg), min(k.l for k in seg)
                if imp > 0 and (x.c[i1] - ll) <= 0.45 * imp and _fresh_up(c, hh): return _S("Wedge/flag/rectangle/triple", "pattern", 1, 0.65, "bull flag breakout")
                if imp < 0 and (hh - x.c[i1]) <= 0.45 * -imp and _fresh_dn(c, ll): return _S("Wedge/flag/rectangle/triple", "pattern", -1, 0.65, "bear flag breakdown")
    return _N("Wedge/flag/rectangle/triple", "pattern", "no completed formation")


def _zigzag(x, k=5):
    out = []
    for s in x.sw:
        if out and out[-1].kind == s.kind:
            if (s.kind == "H" and s.price > out[-1].price) or (s.kind == "L" and s.price < out[-1].price): out[-1] = s
        else: out.append(s)
    return out[-k:]


def m_harmonic(x):
    z = _zigzag(x)
    if len(z) < 5 or z[-1].idx < x.n - 12: return _N("Harmonic patterns", "harmonic", "no completed XABCD near price")
    X, A, B, C, D = z; XA, AB, BC, CD, AD = abs(A.price - X.price), abs(B.price - A.price), abs(C.price - B.price), abs(D.price - C.price), abs(D.price - A.price)
    if XA < 2 * x.a or AB == 0 or BC == 0: return _N("Harmonic patterns", "harmonic")
    ab, bc, ad = AB / XA, BC / AB, AD / XA; t = 0.06
    near = lambda v, r: abs(v - r) <= t; within = lambda v, lo, hi: lo - t <= v <= hi + t
    name = None
    if near(ab, 0.618) and within(bc, 0.382, 0.886) and near(ad, 0.786): name = "Gartley"
    elif within(ab, 0.382, 0.5) and within(bc, 0.382, 0.886) and near(ad, 0.886): name = "Bat"
    elif near(ab, 0.786) and within(bc, 0.382, 0.886) and within(ad, 1.27, 1.618): name = "Butterfly"
    elif within(ab, 0.382, 0.618) and within(bc, 0.382, 0.886) and near(ad, 1.618): name = "Crab"
    if not name: return _N("Harmonic patterns", "harmonic", f"ratios AB/XA {ab:.2f} AD/XA {ad:.2f} match no pattern")
    d = 1 if D.kind == "L" else -1
    if (d > 0 and x.bull()) or (d < 0 and x.bear()): return _S("Harmonic patterns", "harmonic", d, 0.62, f"{'bullish' if d>0 else 'bearish'} {name} completed at D {D.price:.5g}")
    return _N("Harmonic patterns", "harmonic", f"{name} completed but no reversal candle yet")


EXT = [
    ("CCI(20) extreme reversal", "mean_reversion", m_cci), ("Williams %R reversal", "mean_reversion", m_willr), ("Fisher transform", "mean_reversion", m_fisher),
    ("RSI(2) pullback", "mean_reversion", m_rsi2), ("Williams VixFix bottom", "mean_reversion", m_vixfix),
    ("Awesome Oscillator", "momentum", m_ao), ("Elder Ray", "momentum", m_elder),
    ("Aroon(25)", "trend", m_aroon), ("Vortex(14)", "trend", m_vortex), ("Heikin-Ashi trend", "trend", m_heikin), ("Hull MA trend", "trend", m_hull),
    ("KAMA adaptive trend", "trend", m_kama), ("Chandelier exit flip", "trend", m_chandelier), ("Keltner channel breakout", "breakout", m_keltner_break),
    ("Money Flow Index", "volume", m_mfi), ("Chaikin Money Flow", "volume", m_cmf),
    ("Previous-day high/low", "levels", m_prevday), ("Round-number reaction", "levels", m_round),
    ("Wyckoff spring/upthrust", "liquidity", m_wyckoff), ("Multi-candle patterns", "pattern_candle", m_candles2),
    ("Wedge/flag/rectangle/triple", "pattern", m_geom), ("Harmonic patterns", "harmonic", m_harmonic),
]
_have = {m[0] for m in METHODS}
for e in EXT:
    if e[0] not in _have: METHODS.append(e)
