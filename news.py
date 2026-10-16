"""News and economic-calendar engine: safe fetching (SSRF guard, size caps, XML bomb defence), parsing (Fair Economy/ForexFactory-style JSON,
RSS 2.0, Atom), currency tagging, headline sentiment and surprise logic.
Headline sentiment is a crude keyword heuristic and is labelled as such everywhere it is shown: context, not a trading signal."""
from __future__ import annotations
import ipaddress, json, math, re, socket, time, urllib.error, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

DEFAULT_SOURCES = [
    {"name": "Economic calendar (Fair Economy / ForexFactory)", "type": "ff_json", "url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json"},
    {"name": "Federal Reserve press releases", "type": "rss", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "currencies": ["USD"]},
    {"name": "ECB press releases", "type": "rss", "url": "https://www.ecb.europa.eu/rss/press.html", "currencies": ["EUR"]},
]
MAX_BYTES = 2_000_000


class FeedError(Exception): pass


def check_url(url: str, allow_private: bool = False) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname: raise FeedError("only http(s) URLs are allowed")
    if allow_private: return
    try: infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror: raise FeedError("host does not resolve")
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified: raise FeedError("blocked address (private/loopback)")


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    def __init__(self, allow_private): self.allow_private = allow_private
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self.allow_private); return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, timeout: float = 10, allow_private: bool = False, max_bytes: int = MAX_BYTES) -> bytes:
    check_url(url, allow_private)
    op = urllib.request.build_opener(_Redirect(allow_private))
    req = urllib.request.Request(url, headers={"User-Agent": "apex-news/1.0", "Accept": "application/json, application/rss+xml, application/xml, text/xml, */*"})
    try:
        with op.open(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e: raise FeedError(f"http {e.code}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e: raise FeedError(f"unreachable ({type(e).__name__})")
    if len(data) > max_bytes: raise FeedError("response too large")
    return data


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ff_json(data: bytes, source: str = "calendar") -> List[dict]:
    """Fair Economy / ForexFactory-style: [{title,country,date,impact,forecast,previous,(actual)}]. 'country' carries the currency code."""
    try: rows = json.loads(data)
    except ValueError: raise FeedError("calendar is not valid JSON")
    if not isinstance(rows, list): raise FeedError("calendar JSON is not a list")
    out = []
    for r in rows[:5000]:
        try:
            cur = str(r.get("currency") or r.get("country") or "").strip().upper(); imp = str(r.get("impact", "")).strip().lower(); name = str(r.get("title") or r.get("name") or "").strip()[:100]
            if not (len(cur) == 3 and cur.isalpha()) or cur == "ALL" or imp not in ("high", "medium", "low", "holiday") or not name: continue
            t = datetime.fromisoformat(str(r.get("datetimeISO") or r.get("date")).replace("Z", "+00:00"))
            if t.tzinfo is None: continue
            out.append({"time_utc": _utc_iso(t), "currency": cur, "impact": imp, "name": name, "forecast": str(r.get("forecast") or "")[:30], "previous": str(r.get("previous") or "")[:30], "actual": str(r.get("actual") or "")[:30], "source": source})
        except (ValueError, TypeError, AttributeError): continue
    return out


def parse_feed(data: bytes) -> List[dict]:
    """RSS 2.0 / RDF / Atom -> [{guid,title,link,published(epoch)}]. DOCTYPE/ENTITY are refused outright (entity-expansion defence)."""
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", data, re.I): raise FeedError("XML with DOCTYPE/ENTITY declarations is refused")
    try: root = ET.fromstring(data)
    except ET.ParseError: raise FeedError("feed is not valid XML")
    strip = lambda t: t.split("}", 1)[-1]
    out = []
    for el in root.iter():
        if strip(el.tag) not in ("item", "entry"): continue
        f = {strip(c.tag): c for c in el}; text = lambda k: (f[k].text or "").strip() if k in f else ""
        title = re.sub(r"\s+", " ", text("title"))[:300]
        link = text("link") or (f["link"].get("href", "") if "link" in f else "")
        raw = text("pubDate") or text("published") or text("updated") or text("date"); ts = 0
        try: ts = int(parsedate_to_datetime(raw).timestamp())
        except (TypeError, ValueError):
            try: ts = int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
            except ValueError: ts = int(time.time())
        if title: out.append({"guid": (text("guid") or text("id") or link or title)[:300], "title": title, "link": link[:500] if link.startswith(("http://", "https://")) else "", "published": ts})
    return out[:500]


# ---------------- tagging & sentiment ----------------
CUR_WORDS = {
    "USD": r"\b(fed|fomc|powell|federal reserve|u\.s\.|united states|us dollar|dollar|treasury|nonfarm|payrolls)\b", "EUR": r"\b(ecb|lagarde|eurozone|euro area|euro|bund)\b",
    "GBP": r"\b(bank of england|boe|bailey|sterling|pound|britain|u\.k\.|uk)\b", "JPY": r"\b(boj|bank of japan|ueda|yen|japan|japanese)\b", "CHF": r"\b(snb|swiss national bank|swiss franc|switzerland|swiss)\b",
    "AUD": r"\b(rba|reserve bank of australia|australian dollar|aussie|australia|australian)\b", "NZD": r"\b(rbnz|reserve bank of new zealand|new zealand|kiwi)\b", "CAD": r"\b(boc|bank of canada|loonie|canadian|canada|macklem)\b"}
CUR_RE = {k: re.compile(v, re.I) for k, v in CUR_WORDS.items()}
HAWK = ["rate hike", "raise rates", "raises rates", "raised rates", "hike", "tightening", "hawkish", "inflation rises", "inflation higher", "higher than expected", "hotter", "strong jobs", "beats expectations", "above expectations", "higher for longer", "upgrade", "robust growth"]
DOVE = ["rate cut", "cuts rates", "cut rates", "cut its", "easing", "dovish", "inflation slows", "inflation falls", "lower than expected", "cooler", "weak jobs", "below expectations", "slowdown", "recession", "contraction", "downgrade", "stimulus", "unemployment rises", "unemployment increases"]


def tag_currencies(text: str, hint: Optional[List[str]] = None) -> List[str]:
    cur = [c for c, rx in CUR_RE.items() if rx.search(text)]
    for h in hint or []:
        if h not in cur: cur.append(h)
    return cur


def headline_sentiment(text: str) -> float:
    t = text.lower(); h = sum(1 for w in HAWK if w in t); d = sum(1 for w in DOVE if w in t)
    return 0.0 if h + d == 0 else max(-1.0, min(1.0, (h - d) / (h + d)))


def currency_sentiment(items: List[dict], now: float, halflife_h: float = 6.0) -> Dict[str, dict]:
    acc: Dict[str, List[float]] = {}
    for it in items:
        if not it["currencies"] or it["sentiment"] == 0: continue
        w = 0.5 ** (max(0.0, now - it["published"]) / 3600 / halflife_h)
        for c in it["currencies"].split(","):
            if c: a = acc.setdefault(c, [0.0, 0.0, 0]); a[0] += w * it["sentiment"]; a[1] += w; a[2] += 1
    return {c: {"score": round(a[0] / a[1], 2), "n": a[2]} for c, a in acc.items() if a[1] > 0}


INVERSE = ("unemployment", "jobless", "claims")


def _num(s: str) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
    if not m: return None
    v = float(m.group()); u = (s or "").strip().upper()[-1:]
    return v * {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(u, 1.0)


def surprise(title: str, actual: str, forecast: str) -> int:
    """+1 = better than expected for the currency, -1 = worse, 0 = in line/unknown. Unemployment-type series are inverted."""
    a, f = _num(actual), _num(forecast)
    if a is None or f is None or a == f: return 0
    s = 1 if a > f else -1
    return -s if any(w in title.lower() for w in INVERSE) else s


def pair_bias(spec, sentiments: Dict[str, dict]) -> Optional[float]:
    """Headline-sentiment differential for a forex pair (base minus quote). None when there is nothing to say."""
    if spec.asset_class != "forex": return None
    b, q = sentiments.get(spec.base_ccy), sentiments.get(spec.quote_ccy)
    if not b and not q: return None
    return round((b["score"] if b else 0.0) - (q["score"] if q else 0.0), 2)
