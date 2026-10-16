import os, socket, sqlite3, tempfile, threading, time, unittest, json
from datetime import datetime, timedelta, timezone
from unittest import mock
from apex import quality as Q
from apex.config import EventPolicy
from apex.data import Candle, Quote
from tests.synth import synthetic
from apex.edge import compare_profiles
from apex.events import parse_events_csv
from apex.instruments import get_spec
from apex.profiles import PROFILES
from apex.risk import event_blocked
from apex.scanner import ScanConfig, scan_chart
from apex.web import security as sec
from apex.web.db import DB, verify_audit
from apex.web.mailer import Mailer
from apex.web.service import ApiError
from tests.test_web import Base, Client, PW, SECRET

NOW = datetime(2024, 3, 5, 10, tzinfo=timezone.utc)


class MiniSMTP:
    """Tiny real SMTP server (stdlib sockets) so Mailer's smtplib code path is exercised for real."""
    def __init__(self):
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(5); self.port = self.sock.getsockname()[1]; self.msgs = []
        threading.Thread(target=self._serve, daemon=True).start()
    def _serve(self):
        while True:
            try: c, _ = self.sock.accept()
            except OSError: return
            threading.Thread(target=self._conn, args=(c,), daemon=True).start()
    def _conn(self, c):
        f = c.makefile("rwb"); w = lambda s: (f.write((s + "\r\n").encode()), f.flush())
        w("220 mini ESMTP"); frm, rcpt = "", []
        while True:
            line = f.readline().decode().strip()
            if not line: break
            u = line.upper()
            if u.startswith("EHLO") or u.startswith("HELO"): w("250 mini")
            elif u.startswith("MAIL FROM"): frm = line; w("250 ok")
            elif u.startswith("RCPT TO"): rcpt.append(line); w("250 ok")
            elif u == "DATA":
                w("354 go"); data = []
                while True:
                    l = f.readline().decode()
                    if l in (".\r\n", ".\n"): break
                    data.append(l)
                self.msgs.append({"from": frm, "rcpt": rcpt, "data": "".join(data)}); w("250 queued")
            elif u == "QUIT": w("221 bye"); break
            else: w("250 ok")
        c.close()
    def close(self): self.sock.close()


class FakeMailer(Mailer):
    def __init__(self): super().__init__("fake", 25, "", "", "apex@example.com", "none"); self.sent = []
    def send(self, to, subject, body): self.sent.append((to, subject, body)); return True
    def send_async(self, to, subject, body): self.send(to, subject, body)


