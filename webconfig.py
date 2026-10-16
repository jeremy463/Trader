"""Web settings. Secrets (API keys, SMTP password) come from the environment only, never from this file.
Production mode HARD-BLOCKS unsafe or dishonest configuration (plain http, test data, missing legal/company details, unset prices).
Credentials you add later (Paystack, SMTP, data-provider tokens) never stop the app from starting; they appear in the admin go-live checklist."""
from __future__ import annotations
import ipaddress, json, os, re
from dataclasses import dataclass, field, fields
from datetime import date
from typing import Dict, List, Optional
from urllib.parse import urlparse

FEEDS = ("oanda", "twelvedata", "mt5", "csv")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")


@dataclass
class WebSettings:
    # ---- identity & legal (rendered to users; required in production) ----
    brand_name: str = "APEX Trader"
    company_name: str = ""                       # legal entity that operates the service
    support_email: str = ""
    legal_jurisdiction: str = ""                 # e.g. "Federal Republic of Nigeria"
    legal_updated: str = ""                      # ISO date (YYYY-MM-DD) the terms/risk text was last reviewed or changed
    legal_reviewed: bool = False                 # you confirm a qualified lawyer reviewed the legal text for your jurisdiction
    refund_policy: str = ""                      # optional one-paragraph policy shown on /legal
    # ---- network ----
    base_url: str = "http://127.0.0.1:8080"      # public URL users reach (must be https in production)
    host: str = "127.0.0.1"
    port: int = 8080
    db_path: str = "apex_web.db"
    production: bool = False
    secure_cookies: bool = False
    trusted_proxy: bool = False                  # true only when behind a reverse proxy (Caddy/nginx)
    trusted_proxies: List[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])   # peers allowed to set X-Forwarded-For
    # ---- payments ----
    paystack_secret_env: str = "PAYSTACK_SECRET_KEY"
    paystack_webhook_ips: List[str] = field(default_factory=list)   # optional second layer: copy Paystack's published webhook IPs here
    currency: str = "NGN"
    plan_codes: Dict[str, str] = field(default_factory=dict)        # Paystack plan_code -> "pro"|"elite" (subscription renewals)
    price_kobo: Dict[str, int] = field(default_factory=dict)        # REQUIRED in production: {"pro": <kobo>, "elite": <kobo>}
    # ---- market data ----
    feed_type: str = "csv"                       # oanda | twelvedata | mt5 | csv (csv = your own historical files, development only)
    feed_fallback_type: str = ""                 # optional second provider: "" | oanda | twelvedata
    feed_symbols: List[str] = field(default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY"])
    data_dir: str = "market_data"                # csv feed only (development): files like EURUSD_M15.csv
    oanda_account: str = ""
    oanda_practice: bool = True                  # practice accounts are free and still serve real prices
    oanda_token_env: str = "OANDA_TOKEN"
    twelvedata_key_env: str = "TWELVEDATA_API_KEY"
    server_utc_offset_hours: Optional[float] = None
    trading_config: str = ""                     # path to a trading settings JSON -> enables the admin trading panel
    edge_path: str = "apex_edge.json"
    log_path: str = "apex_web.log"
    backup_dir: str = ""                         # where deploy/backup.sh writes backups; the admin checklist warns if the newest is old
    # ---- email ----
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password_env: str = "SMTP_PASSWORD"
    smtp_from: str = ""
    smtp_security: str = "starttls"              # starttls | ssl | none
    # ---- news & calendar ----
    news_enabled: bool = False                   # opt-in: makes outbound requests to the configured feeds
    news_sources: List[dict] = field(default_factory=list)   # [] = DEFAULT_SOURCES; each: {name,type: ff_json|rss, url, currencies?}
    news_interval_minutes: int = 15
    allow_private_feeds: bool = False            # SSRF guard: must stay False in production
    # ---- limits & sessions ----
    watchlist_limits: Dict[str, int] = field(default_factory=lambda: {"free": 1, "pro": 8, "elite": 20, "admin": 20})
    alerts_interval_seconds: int = 60
    alerts_max_per_user_per_day: int = 20
    session_hours_user: int = 168
    session_hours_admin: int = 8
    idle_minutes_user: int = 1440
    idle_minutes_admin: int = 30

    @property
    def host_name(self) -> str: return urlparse(self.base_url).hostname or ""

    def paystack_secret(self) -> str: return os.environ.get(self.paystack_secret_env, "")

    @property
    def data_mode(self) -> str: return "test" if self.feed_type == "csv" else "live"

    def problems(self) -> List[str]:
        """Blocking problems only. Missing credentials are NOT blocking (see service.readiness())."""
        from ..instruments import get_spec
        p: List[str] = []
        if self.feed_type not in FEEDS: p.append(f"feed_type must be one of {FEEDS}")
        if self.feed_fallback_type not in ("", "oanda", "twelvedata") or (self.feed_fallback_type and self.feed_fallback_type == self.feed_type): p.append("feed_fallback_type must be '', oanda or twelvedata and differ from feed_type")
        if not self.feed_symbols: p.append("feed_symbols must list at least one symbol")
        for s in self.feed_symbols:
            try:
                if self.feed_type in ("oanda", "twelvedata") and get_spec(s).asset_class != "forex": p.append(f"{s}: {self.feed_type} serves forex pairs only")
            except KeyError: p.append(f"unknown symbol in feed_symbols: {s!r}")
        for sdef in self.news_sources:
            if not isinstance(sdef, dict) or sdef.get("type") not in ("ff_json", "rss") or not str(sdef.get("url", "")).startswith(("http://", "https://")) or not sdef.get("name"): p.append(f"invalid news source: {sdef!r}")
        for lst, nm in ((self.trusted_proxies, "trusted_proxies"), (self.paystack_webhook_ips, "paystack_webhook_ips")):
            for n in lst:
                try: ipaddress.ip_network(n, strict=False)
                except ValueError: p.append(f"{nm} entry {n!r} is not an IP/CIDR")
        if self.smtp_security not in ("starttls", "ssl", "none"): p.append("smtp_security must be starttls|ssl|none")
        if self.smtp_host and not self.smtp_from: p.append("smtp_from required when smtp_host is set")
        if self.currency not in ("NGN", "GHS", "ZAR", "KES", "USD"): p.append("unsupported currency")
        for k, v in self.price_kobo.items():
            if k not in ("pro", "elite") or isinstance(v, bool) or not isinstance(v, int) or v < 100: p.append(f"price_kobo[{k!r}] must be an integer >= 100 for pro/elite")
        if not self.brand_name.strip() or len(self.brand_name) > 60 or any(c in self.brand_name for c in "\r\n<>&\"'"): p.append("brand_name must be 1-60 plain characters (no line breaks, quotes, < > or &)")
        if self.legal_updated:
            try: date.fromisoformat(self.legal_updated)
            except ValueError: p.append("legal_updated must be an ISO date (YYYY-MM-DD)")
        if self.support_email and not EMAIL_RE.match(self.support_email): p.append("support_email is not a valid email address")
        if self.production:
            if not self.base_url.startswith("https://"): p.append("production requires an https:// base_url")
            if not self.secure_cookies: p.append("production requires secure_cookies=true")
            if self.host not in ("127.0.0.1", "localhost") and not self.trusted_proxy: p.append("bind to 127.0.0.1 behind a TLS reverse proxy (or set trusted_proxy)")
            if self.allow_private_feeds: p.append("production must not allow private feed addresses")
            if self.feed_type == "csv": p.append("production cannot use feed_type=csv (that is test data); use oanda, twelvedata or mt5")
            if self.feed_type == "oanda" and not self.oanda_account: p.append("feed_type=oanda requires oanda_account")
            if self.smtp_host and self.smtp_security == "none": p.append("production SMTP must use starttls or ssl")
            for need, nm in ((self.company_name, "company_name"), (self.support_email, "support_email"), (self.legal_jurisdiction, "legal_jurisdiction"), (self.legal_updated, "legal_updated")):
                if not need.strip(): p.append(f"production requires {nm} (shown to users on /legal)")
            if not self.legal_reviewed: p.append("production requires legal_reviewed=true: have a qualified lawyer review the terms and risk text for your jurisdiction, then set it")
            for t in ("pro", "elite"):
                if t not in self.price_kobo: p.append(f"production requires an explicit price_kobo['{t}'] (no silent defaults for money)")
        return p


def load_web_settings(path: Optional[str]) -> WebSettings:
    d = {}
    if path:
        with open(path) as f: d = json.load(f)
    bad = set(d) - {f.name for f in fields(WebSettings)}
    if bad: raise ValueError(f"unknown web settings keys: {sorted(bad)}")
    s = WebSettings(**d)
    pr = s.problems()
    if pr: raise ValueError("invalid web settings: " + "; ".join(pr))
    return s
