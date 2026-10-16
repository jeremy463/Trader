"""When to trade: DST-correct sessions, weekend/market-open logic, per-symbol volatility-by-hour profile, news blackout windows and a GO / WAIT / AVOID verdict.
Timing only filters WHEN conditions are sensible (liquidity, no scheduled shock). It does not predict direction."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone, date
from typing import Dict, List, Optional
from . import indicators as ind

try:
    from zoneinfo import ZoneInfo
    NY, _t = ZoneInfo("America/New_York"), ZoneInfo("UTC")
    SESS = {"sydney": (ZoneInfo("Australia/Sydney"), 8, 17), "tokyo": (ZoneInfo("Asia/Tokyo"), 9, 18), "london": (ZoneInfo("Europe/London"), 8, 17), "newyork": (NY, 8, 17)}
    TZ_OK = True
except Exception:   # tzdata missing: fixed winter offsets (documented limitation)
    from datetime import timezone as _tz
    NY = _tz(timedelta(hours=-5)); SESS = {"sydney": (_tz(timedelta(hours=11)), 8, 17), "tokyo": (_tz(timedelta(hours=9)), 9, 18), "london": (_tz(timedelta(hours=0)), 8, 17), "newyork": (NY, 8, 17)}; TZ_OK = False

HOME = {"USD": ["newyork"], "EUR": ["london"], "GBP": ["london"], "CHF": ["london"], "JPY": ["tokyo"], "AUD": ["sydney", "tokyo"], "NZD": ["sydney", "tokyo"], "CAD": ["newyork"]}
BLACKOUT_BEFORE, BLACKOUT_AFTER = 30, 15


def _utc(dt: datetime) -> datetime: return dt.astimezone(timezone.utc)
def iso(dt: datetime) -> str: return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_windows(now: datetime, back_h: int = 8, ahead_h: int = 40) -> List[dict]:
    """Every session window overlapping [now-back, now+ahead], in UTC, with the DST of that date applied."""
    out, lo, hi = [], now - timedelta(hours=back_h), now + timedelta(hours=ahead_h)
    for name, (tz, h0, h1) in SESS.items():
        for off in range(-2, 4):
            d = (now + timedelta(days=off)).astimezone(tz).date()
            if d.weekday() >= 5: continue
            s = datetime(d.year, d.month, d.day, h0, tzinfo=tz); e = datetime(d.year, d.month, d.day, h1, tzinfo=tz)
            if _utc(e) > lo and _utc(s) < hi: out.append({"name": name, "start": _utc(s), "end": _utc(e)})
    return sorted(out, key=lambda w: w["start"])


def active_sessions(now: datetime) -> List[str]:
    return sorted({w["name"] for w in session_windows(now, 24, 1) if w["start"] <= now < w["end"]})


def market_open(now: datetime, futures: bool = False) -> bool:
    l = now.astimezone(NY); wd = l.weekday()
    if wd == 5 or (wd == 4 and l.hour >= 17) or (wd == 6 and l.hour < 17): return False
    return not (futures and l.hour == 17)          # CME daily maintenance hour


def next_open(now: datetime) -> datetime:
    l = now.astimezone(NY); d = l.date()
    while d.weekday() != 6 or datetime(d.year, d.month, d.day, 17, tzinfo=NY) <= now: d += timedelta(days=1)
    return _utc(datetime(d.year, d.month, d.day, 17, tzinfo=NY))


def liquidity(now: datetime, currencies: List[str]) -> float:
    act = set(active_sessions(now))
    if not act: return 0.15
    base = 1.0 if {"london", "newyork"} <= act else 0.75 if act & {"london", "newyork"} else 0.45
    home = {s for c in currencies for s in HOME.get(c, [])}
    return min(1.0, base + (0.1 if home & act else 0.0))


def hour_profile(candles, tf_seconds: int) -> Optional[dict]:
    """Average ATR-normalised bar range by UTC hour: WHEN this symbol has been most active (volatility, not direction)."""
    if tf_seconds > 3600 or len(candles) < 400: return None
    atr = ind.atr(candles); s, n = [0.0] * 24, [0] * 24
    for c, a in zip(candles, atr):
        if a == a and a > 0: s[c.ts.hour] += (c.h - c.l) / a; n[c.ts.hour] += 1
    avg = [s[h] / n[h] if n[h] >= 5 else None for h in range(24)]; vals = [v for v in avg if v is not None]
    if len(vals) < 12: return None
    m = sum(vals) / len(vals); hrs = [round(v / m, 2) if v is not None else None for v in avg]
    ranked = sorted([h for h in range(24) if hrs[h] is not None], key=lambda h: hrs[h], reverse=True)
    return {"hours": hrs, "best": ranked[:3], "worst": ranked[-3:], "n": len(candles)}


def _blackouts(events: List[dict]) -> List[dict]:
    out = []
    for e in events:
        if e["impact"] != "high": continue
        t = datetime.fromisoformat(e["time_utc"].replace("Z", "+00:00"))
        out.append({"name": e["name"], "currency": e["currency"], "time": t, "start": t - timedelta(minutes=BLACKOUT_BEFORE), "end": t + timedelta(minutes=BLACKOUT_AFTER)})
    return sorted(out, key=lambda b: b["time"])


def next_window(now: datetime, currencies: List[str], futures: bool, bl: List[dict], profile: Optional[dict], horizon_h: int = 72) -> Optional[dict]:
    """First 60+ minute block with an open market, good liquidity, normal-or-better activity for this symbol and no news blackout."""
    step, start, run = timedelta(minutes=15), None, None
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=15 - now.minute % 15)
    for _ in range(horizon_h * 4):
        ok = market_open(t, futures) and liquidity(t, currencies) >= 0.75 and not any(b["start"] <= t < b["end"] for b in bl)
        if ok and profile and profile["hours"][t.hour] is not None and profile["hours"][t.hour] < 0.85: ok = False
        if ok:
            run = run or t
            if t + step - run >= timedelta(minutes=60) and start is None: start = run
        else:
            if start is not None: return {"start": iso(start), "end": iso(t), "why": _why(start, currencies)}
            run = None
        if start is not None and t - start >= timedelta(hours=4): return {"start": iso(start), "end": iso(t), "why": _why(start, currencies)}
        t += step
    return {"start": iso(start), "end": iso(t), "why": _why(start, currencies)} if start else None


def _why(t: datetime, currencies: List[str]) -> str:
    act = [a for a in active_sessions(t + timedelta(minutes=30))]
    return ("London/New York overlap" if {"london", "newyork"} <= set(act) else " + ".join(a.title() for a in act) + " session") if act else "liquid hours"


def plan_timing(now: datetime, currencies: List[str], futures: bool, events: List[dict], consensus: Optional[str], grade: Optional[str],
                tf_minutes: int, profile: Optional[dict] = None, disorderly: bool = False) -> dict:
    """Verdict + the dates and times that matter. `events` = upcoming/recent calendar rows relevant to the symbol (dicts)."""
    bl = _blackouts([e for e in events if e["currency"] in currencies]); reasons, status = [], "GO"
    liq = liquidity(now, currencies); op = market_open(now, futures)
    cur = next((b for b in bl if b["start"] <= now < b["end"]), None); nxt = next((b for b in bl if b["start"] > now), None)
    def add(ok, text): reasons.append({"ok": ok, "text": text})
    add(op, "Market open" if op else f"Market closed - reopens {iso(next_open(now))}")
    add(liq >= 0.6, f"Liquidity {'good' if liq >= 0.6 else 'thin'} ({', '.join(active_sessions(now)) or 'no major session'})")
    if cur: add(False, f"News blackout: {cur['currency']} {cur['name']} at {iso(cur['time'])}")
    elif nxt and (nxt["time"] - now) <= timedelta(minutes=60): add(False, f"{nxt['currency']} {nxt['name']} in {int((nxt['time'] - now).total_seconds() // 60)} min")
    else: add(True, "No high-impact news in the next hour" if nxt is None or (nxt["time"] - now) > timedelta(minutes=60) else "")
    if profile and profile["hours"][now.hour] is not None: add(profile["hours"][now.hour] >= 0.85, f"This hour is {'normally active' if profile['hours'][now.hour] >= 0.85 else 'normally quiet'} for this symbol ({profile['hours'][now.hour]}x average range)")
    if disorderly: add(False, "Disorderly volatility: stand aside")
    add(consensus is not None, f"Consensus signal: {consensus}" if consensus else "No qualifying consensus yet")
    if grade: add(grade in ("A", "B"), f"Setup grade {grade}")
    if not op: status, head = "AVOID", "Market closed"
    elif cur: status, head = "AVOID", f"Avoid: {cur['currency']} {cur['name']} blackout"
    elif disorderly: status, head = "AVOID", "Avoid: disorderly volatility"
    elif nxt and (nxt["time"] - now) <= timedelta(minutes=60): status, head = "WAIT", f"Wait: {nxt['currency']} {nxt['name']} soon"
    elif consensus is None: status, head = "WAIT", "Wait: no qualifying setup yet"
    elif grade == "C": status, head = "WAIT", "Wait: setup quality too low"
    elif liq < 0.6: status, head = "WAIT", "Wait: thin liquidity"
    else: status, head = "GO", "Conditions fit - setup is tradable now"
    tfm = timedelta(minutes=tf_minutes)
    if tf_minutes < 60: nc = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % tf_minutes) + tfm
    else: nc = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    win = next_window(now, currencies, futures, bl, profile) if status != "GO" else None
    return {"status": status, "headline": head, "reasons": reasons, "now_utc": iso(now), "liquidity": round(liq, 2), "active_sessions": active_sessions(now), "market_open": op,
            "next_window": win, "next_candle_close": iso(nc), "signal_valid_until": iso(now + 3 * tfm) if consensus else None,
            "sessions": [{"name": w["name"], "start": iso(w["start"]), "end": iso(w["end"])} for w in session_windows(now)],
            "blackouts": [{"name": b["name"], "currency": b["currency"], "time": iso(b["time"]), "start": iso(b["start"]), "end": iso(b["end"])} for b in bl if b["end"] > now - timedelta(hours=8) and b["start"] < now + timedelta(hours=40)],
            "tz_exact": TZ_OK}