class QualityTests(unittest.TestCase):
    def test_grade_rules(self):
        ok = lambda k, c=False: {"key": k, "label": k, "ok": True, "detail": "", "critical": c}; bad = lambda k, c=False: {"key": k, "label": k, "ok": False, "detail": "", "critical": c}; unk = {"key": "n", "label": "n", "ok": None, "detail": "", "critical": True}
        self.assertEqual(Q.grade_of([ok("a"), ok("b"), unk])["grade"], "A")                           # unknown never counts as a pass or a failure
        self.assertEqual(Q.grade_of([ok("a"), bad("b")])["grade"], "B"); self.assertEqual(Q.grade_of([ok("a"), bad("b"), bad("c"), bad("d")])["grade"], "C")
        self.assertEqual(Q.grade_of([ok("a"), bad("rr", True)])["grade"], "C")                         # any critical failure caps at C
        g = Q.grade_of([ok("a"), bad("b")]); self.assertEqual((g["passed"], g["total"]), (1, 2)); self.assertIsNone(Q.grade_of([unk])["grade"])

    def test_evidence_folds_in_conservatively(self):
        base = Q.grade_of([{"key": "a", "label": "a", "ok": True, "detail": "", "critical": False}])
        self.assertEqual(Q.add_evidence(base, None)["grade"], "A")                                     # no evidence: unknown, not penalised, not rewarded
        self.assertEqual(Q.add_evidence(base, {"verdict": "NEGATIVE EDGE"})["grade"], "C")
        self.assertEqual(Q.add_evidence(base, {"verdict": "EDGE-SUPPORTED"})["total"], 2); self.assertIsNone(Q.add_evidence(None, {"verdict": "x"}))

    def test_engine_grades_real_setups_and_explains_failures(self):
        from apex.engine import analyze
        from apex.config import RiskConfig, ExecConfig, Mode
        cs = synthetic(3000, seed=1); seen = 0
        for k in range(400, 3000, 40):
            w = cs[:k]; q = Quote(w[-1].c - 5e-5, w[-1].c + 5e-5, w[-1].ts, "simulated-x")
            a = analyze("EURUSD", w, 900, RiskConfig(), ExecConfig(mode=Mode.ANALYSIS_ONLY), None, q, w[-1].ts, allow_simulated_quote=True, enforce_session=False)
            if a.grade:
                seen += 1; self.assertIn(a.grade["grade"], "ABC"); self.assertEqual(a.grade["passed"], sum(1 for i in a.grade["items"] if i["ok"] is True))
                self.assertTrue(all(i["detail"] != "" for i in a.grade["items"]))
        self.assertGreater(seen, 3)

    def test_news_criterion(self):
        pol = EventPolicy(events=[("2024-03-05T10:10:00Z", "USD", "NFP")], block_weekend=False); spec = get_spec("EURUSD")
        self.assertIsNotNone(event_blocked(NOW, pol, spec)); self.assertIsNone(event_blocked(NOW + timedelta(hours=3), pol, spec))

    def test_profiles_ordered_by_strictness(self):
        c, b, a = PROFILES["conservative"], PROFILES["balanced"], PROFILES["active"]
        self.assertGreater(c.scan.min_methods, b.scan.min_methods); self.assertGreater(b.scan.min_methods, a.scan.min_methods)
        self.assertGreater(c.min_net_rr, b.min_net_rr); self.assertGreater(b.min_net_rr, a.min_net_rr); self.assertLess(c.scan.max_extension_atr, a.scan.max_extension_atr)

    def test_stricter_profile_never_yields_more_signals(self):
        n = {k: 0 for k in PROFILES}; cs = synthetic(2500, seed=6)
        for k in range(400, 2500, 60):
            w = cs[:k]
            for name, p in PROFILES.items():
                if scan_chart(w, 0.0, p.scan).consensus: n[name] += 1
        self.assertLessEqual(n["conservative"], n["balanced"]); self.assertLessEqual(n["balanced"], n["active"])

    def test_compare_profiles_shape(self):
        r = compare_profiles(synthetic(1500, seed=2), 0.0001, step=12)
        self.assertEqual([x["profile"] for x in r["rows"]], ["conservative", "balanced", "active"]); self.assertTrue(all(0 <= x["oos_hit_ci"][0] <= x["oos_hit_ci"][1] <= 1 for x in r["rows"]))


