"""Pure-python indicators (spec 5D). NaN-padded lists aligned to input length."""
from __future__ import annotations
import math
from typing import List, Tuple
from .data import Candle

NAN = float("nan")
isnan = math.isnan


def sma(x: List[float], n: int) -> List[float]:
    out, s = [NAN] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n: s -= x[i - n]
        if i >= n - 1: out[i] = s / n
    return out


def ema(x: List[float], n: int) -> List[float]:
    out, k = [NAN] * len(x), 2 / (n + 1)
    if len(x) < n: return out
    e = sum(x[:n]) / n
    out[n - 1] = e
    for i in range(n, len(x)):
        e = x[i] * k + e * (1 - k)
        out[i] = e
    return out


def _wilder(x: List[float], n: int, start: int = 0) -> List[float]:
    out = [NAN] * len(x)
    if len(x) < start + n: return out
    a = sum(x[start:start + n]) / n
    out[start + n - 1] = a
    for i in range(start + n, len(x)):
        a = (a * (n - 1) + x[i]) / n
        out[i] = a
    return out


def true_range(cs: List[Candle]) -> List[float]:
    tr = [cs[0].h - cs[0].l]
    for i in range(1, len(cs)):
        tr.append(max(cs[i].h - cs[i].l, abs(cs[i].h - cs[i - 1].c), abs(cs[i].l - cs[i - 1].c)))
    return tr


def atr(cs: List[Candle], n: int = 14) -> List[float]:
    return _wilder(true_range(cs), n)


def rsi(close: List[float], n: int = 14) -> List[float]:
    g = [0.0] + [max(close[i] - close[i - 1], 0) for i in range(1, len(close))]
    l = [0.0] + [max(close[i - 1] - close[i], 0) for i in range(1, len(close))]
    ag, al = _wilder(g, n, 1), _wilder(l, n, 1)
    return [NAN if isnan(a) else (100.0 if b == 0 else 100 - 100 / (1 + a / b)) for a, b in zip(ag, al)]


def macd(close: List[float], f=12, s=26, sig=9) -> Tuple[List[float], List[float], List[float]]:
    ef, es = ema(close, f), ema(close, s)
    line = [NAN if isnan(a) or isnan(b) else a - b for a, b in zip(ef, es)]
    valid = [v for v in line if not isnan(v)]
    sg_v = ema(valid, sig)
    off = len(line) - len(valid)
    sg = [NAN] * off + sg_v
    hist = [NAN if isnan(a) or isnan(b) else a - b for a, b in zip(line, sg)]
    return line, sg, hist


def bollinger(close: List[float], n=20, k=2.0):
    mid = sma(close, n)
    up, lo, bw = [NAN] * len(close), [NAN] * len(close), [NAN] * len(close)
    for i in range(n - 1, len(close)):
        w = close[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((v - m) ** 2 for v in w) / n)
        up[i], lo[i] = m + k * sd, m - k * sd
        bw[i] = (up[i] - lo[i]) / m if m else NAN
    return mid, up, lo, bw


def donchian(cs: List[Candle], n=20):
    up, lo = [NAN] * len(cs), [NAN] * len(cs)
    for i in range(n - 1, len(cs)):
        up[i] = max(c.h for c in cs[i - n + 1:i + 1])
        lo[i] = min(c.l for c in cs[i - n + 1:i + 1])
    return up, lo


def adx(cs: List[Candle], n=14):
    pdm, mdm = [0.0], [0.0]
    for i in range(1, len(cs)):
        up, dn = cs[i].h - cs[i - 1].h, cs[i - 1].l - cs[i].l
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    tr = true_range(cs)
    atr_w, p_w, m_w = _wilder(tr, n, 1), _wilder(pdm, n, 1), _wilder(mdm, n, 1)
    pdi = [NAN if isnan(a) or not a else 100 * p / a for p, a in zip(p_w, atr_w)]
    mdi = [NAN if isnan(a) or not a else 100 * m / a for m, a in zip(m_w, atr_w)]
    dx = [NAN if isnan(p) or isnan(m) or (p + m) == 0 else 100 * abs(p - m) / (p + m) for p, m in zip(pdi, mdi)]
    first = next((i for i, v in enumerate(dx) if not isnan(v)), None)
    out = [NAN] * len(cs)
    if first is not None and len(cs) >= first + n:
        a = sum(dx[first:first + n]) / n
        out[first + n - 1] = a
        for i in range(first + n, len(cs)):
            a = (a * (n - 1) + dx[i]) / n
            out[i] = a
    return out, pdi, mdi


def rolling_vwap(cs: List[Candle], n=48) -> List[float]:
    out = [NAN] * len(cs)
    for i in range(n - 1, len(cs)):
        w = cs[i - n + 1:i + 1]
        vv = sum(c.v for c in w)
        out[i] = (sum((c.h + c.l + c.c) / 3 * c.v for c in w) / vv) if vv else NAN
    return out


def percentile_rank(x: List[float], i: int, lookback: int = 200) -> float:
    w = [v for v in x[max(0, i - lookback + 1):i + 1] if not isnan(v)]
    if len(w) < 20 or isnan(x[i]): return 0.5
    return sum(1 for v in w if v <= x[i]) / len(w)


def last(x: List[float]) -> float:
    for v in reversed(x):
        if not isnan(v): return v
    return NAN
