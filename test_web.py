import hashlib, hmac, http.client, json, os, shutil, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from apex.web import security as sec
from apex.web.billing import Billing, PaystackClient, PaystackError, verify_signature, PERIOD, DEFAULT_TIERS
from apex.web.main import build_service, create_admin, check
from apex.web.server import make_server
from apex.web.service import Service, ApiError
from apex.web.webconfig import WebSettings, load_web_settings
from apex.web.db import verify_audit
from apex import scanner as sc

SECRET = "sk_test_unit_secret"
PW = "Correct-Horse-Battery-9"
TOTP_SECRET = sec.new_totp_secret()
from tests.fixtures import DEMO


class FakePaystack:
    def __init__(self): self.init_calls, self.txns, self.fail_verify = [], {}, False
    def initialize(self, email, amount_kobo, reference, callback_url, currency, metadata):
        self.init_calls.append(dict(email=email, amount=amount_kobo, reference=reference, currency=currency, metadata=metadata, callback=callback_url))
        return {"authorization_url": "https://checkout.paystack.com/" + reference, "access_code": "x", "reference": reference}
    def verify(self, reference):
        if self.fail_verify: raise PaystackError("unreachable")
        return self.txns.get(reference) or {"status": "failed", "reference": reference, "amount": 0, "currency": "NGN", "customer": {"email": ""}}


class Resp:
    def __init__(self, r, body): self.status, self.headers, self.raw = r.status, r, body; self.cookies = r.msg.get_all("Set-Cookie") or []
    def json(self): return json.loads(self.raw)


class Client:
    def __init__(self, port): self.port, self.cookie, self.csrf = port, None, ""
    def req(self, method, path, body=None, headers=None, raw=None, csrf=True, ctype="application/json"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60); h = dict(headers or {})
        if self.cookie: h["Cookie"] = self.cookie
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if method == "POST":
            h.setdefault("Content-Type", ctype)
            if csrf and self.csrf: h.setdefault("X-CSRF-Token", self.csrf)
        c.request(method, path, body=data, headers=h); r = c.getresponse(); out = Resp(r, r.read()); c.close()
        for ck in out.cookies:
            kv = ck.split(";")[0]; name, val = kv.split("=", 1)
            if "Max-Age=0" in ck: self.cookie = None
            else: self.cookie = kv
        return out
    def login(self, email, pw=PW, totp=None):
        r = self.req("POST", "/api/login", {"email": email, "password": pw, **({"totp": totp} if totp else {})})
        if r.status == 200 and not r.json().get("need_totp"):
            m = self.req("GET", "/api/me"); self.csrf = m.json()["csrf"]
        return r