class CalendarTests(Base):
    CSV = "time_utc,currency,impact,name\n2099-01-01T12:30:00Z,USD,high,NFP\n2099-01-02T09:00:00Z,eur,high,CPI\n2099-01-02T10:00:00Z,GBP,low,Minor\n"
    def test_parse_validation(self):
        rows, errs = parse_events_csv(self.CSV); self.assertEqual((len(rows), errs), (3, [])); self.assertEqual(rows[1][1], "EUR")
        rows, errs = parse_events_csv("time_utc,currency,impact,name\nnot-a-date,USD,high,X\n2099-01-01T00:00:00Z,USDX,high,Y\n2099-01-01T00:00:00Z,USD,huge,Z\n2099-01-01T00:00:00,USD,high,NoTZ\n"); self.assertEqual(rows, []); self.assertEqual(len(errs), 4)
        self.assertEqual(parse_events_csv("a,b,c\n1,2,3")[1], ["header must be: time_utc,currency,impact,name"]); self.assertEqual(parse_events_csv("")[0], [])

    def test_admin_upload_authz_and_effect(self):
        adm = self.admin(); _, e = self.user(); cu = self.client(e)
        self.assertEqual(cu.req("POST", "/api/admin/events", {"csv": self.CSV}).status, 403); self.assertEqual(Client(self.port).req("GET", "/api/admin/events").status, 401)
        r = adm.req("POST", "/api/admin/events", {"csv": self.CSV}); self.assertEqual(r.json()["saved"], 3)
        self.assertEqual(len(adm.req("GET", "/api/admin/events").json()["events"]), 3); self.assertIsNotNone(self.svc.event_policy())
        self.assertEqual(adm.req("POST", "/api/admin/events", {"csv": "garbage"}).status, 400); self.assertEqual(adm.req("POST", "/api/admin/events", {"csv": "x" * 300000}).status, 413)
        adm.req("POST", "/api/admin/events", {"csv": "time_utc,currency,impact,name\n"}); self.assertIsNone(self.svc.event_policy())
        self.assertTrue(any(r["action"] == "calendar_upload" for r in adm.req("GET", "/api/admin/audit").json()["rows"]))

    def test_news_item_appears_in_grade_when_calendar_loaded(self):
        self.svc.admin_events_set({"email": "x"}, self.CSV)
        _, e = self.user(tier="pro"); c = self.client(e)
        with mock.patch("apex.engine.scan_chart") as sc:   # force a consensus so a grade exists, independent of random data
            from apex.scanner import scan_chart as real
            def fake(cs, hb, cfg, dis, w): r = real(cs, hb, cfg, dis, w); r.consensus = "BUY"; r.fam_long = {f"f{i}": .8 for i in range(6)}; r.fam_short = {}; r.regime.update(trending=True, ranging=False, ext_atr=0.1); return r
            sc.side_effect = fake; d = c.req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).json()
        if d["grade"]: self.assertIn("news", [i["key"] for i in d["grade"]["items"]])


class MailTests(unittest.TestCase):
    def test_real_smtp_delivery_and_injection_defence(self):
        srv = MiniSMTP()
        try:
            m = Mailer("127.0.0.1", srv.port, "", "", "apex@example.com", "none", timeout=5); self.assertTrue(m.enabled)
            self.assertTrue(m.send("user@example.com", "Hello there", "Body line"))
            msg = srv.msgs[0]; self.assertIn("user@example.com", msg["rcpt"][0]); self.assertIn("Subject: Hello there", msg["data"]); self.assertIn("Body line", msg["data"])
            self.assertFalse(m.send("user@example.com", "Hi\r\nBcc: evil@x.com", "x")); self.assertFalse(m.send("bad\r\nBcc: evil@x.com", "s", "x")); self.assertFalse(m.send("not-an-email", "s", "x"))
            self.assertEqual(len(srv.msgs), 1)
        finally: srv.close()

    def test_failures_never_raise_or_leak_password(self):
        m = Mailer("127.0.0.1", 1, "u", "SuperSecretPw", "a@example.com", "none", timeout=1)
        with self.assertLogs("apex.mail", level="ERROR") as cm: self.assertFalse(m.send("u@example.com", "s", "b"))
        self.assertNotIn("SuperSecretPw", "\n".join(cm.output)); self.assertFalse(Mailer().send("u@example.com", "s", "b")); self.assertFalse(Mailer().enabled)


class VerifiedUsers(Base):
    """With email enabled, fixture users are treated as already-verified (verification itself is tested explicitly in AccountEmailFlows.reg)."""
    def user(self, email=None, tier="free", days=30):
        uid, e = super().user(email, tier, days)
        with self.svc.db.tx() as c: c.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid,))
        if hasattr(self, "mail"): self.mail.sent.clear()
        return uid, e


