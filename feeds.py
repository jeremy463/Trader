"""Market data feeds. All return CLOSED candles only (no repainting). Network/feed errors raise; callers must not trade on errors."""
from __future__ import annotations
import json, logging, os, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from .data import Candle, Quote, load_csv, _parse_ts

log = logging.getLogger("apex.feed")


def retry(fn, tries=3, base=0.5):
    for k in range(tries):
        try: return fn()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429: raise          # permanent client errors (bad key, bad symbol) are not retried
            if k == tries - 1: raise
            time.sleep(base * 2 ** k)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if k == tries - 1: raise
            log.warning("feed error %s; retry %d", e, k + 1); time.sleep(base * 2 ** k)


class CSVFeed:
    """Replay of YOUR OWN historical CSV (development/testing only; forbidden in production). Quotes are simulated from closes."""
    label = "TEST DATA (csv)"
    def __init__(self, path: str, spread: float, start: Optional[int] = None):
        self.all = load_csv(path); self.spread = spread
        self.cursor = start or min(len(self.all), 600)

    def advance(self, n: int = 1) -> int:
        self.cursor = min(len(self.all), self.cursor + n); return self.cursor

    def candles(self, symbol, tf_seconds, count=600) -> List[Candle]: return self.all[max(0, self.cursor - count):self.cursor]
    def quote(self, symbol) -> Optional[Quote]:
        c = self.all[self.cursor - 1]; return Quote(c.c - self.spread / 2, c.c + self.spread / 2, c.ts, "simulated-csv")
    def now(self) -> datetime: return self.all[self.cursor - 1].ts


class OandaFeed:
    """OANDA v20 REST (practice accounts are free and serve real prices). Completed candles only. Validate with `web-check` on your server."""
    label = "OANDA"
    GRAN = {60: "M1", 300: "M5", 900: "M15", 1800: "M30", 3600: "H1", 14400: "H4", 86400: "D"}

    def __init__(self, account_id: str, token_env="OANDA_TOKEN", practice=True, timeout=10, base=None):
        self.acct, self.timeout = account_id, timeout
        self.token = os.environ.get(token_env)
        if not self.token: raise RuntimeError(f"env {token_env} not set")
        self.base = (base or ("https://api-fxpractice.oanda.com" if practice else "https://api-fxtrade.oanda.com")).rstrip("/")

    def _get(self, path):
        req = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {self.token}"})
        return retry(lambda: json.load(urllib.request.urlopen(req, timeout=self.timeout)))

    @staticmethod
    def _inst(sym): return sym[:3] + "_" + sym[3:]

    def candles(self, symbol, tf_seconds, count=600) -> List[Candle]:
        d = self._get(f"/v3/instruments/{self._inst(symbol)}/candles?granularity={self.GRAN[tf_seconds]}&count={count + 1}&price=M")
        out = [Candle(_parse_ts(c["time"].split(".")[0] + "Z"), float(c["mid"]["o"]), float(c["mid"]["h"]), float(c["mid"]["l"]),
                      float(c["mid"]["c"]), float(c["volume"])) for c in d["candles"] if c.get("complete")]
        if not out: raise RuntimeError("no completed candles returned")
        return out

    def quote(self, symbol) -> Optional[Quote]:
        d = self._get(f"/v3/accounts/{self.acct}/pricing?instruments={self._inst(symbol)}")["prices"][0]
        if not d.get("tradeable"): return None
        return Quote(float(d["bids"][0]["price"]), float(d["asks"][0]["price"]), _parse_ts(d["time"].split(".")[0] + "Z"), "oanda")

    def now(self) -> datetime: return datetime.now(timezone.utc)


class FeedError(RuntimeError): pass