class Base(unittest.TestCase):
    secure = False
    def setUp(self):
        self.d = tempfile.mkdtemp(); os.environ["PAYSTACK_SECRET_KEY"] = SECRET
        self.s = WebSettings(db_path=os.path.join(self.d, "w.db"), log_path=os.path.join(self.d, "w.log"), port=0, data_dir=DEMO, secure_cookies=self.secure,
                             plan_codes={"PLN_pro": "pro"}, edge_path=os.path.join(self.d, "edge.json"))
        self.fake = FakePaystack(); self.svc = build_service(self.s, paystack_client=self.fake)
        self.srv = make_server(self.svc); self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start(); self.n = 0
    def tearDown(self): self.srv.shutdown(); self.srv.server_close(); shutil.rmtree(self.d, ignore_errors=True)
    def user(self, email=None, tier="free", days=30):
        self.n += 1; email = email or f"user{self.n}@example.com"
        uid = self.svc.register(email, PW, f"ip{self.n}-{time.time()}")
        if tier != "free":
            with self.svc.db.tx() as c: c.execute("UPDATE users SET tier=?, paid_until=? WHERE id=?", (tier, int(time.time()) + days * 86400, uid))
        return uid, email
    def client(self, email, pw=PW):
        c = Client(self.port); r = c.login(email, pw); self.assertEqual(r.status, 200, r.raw); return c
    def admin(self, email="boss@example.com"):
        with self.svc.db.tx() as c:
            c.execute("INSERT INTO users(email,pw_hash,role,totp_secret,totp_enabled,created) VALUES(?,?, 'admin', ?,1,?)", (email, sec.hash_password(PW), TOTP_SECRET, int(time.time())))
        c = Client(self.port); step = int(time.time() // 30) + 1
        r = c.login(email); self.assertTrue(r.json().get("need_totp")); self.assertIsNone(c.cookie)
        r = c.login(email, totp=sec.totp_code(TOTP_SECRET, step)); self.assertEqual(r.status, 200, r.raw); return c
    def uid(self, email): return self.svc.db.one("SELECT id FROM users WHERE email=?", (email,))["id"]
    def row(self, email): return self.svc.db.one("SELECT * FROM users WHERE email=?", (email,))
    def sign(self, body: bytes, secret=SECRET): return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    def webhook(self, ev, sig=True, secret=SECRET):
        body = json.dumps(ev).encode(); c = Client(self.port)
        return c.req("POST", "/webhooks/paystack", raw=body, headers={"x-paystack-signature": self.sign(body, secret)} if sig else {})
    def pending(self, email, tier="pro"):
        c = self.client(email); r = c.req("POST", "/api/checkout", {"tier": tier}); self.assertEqual(r.status, 200, r.raw)
        return c, self.fake.init_calls[-1]["reference"]
    def charge(self, ref, email, amount, cur="NGN", status="success"):
        return {"event": "charge.success", "data": {"reference": ref, "status": status, "amount": amount, "currency": cur, "customer": {"email": email}}}


class AuthTests(Base):
    def test_static_hardening(self):
        c = Client(self.port); r = c.req("GET", "/")
        self.assertEqual(r.status, 200); h = r.headers
        self.assertIn("script-src 'self'", h.getheader("Content-Security-Policy")); self.assertNotIn("unsafe-inline", h.getheader("Content-Security-Policy"))
        self.assertEqual(h.getheader("X-Content-Type-Options"), "nosniff"); self.assertEqual(h.getheader("X-Frame-Options"), "DENY")
        html, js = r.raw.decode(), open(os.path.join(os.path.dirname(__file__), "..", "apex", "web", "static", "app.js")).read()
        self.assertNotIn("<script>", html); self.assertNotIn("onclick=", html)
        for bad in ("innerHTML", "eval(", "document.write", "outerHTML", "insertAdjacentHTML"): self.assertNotIn(bad, js)
        self.assertNotIn("style:", js.replace("tot.style", ""))

    def test_register_validation_and_duplicates(self):
        c = Client(self.port)
        for body in ({"email": "bad", "password": PW}, {"email": "a@b.co", "password": "short"}, {"email": "a@b.co", "password": "aaaaaaaaaaaaaaaa"}, {"email": "alice@b.co", "password": "alice-is-here-1234"}):
            self.assertEqual(c.req("POST", "/api/register", body).status, 400, body)
        self.svc.rl = sec.RateLimiter()
        self.assertEqual(c.req("POST", "/api/register", {"email": "terms@example.com", "password": PW}).status, 400)     # terms must be accepted
        self.assertEqual(c.req("POST", "/api/register", {"email": "terms@example.com", "password": PW, "accept_terms": "yes"}).status, 400)
        self.assertEqual(c.req("POST", "/api/register", {"email": "ok@example.com", "password": PW, "accept_terms": True}).status, 201)
        self.assertEqual(c.req("POST", "/api/register", {"email": "OK@example.com", "password": PW, "accept_terms": True}).status, 409)
        self.assertEqual(c.req("POST", "/api/register", {"email": 5, "password": PW}).status, 400)

    def test_passwords_stored_as_scrypt_only(self):
        _, e = self.user(); h = self.row(e)["pw_hash"]
        self.assertTrue(h.startswith("scrypt$")); self.assertNotIn(PW, h); self.assertTrue(sec.verify_password(PW, h)); self.assertFalse(sec.verify_password(PW + "x", h))

    def test_sql_injection_attempts(self):
        _, e = self.user(); c = Client(self.port)
        for em in ("x@x.com' OR '1'='1", "' OR 1=1 --", e + "'--", "a@b.co\"; DROP TABLE users;--"):
            r = c.req("POST", "/api/login", {"email": em, "password": "' OR '1'='1"}); self.assertIn(r.status, (400, 401))
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) n FROM users")["n"], 1)
        self.assertEqual(c.req("POST", "/api/login", {"email": e + "' --", "password": PW}).status, 401)

    def test_login_generic_errors_and_lockout_no_enumeration(self):
        _, e = self.user(); c = Client(self.port)
        a = c.req("POST", "/api/login", {"email": e, "password": "wrong-password-1"}); b = c.req("POST", "/api/login", {"email": "ghost@example.com", "password": "wrong-password-1"})
        self.assertEqual((a.status, a.json()), (b.status, b.json()))
        for _ in range(5): c.req("POST", "/api/login", {"email": e, "password": "wrong-password-1"}); c.req("POST", "/api/login", {"email": "ghost@example.com", "password": "wrong-password-1"})
        ra = c.req("POST", "/api/login", {"email": e, "password": PW}); rb = c.req("POST", "/api/login", {"email": "ghost@example.com", "password": PW})
        self.assertEqual((ra.status, rb.status), (429, 429)); self.assertTrue(ra.headers.getheader("Retry-After"))

    def test_ip_login_rate_limit(self):
        c = Client(self.port); codes = [c.req("POST", "/api/login", {"email": f"u{i}@example.com", "password": "wrong-password-1"}).status for i in range(25)]
        self.assertIn(429, codes)

    def test_cookie_flags_and_session_storage(self):
        _, e = self.user(); c = Client(self.port); r = c.login(e); ck = r.cookies[0]
        self.assertIn("HttpOnly", ck); self.assertIn("SameSite=Lax", ck); self.assertNotIn("Secure", ck)
        token = c.cookie.split("=", 1)[1]
        self.assertIsNone(self.svc.db.one("SELECT 1 FROM sessions WHERE id=?", (token,)))                       # raw token never stored
        self.assertIsNotNone(self.svc.db.one("SELECT 1 FROM sessions WHERE id=?", (sec.token_hash(token),)))
        self.assertEqual(c.req("POST", "/api/logout", {}).status, 200)

    def test_logout_invalidates_server_side(self):
        _, e = self.user(); c = self.client(e); old = c.cookie; c.req("POST", "/api/logout", {})
        c2 = Client(self.port); c2.cookie = old; self.assertEqual(c2.req("GET", "/api/me").status, 401)

    def test_expired_and_idle_sessions(self):
        _, e = self.user(); c = self.client(e)
        with self.svc.db.tx() as cx: cx.execute("UPDATE sessions SET expires=?", (int(time.time()) - 1,))
        self.assertEqual(c.req("GET", "/api/me").status, 401)
        c = self.client(e)
        with self.svc.db.tx() as cx: cx.execute("UPDATE sessions SET last_seen=?", (int(time.time()) - 10 ** 6,))
        self.assertEqual(c.req("GET", "/api/me").status, 401)

    def test_csrf_origin_and_content_type(self):
        _, e = self.user(); c = self.client(e); body = {"symbol": "EURUSD", "tf": 15}
        self.assertEqual(c.req("POST", "/api/scan", body, csrf=False).status, 403)
        self.assertEqual(c.req("POST", "/api/scan", body, headers={"X-CSRF-Token": "wrong"}).status, 403)
        self.assertEqual(c.req("POST", "/api/scan", body, headers={"Origin": "http://evil.example"}).status, 403)
        self.assertEqual(c.req("POST", "/api/scan", raw=b"symbol=EURUSD", ctype="application/x-www-form-urlencoded").status, 415)
        self.assertEqual(c.req("POST", "/api/scan", body).status, 200)
        c0 = Client(self.port); self.assertEqual(c0.req("POST", "/api/login", {"email": e, "password": PW}, headers={"Origin": "http://evil.example"}).status, 403)

    def test_host_header_and_body_limits(self):
        c = Client(self.port); self.assertEqual(c.req("GET", "/api/health", headers={"Host": "evil.example"}).status, 403)
        self.assertEqual(c.req("POST", "/api/login", raw=b"x" * 20000).status, 413)
        self.assertEqual(c.req("POST", "/api/login", raw=b"{bad").status, 400)
        self.assertEqual(c.req("POST", "/api/login", raw=b"[1,2]").status, 400)
        self.assertEqual(c.req("PUT", "/api/me").status, 405); self.assertEqual(c.req("GET", "/nope").status, 404)

    def test_global_rate_limit(self):
        c = Client(self.port); codes = {c.req("GET", "/api/health").status for _ in range(260)}; self.assertIn(429, codes)

    def test_secure_cookie_mode(self):
        self.tearDown(); self.secure = True; self.setUp()
        _, e = self.user(); r = Client(self.port).login(e)
        self.assertIn("Secure", r.cookies[0]); self.assertTrue(r.cookies[0].startswith("__Host-apex_sid="))
        self.assertIsNotNone(r.headers.getheader("Strict-Transport-Security"))

    def test_error_responses_leak_nothing(self):
        _, e = self.user(); c = self.client(e)
        with mock.patch.object(self.svc.md, "get", side_effect=RuntimeError("secret internal path /etc/passwd")):
            r = c.req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15})
        self.assertEqual(r.status, 503); self.assertNotIn(b"passwd", r.raw); self.assertNotIn(b"Traceback", r.raw)

    def test_password_change_revokes_other_sessions_and_reset_flow(self):
        _, e = self.user(); a, b = self.client(e), self.client(e)
        self.assertEqual(a.req("POST", "/api/password", {"old": "wrong-old-password", "new": "Another-Strong-Pass-77"}).status, 403)
        self.assertEqual(a.req("POST", "/api/password", {"old": PW, "new": "Another-Strong-Pass-77"}).status, 200)
        self.assertEqual(b.req("GET", "/api/me").status, 401); self.assertEqual(a.req("GET", "/api/me").status, 200)
        adm = self.admin(); tok = adm.req("POST", "/api/admin/reset-token", {"id": self.uid(e)}).json()["token"]
        c = Client(self.port); self.assertEqual(c.req("POST", "/api/reset-password", {"token": tok, "password": "Brand-New-Passw0rd-xyz"}).status, 200)
        self.assertEqual(c.req("POST", "/api/reset-password", {"token": tok, "password": "Brand-New-Passw0rd-abc"}).status, 400)   # single use
        self.assertEqual(a.req("GET", "/api/me").status, 401)
        self.assertEqual(Client(self.port).login(e, "Brand-New-Passw0rd-xyz").status, 200)
        self.assertEqual(c.req("POST", "/api/reset-password", {"token": "bogus", "password": "Brand-New-Passw0rd-abc"}).status, 400)

    def test_delete_account(self):
        uid, e = self.user(); c = self.client(e)
        self.assertEqual(c.req("POST", "/api/delete-account", {"password": "nope-nope-nope-1"}).status, 403)
        self.assertEqual(c.req("POST", "/api/delete-account", {"password": PW}).status, 200)
        self.assertEqual(Client(self.port).login(e).status, 401); self.assertTrue(self.svc.db.one("SELECT email FROM users WHERE id=?", (uid,))["email"].startswith("deleted+"))


