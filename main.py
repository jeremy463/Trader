"""Assemble and run the web service; admin bootstrap (admin accounts can ONLY be created from the server CLI)."""
from __future__ import annotations
import getpass, logging, os, sys, time
from typing import Optional
from . import security as sec
from .billing import Billing, PaystackClient, DEFAULT_TIERS, Tier
from .db import DB, audit
from .service import Service, MarketData
from .mailer import Mailer
from .server import make_server
from .webconfig import WebSettings, load_web_settings
from ..logsetup import setup_logging

log = logging.getLogger("apex.web")


def _one_feed(kind: str, s: WebSettings):
    if kind == "oanda":
        from ..feeds import OandaFeed; return OandaFeed(s.oanda_account, token_env=s.oanda_token_env, practice=s.oanda_practice)
    if kind == "twelvedata":
        from ..feeds import TwelveDataFeed; return TwelveDataFeed(s.twelvedata_key_env)
    if kind == "mt5":
        from ..mt5_adapter import load_mt5, MT5Feed
        f = MT5Feed(load_mt5(), s.server_utc_offset_hours)
        if f.offset is None: f.detect_offset(s.feed_symbols[0])
        return f
    return None   # csv: handled inside MarketData


def make_feed(s: WebSettings):
    """Build the configured market-data provider. A provider whose credentials are missing is skipped (the admin checklist flags it) instead of crashing startup."""
    feeds = []
    for kind in (s.feed_type, s.feed_fallback_type):
        if not kind or kind == "csv": continue
        try: feeds.append(_one_feed(kind, s))
        except Exception as e: log.error("market-data provider %s unavailable: %s", kind, e)
    if not feeds: return None
    if len(feeds) == 2:
        from ..feeds import FailoverFeed; return FailoverFeed(feeds[0], feeds[1])
    return feeds[0]


def build_service(s: WebSettings, feed=None, paystack_client=None, trading_app=None) -> Service:
    setup_logging(s.log_path)
    db = DB(s.db_path)
    secret = s.paystack_secret()
    client = paystack_client if paystack_client is not None else (PaystackClient(secret) if secret else None)
    tiers = dict(DEFAULT_TIERS)
    for k, v in s.price_kobo.items():
        if k in ("pro", "elite") and isinstance(v, int) and v > 0: t = tiers[k]; tiers[k] = Tier(t.name, t.label, v, t.scans_per_day, t.all_methods, t.plan, t.edge, t.upload, t.symbols, t.alerts)
    billing = Billing(db, client, secret, s.currency, s.base_url, tiers, s.plan_codes)
    if client is None: log.warning("PAYSTACK secret not set: billing DISABLED (checkout will return 503, webhooks rejected)")
    if feed is None: feed = make_feed(s)
    mailer = Mailer(s.smtp_host, s.smtp_port, s.smtp_user, os.environ.get(s.smtp_password_env, ""), s.smtp_from, s.smtp_security)
    if not mailer.enabled: log.warning("SMTP not configured: email verification, emailed resets, receipts and alerts are DISABLED")
    svc = Service(s, db, billing, MarketData(s, feed), mailer)
    def receipt(user, tier, until, ref):
        mailer.send_async(user["email"], f"{s.brand_name} payment received", f"Thank you. Your {tier.title()} plan is active until {time.strftime('%Y-%m-%d', time.gmtime(until))} (UTC).\nReference: {ref}\n\nAnalysis only, not financial advice.")
    billing.on_credit = receipt
    if trading_app is None and s.trading_config:
        from ..settings import load_settings
        from ..app import App
        trading_app = App(load_settings(s.trading_config))
    svc.trading = trading_app
    return svc


def run(config: Optional[str]) -> int:
    s = load_web_settings(config)
    svc = build_service(s)
    n_admin = svc.db.one("SELECT COUNT(*) AS n FROM users WHERE role='admin'")["n"]
    if not n_admin: print("WARNING: no admin account yet. Run: python -m apex.cli web-create-admin --email you@example.com")
    srv = make_server(svc)
    import signal, threading
    stop = threading.Event()
    def worker():
        while not stop.wait(s.alerts_interval_seconds):
            try: svc.alerts_run_once()
            except Exception: log.exception("alert worker error")
    if svc.mailer.enabled: threading.Thread(target=worker, daemon=True).start()
    def news_worker():
        while True:
            try: svc.news_sync()
            except Exception: log.exception("news worker error")
            if stop.wait(max(1, s.news_interval_minutes) * 60): return
    if s.news_enabled: threading.Thread(target=news_worker, daemon=True).start()
    def term(*_): threading.Thread(target=srv.shutdown, daemon=True).start()
    for sg in (signal.SIGTERM, signal.SIGINT):
        try: signal.signal(sg, term)
        except ValueError: pass
    print(f"APEX web service on http://{s.host}:{s.port}  (public URL {s.base_url}; production={s.production}; billing={'ON' if svc.billing.enabled else 'OFF'})")
    try: srv.serve_forever()
    finally: stop.set(); srv.server_close()
    return 0