class TwelveDataFeed:
    """Twelve Data REST (forex pairs). Bars carry the bar-OPEN time in UTC (we request timezone=UTC); the still-forming last bar is dropped
    so analysis never repaints. The provider has no bid/ask on this endpoint set, so quote() returns the last price with an ASSUMED 1-pip
    spread, labelled as such. Free-plan rate limits are low: the service caches market data (10 s) and the radar costs scans."""
    label = "Twelve Data"
    BASE = "https://api.twelvedata.com"
    INTERVAL = {60: "1min", 300: "5min", 900: "15min", 1800: "30min", 3600: "1h", 14400: "4h", 86400: "1day"}

    def __init__(self, key_env="TWELVEDATA_API_KEY", timeout=12, base=None):
        self._key = os.environ.get(key_env, "")
        if not self._key: raise RuntimeError(f"env {key_env} not set")
        self.timeout, self.base = timeout, (base or self.BASE).rstrip("/")

    @staticmethod
    def _sym(symbol: str) -> str:
        if len(symbol) != 6 or not symbol.isalpha(): raise FeedError(f"{symbol}: Twelve Data feed supports forex pairs only")
        return symbol[:3] + "/" + symbol[3:]

    def _get(self, path: str, params: dict) -> dict:
        from urllib.parse import urlencode
        url = f"{self.base}{path}?{urlencode({**params, 'apikey': self._key})}"
        try: raw = retry(lambda: urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "apex-feed/1.0"}), timeout=self.timeout).read(2_000_000))
        except urllib.error.HTTPError as e: raise FeedError("rate limited by provider" if e.code == 429 else f"provider http {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e: raise FeedError(f"provider unreachable ({type(e).__name__})") from None   # never echo the URL: it contains the key
        try: d = json.loads(raw)
        except ValueError: raise FeedError("provider returned invalid JSON") from None
        if isinstance(d, dict) and d.get("status") == "error": raise FeedError("provider error: " + str(d.get("message", "unknown"))[:160].replace(self._key, "***"))
        return d

    def candles(self, symbol, tf_seconds, count=600) -> List[Candle]:
        if tf_seconds not in self.INTERVAL: raise FeedError("unsupported timeframe")
        d = self._get("/time_series", {"symbol": self._sym(symbol), "interval": self.INTERVAL[tf_seconds], "outputsize": min(count + 1, 5000), "timezone": "UTC", "order": "ASC"})
        out = []
        for v in d.get("values", []):
            try:
                t = datetime.fromisoformat(v["datetime"].replace(" ", "T")).replace(tzinfo=timezone.utc)
                out.append(Candle(t, float(v["open"]), float(v["high"]), float(v["low"]), float(v["close"]), float(v.get("volume") or 0)))
            except (KeyError, ValueError, TypeError): continue
        out.sort(key=lambda c: c.ts)
        now = datetime.now(timezone.utc)
        if out and out[-1].ts + timedelta(seconds=tf_seconds) > now: out.pop()      # drop the forming bar
        if len(out) < 60: raise FeedError("provider returned too few completed candles")
        return out[-count:]

    def quote(self, symbol) -> Optional[Quote]:
        d = self._get("/price", {"symbol": self._sym(symbol)})
        try: px = float(d["price"])
        except (KeyError, ValueError, TypeError): return None
        from .instruments import get_spec
        half = get_spec(symbol).pip_size / 2
        return Quote(px - half, px + half, datetime.now(timezone.utc), "simulated-spread (Twelve Data last price, 1-pip spread assumed)")

    def now(self) -> datetime: return datetime.now(timezone.utc)


class FailoverFeed:
    """Primary provider with an automatic fallback. `label` names whichever provider actually served the last request."""
    def __init__(self, primary, secondary):
        self.primary, self.secondary, self.log = primary, secondary, logging.getLogger("apex.feed"); self._last = primary

    @property
    def label(self) -> str: return getattr(self._last, "label", "feed")

    def _try(self, fn: str, *a):
        try: r = getattr(self.primary, fn)(*a); self._last = self.primary; return r
        except Exception as e:
            self.log.warning("primary feed failed (%s): %s; using fallback", fn, str(e)[:120])
            r = getattr(self.secondary, fn)(*a); self._last = self.secondary; return r

    def candles(self, symbol, tf_seconds, count=600): return self._try("candles", symbol, tf_seconds, count)
    def quote(self, symbol): return self._try("quote", symbol)
    def now(self): return datetime.now(timezone.utc)