class AuthzTests(Base):
    def test_unauthenticated_and_user_blocked_from_admin(self):
        _, e = self.user(); c0, cu = Client(self.port), self.client(e)
        for p in ("/api/admin/users", "/api/admin/payments", "/api/admin/audit", "/api/admin/trade/status"):
            self.assertEqual(c0.req("GET", p).status, 401, p); self.assertEqual(cu.req("GET", p).status, 403, p)
        for p, b in (("/api/admin/grant", {"id": 1, "tier": "elite", "days": 99}), ("/api/admin/status", {"id": 1, "status": "active"}), ("/api/admin/trade/scan", {}), ("/api/admin/trade/allow", {"id": "x"}), ("/api/admin/trade/kill", {})):
            self.assertEqual(cu.req("POST", p, b).status, 403, p)

    def test_no_privilege_escalation_paths(self):
        uid, e = self.user(); cu = self.client(e); adm = self.admin()
        cu.req("POST", "/api/risk", {"equity": 1000, "risk_pct": 0.5, "role": "admin", "tier": "elite"})
        r = self.row(e); self.assertEqual((r["role"], r["tier"]), ("user", "free"))
        aid = self.uid("boss@example.com")
        for p, b in (("/api/admin/grant", {"id": aid, "tier": "free", "days": 0}), ("/api/admin/status", {"id": aid, "status": "suspended"}), ("/api/admin/reset-token", {"id": aid}), ("/api/admin/revoke", {"id": aid})):
            self.assertEqual(adm.req("POST", p, b).status, 403, p)
        self.assertEqual(adm.req("POST", "/api/admin/grant", {"id": uid, "tier": "elite", "days": 5, "role": "admin"}).status, 200); self.assertEqual(self.row(e)["role"], "user")

    def test_users_only_see_own_payments(self):
        _, e1 = self.user(); _, e2 = self.user(); c1 = self.client(e1); c1.req("POST", "/api/checkout", {"tier": "pro"})
        c2 = self.client(e2); self.assertEqual(c2.req("GET", "/api/payments").json()["payments"], []); self.assertEqual(len(c1.req("GET", "/api/payments").json()["payments"]), 1)
        self.assertNotIn("email", [k for k, v in c1.req("GET", "/api/payments").json()["payments"][0].items() if v])

    def test_admin_2fa_enforced_and_replay_protected(self):
        with self.svc.db.tx() as c:
            c.execute("INSERT INTO users(email,pw_hash,role,totp_secret,totp_enabled,created) VALUES('a2@example.com',?, 'admin', ?,1,?)", (sec.hash_password(PW), TOTP_SECRET, int(time.time())))
        cur = int(time.time() // 30); cl = Client(self.port)
        self.assertEqual(cl.login("a2@example.com", totp="000000").status, 401)
        code = sec.totp_code(TOTP_SECRET, cur); self.assertEqual(cl.login("a2@example.com", totp=code).status, 200)
        self.assertEqual(Client(self.port).login("a2@example.com", totp=code).status, 401)                    # replay of the same code
        self.assertEqual(Client(self.port).login("a2@example.com", totp=sec.totp_code(TOTP_SECRET, cur + 1)).status, 200)
        uid = self.uid("a2@example.com")
        with self.svc.db.tx() as c: c.execute("UPDATE sessions SET mfa_ok=0")
        self.assertEqual(cl.req("GET", "/api/admin/users").status, 401)                                      # a session without MFA is useless

    def test_production_admin_without_2fa_refused(self):
        self.svc.s.production = True
        with self.svc.db.tx() as c: c.execute("INSERT INTO users(email,pw_hash,role,created) VALUES('n@example.com',?, 'admin', ?)", (sec.hash_password(PW), int(time.time())))
        self.assertEqual(Client(self.port).login("n@example.com").status, 403)

    def test_admin_actions_suspend_grant_audit(self):
        uid, e = self.user(); cu = self.client(e); adm = self.admin()
        self.assertEqual(adm.req("POST", "/api/admin/status", {"id": uid, "status": "suspended"}).status, 200)
        self.assertEqual(cu.req("GET", "/api/me").status, 401); self.assertEqual(Client(self.port).login(e).status, 403)
        adm.req("POST", "/api/admin/status", {"id": uid, "status": "active"}); cu = self.client(e)
        adm.req("POST", "/api/admin/grant", {"id": uid, "tier": "pro", "days": 10}); self.assertEqual(cu.req("GET", "/api/me").json()["tier"], "pro")
        self.assertEqual(adm.req("POST", "/api/admin/grant", {"id": uid, "tier": "gold", "days": 10}).status, 400)
        self.assertEqual(adm.req("POST", "/api/admin/grant", {"id": uid, "tier": "pro", "days": -1}).status, 400)
        self.assertEqual(adm.req("POST", "/api/admin/grant", {"id": 9999, "tier": "pro", "days": 1}).status, 404)
        a = adm.req("GET", "/api/admin/audit").json(); self.assertTrue(a["chain_ok"]); self.assertTrue(any(r["action"] == "grant_tier" for r in a["rows"]))
        with self.svc.db.tx() as c: c.execute("UPDATE audit SET detail='tampered' WHERE id=(SELECT MIN(id) FROM audit WHERE detail<>'')")
        self.assertFalse(verify_audit(self.svc.db))
        users = adm.req("GET", "/api/admin/users").json()["users"]; self.assertTrue(any(u["email"] == e for u in users))
        self.assertNotIn(b"pw_hash", adm.req("GET", "/api/admin/users").raw); self.assertNotIn(b"totp_secret", adm.req("GET", "/api/admin/users").raw)


class TierTests(Base):
    def test_free_gating_and_no_leak(self):
        _, e = self.user(); c = self.client(e); r = c.req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}); d = r.json()
        self.assertEqual(r.status, 200); self.assertEqual(d["scanned"], 10); self.assertEqual(sum(1 for m in d["methods"] if m["locked"]), sc.METHOD_COUNT - 10)
        self.assertIsNone(d["plan"]); self.assertIsNone(d["report"]); self.assertNotIn(b"STOP-LOSS", r.raw); self.assertNotIn(b"tp1", r.raw)
        self.assertEqual(c.req("POST", "/api/scan", {"symbol": "GBPUSD", "tf": 15}).status, 403)
        self.assertEqual(c.req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 7}).status, 400)
        self.assertEqual(c.req("POST", "/api/scan", {"symbol": "EURUSD'; --", "tf": 15}).status, 403)
        self.assertEqual(c.req("POST", "/api/edge", {"symbol": "EURUSD", "tf": 15}).status, 402)

    def test_daily_quota(self):
        _, e = self.user(); c = self.client(e)
        codes = [c.req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).status for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [402]); self.assertEqual(c.req("GET", "/api/me").json()["scans_used"], 5)

    def test_pro_gets_everything_but_edge(self):
        _, e = self.user(tier="pro"); c = self.client(e); d = c.req("POST", "/api/scan", {"symbol": "GBPUSD", "tf": 15}).json()
        self.assertEqual(d["scanned"], sc.METHOD_COUNT); self.assertFalse(any(m["locked"] for m in d["methods"])); self.assertIn("A. MARKET SNAPSHOT", d["report"])
        self.assertEqual(c.req("POST", "/api/edge", {"symbol": "EURUSD", "tf": 15}).status, 402)

    def test_elite_edge(self):
        _, e = self.user(tier="elite"); c = self.client(e); r = c.req("POST", "/api/edge", {"symbol": "EURUSD", "tf": 15})
        self.assertEqual(r.status, 200); self.assertTrue(any(x["method"] == "CONSENSUS" for x in r.json()["rows"]))

    def test_expired_paid_tier_downgrades(self):
        _, e = self.user(tier="pro", days=-1); c = self.client(e); self.assertEqual(c.req("GET", "/api/me").json()["tier"], "free")
        self.assertEqual(c.req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).json()["scanned"], 10)

    def test_risk_settings_bounds_and_sizing(self):
        _, e = self.user(tier="pro"); c = self.client(e)
        for b in ({"equity": 5, "risk_pct": 1}, {"equity": 1000, "risk_pct": 50}, {"equity": "x", "risk_pct": 1}): self.assertEqual(c.req("POST", "/api/risk", b).status, 400)
        self.assertEqual(c.req("POST", "/api/risk", {"equity": 50000, "risk_pct": 0.5}).status, 200)
        me = c.req("GET", "/api/me").json(); self.assertEqual((me["equity"], me["risk_pct"]), (50000, 0.5))


