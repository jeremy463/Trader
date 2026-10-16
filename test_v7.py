import contextlib, io, json, os, tempfile, threading, time, unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse
from apex import feeds, timing as T
from apex.engine import analyze
from apex.config import RiskConfig, ExecConfig
from apex.scanner import METHOD_COUNT, FAMILIES
from apex.web import security as sec
from apex.web.mailer import Mailer
from apex.web.server import make_server
from apex.web.webconfig import WebSettings, load_web_settings
from tests.test_web import Base, Client, PW, SECRET
from tests.test_v5 import FakeMailer, MiniSMTP
from tests.synth import synthetic
from tests.fixtures import TESTDATA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD = dict(production=True, base_url="https://app.example.com", secure_cookies=True, feed_type="oanda", oanda_account="101-001-1-001", company_name="Example Ltd", support_email="help@example.com",
            legal_jurisdiction="Nigeria", legal_updated="2026-01-15", legal_reviewed=True, price_kobo={"pro": 1500000, "elite": 4500000})


class ProductionSettings(unittest.TestCase):
    def probs(self, **kw): return WebSettings(**{**PROD, **kw}).problems()
    def test_complete_config_is_valid_and_secrets_do_not_block(self):
        self.assertEqual(self.probs(), [])
        for k in ("PAYSTACK_SECRET_KEY", "OANDA_TOKEN", "SMTP_PASSWORD", "TWELVEDATA_API_KEY"): self.assertNotIn(k, os.environ.get(k, "") and "x")   # irrelevant to validity
    def test_each_hard_requirement_blocks(self):
        cases = {"company_name": ("", "company_name"), "support_email": ("", "support_email"), "legal_jurisdiction": ("", "legal_jurisdiction"), "legal_updated": ("", "legal_updated"),
                 "legal_reviewed": (False, "legal_reviewed"), "feed_type": ("csv", "csv"), "secure_cookies": (False, "secure_cookies"), "base_url": ("http://a.example.com", "https"),
                 "oanda_account": ("", "oanda_account"), "allow_private_feeds": (True, "private"), "price_kobo": ({"pro": 1500000}, "elite")}
        for k, (v, needle) in cases.items(): self.assertTrue(any(needle in p for p in self.probs(**{k: v})), (k, self.probs(**{k: v})))
    def test_prices_must_be_sane(self):
        for bad in ({"pro": 0, "elite": 100000}, {"pro": 1.5, "elite": 100000}, {"pro": True, "elite": 100000}, {"pro": 100000, "elite": 100000, "gold": 5}): self.assertTrue(self.probs(price_kobo=bad), bad)
    def test_input_hardening(self):
        for b in ("Bad\nBrand", "<script>", 'a"b', "x" * 61, "  ", "a&b"): self.assertTrue(self.probs(brand_name=b), repr(b))
        self.assertTrue(self.probs(legal_updated="15/01/2026")); self.assertTrue(self.probs(support_email="not-an-email")); self.assertTrue(self.probs(feed_fallback_type="oanda")); self.assertTrue(self.probs(feed_fallback_type="bogus"))
        self.assertTrue(self.probs(feed_symbols=["EURUSD", "NOPE"])); self.assertTrue(self.probs(feed_symbols=["ES"])); self.assertTrue(self.probs(feed_symbols=[])); self.assertTrue(self.probs(paystack_webhook_ips=["not-an-ip"]))
        self.assertEqual(self.probs(paystack_webhook_ips=["203.0.113.0/24", "2001:db8::/32"]), []); self.assertEqual(self.probs(feed_fallback_type="twelvedata"), [])
    def test_development_may_use_test_data_but_it_is_labelled(self):
        s = WebSettings(); self.assertEqual((s.problems(), s.data_mode), ([], "test")); self.assertEqual(WebSettings(**PROD).data_mode, "live")
    def test_loader_refuses_production_test_data(self):
        p = os.path.join(tempfile.mkdtemp(), "c.json")
        with open(p, "w") as f: json.dump({**PROD, "feed_type": "csv"}, f)
        with self.assertRaises(ValueError) as cm: load_web_settings(p)
        self.assertIn("test data", str(cm.exception))