def backup(config: Optional[str], out_path: str) -> int:
    """Online SQLite backup (safe while serving); output is chmod 600."""
    s = load_web_settings(config); DB(s.db_path).backup(out_path); print(f"backup written to {out_path} (mode 600)"); return 0


def _say(x): print(x, flush=True)


def _ask(prompt: str) -> str:
    print(prompt, end="", flush=True); return sys.stdin.readline().strip()


def create_admin(config: Optional[str], email: str, password: Optional[str] = None, confirm_code: Optional[str] = None, out=_say) -> int:
    s = load_web_settings(config); db = DB(s.db_path)
    if not sec.valid_email(email): out("invalid email"); return 2
    pw = password or os.environ.get("APEX_ADMIN_PASSWORD") or getpass.getpass("Admin password (12+ chars): ")
    if password is None and not os.environ.get("APEX_ADMIN_PASSWORD") and getpass.getpass("Repeat: ") != pw: out("passwords differ"); return 2
    prob = sec.password_problem(pw, email)
    if prob: out(prob); return 2
    secret = sec.new_totp_secret()
    out(f"\nAdd this to your authenticator app (Google Authenticator / Authy / 1Password):\n  secret: {secret}\n  uri:    {sec.totp_uri(secret, email)}")
    code = confirm_code or _ask("Enter the 6-digit code from the app to confirm 2FA: ")
    step = sec.totp_verify(secret, code, 0)
    if step is None: out("code did not match - admin NOT created"); return 2
    with db.tx() as c:
        ex = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if ex: c.execute("UPDATE users SET role='admin', pw_hash=?, totp_secret=?, totp_enabled=1, totp_last_step=?, status='active' WHERE id=?", (sec.hash_password(pw), secret, step, ex["id"])); uid = ex["id"]
        else: uid = c.execute("INSERT INTO users(email,pw_hash,role,totp_secret,totp_enabled,totp_last_step,created) VALUES(?,?, 'admin', ?,1,?,?)", (email, sec.hash_password(pw), secret, step, int(time.time()))).lastrowid
        audit(c, "cli", "create_admin", str(uid))
    out(f"admin account ready: {email} (2FA enabled)"); return 0


def reset_admin_2fa(config: Optional[str], email: str, confirm_code: Optional[str] = None, out=_say) -> int:
    s = load_web_settings(config); db = DB(s.db_path)
    u = db.one("SELECT * FROM users WHERE email=? AND role='admin'", (email,))
    if not u: out("no such admin"); return 2
    secret = sec.new_totp_secret(); out(f"new secret: {secret}\nuri: {sec.totp_uri(secret, email)}")
    code = confirm_code or _ask("6-digit code: "); step = sec.totp_verify(secret, code, 0)
    if step is None: out("code mismatch - unchanged"); return 2
    with db.tx() as c:
        c.execute("UPDATE users SET totp_secret=?, totp_enabled=1, totp_last_step=? WHERE id=?", (secret, step, u["id"])); c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],)); audit(c, "cli", "reset_admin_2fa", str(u["id"]))
    out("2FA reset; all admin sessions revoked"); return 0


def check(config: Optional[str], strict: bool = False) -> int:
    """Live go-live readiness (the same checks as Admin > Go-live checklist). Exit 1 on any FAIL (or any WARN with --strict)."""
    s = load_web_settings(config); svc = build_service(s); r = svc.readiness(force=True); tag = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    for row in r["rows"]: print(f"{tag[row['status']]} {row['label']}: {row['detail']}")
    c = r["counts"]; print(f"\n{c['ok']} ok, {c['warn']} warnings, {c['fail']} failures -> " + ("READY for customers" if r["ready"] and not (strict and c["warn"]) else "NOT ready"))
    return 0 if r["ready"] and not (strict and c["warn"]) else 1