class PaymentTests(Base):
    def test_checkout_uses_server_price_and_identity(self):
        _, e = self.user(); c = self.client(e)
        r = c.req("POST", "/api/checkout", {"tier": "elite", "amount": 1, "email": "other@x.com", "price": 1}); self.assertEqual(r.status, 200)
        call = self.fake.init_calls[-1]; self.assertEqual((call["amount"], call["email"], call["currency"]), (DEFAULT_TIERS["elite"].price_kobo, e, "NGN"))
        self.assertTrue(r.json()["url"].startswith("https://")); self.assertEqual(c.req("POST", "/api/checkout", {"tier": "free"}).status, 400)
        self.assertEqual(c.req("POST", "/api/checkout", {"tier": "gold"}).status, 400)
        self.assertEqual(Client(self.port).req("POST", "/api/checkout", {"tier": "pro"}).status, 401)
        row = self.svc.db.one("SELECT * FROM payments WHERE reference=?", (call["reference"],)); self.assertEqual((row["status"], row["amount_kobo"]), ("pending", DEFAULT_TIERS["elite"].price_kobo))

    def test_billing_disabled_returns_503(self):
        self.svc.billing.client = None; _, e = self.user(); c = self.client(e); self.assertEqual(c.req("POST", "/api/checkout", {"tier": "pro"}).status, 503)

    def test_provider_failure_is_clean_502(self):
        _, e = self.user(); c = self.client(e)
        with mock.patch.object(self.fake, "initialize", side_effect=PaystackError("boom sk_test_unit_secret")): r = c.req("POST", "/api/checkout", {"tier": "pro"})
        self.assertEqual(r.status, 502); self.assertNotIn(b"sk_test", r.raw)

    def test_webhook_signature_required(self):
        _, e = self.user(); _, ref = self.pending(e); ev = self.charge(ref, e, DEFAULT_TIERS["pro"].price_kobo)
        self.assertEqual(self.webhook(ev, sig=False).status, 401); self.assertEqual(self.webhook(ev, secret="sk_test_wrong").status, 401)
        self.assertEqual(self.row(e)["tier"], "free")
        c = Client(self.port); self.assertEqual(c.req("POST", "/webhooks/paystack", raw=json.dumps(ev).encode(), headers={"x-paystack-signature": "abc"}).status, 401)
        self.assertTrue(verify_signature(SECRET, b"x", hmac.new(SECRET.encode(), b"x", hashlib.sha512).hexdigest().upper()))

    def test_webhook_credits_once_and_replay_is_idempotent(self):
        _, e = self.user(); _, ref = self.pending(e); ev = self.charge(ref, e, DEFAULT_TIERS["pro"].price_kobo)
        r = self.webhook(ev); self.assertEqual((r.status, r.json()["result"]), (200, "credited")); u1 = self.row(e)
        self.assertEqual((u1["tier"], u1["paid_until"] > time.time() + PERIOD - 60), ("pro", True))
        self.assertEqual(self.webhook(ev).json()["result"], "already"); self.assertEqual(self.row(e)["paid_until"], u1["paid_until"])

    def test_concurrent_duplicate_webhooks_credit_once(self):
        _, e = self.user(); _, ref = self.pending(e); ev = self.charge(ref, e, DEFAULT_TIERS["pro"].price_kobo); out = []
        ts = [threading.Thread(target=lambda: out.append(self.webhook(ev).json()["result"])) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(out.count("credited"), 1); self.assertLess(self.row(e)["paid_until"], time.time() + PERIOD + 120)

    def test_tampered_payments_never_credit(self):
        for amt, cur, em, label in ((1000, "NGN", None, "amount"), (None, "USD", None, "currency"), (None, "NGN", "attacker@example.com", "email")):
            _, e = self.user(); _, ref = self.pending(e)
            r = self.webhook(self.charge(ref, em or e, amt if amt is not None else DEFAULT_TIERS["pro"].price_kobo, cur)); self.assertEqual(r.json()["result"], "mismatch", label)
            self.assertEqual(self.row(e)["tier"], "free", label); self.assertEqual(self.svc.db.one("SELECT status FROM payments WHERE reference=?", (ref,))["status"], "mismatch")

    def test_unknown_reference_failed_and_other_events(self):
        _, e = self.user(); self.assertEqual(self.webhook(self.charge("apx_" + "0" * 24, e, 1500000)).json()["result"], "unknown")
        _, ref = self.pending(e); self.assertEqual(self.webhook(self.charge(ref, e, 1500000, status="failed")).json()["result"], "failed")
        self.assertEqual(self.row(e)["tier"], "free"); self.assertEqual(self.webhook({"event": "transfer.success", "data": {"reference": ref}}).json()["result"], "ignored")
        self.assertEqual(self.webhook({"event": "charge.success"}).json()["result"], "ignored")

    def test_subscription_renewal_via_plan_code(self):
        _, e = self.user(); ev = {"event": "charge.success", "data": {"reference": "renew_1", "status": "success", "amount": 1500000, "currency": "NGN", "customer": {"email": e}, "plan": {"plan_code": "PLN_pro"}}}
        self.assertEqual(self.webhook(ev).json()["result"], "credited"); self.assertEqual(self.row(e)["tier"], "pro")
        bad = json.loads(json.dumps(ev)); bad["data"].update(reference="renew_2", amount=100); self.assertEqual(self.webhook(bad).json()["result"], "mismatch")
        ghost = json.loads(json.dumps(ev)); ghost["data"].update(reference="renew_3", customer={"email": "nobody@example.com"}); self.assertEqual(self.webhook(ghost).json()["result"], "unknown")
        nop = json.loads(json.dumps(ev)); nop["data"]["plan"] = {"plan_code": "PLN_evil"}; nop["data"]["reference"] = "renew_4"; self.assertEqual(self.webhook(nop).json()["result"], "unknown")

    def test_redirect_return_verifies_with_paystack(self):
        _, e = self.user(); c, ref = self.pending(e)
        r = c.req("GET", "/billing/return?reference=" + ref); self.assertEqual((r.status, r.headers.getheader("Location")), (302, "/?payment=failed")); self.assertEqual(self.row(e)["tier"], "free")
        _, ref = self.pending(e); self.fake.txns[ref] = {"status": "success", "reference": ref, "amount": 1500000, "currency": "NGN", "customer": {"email": e}}
        r = c.req("GET", "/billing/return?reference=" + ref); self.assertEqual(r.headers.getheader("Location"), "/?payment=credited"); self.assertEqual(self.row(e)["tier"], "pro")
        self.assertEqual(c.req("GET", "/billing/return?reference=" + ref).headers.getheader("Location"), "/?payment=already")
        _, ref = self.pending(e, "elite"); self.fake.fail_verify = True
        self.assertEqual(c.req("GET", "/billing/return?reference=" + ref).headers.getheader("Location"), "/?payment=unverified")
        n = len(self.fake.txns); self.assertEqual(c.req("GET", "/billing/return?reference=../../etc").headers.getheader("Location"), "/?payment=invalid")
        self.assertEqual(c.req("GET", "/billing/return?reference=x%0d%0aSet-Cookie:a=b").headers.getheader("Location"), "/?payment=invalid")
        # attacker cannot credit themselves by forging a 'success' reference of someone else's pending payment with a different amount
        self.fake.fail_verify = False; _, e2 = self.user(); _, ref2 = self.pending(e2, "elite")
        self.fake.txns[ref2] = {"status": "success", "reference": ref2, "amount": 1500000, "currency": "NGN", "customer": {"email": e2}}
        Client(self.port).req("GET", "/billing/return?reference=" + ref2); self.assertEqual(self.row(e2)["tier"], "free")

    def test_proration_renewal_and_downgrade_rules(self):
        _, e = self.user(tier="pro", days=15); self.assertEqual(self.row(e)["tier"], "pro")
        c, ref = self.pending(e, "elite"); self.webhook(self.charge(ref, e, DEFAULT_TIERS["elite"].price_kobo)); u = self.row(e)
        days = (u["paid_until"] - time.time()) / 86400; self.assertEqual(u["tier"], "elite"); self.assertAlmostEqual(days, 35, delta=0.2)   # 30 + 15*15000/45000
        before = u["paid_until"]; c2, ref2 = self.pending(e, "elite"); self.webhook(self.charge(ref2, e, DEFAULT_TIERS["elite"].price_kobo))
        self.assertAlmostEqual(self.row(e)["paid_until"] - before, PERIOD, delta=5)
        self.assertEqual(c2.req("POST", "/api/checkout", {"tier": "pro"}).status, 400)

    def test_real_paystack_http_client(self):
        seen = []
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _r(self, code, obj):
                b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            def do_POST(self):
                n = int(self.headers["Content-Length"]); seen.append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(n))))
                self._r(200, {"status": True, "data": {"authorization_url": "https://checkout.paystack.com/abc", "access_code": "a", "reference": "r1"}})
            def do_GET(self):
                seen.append((self.path, self.headers["Authorization"], None))
                if "bad" in self.path: self._r(400, {"status": False, "message": "nope"})
                elif "down" in self.path: self._r(502, {})
                else: self._r(200, {"status": True, "data": {"status": "success", "reference": "r1", "amount": 5, "currency": "NGN"}})
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            pc = PaystackClient("sk_test_zzz", base=f"http://127.0.0.1:{srv.server_address[1]}")
            d = pc.initialize("a@b.co", 123400, "r1", "http://cb", "NGN", {"x": 1}); self.assertEqual(d["authorization_url"], "https://checkout.paystack.com/abc")
            self.assertEqual(seen[0][0], "/transaction/initialize"); self.assertEqual(seen[0][1], "Bearer sk_test_zzz"); self.assertEqual((seen[0][2]["amount"], seen[0][2]["email"], seen[0][2]["reference"]), (123400, "a@b.co", "r1"))
            self.assertEqual(pc.verify("r1")["status"], "success"); self.assertEqual(seen[1][0], "/transaction/verify/r1")
            self.assertEqual(pc.verify("a/b?c")["amount"], 5); self.assertEqual(seen[2][0], "/transaction/verify/a%2Fb%3Fc")
            for ref in ("bad", "down"):
                with self.assertRaises(PaystackError) as cm: pc.verify(ref)
                self.assertNotIn("sk_test_zzz", str(cm.exception))
        finally: srv.shutdown(); srv.server_close()
        with self.assertRaises(PaystackError): PaystackClient("sk", base="http://127.0.0.1:1", timeout=1).verify("x")