class FakeProviders:
    """Local servers imitating Twelve Data and OANDA response shapes (the real services cannot be reached from the build environment)."""
    def __init__(self):
        outer = self; self.hits, self.mode = [], "ok"
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _j(self, code, obj): b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            def do_GET(self):
                u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}; outer.hits.append((u.path, q, dict(self.headers)))
                if outer.mode == "429": return self._j(429, {})
                if outer.mode == "500": return self._j(500, {})
                if outer.mode == "error" and u.path in ("/time_series", "/price"): return self._j(200, {"code": 401, "message": "**apikey** parameter is incorrect or not specified. key=" + q.get("apikey", ""), "status": "error"})
                if u.path == "/time_series":
                    step = 900; now = int(time.time()); last_open = now - now % step; n = 100 if outer.mode != "few" else 10
                    vals = [{"datetime": datetime.fromtimestamp(last_open - (n - 1 - i) * step, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "open": "1.1000", "high": "1.1010", "low": "1.0990", "close": "1.1005", "volume": "0"} for i in range(n)]
                    return self._j(200, {"meta": {"symbol": q.get("symbol")}, "values": vals, "status": "ok"})
                if u.path == "/price": return self._j(200, {"price": "1.10250"})
                if u.path.startswith("/v3/instruments/") and u.path.endswith("/candles"):
                    cs = [{"complete": True, "volume": 10 + i, "time": datetime.fromtimestamp(1_700_000_000 + i * 900, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z"), "mid": {"o": "1.1", "h": "1.101", "l": "1.099", "c": "1.1005"}} for i in range(80)]
                    cs.append({"complete": False, "volume": 1, "time": "2099-01-01T00:00:00.000000000Z", "mid": {"o": "9", "h": "9", "l": "9", "c": "9"}}); return self._j(200, {"candles": cs})
                if u.path.endswith("/pricing"): return self._j(200, {"prices": [{"tradeable": outer.mode != "closed", "time": "2026-10-07T12:00:00.000000000Z", "bids": [{"price": "1.0999"}], "asks": [{"price": "1.1001"}]}]})
                if u.path == "/balance": return self._j(200, {"status": True, "message": "Balances retrieved", "data": [{"currency": "NGN", "balance": 0}]})
                self._j(404, {})
        self.srv = HTTPServer(("127.0.0.1", 0), H); self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"; threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def close(self): self.srv.shutdown(); self.srv.server_close()


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.fp = FakeProviders(); os.environ["TD_TEST_KEY"] = "SUPERSECRETKEY123"; os.environ["OANDA_TEST_TOKEN"] = "oanda-secret-token"
    @classmethod
    def tearDownClass(cls): cls.fp.close()
    def setUp(self): self.fp.mode = "ok"; self.fp.hits.clear()
    def td(self): return feeds.TwelveDataFeed("TD_TEST_KEY", base=self.fp.base)

    def test_twelvedata_candles_drop_forming_bar_and_use_utc_asc(self):
        cs = self.td().candles("EURUSD", 900, 600); self.assertEqual(len(cs), 99)                                  # 100 returned, the still-forming bar removed
        self.assertTrue(all(c.ts.tzinfo is not None and c.ts.utcoffset() == timedelta(0) for c in cs)); self.assertTrue(all(a.ts < b.ts for a, b in zip(cs, cs[1:])))
        self.assertGreater((datetime.now(timezone.utc) - cs[-1].ts).total_seconds(), 899)                           # last kept bar is fully closed
        path, q, _ = self.fp.hits[-1]; self.assertEqual((path, q["symbol"], q["interval"], q["timezone"], q["order"]), ("/time_series", "EUR/USD", "15min", "UTC", "ASC"))
    def test_twelvedata_never_leaks_the_key(self):
        for mode, needle in (("error", "provider error"), ("429", "rate limited"), ("500", "http 500"), ("few", "too few")):
            self.fp.mode = mode
            with self.assertRaises(feeds.FeedError) as cm: self.td().candles("EURUSD", 900, 600)
            self.assertIn(needle, str(cm.exception)); self.assertNotIn("SUPERSECRETKEY123", str(cm.exception), mode)
        dead = feeds.TwelveDataFeed("TD_TEST_KEY", base="http://127.0.0.1:1", timeout=1)
        with self.assertRaises(feeds.FeedError) as cm: dead.candles("EURUSD", 900, 600)
        self.assertNotIn("SUPERSECRETKEY123", str(cm.exception)); self.assertNotIn("apikey", str(cm.exception))
    def test_twelvedata_validation_and_quote_is_honest_about_spread(self):
        with self.assertRaises(feeds.FeedError): self.td().candles("ES", 900, 100)
        with self.assertRaises(feeds.FeedError): self.td().candles("EURUSD", 123, 100)
        with self.assertRaises(RuntimeError): feeds.TwelveDataFeed("NO_SUCH_ENV")
        q = self.td().quote("EURUSD"); self.assertTrue(q.source.startswith("simulated-spread")); self.assertAlmostEqual(q.spread, 0.0001, 8); self.assertAlmostEqual(q.mid, 1.1025, 6)
        self.fp.mode = "error"
        with self.assertRaises(feeds.FeedError): self.td().quote("EURUSD")
    def test_permanent_client_errors_are_not_retried(self):
        self.fp.mode = "500"; t0 = time.time()
        with self.assertRaises(feeds.FeedError): self.td().candles("EURUSD", 900, 100)
        self.assertGreaterEqual(len(self.fp.hits), 3)                                                                # server errors ARE retried
        n = len(self.fp.hits)
        class Boom(urllib_error().HTTPError): pass
        calls = []
        def fn(): calls.append(1); raise urllib_error().HTTPError("u", 401, "no", {}, None)
        with self.assertRaises(urllib_error().HTTPError): feeds.retry(fn)
        self.assertEqual(len(calls), 1)                                                                                # 401 fails fast
    def test_oanda_candles_complete_only_and_quote(self):
        o = feeds.OandaFeed("101-001-1-001", token_env="OANDA_TEST_TOKEN", practice=True, base=self.fp.base); cs = o.candles("EURUSD", 900, 600)
        self.assertEqual(len(cs), 80); self.assertTrue(all(c.c == 1.1005 for c in cs)); self.assertEqual(self.fp.hits[-1][0], "/v3/instruments/EUR_USD/candles")
        self.assertEqual(self.fp.hits[-1][2].get("Authorization"), "Bearer oanda-secret-token"); q = o.quote("EURUSD"); self.assertEqual((q.bid, q.ask, q.source), (1.0999, 1.1001, "oanda"))
        self.fp.mode = "closed"; self.assertIsNone(o.quote("EURUSD"))
        with self.assertRaises(RuntimeError): feeds.OandaFeed("x", token_env="NO_SUCH_ENV")
    def test_failover_switches_label_and_recovers(self):
        class Bad:
            label = "Primary"
            def candles(self, *a): raise feeds.FeedError("down")
            def quote(self, *a): raise feeds.FeedError("down")
        class Good:
            label = "Backup"
            def candles(self, *a): return ["ok"]
            def quote(self, *a): return "q"
        f = feeds.FailoverFeed(Bad(), Good()); self.assertEqual(f.candles("EURUSD", 900, 5), ["ok"]); self.assertEqual(f.label, "Backup")
        f2 = feeds.FailoverFeed(Good(), Bad()); f2.candles("EURUSD", 900, 5); self.assertEqual(f2.label, "Backup")
        class P2(Good): label = "Primary"
        f3 = feeds.FailoverFeed(P2(), Good()); f3.candles("EURUSD", 900, 5); self.assertEqual(f3.label, "Primary")
        with self.assertRaises(feeds.FeedError): feeds.FailoverFeed(Bad(), Bad()).candles("EURUSD", 900, 5)
    def test_make_feed_skips_providers_without_credentials(self):
        from apex.web.main import make_feed
        os.environ.pop("TWELVEDATA_API_KEY", None); os.environ.pop("OANDA_TOKEN", None)
        self.assertIsNone(make_feed(WebSettings(feed_type="oanda", oanda_account="1")))                              # no credentials: no crash, readiness flags it
        os.environ["OANDA_TOKEN"] = "x"; os.environ["TWELVEDATA_API_KEY"] = "y"
        try:
            f = make_feed(WebSettings(feed_type="oanda", oanda_account="1", feed_fallback_type="twelvedata")); self.assertIsInstance(f, feeds.FailoverFeed); self.assertEqual(f.label, "OANDA")
            self.assertIsInstance(make_feed(WebSettings(feed_type="twelvedata")), feeds.TwelveDataFeed); self.assertIsNone(make_feed(WebSettings(feed_type="csv")))
        finally: os.environ.pop("OANDA_TOKEN"); os.environ.pop("TWELVEDATA_API_KEY")
    def test_paystack_balance_call(self):
        from apex.web.billing import PaystackClient
        pc = PaystackClient("sk_test_abc", base=self.fp.base); self.assertEqual(pc.balance()[0]["currency"], "NGN"); self.assertEqual((self.fp.hits[-1][0], self.fp.hits[-1][2].get("Authorization")), ("/balance", "Bearer sk_test_abc"))


def urllib_error():
    import urllib.error; return urllib.error


class ClosedMarketHonesty(Base):
    def test_closed_market_is_not_reported_as_stale_data(self):
        cs = synthetic(600, seed=1); late = cs[-1].ts + timedelta(hours=40)
        a = analyze("EURUSD", cs, 900, RiskConfig(), ExecConfig(), None, None, late, enforce_session=False)
        self.assertTrue(any("stale" in m for m in a.missing)); self.assertEqual(a.classification, "DATA INSUFFICIENT")
        b = analyze("EURUSD", cs, 900, RiskConfig(), ExecConfig(), None, None, late, enforce_session=False, market_closed=True)
        self.assertFalse(any("stale" in m for m in b.missing)); self.assertNotEqual(b.classification, "DATA INSUFFICIENT")
    def test_deep_health_does_not_alarm_on_weekends(self):
        class Old:
            label = "Fake"
            def candles(self, s, tf, n): return synthetic(600, seed=1)
            def quote(self, s): return None
            def now(self): return datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        self.svc.s.feed_type = "oanda"; self.svc.md.feed = Old(); self.svc.md._c.clear()
        with mock.patch.object(T, "market_open", return_value=False): self.assertTrue(self.svc.health()["data_fresh"])
        self.svc.md._c.clear()
        with mock.patch.object(T, "market_open", return_value=True): self.assertFalse(self.svc.health()["data_fresh"])    # open market + old candles = genuinely stale
    def test_scan_source_label_says_test_data_honestly(self):
        _, e = self.user(); d = self.client(e).req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).json(); self.assertIn("TEST DATA", d["source"]); self.assertIn("not live market data", d["source"].lower()); self.assertFalse(d["source"].lower().endswith("(live)"))


class LegalAndPublic(Base):
    def setUp(self):
        super().setUp(); s = self.svc.s; s.company_name, s.support_email, s.legal_jurisdiction, s.legal_updated, s.brand_name = "Northwind Markets Ltd", "help@northwind.example", "Federal Republic of Nigeria", "2026-03-04", "Northwind Analytics"
    def test_legal_page_uses_real_configured_details_and_no_template_banner(self):
        r = Client(self.port).req("GET", "/legal"); body = r.raw.decode(); self.assertEqual(r.status, 200)
        for needle in ("Northwind Markets Ltd", "Federal Republic of Nigeria", "help@northwind.example", "2026-03-04", "Northwind Analytics", "Risk disclosure", "Paystack"): self.assertIn(needle, body)
        for bad in ("Template text", "lawyer review", "YOUR", "lorem", "<script"): self.assertNotIn(bad, body)
        self.assertNotIn("Refunds", body); self.svc.s.refund_policy = "Passes are non-refundable except where the law requires."; self.assertIn("non-refundable", Client(self.port).req("GET", "/legal").raw.decode())
    def test_legal_page_escapes_everything(self):
        self.svc.s.company_name = '<script>alert(1)</script>"&'; self.svc.s.refund_policy = "<img src=x onerror=alert(1)>"; body = Client(self.port).req("GET", "/legal").raw.decode()
        self.assertNotIn("<script>alert", body); self.assertNotIn("<img src=x", body); self.assertIn("&lt;script&gt;", body)
    def test_robots_and_security_txt(self):
        c = Client(self.port); r = c.req("GET", "/robots.txt"); self.assertIn("Disallow: /api/", r.raw.decode()); self.assertIn("Disallow: /webhooks/", r.raw.decode())
        st = c.req("GET", "/.well-known/security.txt"); txt = st.raw.decode(); self.assertIn("Contact: mailto:help@northwind.example", txt)
        exp = datetime.strptime([l for l in txt.splitlines() if l.startswith("Expires:")][0].split(": ")[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc); self.assertGreater(exp, datetime.now(timezone.utc) + timedelta(days=300))
        self.svc.s.support_email = ""; self.assertEqual(c.req("GET", "/.well-known/security.txt").status, 404)
    def test_public_config_has_live_counts_and_data_mode(self):
        p = Client(self.port).req("GET", "/api/public").json(); self.assertEqual((p["methods"], p["families"], p["brand"], p["data_mode"], p["support_email"]), (METHOD_COUNT, len(FAMILIES), "Northwind Analytics", "test", "help@northwind.example"))
        self.assertNotIn("secret", json.dumps(p).lower())
    def test_emails_use_the_configured_brand(self):
        m = FakeMailer(); self.svc.mailer = m; self.svc.rl = sec.RateLimiter(); Client(self.port).req("POST", "/api/register", {"email": "brand@example.com", "password": PW, "accept_terms": True})
        self.assertIn("Northwind Analytics", m.sent[-1][1]); self.assertIn("Northwind Analytics", m.sent[-1][2]); self.assertNotIn("APEX", m.sent[-1][1] + m.sent[-1][2])
        self.svc.billing.on_credit = None
    def test_static_files_contain_no_hardcoded_demo_or_template_content(self):
        for root_, _, files in os.walk(os.path.join(ROOT, "apex")):
            for fn in files:
                if fn.endswith((".py", ".js", ".html", ".css", ".svg")):
                    s = open(os.path.join(root_, fn), encoding="utf-8").read()
                    for bad in ("DEMO DATA", "def synthetic", "market_data_demo", "Template text"): self.assertNotIn(bad, s, f"{fn}: {bad}")
        self.assertFalse(os.path.exists(os.path.join(ROOT, "market_data_demo"))); self.assertFalse(os.path.exists(os.path.join(ROOT, "sample_EURUSD_M15.csv")))


class HostAndWebhookHardening(Base):
    def prod_server(self, **kw):
        s = self.svc.s; s.production, s.base_url, s.paystack_webhook_ips, s.trusted_proxy = True, "https://app.example.com", kw.get("ips", []), kw.get("proxy", False)
        srv = make_server(self.svc); threading.Thread(target=srv.serve_forever, daemon=True).start(); self.addCleanup(lambda: (srv.shutdown(), srv.server_close())); return srv.server_address[1]
    def test_loopback_host_may_probe_liveness_only(self):
        port = self.prod_server(); c = Client(port)
        self.assertEqual(c.req("GET", "/api/health").status, 200); self.assertEqual(c.req("GET", "/api/health/deep").status, 403); self.assertEqual(c.req("GET", "/api/public").status, 403); self.assertEqual(c.req("GET", "/").status, 403)
        self.assertEqual(c.req("GET", "/api/health", headers={"Host": "evil.example"}).status, 403); self.assertEqual(c.req("GET", "/api/public", headers={"Host": "app.example.com"}).status, 200)
    def test_webhook_ip_allowlist(self):
        self.svc.s.trusted_proxies = ["127.0.0.1"]; port = self.prod_server(ips=["203.0.113.0/24"], proxy=True); c = Client(port)
        ev = json.dumps({"event": "charge.success", "data": {"reference": "nope", "status": "success", "amount": 1, "currency": "NGN", "customer": {"email": "x@y.zz"}}}).encode(); sig = self.sign(ev)
        H = lambda ip: {"x-paystack-signature": sig, "Host": "app.example.com", "X-Forwarded-For": ip}
        self.assertEqual(c.req("POST", "/webhooks/paystack", raw=ev, headers=H("198.51.100.7")).status, 403)       # valid signature, wrong source address
        self.assertEqual(c.req("POST", "/webhooks/paystack", raw=ev, headers=H("203.0.113.9")).status, 200)
        self.assertEqual(c.req("POST", "/webhooks/paystack", raw=ev, headers={"x-paystack-signature": "bad", "Host": "app.example.com", "X-Forwarded-For": "203.0.113.9"}).status, 401)   # right address, bad signature


class AccountCompliance(Base):
    def test_export_contains_own_data_only_and_no_secrets(self):
        uid, e = self.user(tier="pro"); c = self.client(e); _, e2 = self.user(email="other@example.com"); c2 = self.client(e2)
        c.req("POST", "/api/journal", {"symbol": "EURUSD", "side": "BUY", "entry": 1.1, "stop": 1.09, "exit": 1.12, "grade": "A", "notes": "mine"}); c2.req("POST", "/api/journal", {"symbol": "EURUSD", "side": "SELL", "entry": 1.1, "stop": 1.11, "exit": 1.09, "grade": "", "notes": "theirs"})
        c.req("POST", "/api/watchlist", {"symbols": ["EURUSD"]}); _, ref = self.pending(e); self.webhook(self.charge(ref, e, 1500000))
        r = c.req("GET", "/api/account/export"); self.assertEqual(r.status, 200); self.assertIn("attachment", r.headers.getheader("Content-Disposition")); d = r.json(); raw = r.raw.decode()
        self.assertEqual(d["account"]["email"], e); self.assertEqual(d["watchlist"], ["EURUSD"]); self.assertEqual([j["notes"] for j in d["journal"]], ["mine"]); self.assertEqual(d["payments"][0]["status"], "success")
        for secret in ("pw_hash", "scrypt$", "totp", "other@example.com", "theirs", "csrf"): self.assertNotIn(secret, raw)
        self.assertEqual(Client(self.port).req("GET", "/api/account/export").status, 401)
    def test_session_listing_masking_and_revocation(self):
        _, e = self.user(); a = self.client(e); b = self.client(e); s = a.req("GET", "/api/sessions").json()["sessions"]; self.assertEqual(len(s), 2); self.assertEqual(sum(1 for x in s if x["current"]), 1)
        self.assertTrue(all(x["ip"].endswith(".x") or "..." in x["ip"] for x in s))                                    # address is masked
        r = a.req("POST", "/api/sessions/revoke-others", {}); self.assertEqual(r.json()["revoked"], 1); self.assertEqual(a.req("GET", "/api/me").status, 200); self.assertEqual(b.req("GET", "/api/me").status, 401)
        self.assertEqual(Client(self.port).req("GET", "/api/sessions").status, 401); self.assertEqual(a.req("POST", "/api/sessions/revoke-others", {}, csrf=False).status, 403)


class Readiness(Base):
    def rows(self, force=True): return {r["key"]: r for r in self.svc.readiness(force=force)["rows"]}
    def test_endpoint_authz_shape_cache_and_rate_limit(self):
        _, e = self.user(); self.assertEqual(self.client(e).req("GET", "/api/admin/readiness").status, 403); self.assertEqual(Client(self.port).req("GET", "/api/admin/readiness").status, 401)
        adm = self.admin(); r = adm.req("GET", "/api/admin/readiness").json(); self.assertTrue({"ready", "rows", "counts", "checked"} <= set(r)); self.assertTrue(all({"key", "label", "status", "detail"} <= set(x) for x in r["rows"]))
        self.assertEqual(sum(r["counts"].values()), len(r["rows"])); self.assertEqual(self.svc.readiness()["checked"], self.svc.readiness()["checked"])
        codes = [adm.req("GET", "/api/admin/readiness?force=1").status for _ in range(8)]; self.assertIn(429, codes)
    def test_development_flags_test_data_as_warning_and_production_as_failure(self):
        r = self.rows(); self.assertEqual((r["data"]["status"], r["mode"]["status"]), ("warn", "warn")); self.assertIn("TEST DATA", r["data"]["detail"])
        self.svc.s.production = True; r = self.rows(); self.assertEqual(r["data"]["status"], "fail"); self.assertFalse(self.svc.readiness()["ready"])
    def test_missing_provider_credentials_give_an_actionable_message(self):
        self.svc.s.feed_type = "oanda"; self.svc.md.feed = None; self.svc.md._c.clear(); r = self.rows()["data"]
        self.assertEqual(r["status"], "fail"); self.assertIn("OANDA_TOKEN", r["detail"]); self.assertNotIn("NoneType", r["detail"])
        _, e = self.user(); self.assertEqual(self.client(e).req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).status, 503)        # users see a clean 503, never internals
        self.svc.s.feed_type = "twelvedata"; self.assertIn("TWELVEDATA_API_KEY", self.rows()["data"]["detail"])

    def test_paystack_row_reports_mode_and_validity(self):
        bal = mock.Mock(); self.svc.billing.client = bal; os.environ["PAYSTACK_SECRET_KEY"] = "sk_test_x"; r = self.rows()["paystack"]; self.assertEqual(r["status"], "warn"); self.assertIn("TEST", r["detail"]); self.assertIn("accepted", r["detail"])
        os.environ["PAYSTACK_SECRET_KEY"] = "sk_live_x"; r = self.rows()["paystack"]; self.assertEqual(r["status"], "ok"); self.assertIn("LIVE", r["detail"])
        bal.balance.side_effect = RuntimeError("paystack http 401"); r = self.rows()["paystack"]; self.assertEqual(r["status"], "fail"); self.assertIn("401", r["detail"])
        os.environ.pop("PAYSTACK_SECRET_KEY"); r = self.rows()["paystack"]; self.assertEqual(r["status"], "warn"); self.assertIn("not set", r["detail"]); os.environ["PAYSTACK_SECRET_KEY"] = SECRET
        self.assertIn("/webhooks/paystack", self.rows()["webhook"]["detail"])
    def test_smtp_row_does_a_real_connection_without_sending(self):
        self.assertEqual(self.rows()["smtp"]["status"], "warn"); srv = MiniSMTP()
        try:
            self.svc.mailer = Mailer("127.0.0.1", srv.port, "", "", "a@example.com", "none", timeout=3); r = self.rows()["smtp"]; self.assertEqual(r["status"], "ok"); self.assertIn("connected", r["detail"]); self.assertEqual(srv.msgs, [])
            self.svc.mailer = Mailer("127.0.0.1", 1, "", "", "a@example.com", "none", timeout=1); self.assertEqual(self.rows()["smtp"]["status"], "fail")
        finally: srv.close()
    def test_backup_row(self):
        self.assertEqual(self.rows()["backup"]["status"], "warn"); d = tempfile.mkdtemp(); self.svc.s.backup_dir = d; self.assertEqual(self.rows()["backup"]["status"], "fail")
        p = os.path.join(d, "apex-1.db"); open(p, "w").write("x"); self.assertEqual(self.rows()["backup"]["status"], "ok")
        old = time.time() - 72 * 3600; os.utime(p, (old, old)); r = self.rows()["backup"]; self.assertEqual(r["status"], "fail"); self.assertIn("72", r["detail"])
    def test_admin_2fa_audit_news_and_legal_rows(self):
        self.assertEqual(self.rows()["admin2fa"]["status"], "fail"); self.admin(); r = self.rows(); self.assertEqual((r["admin2fa"]["status"], r["audit"]["status"]), ("ok", "ok"))
        self.assertEqual(r["news"]["status"], "warn"); self.assertEqual(r["calendar"]["status"], "warn"); self.assertEqual(r["failover"]["status"], "warn")
        with self.svc.db.tx() as c: c.execute("INSERT INTO events(time_utc,currency,impact,name,source) VALUES(?,?,?,?,?)", ((datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "USD", "high", "NFP", "manual"))
        self.assertEqual(self.rows()["calendar"]["status"], "ok"); self.svc.s.legal_reviewed, self.svc.s.company_name, self.svc.s.support_email = True, "X Ltd", "a@example.com"; self.assertEqual(self.rows()["legal"]["status"], "ok")
        self.svc.s.feed_fallback_type = "twelvedata"; self.assertEqual(self.rows()["failover"]["status"], "ok"); self.assertEqual(self.rows()["timezones"]["status"], "ok" if T.TZ_OK else "warn")


class OperatorTools(unittest.TestCase):
    def cfg(self, extra=None):
        d = tempfile.mkdtemp(); p = os.path.join(d, "web.json")
        with open(p, "w") as f: json.dump({"db_path": os.path.join(d, "w.db"), "log_path": os.path.join(d, "w.log"), **(extra or {})}, f)
        return p
    def run_cli(self, fn, *a, **k):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): rc = fn(*a, **k)
        return rc, buf.getvalue()
    def test_web_init_writes_a_correct_production_config(self):
        from apex.web.main import web_init
        out = os.path.join(tempfile.mkdtemp(), "web.json"); rc, text = self.run_cli(web_init, out, "app.acme.example", "Acme Ltd", "help@acme.example", "Nigeria", "oanda", "101-1-1-001", "twelvedata", "EURUSD, gbpusd", 15000, 45000.5, "NGN", False)
        self.assertEqual(rc, 0); d = json.load(open(out)); self.assertEqual((d["base_url"], d["price_kobo"], d["feed_symbols"], d["legal_reviewed"], d["production"], d["secure_cookies"]), ("https://app.acme.example", {"pro": 1500000, "elite": 4500050}, ["EURUSD", "GBPUSD"], False, True, True))
        self.assertEqual(d["legal_updated"], datetime.now().date().isoformat()); self.assertEqual(oct(os.stat(out).st_mode & 0o777), "0o640"); todo = WebSettings(**d).problems(); self.assertEqual(len(todo), 1); self.assertIn("legal_reviewed", todo[0]); self.assertIn("legal_reviewed", text)
        rc2, text2 = self.run_cli(web_init, out, "x.example", "A", "a@b.co", "N", "oanda", "1", "", "EURUSD", 1, 2, "NGN", False); self.assertEqual(rc2, 2); self.assertIn("exists", text2)
        d["legal_reviewed"] = True; self.assertEqual(WebSettings(**d).problems(), [])
    def test_paystack_check_branches(self):
        from apex.web import main as M
        path = self.cfg(); os.environ.pop("PAYSTACK_SECRET_KEY", None)
        rc, t = self.run_cli(M.paystack_check, path); self.assertEqual(rc, 1); self.assertIn("not set", t)
        os.environ["PAYSTACK_SECRET_KEY"] = "garbage"; rc, t = self.run_cli(M.paystack_check, path); self.assertEqual(rc, 1); self.assertIn("does not look like", t)
        class Ok:
            def __init__(s, k): pass
            def balance(s): return []
        class Bad(Ok):
            def balance(s): raise RuntimeError("paystack http 401")
        try:
            os.environ["PAYSTACK_SECRET_KEY"] = "sk_test_abc"
            with mock.patch.object(M, "PaystackClient", Ok): rc, t = self.run_cli(M.paystack_check, path)
            self.assertEqual(rc, 0); self.assertIn("TEST secret key accepted", t); self.assertIn("/webhooks/paystack", t); self.assertIn("moves no real money", t); self.assertNotIn("sk_test_abc", t)
            with mock.patch.object(M, "PaystackClient", Bad): rc, t = self.run_cli(M.paystack_check, path)
            self.assertEqual(rc, 1); self.assertIn("401", t); os.environ["PAYSTACK_SECRET_KEY"] = "sk_live_abc"
            with mock.patch.object(M, "PaystackClient", Ok): rc, t = self.run_cli(M.paystack_check, path)
            self.assertIn("LIVE secret key accepted", t); self.assertNotIn("moves no real money", t)
        finally: os.environ["PAYSTACK_SECRET_KEY"] = SECRET
    def test_web_check_exit_codes(self):
        from apex.web import main as M
        saved = os.environ.pop("PAYSTACK_SECRET_KEY", None)        # isolate: a leftover key would (correctly) be validated against the real Paystack API
        try:
            path = self.cfg({"data_dir": TESTDATA}); rc, t = self.run_cli(M.check, path); self.assertEqual(rc, 1); self.assertIn("FAIL Admin account with 2FA", t); self.assertIn("NOT ready", t)
            from apex.web.db import DB
            with DB(json.load(open(path))["db_path"]).tx() as c: c.execute("INSERT INTO users(email,pw_hash,role,totp_secret,totp_enabled,created) VALUES('a@example.com','h','admin','S',1,1)")
            rc, t = self.run_cli(M.check, path); self.assertEqual(rc, 0); self.assertIn("READY for customers", t); self.assertIn("WARN", t)
            rc, t = self.run_cli(M.check, path, True); self.assertEqual(rc, 1)                                       # --strict turns warnings into failures
            os.environ["PAYSTACK_SECRET_KEY"] = "sk_test_unreachable"                                                 # a key Paystack cannot validate is a FAIL, not silently ignored
            with mock.patch("apex.web.billing.PaystackClient.balance", side_effect=RuntimeError("paystack http 401")): rc, t = self.run_cli(M.check, path)
            self.assertEqual(rc, 1); self.assertIn("FAIL Paystack secret key", t); self.assertIn("401", t); self.assertNotIn("sk_test_unreachable", t)
        finally:
            if saved is not None: os.environ["PAYSTACK_SECRET_KEY"] = saved
            else: os.environ.pop("PAYSTACK_SECRET_KEY", None)
    def test_cli_wiring_for_new_commands(self):
        from apex import cli
        out = os.path.join(tempfile.mkdtemp(), "w.json")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["web-init", "--out", out, "--domain", "d.example", "--company", "C", "--support-email", "a@b.co", "--jurisdiction", "N", "--pro-price", "100", "--elite-price", "200"])
        self.assertEqual(rc, 0); self.assertTrue(os.path.exists(out))
        with self.assertRaises(SystemExit): cli.main(["--demo"])                                                      # demo mode is gone
        with self.assertRaises(SystemExit): cli.main(["scan", "--demo"])


if __name__ == "__main__":
    unittest.main()