def paystack_check(config: Optional[str]) -> int:
    """Validate the Paystack secret key and tell you exactly what to register in the dashboard."""
    s = load_web_settings(config); key = s.paystack_secret()
    if not key: print(f"FAIL env {s.paystack_secret_env} is not set. Add your key there (see .env.example), then rerun."); return 1
    mode = "LIVE" if key.startswith("sk_live_") else "TEST" if key.startswith("sk_test_") else None
    if not mode: print("FAIL the value does not look like a Paystack secret key (expected sk_live_... or sk_test_...)"); return 1
    try: PaystackClient(key).balance(); ok = True
    except Exception as e: ok, err = False, str(e)
    print(f"{'OK  ' if ok else 'FAIL'} {mode} secret key " + ("accepted by Paystack" if ok else f"rejected or unreachable: {err}"))
    print(f"\nIn Paystack Dashboard > Settings > API Keys & Webhooks ({mode.lower()} mode):\n  Webhook URL : {s.base_url}/webhooks/paystack\n  Callback URL: {s.base_url}/billing/return   (sent per transaction by the app)")
    print("Optional second layer: copy Paystack's published webhook IP addresses (from their webhooks documentation) into `paystack_webhook_ips`.")
    if mode == "TEST": print("NOTE: a TEST key moves no real money. Swap in the sk_live_ key only after a full test payment works end to end.")
    if s.production and not s.price_kobo: print("WARN price_kobo is not set"); 
    return 0 if ok else 1


def web_init(out: str, domain: str, company: str, support_email: str, jurisdiction: str, feed: str, oanda_account: str, fallback: str, symbols: str,
             pro_price: float, elite_price: float, currency: str, force: bool) -> int:
    """Write a production config with safe defaults. Prices are given in major units (e.g. 15000 for NGN 15,000) and stored in kobo."""
    import json as _json
    from datetime import date
    if os.path.exists(out) and not force: print(f"{out} exists; use --force to overwrite"); return 2
    cfg = {"production": True, "base_url": f"https://{domain}", "secure_cookies": True, "trusted_proxy": True, "host": "127.0.0.1", "port": 8080, "trusted_proxies": ["127.0.0.1", "::1"],
           "brand_name": "APEX Trader", "company_name": company, "support_email": support_email, "legal_jurisdiction": jurisdiction, "legal_updated": date.today().isoformat(), "legal_reviewed": False,
           "db_path": "/var/lib/apex/apex_web.db", "log_path": "/var/log/apex/web.log", "edge_path": "/var/lib/apex/apex_edge.json", "backup_dir": "/var/backups/apex",
           "currency": currency, "price_kobo": {"pro": int(round(pro_price * 100)), "elite": int(round(elite_price * 100))}, "paystack_webhook_ips": [],
           "feed_type": feed, "feed_fallback_type": fallback, "feed_symbols": [x.strip().upper() for x in symbols.split(",") if x.strip()], "oanda_account": oanda_account, "oanda_practice": True,
           "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_from": "", "smtp_security": "starttls", "news_enabled": True, "news_interval_minutes": 15, "alerts_interval_seconds": 60}
    todo = WebSettings(**cfg).problems()
    with open(out, "w") as f: _json.dump(cfg, f, indent=2)
    try: os.chmod(out, 0o640)
    except OSError: pass
    print(f"wrote {out}")
    print("\nBefore the service will start you must resolve:" if todo else "\nConfig is valid.")
    for t in todo: print("  -", t)
    print("\nThen: set secrets in the env file (.env.example lists them), create the admin (web-create-admin), run web-check and paystack-check, and follow deploy/DEPLOY.md.")
    print("Remember: switch `oanda_practice` to false only if you want your live OANDA account's prices; practice accounts serve real prices too.")
    return 0


def news_check(config: Optional[str]) -> int:
    """Fetch every configured source once (no DB writes) and report what we can actually read from THIS server."""
    from . import service as _s
    from .. import news as N
    s = load_web_settings(config); ok = True
    for src in (s.news_sources or N.DEFAULT_SOURCES):
        try:
            data = N.fetch(src["url"], 10, s.allow_private_feeds)
            n = len(N.parse_ff_json(data, src["name"])) if src["type"] == "ff_json" else len(N.parse_feed(data))
            print(f"OK   {src['name']}: {n} {'events' if src['type'] == 'ff_json' else 'items'}")
        except Exception as e: ok = False; print(f"FAIL {src['name']}: {e}")
    print("all sources readable" if ok else "some sources failed: check the URL, your firewall, and the provider's terms"); return 0 if ok else 1