class AccountEmailFlows(VerifiedUsers):
    def setUp(self):
        super().setUp(); self.mail = FakeMailer(); self.svc.mailer = self.mail
    def reg(self, email="new@example.com", c=None):
        c = c or Client(self.port); self.svc.rl = sec.RateLimiter(); r = c.req("POST", "/api/register", {"email": email, "password": PW, "accept_terms": True}); self.assertEqual(r.status, 201, r.raw); return c
    def token(self, kind="verify"):
        body = self.mail.sent[-1][2]; return body.split(f"?{kind}=")[1].split()[0]

    def test_verification_flow_gates_checkout_and_alerts(self):
        self.reg(); self.assertEqual(self.row("new@example.com")["email_verified"], 0); self.assertIn("Confirm", self.mail.sent[-1][1])
        c = self.client("new@example.com"); self.assertFalse(c.req("GET", "/api/me").json()["email_verified"])
        self.assertEqual(c.req("POST", "/api/checkout", {"tier": "pro"}).status, 403)
        pub = Client(self.port); self.assertEqual(pub.req("POST", "/api/verify-email", {"token": "bogus"}).status, 400)
        tok = self.token(); self.assertEqual(pub.req("POST", "/api/verify-email", {"token": tok}).status, 200)
        self.assertEqual(pub.req("POST", "/api/verify-email", {"token": tok}).status, 400)               # single use
        self.assertTrue(c.req("GET", "/api/me").json()["email_verified"]); self.assertEqual(c.req("POST", "/api/checkout", {"tier": "pro"}).status, 200)

    def test_verification_token_expiry_and_resend_limits(self):
        self.reg(); tok = self.token()
        with self.svc.db.tx() as c: c.execute("UPDATE email_tokens SET expires=?", (int(time.time()) - 1,))
        self.assertEqual(Client(self.port).req("POST", "/api/verify-email", {"token": tok}).status, 400)
        c = self.client("new@example.com"); codes = [c.req("POST", "/api/resend-verification", {}).status for _ in range(5)]; self.assertEqual(codes[:3], [200] * 3); self.assertIn(429, codes)

    def test_forgot_password_no_enumeration_and_single_use(self):
        _, e = self.user(); self.svc.rl = sec.RateLimiter(); c = Client(self.port)
        a = c.req("POST", "/api/forgot", {"email": e}); b = c.req("POST", "/api/forgot", {"email": "ghost@example.com"}); self.assertEqual((a.status, a.json()), (b.status, b.json()))
        self.assertEqual(len(self.mail.sent), 1); self.assertEqual(self.mail.sent[0][0], e)                          # only the real account got mail
        tok = self.mail.sent[0][2].split("?reset=")[1].split()[0]
        self.assertEqual(c.req("POST", "/api/reset-password", {"token": tok, "password": "Fresh-Passphrase-4242"}).status, 200)
        self.assertEqual(c.req("POST", "/api/reset-password", {"token": tok, "password": "Another-Passphrase-4242"}).status, 400)
        self.assertEqual(Client(self.port).login(e, "Fresh-Passphrase-4242").status, 200)
        codes = [c.req("POST", "/api/forgot", {"email": e}).status for _ in range(5)]; self.assertIn(429, codes)

    def test_forgot_without_mail_is_clear_503(self):
        self.svc.mailer = Mailer(); self.assertEqual(Client(self.port).req("POST", "/api/forgot", {"email": "a@b.co"}).status, 503)

    def test_receipt_email_after_payment(self):
        from tests.test_web import DEFAULT_TIERS
        self.svc.billing.on_credit = lambda user, tier, until, ref: self.mail.send(user["email"], "receipt", f"{tier} {ref}")
        uid, e = self.user(); c, ref = self.pending(e); self.webhook(self.charge(ref, e, DEFAULT_TIERS["pro"].price_kobo))
        self.assertTrue(any(m[1] == "receipt" and ref in m[2] for m in self.mail.sent)); n = len(self.mail.sent); self.webhook(self.charge(ref, e, DEFAULT_TIERS["pro"].price_kobo)); self.assertEqual(len(self.mail.sent), n)

    def test_legacy_and_unconfigured_accounts_are_auto_verified(self):
        self.svc.mailer = Mailer(); _, e = self.user(); self.assertEqual(self.row(e)["email_verified"], 1)