class AdminBootstrapAndConfig(Base):
    def test_create_admin_cli_requires_valid_2fa(self):
        cfg = os.path.join(self.d, "c.json"); json.dump({"db_path": os.path.join(self.d, "b.db")}, open(cfg, "w")); msgs = []
        with mock.patch("apex.web.main.sec.new_totp_secret", return_value=TOTP_SECRET):
            self.assertEqual(create_admin(cfg, "root@example.com", PW, "000000", out=msgs.append), 2)
            from apex.web.db import DB; self.assertIsNone(DB(os.path.join(self.d, "b.db")).one("SELECT 1 FROM users"))
            self.assertEqual(create_admin(cfg, "root@example.com", "short", "x", out=msgs.append), 2)
            self.assertEqual(create_admin(cfg, "root@example.com", PW, sec.totp_code(TOTP_SECRET, int(time.time() // 30)), out=msgs.append), 0)
        r = DB(os.path.join(self.d, "b.db")).one("SELECT * FROM users"); self.assertEqual((r["role"], r["totp_enabled"]), ("admin", 1))

    def test_no_web_path_creates_admin(self):
        c = Client(self.port); c.req("POST", "/api/register", {"email": "sneaky@example.com", "password": PW, "role": "admin", "accept_terms": True}); self.assertEqual(self.row("sneaky@example.com")["role"], "user")

    def test_production_config_validation(self):
        base = dict(production=True, base_url="https://app.example.com", secure_cookies=True, feed_type="oanda", oanda_account="101-001-1-001", company_name="Example Ltd", support_email="help@example.com",
                    legal_jurisdiction="Nigeria", legal_updated="2026-01-15", legal_reviewed=True, price_kobo={"pro": 1500000, "elite": 4500000})
        p = os.path.join(self.d, "p.json")
        def load(d):
            with open(p, "w") as f: json.dump(d, f)
            return load_web_settings(p)
        self.assertEqual(load(base).host_name, "app.example.com")
        for bad in ({**base, "base_url": "http://app.example.com"}, {**base, "secure_cookies": False}, {**base, "host": "0.0.0.0"}, {"unknown_key": 1}, {"currency": "XXX"}, {**base, "feed_type": "csv"}):
            with self.assertRaises(ValueError): load(bad)
        os.environ.pop("PAYSTACK_SECRET_KEY")
        try: self.assertEqual(load(base).host_name, "app.example.com")          # credentials you add later never block startup
        finally: os.environ["PAYSTACK_SECRET_KEY"] = SECRET


class TradingPanel(Base):
    def test_admin_only_trading_flow(self):
        from tests.test_apex import mkapp
        app = mkapp(1, 800); self.svc.trading = app; adm = self.admin(); _, e = self.user(); cu = self.client(e)
        self.assertEqual(cu.req("POST", "/api/admin/trade/scan", {}).status, 403)
        d = adm.req("POST", "/api/admin/trade/scan", {}).json(); pid = d["proposal"]["id"]; self.assertEqual(len(app.broker.positions()), 0)
        r = adm.req("POST", "/api/admin/trade/allow", {"id": pid}).json(); self.assertTrue(r["ok"], r); self.assertEqual(len(app.broker.positions()), 1)
        self.assertTrue(adm.req("POST", "/api/admin/trade/kill", {}).json()["kill_switch"]); self.assertEqual(adm.req("GET", "/api/admin/trade/status").json()["kill"], True)
        self.assertTrue(self.svc.me(self.row("boss@example.com"), {"csrf": "x"})["trading"])

    def test_trading_disabled_returns_404(self):
        adm = self.admin(); self.assertEqual(adm.req("GET", "/api/admin/trade/status").status, 404)


if __name__ == "__main__":
    unittest.main()
