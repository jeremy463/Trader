"""Hardened HTTP front-end (stdlib). Run behind a TLS reverse proxy (see deploy/Caddyfile) in production."""
from __future__ import annotations
import json, logging, os, re, socket, threading, uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from . import security as sec
from .service import Service, ApiError
from .legal import legal_html

log = logging.getLogger("apex.web.http")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
CT = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8"}
REF_RE = re.compile(r"^apx_[0-9a-f]{24}$")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")
PRE_AUTH_POST = {"/api/login", "/api/register", "/api/reset-password"}


class Ctx:
    def __init__(self, h, body, user, sess, ip, token): self.h, self.body, self.user, self.sess, self.ip, self.token = h, body, user, sess, ip, token
    def s(self, k, maxlen=200):
        v = self.body.get(k)
        if not isinstance(v, str) or len(v) > maxlen: raise ApiError(400, f"invalid '{k}'")
        return v
    def i(self, k):
        v = self.body.get(k)
        if isinstance(v, bool) or not isinstance(v, int): raise ApiError(400, f"invalid '{k}'")
        return v


def make_server(svc: Service, max_conn: int = 64):
    cfg = svc.s
    secure = cfg.secure_cookies
    import ipaddress
    wh_nets = [ipaddress.ip_network(n, strict=False) for n in cfg.paystack_webhook_ips]
    proxy_nets = [ipaddress.ip_network(n, strict=False) for n in cfg.trusted_proxies]
    CK = "__Host-apex_sid" if secure else "apex_sid"
    origin = f"{urlparse(cfg.base_url).scheme}://{urlparse(cfg.base_url).netloc}"
    allowed_hosts = {cfg.host_name} | (set() if cfg.production else {"127.0.0.1", "localhost"})
    static = {}
    for dp, _, files in os.walk(STATIC_DIR):
        for fn in files:
            rel = os.path.relpath(os.path.join(dp, fn), STATIC_DIR).replace(os.sep, "/")
            with open(os.path.join(dp, fn), "rb") as f: static[rel] = f.read()
    routes_get, routes_post = {}, {}

    def route(method, path, auth="user"):
        def deco(fn): (routes_get if method == "GET" else routes_post)[path] = (fn, auth); return fn
        return deco

    # ---------- public ----------
    @route("GET", "/api/health", None)
    def health(c): return 200, {"ok": True}

    @route("POST", "/api/register", None)
    def register(c):
        svc.register(c.s("email", 254), c.s("password", 128), c.ip, c.body.get("accept_terms") is True); return 201, {"ok": True}

    @route("GET", "/api/public", None)
    def public(c): return 200, svc.public_config()

    @route("POST", "/api/verify-email", None)
    def verify_email(c): svc.verify_email(c.s("token", 100), c.ip); return 200, {"ok": True}

    @route("POST", "/api/forgot", None)
    def forgot(c): svc.forgot(c.s("email", 254), c.ip); return 200, {"ok": True}

    @route("GET", "/api/health/deep", None)
    def health_deep(c):
        h = svc.health(); return (200 if h["ok"] else 503), h

    @route("POST", "/api/login", None)
    def login(c):
        r = svc.login(c.s("email", 254), c.s("password", 128), c.body.get("totp") if isinstance(c.body.get("totp"), str) else None, c.ip)
        if r.get("need_totp"): return 200, r
        return 200, {"ok": True, "role": r["role"], "_cookie": r["token"]}

    @route("POST", "/api/reset-password", None)
    def reset(c): svc.reset_password(c.s("token", 100), c.s("password", 128), c.ip); return 200, {"ok": True}

    # ---------- authenticated ----------
    @route("GET", "/api/me")
    def me(c): return 200, svc.me(c.user, c.sess)

    @route("POST", "/api/logout")
    def logout(c): svc.logout(c.token); return 200, {"ok": True, "_clear": True}

    @route("POST", "/api/scan")
    def scan(c): return 200, svc.scan(c.user, c.s("symbol", 12), c.i("tf"))

    @route("POST", "/api/edge")
    def edge(c): return 200, svc.edge(c.user, c.s("symbol", 12), c.i("tf"))

    @route("POST", "/api/risk")
    def risk(c): svc.set_risk(c.user, c.body.get("equity"), c.body.get("risk_pct"), c.body.get("profile") if isinstance(c.body.get("profile"), str) else None); return 200, {"ok": True}

    @route("POST", "/api/password")
    def pw(c): svc.change_password(c.user, c.s("old", 128), c.s("new", 128), c.token); return 200, {"ok": True}

    @route("POST", "/api/delete-account")
    def delacct(c): svc.delete_account(c.user, c.s("password", 128)); return 200, {"ok": True, "_clear": True}

    @route("POST", "/api/checkout")
    def checkout(c):
        if not svc.billing.enabled: raise ApiError(503, "payments are not configured yet")
        if not c.user["email_verified"]: raise ApiError(403, "verify your email address before purchasing")
        if not svc.rl.allow(f"co:{c.user['id']}", 10, 600): raise ApiError(429, "too many checkout attempts")
        try: url = svc.billing.checkout(c.user, c.s("tier", 10))
        except ValueError as e: raise ApiError(400, str(e))
        except Exception as e:
            log.error("checkout failed: %s", e); raise ApiError(502, "payment provider unavailable, try again")
        if not url.startswith("https://"): raise ApiError(502, "invalid payment URL from provider")
        return 200, {"url": url}

    @route("POST", "/api/validate")
    def validate(c): return 200, svc.validate(c.user, c.s("symbol", 12), c.i("tf"))

    @route("GET", "/api/sessions")
    def sessions(c): return 200, {"sessions": svc.sessions(c.user, c.token)}

    @route("POST", "/api/sessions/revoke-others")
    def revoke_others(c): return 200, {"revoked": svc.revoke_other_sessions(c.user, c.token)}

    @route("POST", "/api/resend-verification")
    def resend(c): svc.resend_verification(c.user); return 200, {"ok": True}

    @route("POST", "/api/profiles-compare")
    def pcompare(c): return 200, svc.compare(c.user, c.s("symbol", 12), c.i("tf"))

    @route("GET", "/api/alerts")
    def alerts(c): return 200, svc.alert_list(c.user)

    @route("POST", "/api/alerts")
    def alert_add(c): svc.alert_add(c.user, c.s("symbol", 12), c.i("tf"), c.s("min_grade", 1)); return 201, {"ok": True}

    @route("POST", "/api/alerts/delete")
    def alert_del(c): svc.alert_delete(c.user, c.i("id")); return 200, {"ok": True}

    @route("POST", "/api/alerts/toggle")
    def alert_tog(c): svc.alert_toggle(c.user, c.i("id"), c.body.get("active") is True); return 200, {"ok": True}

    def _currs(path):
        from urllib.parse import parse_qs, urlparse as _u
        q = parse_qs(_u(path).query); return [x for x in (q.get("c") or [""])[0].upper().split(",") if x][:8], q

    @route("GET", "/api/calendar")
    def calendar(c):
        cs, q = _currs(c.h.path); days = (q.get("days") or ["7"])[0]
        return 200, svc.calendar(c.user, int(days) if days.isdigit() else 7, cs)

    @route("GET", "/api/news")
    def news(c):
        cs, q = _currs(c.h.path); hours = (q.get("hours") or ["48"])[0]
        return 200, svc.news(c.user, cs, int(hours) if hours.isdigit() else 48)

    @route("POST", "/api/radar")
    def radar(c): return 200, svc.radar(c.user, c.i("tf"))

    @route("GET", "/api/watchlist")
    def watch_get(c): return 200, svc.watch_get(c.user)

    @route("POST", "/api/watchlist")
    def watch_set(c):
        syms = c.body.get("symbols")
        if not isinstance(syms, list) or len(syms) > 40 or any(not isinstance(x, str) or len(x) > 12 for x in syms): raise ApiError(400, "invalid symbols")
        svc.watch_set(c.user, syms); return 200, {"ok": True}

    @route("GET", "/api/journal")
    def journal(c): return 200, svc.journal(c.user)

    @route("POST", "/api/journal")
    def journal_add(c):
        b = c.body; svc.journal_add(c.user, c.s("symbol", 12), c.s("side", 4), b.get("entry"), b.get("stop"), b.get("exit"), c.s("grade", 1) if "grade" in b else "", c.s("notes", 300) if "notes" in b else ""); return 201, {"ok": True}

    @route("POST", "/api/journal/close")
    def journal_close(c): svc.journal_close(c.user, c.i("id"), c.body.get("exit")); return 200, {"ok": True}

    @route("POST", "/api/journal/delete")
    def journal_del(c): svc.journal_delete(c.user, c.i("id")); return 200, {"ok": True}

    @route("GET", "/api/payments")
    def payments(c): return 200, {"payments": svc.payments(c.user, False)}

    # ---------- admin ----------
    @route("GET", "/api/admin/users", "admin")
    def a_users(c): return 200, {"users": svc.admin_users()}

    @route("POST", "/api/admin/grant", "admin")
    def a_grant(c): svc.admin_grant(c.user, c.i("id"), c.s("tier", 10), c.i("days")); return 200, {"ok": True}

    @route("POST", "/api/admin/status", "admin")
    def a_status(c): svc.admin_status(c.user, c.i("id"), c.s("status", 12)); return 200, {"ok": True}

    @route("POST", "/api/admin/revoke", "admin")
    def a_revoke(c): svc.admin_revoke(c.user, c.i("id")); return 200, {"ok": True}

    @route("POST", "/api/admin/reset-token", "admin")
    def a_reset(c): return 200, {"token": svc.admin_issue_reset(c.user, c.i("id")), "expires_in": 3600}

    @route("GET", "/api/admin/events", "admin")
    def a_events(c): return 200, {"events": svc.admin_events_list()}

    @route("POST", "/api/admin/events", "admin")
    def a_events_set(c): return 200, svc.admin_events_set(c.user, c.s("csv", 200_000))

    @route("GET", "/api/admin/readiness", "admin")
    def a_ready(c):
        if not svc.rl.allow("ready", 6, 60): raise ApiError(429, "checked a moment ago; wait a minute")
        return 200, svc.readiness(force=c.h.path.endswith("force=1"))

    @route("GET", "/api/admin/feeds", "admin")
    def a_feeds(c): return 200, {"feeds": svc.feeds_status(), "enabled": svc.s.news_enabled}

    @route("POST", "/api/admin/feeds/sync", "admin")
    def a_feeds_sync(c):
        if not svc.rl.allow("feedsync", 4, 60): raise ApiError(429, "sync was just run; wait a minute")
        return 200, {"result": svc.news_sync(), "feeds": svc.feeds_status()}

    @route("GET", "/api/admin/stats", "admin")
    def a_stats(c): return 200, svc.admin_stats()

    @route("GET", "/api/admin/payments", "admin")
    def a_pay(c): return 200, {"payments": svc.payments(c.user, True)}

    @route("GET", "/api/admin/audit", "admin")
    def a_audit(c): return 200, svc.audit_tail(150)

    def need_trading():
        if svc.trading is None: raise ApiError(404, "trading panel not enabled")
        return svc.trading

    @route("GET", "/api/admin/trade/status", "admin")
    def t_status(c): return 200, need_trading().status()

    @route("POST", "/api/admin/trade/scan", "admin")
    def t_scan(c): return 200, need_trading().scan()

    @route("POST", "/api/admin/trade/allow", "admin")
    def t_allow(c): return 200, need_trading().allow(c.s("id", 64), c.user["email"])

    @route("POST", "/api/admin/trade/reject", "admin")
    def t_reject(c): return 200, need_trading().reject(c.s("id", 64))

    @route("POST", "/api/admin/trade/kill", "admin")
    def t_kill(c): return 200, need_trading().kill()

    @route("POST", "/api/admin/trade/advance", "admin")
    def t_adv(c): return 200, need_trading().advance(max(1, min(c.i("n"), 50)))

    sem = threading.BoundedSemaphore(max_conn)

    class H(BaseHTTPRequestHandler):
        timeout = 15
        server_version = "apex"; sys_version = ""

        def log_message(self, fmt, *a): log.info("%s %s", self.client_address[0], fmt % a)

        def ip(self):
            peer = self.client_address[0]
            try: trusted = cfg.trusted_proxy and any(ipaddress.ip_address(peer) in n for n in proxy_nets)
            except ValueError: trusted = False
            if trusted:
                xf = self.headers.get("X-Forwarded-For", "")
                if xf: return xf.split(",")[-1].strip()[:45]
            return peer

        def send_json(self, code, obj, cookie=None, clear=False, extra=None):
            self.send_bytes(code, json.dumps(obj, default=str).encode(), "application/json", cookie, clear, extra)

        def send_bytes(self, code, body, ctype, cookie=None, clear=False, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP); self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY"); self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin"); self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=()")
            extra = dict(extra or {})
            self.send_header("Cache-Control", extra.pop("Cache-Control", None) or ("no-store" if ctype.startswith("application/json") else "no-cache"))
            if secure: self.send_header("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
            attrs = "Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")
            if cookie: self.send_header("Set-Cookie", f"{CK}={cookie}; {attrs}; Max-Age={cfg.session_hours_user * 3600}")
            if clear: self.send_header("Set-Cookie", f"{CK}=; {attrs}; Max-Age=0")
            for k, val in extra.items(): self.send_header(k, val)
            self.end_headers(); self.wfile.write(body)

        def fail(self, code, msg, extra=None):
            hdr = {"Retry-After": str(extra["retry_after"])} if extra and "retry_after" in extra else None
            self.send_json(code, {"error": msg, **(extra or {})}, extra=hdr)

        def handle_any(self, method):
            rid = uuid.uuid4().hex[:8]
            try:
                host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
                path = urlparse(self.path)
                if host not in allowed_hosts and not (path.path == "/api/health" and host in ("127.0.0.1", "localhost", "::1")): return self.fail(403, "forbidden")   # loopback may probe liveness only
                ip = self.ip()
                if not svc.rl.allow("ip:" + ip, 240, 60): return self.fail(429, "rate limit exceeded", {"retry_after": 30})
                if method == "GET" and path.path in ("/", "/index.html"): return self.send_bytes(200, static["index.html"], CT[".html"])
                if method == "GET" and path.path == "/legal": return self.send_bytes(200, legal_html(cfg), CT[".html"])
                if method == "GET" and path.path == "/robots.txt": return self.send_bytes(200, b"User-agent: *\nDisallow: /api/\nDisallow: /billing/\nDisallow: /webhooks/\nAllow: /\nAllow: /legal\n", "text/plain; charset=utf-8")
                if method == "GET" and path.path == "/.well-known/security.txt":
                    if not cfg.support_email: return self.fail(404, "not found")
                    exp = (datetime.now(timezone.utc) + timedelta(days=330)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    return self.send_bytes(200, f"Contact: mailto:{cfg.support_email}\nExpires: {exp}\nPreferred-Languages: en\nCanonical: {cfg.base_url}/.well-known/security.txt\n".encode(), "text/plain; charset=utf-8")
                if method == "GET" and path.path == "/api/account/export":
                    ck = SimpleCookie(self.headers.get("Cookie") or ""); r = svc.session(ck[CK].value if CK in ck else None)
                    if not r: return self.fail(401, "not authenticated")
                    return self.send_bytes(200, json.dumps(svc.export_account(r[0]), indent=1, default=str).encode(), "application/json", extra={"Content-Disposition": 'attachment; filename="my-data.json"'})
                if method == "GET" and path.path.startswith("/static/"):
                    n = path.path[8:]
                    if n in static and n not in ("index.html", "legal.html"):
                        ex = {"Cache-Control": "public, max-age=31536000, immutable"} if n.startswith("fonts/") else None
                        return self.send_bytes(200, static[n], CT.get(os.path.splitext(n)[1], "application/octet-stream"), extra=ex)
                    return self.fail(404, "not found")
                if method == "GET" and path.path == "/api/journal/export":
                    ck = SimpleCookie(self.headers.get("Cookie") or ""); r = svc.session(ck[CK].value if CK in ck else None)
                    if not r: return self.fail(401, "not authenticated")
                    return self.send_bytes(200, svc.journal_csv(r[0]).encode(), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="apex-journal.csv"'})
                if method == "GET" and path.path == "/billing/return":
                    ref = (parse_qs(path.query).get("reference") or [""])[0]
                    res = svc.billing.verify_and_settle(ref) if REF_RE.match(ref) else "invalid"
                    if res not in ("credited", "already", "pending", "failed", "mismatch", "unverified", "unknown", "disabled"): res = "invalid"
                    return self.send_bytes(302, b"", "text/plain", extra={"Location": f"/?payment={res}"})
                if method == "POST" and path.path == "/webhooks/paystack":
                    if wh_nets and not any(ipaddress.ip_address(ip) in n for n in wh_nets if ":" in ip or "." in ip): return self.fail(403, "forbidden")
                    if not svc.rl.allow("wh:" + ip, 300, 60): return self.fail(429, "rate limit exceeded")
                    n = int(self.headers.get("Content-Length") or 0)
                    if n <= 0 or n > 65536: return self.fail(413, "bad size")
                    code, msg = svc.billing.handle_webhook(self.rfile.read(n), self.headers.get("x-paystack-signature", ""))
                    return self.send_json(code, {"result": msg})
                table = routes_get if method == "GET" else routes_post
                ent = table.get(path.path)
                if not ent: return self.fail(404, "not found")
                fn, auth = ent
                body = {}
                if method == "POST":
                    if "application/json" not in (self.headers.get("Content-Type") or ""): return self.fail(415, "json required")
                    o = self.headers.get("Origin")
                    if o is not None and o != origin: return self.fail(403, "bad origin")
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > (262144 if path.path == "/api/admin/events" else 16384): return self.fail(413, "request too large")
                    try: body = json.loads(self.rfile.read(n) or b"{}")
                    except ValueError: return self.fail(400, "bad json")
                    if not isinstance(body, dict): return self.fail(400, "bad json")
                ck = SimpleCookie(self.headers.get("Cookie") or ""); token = ck[CK].value if CK in ck else None
                user = sess = None
                if auth:
                    r = svc.session(token)
                    if not r: return self.fail(401, "not authenticated")
                    user, sess = r
                    if auth == "admin" and user["role"] != "admin": return self.fail(403, "forbidden")
                    if method == "POST" and not sec.ct_eq(self.headers.get("X-CSRF-Token") or "", sess["csrf"]): return self.fail(403, "csrf check failed")
                code, out = fn(Ctx(self, body, user, sess, ip, token))
                ck_val, clr = out.pop("_cookie", None) if isinstance(out, dict) else None, out.pop("_clear", False) if isinstance(out, dict) else False
                self.send_json(code, out, cookie=ck_val, clear=clr)
            except ApiError as e: self.fail(e.code, e.msg, e.extra)
            except (BrokenPipeError, ConnectionResetError, socket.timeout): pass
            except Exception:
                log.exception("unhandled error rid=%s path=%s", rid, self.path)
                try: self.send_json(500, {"error": "internal error", "request_id": rid})
                except Exception: pass

        def do_GET(self): self.handle_any("GET")
        def do_POST(self): self.handle_any("POST")
        def do_PUT(self): self.fail(405, "method not allowed")
        do_DELETE = do_PATCH = do_PUT

    class Srv(ThreadingHTTPServer):
        daemon_threads = True; request_queue_size = 128
        def process_request(self, request, client_address):
            if not sem.acquire(blocking=False):
                try: request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                finally: self.shutdown_request(request)
                return
            super().process_request(request, client_address)
        def process_request_thread(self, request, client_address):
            try: super().process_request_thread(request, client_address)
            finally: sem.release()

    return Srv((cfg.host, cfg.port), H)