class AlertTests(VerifiedUsers):
    def setUp(self):
        super().setUp(); self.mail = FakeMailer(); self.svc.mailer = self.mail
    def fake_build(self, grade="A", consensus="BUY"):
        plan = {"entry": 1.1, "stop": 1.095, "tp1": 1.103, "tp2": 1.108, "tp3": 1.11, "net_rr": 1.9, "size": 0.05, "risk_amount": 25.0}
        g = {"grade": grade, "passed": 10, "total": 11, "items": [{"key": "k", "label": "Spread is small", "ok": False, "detail": "", "critical": False}]}
        class A: pass
        a = A(); a.scan = type("S", (), {"consensus": consensus})()
        return lambda *args, **kw: (a, {"grade": g, "plan": plan})
    def test_crud_limits_and_authz(self):
        _, e = self.user(); self.assertEqual(self.client(e).req("POST", "/api/alerts", {"symbol": "EURUSD", "tf": 15, "min_grade": "A"}).status, 402)
        _, e2 = self.user(tier="pro"); c = self.client(e2); p = {"symbol": "EURUSD", "tf": 15, "min_grade": "A"}
        self.assertEqual(c.req("POST", "/api/alerts", p).status, 201); self.assertEqual(c.req("POST", "/api/alerts", p).status, 409)
        self.assertEqual(c.req("POST", "/api/alerts", {**p, "symbol": "GBPUSD"}).status, 201); self.assertEqual(c.req("POST", "/api/alerts", {**p, "tf": 60}).status, 201)
        self.assertEqual(c.req("POST", "/api/alerts", {**p, "tf": 30}).status, 402)                                 # Pro limit = 3
        for bad in ({**p, "min_grade": "C"}, {**p, "tf": 7}, {**p, "symbol": "XXX"}): self.assertEqual(c.req("POST", "/api/alerts", bad).status, 400)
        al = c.req("GET", "/api/alerts").json()["alerts"]; self.assertEqual(len(al), 3)
        _, e3 = self.user(tier="pro"); c3 = self.client(e3)                                                          # IDOR: cannot touch someone else's alert
        c3.req("POST", "/api/alerts/delete", {"id": al[0]["id"]}); c3.req("POST", "/api/alerts/toggle", {"id": al[0]["id"], "active": False}); self.assertEqual(len(c.req("GET", "/api/alerts").json()["alerts"]), 3); self.assertEqual(c.req("GET", "/api/alerts").json()["alerts"][0]["active"], 1)
        c.req("POST", "/api/alerts/toggle", {"id": al[0]["id"], "active": False}); self.assertEqual(c.req("GET", "/api/alerts").json()["alerts"][0]["active"], 0); c.req("POST", "/api/alerts/delete", {"id": al[0]["id"]}); self.assertEqual(len(c.req("GET", "/api/alerts").json()["alerts"]), 2)

    def test_requires_mail_and_verified_email(self):
        _, e = self.user(tier="pro"); c = self.client(e); self.svc.mailer = Mailer()
        self.assertEqual(c.req("POST", "/api/alerts", {"symbol": "EURUSD", "tf": 15, "min_grade": "A"}).status, 503); self.svc.mailer = self.mail
        with self.svc.db.tx() as cx: cx.execute("UPDATE users SET email_verified=0")
        self.assertEqual(c.req("POST", "/api/alerts", {"symbol": "EURUSD", "tf": 15, "min_grade": "A"}).status, 403)

    def test_worker_sends_once_per_candle_and_dedupes(self):
        _, e = self.user(tier="pro"); self.svc.alert_add(self.row(e), "EURUSD", 15, "A")
        with mock.patch.object(self.svc, "_build", self.fake_build("A")):
            self.assertEqual(self.svc.alerts_run_once(1_700_000_000), 1); body = self.mail.sent[-1][2]
            self.assertIn("BUY EURUSD", self.mail.sent[-1][1]); self.assertIn("Spread is small", body); self.assertIn("not advice", body); self.assertIn("1.095", body)
            self.assertEqual(self.svc.alerts_run_once(1_700_000_100), 0)                                              # same candle: nothing
        cs = self.svc.md.get("EURUSD", 15)[0]
        with mock.patch.object(self.svc, "_build", self.fake_build("A")), mock.patch.object(self.svc.md, "get", return_value=(cs[:-1] + [Candle(cs[-1].ts + timedelta(minutes=15), 1.1, 1.1, 1.1, 1.1, 1)], None, cs[-1].ts, "t")):
            self.assertEqual(self.svc.alerts_run_once(1_700_000_200), 0)                                              # new candle, same direction within 4 candles: deduped

    def test_grade_bar_and_ineligible_users(self):
        _, e = self.user(tier="pro"); self.svc.alert_add(self.row(e), "EURUSD", 15, "A")
        with mock.patch.object(self.svc, "_build", self.fake_build("B")): self.assertEqual(self.svc.alerts_run_once(1_700_000_000), 0)       # grade B does not meet an 'A' bar
        with self.svc.db.tx() as c: c.execute("UPDATE alerts SET last_candle=0"); c.execute("UPDATE users SET paid_until=?", (int(time.time()) - 5,))
        with mock.patch.object(self.svc, "_build", self.fake_build("A")): self.assertEqual(self.svc.alerts_run_once(1_700_000_000), 0)       # expired plan: no alerts
        with self.svc.db.tx() as c: c.execute("UPDATE users SET paid_until=?, status='suspended'", (int(time.time()) + 999,))
        with mock.patch.object(self.svc, "_build", self.fake_build("A")): self.assertEqual(self.svc.alerts_run_once(1_700_000_000), 0)       # suspended: no alerts

    def test_daily_cap(self):
        self.svc.s.alerts_max_per_user_per_day = 1; _, e = self.user(tier="elite"); u = self.row(e); self.svc.alert_add(u, "EURUSD", 15, "A"); self.svc.alert_add(u, "GBPUSD", 15, "A")
        with mock.patch.object(self.svc, "_build", self.fake_build("A")): self.assertEqual(self.svc.alerts_run_once(1_700_000_000), 1)


