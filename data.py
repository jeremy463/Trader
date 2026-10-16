"""Market data model and validation (spec 3)."""
from __future__ import annotations
import csv, math, statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional


@dataclass(frozen=True)
class Candle:
    ts: datetime
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    ts: datetime
    source: str

    @property
    def spread(self): return self.ask - self.bid
    @property
    def mid(self): return (self.ask + self.bid) / 2


@dataclass
class DataQuality:
    ok: bool = True
    blocking: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    n: int = 0


def _parse_ts(x: str) -> datetime:
    x = x.strip()
    try:
        f = float(x)
        if f > 1e11: f /= 1000.0
        return datetime.fromtimestamp(f, tz=timezone.utc)
    except ValueError:
        d = datetime.fromisoformat(x.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load_csv(path: str) -> List[Candle]:
    out = []
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        r.fieldnames = [h.strip().lower() for h in r.fieldnames]
        tcol = next(k for k in ("time", "timestamp", "datetime", "date") if k in r.fieldnames)
        for row in r:
            out.append(Candle(_parse_ts(row[tcol]), float(row["open"]), float(row["high"]),
                              float(row["low"]), float(row["close"]), float(row.get("volume") or 0)))
    return out


def validate_candles(cs: List[Candle], tf_seconds: int, now: Optional[datetime] = None,
                     stale_factor: float = 3.0, min_bars: int = 250) -> DataQuality:
    q = DataQuality(n=len(cs))
    if len(cs) < min_bars:
        q.blocking.append(f"only {len(cs)} candles (<{min_bars})")
    if len(cs) < 2:
        q.ok = False
        return q
    seen, dup, unsorted, bad, gaps = set(), 0, 0, 0, 0
    ranges = []
    for i, c in enumerate(cs):
        if c.ts in seen: dup += 1
        seen.add(c.ts)
        if i and c.ts < cs[i - 1].ts: unsorted += 1
        if not (c.l <= min(c.o, c.c) and c.h >= max(c.o, c.c) and c.h >= c.l) or any(math.isnan(x) for x in (c.o, c.h, c.l, c.c)):
            bad += 1
        ranges.append(c.h - c.l)
        if i:
            dt = (c.ts - cs[i - 1].ts).total_seconds()
            if dt > 1.5 * tf_seconds and dt < 40 * 3600: gaps += 1  # >40h treated as weekend/holiday
    if dup: q.blocking.append(f"{dup} duplicate timestamps")
    if unsorted: q.blocking.append("candles not chronological")
    if bad: q.blocking.append(f"{bad} inconsistent OHLC candles")
    if gaps / len(cs) > 0.02: q.blocking.append(f"{gaps} missing-candle gaps ({gaps/len(cs):.1%})")
    elif gaps: q.warnings.append(f"{gaps} small gaps in data")
    med = statistics.median(ranges) or 1e-12
    jumps = sum(1 for i in range(1, len(cs)) if abs(cs[i].c - cs[i - 1].c) > 15 * med)
    if jumps: q.warnings.append(f"{jumps} abnormal price jumps (>15x median range)")
    if now is not None:
        age = (now - cs[-1].ts).total_seconds()
        if age > stale_factor * tf_seconds: q.blocking.append(f"stale data: last candle {age/60:.0f} min old")
    else:
        q.warnings.append("no 'now' supplied: treated as a historical SNAPSHOT, not live data")
    q.ok = not q.blocking
    return q


def resample(cs: List[Candle], n: int) -> List[Candle]:
    """Aggregate every n candles (aligned to the END so the latest bar is complete)."""
    out, start = [], len(cs) % n
    for i in range(start, len(cs) - n + 1, n):
        g = cs[i:i + n]
        out.append(Candle(g[-1].ts, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g)))
    return out