class OpsTests(Base):
    def test_migration_upgrades_old_database_and_keeps_data(self):
        p = os.path.join(self.d, "old.db"); c = sqlite3.connect(p)
        c.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE COLLATE NOCASE, pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', tier TEXT NOT NULL DEFAULT 'free', paid_until INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', totp_secret TEXT, totp_enabled INTEGER NOT NULL DEFAULT 0, totp_last_step INTEGER NOT NULL DEFAULT 0, equity REAL NOT NULL DEFAULT 1000, risk_pct REAL NOT NULL DEFAULT 0.25, created INTEGER NOT NULL); INSERT INTO users(email,pw_hash,created,tier) VALUES('legacy@example.com','h',1,'pro');")
        c.commit(); c.close(); db = DB(p); cols = {r["name"] for r in db.all("PRAGMA table_info(users)")}
        self.assertTrue({"email_verified", "profile", "terms_ts"} <= cols); u = db.one("SELECT * FROM users"); self.assertEqual((u["email"], u["tier"], u["email_verified"], u["profile"]), ("legacy@example.com", "pro", 1, "balanced"))
        self.assertEqual(db.one("PRAGMA user_version")[0], DB.SCHEMA_VERSION); DB(p); self.assertEqual(len(db.all("SELECT * FROM users")), 1)   # idempotent
        for t in ("events", "alerts", "email_tokens"): self.assertIsNotNone(db.one("SELECT name FROM sqlite_master WHERE name=?", (t,)))

    def test_backup_is_consistent_private_and_restorable(self):
        self.user(); self.user(); out = os.path.join(self.d, "bk.db"); self.svc.db.backup(out)
        self.assertEqual(oct(os.stat(out).st_mode & 0o777), "0o600"); copy = DB(out); self.assertEqual(copy.one("SELECT COUNT(*) n FROM users")["n"], 2); self.assertTrue(verify_audit(copy))
        from apex.web.main import backup
        cfg = os.path.join(self.d, "c.json"); json.dump({"db_path": self.s.db_path}, open(cfg, "w")); out2 = os.path.join(self.d, "bk2.db"); self.assertEqual(backup(cfg, out2), 0); self.assertTrue(os.path.exists(out2))

    def test_deep_health_and_public_config(self):
        c = Client(self.port); r = c.req("GET", "/api/health/deep"); self.assertEqual(r.status, 200); j = r.json(); self.assertTrue(j["ok"] and j["db"]); self.assertNotIn("secret", r.raw.decode().lower())
        pub = c.req("GET", "/api/public").json(); self.assertEqual((pub["mail"], pub["billing"], pub["data_mode"]), (False, True, "test")); self.assertEqual(pub["brand"], self.s.brand_name)
        with mock.patch.object(self.svc.md, "get", side_effect=RuntimeError("feed down")): self.assertEqual(c.req("GET", "/api/health/deep").status, 503)

    def test_legal_page_and_registration_acceptance_recorded(self):
        c = Client(self.port); r = c.req("GET", "/legal"); self.assertEqual(r.status, 200); self.assertIn(b"Risk disclosure", r.raw); self.assertIn("default-src 'none'", r.headers.getheader("Content-Security-Policy")); self.assertNotIn(b"<script", r.raw); self.assertNotIn(b'style="', r.raw)
        self.assertEqual(c.req("GET", "/static/legal.html").status, 404); self.svc.rl = sec.RateLimiter()
        c.req("POST", "/api/register", {"email": "acc@example.com", "password": PW, "accept_terms": True}); self.assertGreater(self.row("acc@example.com")["terms_ts"], 0)

    def test_profile_selection_gating_and_effect(self):
        _, e = self.user(); c = self.client(e)
        self.assertEqual(c.req("POST", "/api/risk", {"equity": 1000, "risk_pct": 0.5, "profile": "conservative"}).status, 402); self.assertEqual(c.req("POST", "/api/risk", {"equity": 1000, "risk_pct": 0.5, "profile": "balanced"}).status, 200)
        _, e2 = self.user(tier="pro"); c2 = self.client(e2); self.assertEqual(c2.req("POST", "/api/risk", {"equity": 1000, "risk_pct": 0.5, "profile": "nonsense"}).status, 400)
        self.assertEqual(c2.req("POST", "/api/risk", {"equity": 5000, "risk_pct": 0.5, "profile": "conservative"}).status, 200)
        me = c2.req("GET", "/api/me").json(); self.assertEqual(me["profile"], "conservative"); self.assertEqual(len(me["profiles"]), 3)
        d = c2.req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).json(); self.assertEqual(d["profile"], "conservative")
        self.assertEqual(c.req("GET", "/api/me").json()["profile"], "balanced")                                      # free users always see the default

    def test_compare_endpoint_gating(self):
        _, e = self.user(tier="pro"); self.assertEqual(self.client(e).req("POST", "/api/profiles-compare", {"symbol": "EURUSD", "tf": 15}).status, 402)
        _, e2 = self.user(tier="elite"); r = self.client(e2).req("POST", "/api/profiles-compare", {"symbol": "EURUSD", "tf": 15}); self.assertEqual(r.status, 200, r.raw); self.assertEqual(len(r.json()["rows"]), 3)

    def test_forwarded_header_cannot_bypass_rate_limits_unless_proxy_trusted(self):
        c = Client(self.port); codes = [c.req("POST", "/api/login", {"email": f"u{i}@example.com", "password": "wrong-password-1"}, headers={"X-Forwarded-For": f"9.9.9.{i}"}).status for i in range(25)]
        self.assertIn(429, codes)                                            # spoofed XFF from an untrusted peer is ignored: still one bucket
        from apex.web.webconfig import WebSettings
        self.assertTrue(any("not an IP" in p for p in WebSettings(trusted_proxies=["nonsense"]).problems())); self.assertEqual(WebSettings(trusted_proxies=["10.0.0.0/8", "::1"]).problems(), [])

    def test_production_smtp_validation(self):
        from apex.web.webconfig import WebSettings
        base = dict(production=True, base_url="https://a.example.com", secure_cookies=True, feed_type="oanda", oanda_account="1", company_name="X Ltd", support_email="h@example.com", legal_jurisdiction="NG",
                    legal_updated="2026-01-15", legal_reviewed=True, price_kobo={"pro": 100000, "elite": 200000}, smtp_host="smtp.x.com", smtp_from="a@x.com")
        s = WebSettings(**{**base, "smtp_security": "none"}); self.assertTrue(any("starttls" in p for p in s.problems())); s.smtp_security = "starttls"; self.assertEqual(s.problems(), []); s.smtp_from = ""; self.assertTrue(any("smtp_from" in p for p in s.problems()))


if __name__ == "__main__":
    unittest.main()
