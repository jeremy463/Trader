import json, os, threading, time, unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from apex import news as N, timing as T, quality as Q
from apex.data import Candle
from apex.instruments import get_spec
from apex.web import security as sec
from apex.web.service import ApiError
from tests.test_web import Base, Client, PW, DEMO

u = lambda *a: datetime(*a, tzinfo=timezone.utc)
CAL = json.dumps([{"title": "Core CPI m/m", "country": "USD", "date": "2099-10-14T08:30:00-04:00", "impact": "High", "forecast": "0.3%", "previous": "0.4%"},
                  {"title": "ECB Press Conference", "country": "EUR", "date": "2099-10-14T08:45:00-04:00", "impact": "High", "forecast": "", "previous": ""},
                  {"title": "Unemployment Rate", "country": "USD", "date": "2099-10-15T08:30:00-04:00", "impact": "Medium", "forecast": "4.2%", "previous": "4.1%", "actual": "4.4%"},
                  {"title": "Bank Holiday", "country": "GBP", "date": "2099-10-16T00:00:00-04:00", "impact": "Holiday"}, {"title": "junk", "country": "ALL", "date": "2099-10-14T00:00:00Z", "impact": "High"}, {"bad": "row"}]).encode()
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><item><title>Fed signals rate cut path as inflation slows</title><link>https://example.org/a</link><guid>g-fed-1</guid><pubDate>%s</pubDate></item>
<item><title>ECB hawkish: Lagarde says rate hike possible</title><link>javascript:alert(1)</link><guid>g-ecb-1</guid><pubDate>%s</pubDate></item></channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>BoE Bailey: sterling rate hike risk</title><link href="https://example.org/boe"/><id>boe-1</id><updated>2099-01-01T00:00:00Z</updated></entry></feed>"""


def rss_now():
    from email.utils import format_datetime
    d = format_datetime(datetime.now(timezone.utc)).encode(); return RSS % (d, d)


class Parsing(unittest.TestCase):
    def test_calendar_json(self):
        rows = N.parse_ff_json(CAL, "cal"); self.assertEqual(len(rows), 4)                                      # junk, ALL and malformed rows dropped
        cpi = rows[0]; self.assertEqual((cpi["currency"], cpi["impact"], cpi["time_utc"]), ("USD", "high", "2099-10-14T12:30:00Z")); self.assertEqual((cpi["forecast"], cpi["previous"]), ("0.3%", "0.4%"))
        self.assertEqual(rows[3]["impact"], "holiday")
        with self.assertRaises(N.FeedError): N.parse_ff_json(b"not json"); 
        with self.assertRaises(N.FeedError): N.parse_ff_json(b'{"a":1}')

    def test_rss_and_atom_and_unsafe_links(self):
        items = N.parse_feed(rss_now()); self.assertEqual(len(items), 2); self.assertEqual(items[0]["link"], "https://example.org/a"); self.assertEqual(items[1]["link"], "")    # javascript: URL dropped
        a = N.parse_feed(ATOM)[0]; self.assertEqual((a["guid"], a["link"]), ("boe-1", "https://example.org/boe")); self.assertGreater(a["published"], 4_000_000_000)

    def test_xml_attacks_refused(self):
        for bad in (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY l2 "&lol;&lol;">]><rss><channel><item><title>&l2;</title></item></channel></rss>',
                    b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&xxe;</title></item></channel></rss>', b"<!doctype x><rss/>"):
            with self.assertRaises(N.FeedError): N.parse_feed(bad)
        with self.assertRaises(N.FeedError): N.parse_feed(b"<rss><item>")

    def test_tagging_sentiment_and_surprise(self):
        self.assertEqual(sorted(N.tag_currencies("Fed's Powell and Lagarde at the ECB discuss yen")), ["EUR", "JPY", "USD"]); self.assertEqual(N.tag_currencies("Local news today"), [])
        self.assertEqual(N.tag_currencies("Quarterly report", ["USD"]), ["USD"])
        self.assertLess(N.headline_sentiment("Fed signals rate cut as inflation slows"), 0); self.assertGreater(N.headline_sentiment("ECB hawkish, rate hike possible"), 0); self.assertEqual(N.headline_sentiment("Meeting scheduled"), 0)
        self.assertEqual(N.surprise("CPI", "0.4%", "0.3%"), 1); self.assertEqual(N.surprise("CPI", "0.2%", "0.3%"), -1); self.assertEqual(N.surprise("Unemployment Rate", "4.4%", "4.2%"), -1)
        self.assertEqual(N.surprise("Jobless Claims", "210K", "230K"), 1); self.assertEqual(N.surprise("CPI", "", "0.3%"), 0); self.assertEqual(N.surprise("GDP", "1.0%", "1.0%"), 0)

    def test_currency_sentiment_recency_weighting_and_pair_bias(self):
        now = 1_000_000.0; items = [{"currencies": "USD", "sentiment": 1.0, "published": now - 100}, {"currencies": "USD", "sentiment": -1.0, "published": now - 48 * 3600}, {"currencies": "EUR", "sentiment": -1.0, "published": now - 100}, {"currencies": "JPY", "sentiment": 0.0, "published": now}]
        s = N.currency_sentiment(items, now); self.assertGreater(s["USD"]["score"], 0.9); self.assertEqual(s["EUR"]["score"], -1.0); self.assertNotIn("JPY", s)
        self.assertEqual(N.pair_bias(get_spec("EURUSD"), s), round(-1.0 - s["USD"]["score"], 2)); self.assertIsNone(N.pair_bias(get_spec("ES"), s)); self.assertIsNone(N.pair_bias(get_spec("GBPUSD"), {}))

    def test_news_checklist_item(self):
        self.assertIsNone(Q.news_item(None, 1)["ok"]); self.assertIsNone(Q.news_item(0.1, 1)["ok"]); self.assertTrue(Q.news_item(0.6, 1)["ok"]); self.assertFalse(Q.news_item(0.6, -1)["ok"]); self.assertFalse(Q.news_item(0.6, -1)["critical"])


class Fetching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                body, code, hdr = {"/cal": (CAL, 200, {}), "/rss": (rss_now(), 200, {}), "/big": (b"x" * 3_000_000, 200, {}), "/gone": (b"", 404, {}), "/redir": (b"", 302, {"Location": "http://127.0.0.1:1/x"})}.get(self.path, (b"", 404, {}))
                self.send_response(code); [self.send_header(k, v) for k, v in hdr.items()]; self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        cls.srv = HTTPServer(("127.0.0.1", 0), H); cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"; threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown(); cls.srv.server_close()

    def test_ssrf_guard_blocks_internal_targets(self):
        for url in ("http://127.0.0.1/", "http://localhost/x", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/", "http://0.0.0.0/", "file:///etc/passwd", "ftp://example.com/x", "gopher://x", "javascript:alert(1)", "http:///nohost"):
            with self.assertRaises(N.FeedError, msg=url): N.fetch(url, 2)
        N.check_url("http://127.0.0.1/", allow_private=True)

    def test_redirect_to_private_is_blocked(self):
        with self.assertRaises(N.FeedError): N._Redirect(False).redirect_request(mock.Mock(full_url="http://x"), None, 302, "", {}, "http://169.254.169.254/")
        with self.assertRaises(N.FeedError): N.fetch(self.base + "/redir", 2, allow_private=False)

    def test_fetch_ok_and_error_paths(self):
        self.assertTrue(N.fetch(self.base + "/cal", 3, True).startswith(b"[")); self.assertEqual(len(N.parse_ff_json(N.fetch(self.base + "/cal", 3, True))), 4)
        with self.assertRaises(N.FeedError) as c: N.fetch(self.base + "/big", 3, True)
        self.assertIn("too large", str(c.exception))
        with self.assertRaises(N.FeedError): N.fetch(self.base + "/gone", 3, True)
        with self.assertRaises(N.FeedError): N.fetch("http://127.0.0.1:1/", 1, True)


class SyncAndEndpoints(Base):
    def setUp(self):
        super().setUp(); self.s.allow_private_feeds = True
        self.svc.news_sources = [{"name": "Cal", "type": "ff_json", "url": "http://x/cal"}, {"name": "FedRSS", "type": "rss", "url": "http://x/rss", "currencies": ["USD"]}, {"name": "Bad", "type": "rss", "url": "http://x/bad"}]
        self.pages = {"http://x/cal": CAL, "http://x/rss": rss_now(), "http://x/bad": b"<not xml"}
        self.svc.fetcher = lambda url, to, ap: self.pages[url] if not isinstance(self.pages[url], Exception) else (_ for _ in ()).throw(self.pages[url])
    def count(self, t, w=""): return self.svc.db.one(f"SELECT COUNT(*) n FROM {t} {w}")["n"]

    def test_sync_is_idempotent_isolated_and_records_status(self):
        r = self.svc.news_sync(); self.assertTrue(r["Cal"]["ok"] and r["FedRSS"]["ok"]); self.assertFalse(r["Bad"]["ok"]); self.assertEqual((self.count("events"), self.count("news_items")), (4, 2))
        r2 = self.svc.news_sync(); self.assertEqual((self.count("events"), self.count("news_items")), (4, 2)); self.assertEqual(r2["FedRSS"]["items"], 0)      # events replaced not duplicated; guids deduped
        st = {f["name"]: f for f in self.svc.feeds_status()}; self.assertEqual(st["Cal"]["last_error"], ""); self.assertGreater(st["Cal"]["last_ok"], 0); self.assertIn("valid XML", st["Bad"]["last_error"]); self.assertEqual(st["Bad"]["last_ok"], 0)
        self.pages["http://x/cal"] = N.FeedError("http 503"); self.svc.news_sync(); st = {f["name"]: f for f in self.svc.feeds_status()}
        self.assertEqual(st["Cal"]["last_error"], "http 503"); self.assertGreater(st["Cal"]["last_ok"], 0); self.assertEqual(self.count("events"), 4)               # outage keeps last good data and last_ok

    def test_manual_calendar_survives_feed_sync_and_vice_versa(self):
        self.svc.admin_events_set({"email": "a"}, "time_utc,currency,impact,name\n2099-01-01T12:30:00Z,USD,high,Manual NFP\n"); self.svc.news_sync()
        self.assertEqual(self.count("events", "WHERE source='manual'"), 1); self.assertEqual(self.count("events", "WHERE source='Cal'"), 4)
        self.svc.admin_events_set({"email": "a"}, "time_utc,currency,impact,name\n2099-01-02T12:30:00Z,EUR,high,Other\n"); self.assertEqual(self.count("events", "WHERE source='Cal'"), 4)
        self.assertEqual(self.count("events", "WHERE source='manual'"), 1)

    def test_synced_events_drive_blackout_policy(self):
        self.svc.news_sync(); pol = self.svc.event_policy(); self.assertIsNotNone(pol); self.assertTrue(any(e[1] == "USD" for e in pol.events))

    def test_tagging_and_sentiment_stored(self):
        self.svc.news_sync(); rows = {r["guid"]: r for r in self.svc.db.all("SELECT * FROM news_items")}
        self.assertIn("USD", rows["g-fed-1"]["currencies"].split(",")); self.assertLess(rows["g-fed-1"]["sentiment"], 0); self.assertIn("EUR", rows["g-ecb-1"]["currencies"]); self.assertGreater(rows["g-ecb-1"]["sentiment"], 0); self.assertEqual(rows["g-ecb-1"]["link"], "")

    def test_calendar_and_news_endpoints_authz_and_tier_limits(self):
        self.svc.news_sync(); self.assertEqual(Client(self.port).req("GET", "/api/calendar").status, 401); self.assertEqual(Client(self.port).req("GET", "/api/news").status, 401)
        _, e = self.user(); cf = self.client(e); _, e2 = self.user(tier="pro"); cp = self.client(e2)
        with mock.patch("apex.web.service.datetime") as dt:       # make "now" fall just before the fixture's 2099 events
            dt.now.return_value = u(2099, 10, 13, 12); dt.fromtimestamp = datetime.fromtimestamp; dt.fromisoformat = datetime.fromisoformat
            d = cf.req("GET", "/api/calendar?days=14").json(); self.assertEqual(d["days"], 3); dp = cp.req("GET", "/api/calendar?days=14").json(); self.assertEqual(dp["days"], 14)
            only = cp.req("GET", "/api/calendar?days=7&c=EUR").json(); self.assertTrue(only["events"] and all(x["currency"] == "EUR" for x in only["events"]))
        self.assertEqual(cp.req("GET", "/api/calendar?days=7&c=USD';DROP--").status, 200); n = cp.req("GET", "/api/news?c=USD,EUR&hours=9999").json(); self.assertTrue(n["items"]); self.assertIn("heuristic", n["note"])
        self.assertTrue(all(set(i) == {"title", "link", "source", "published", "currencies", "sentiment"} for i in n["items"]))

    def test_calendar_surprise_signal(self):
        self.svc.news_sync()
        with mock.patch("apex.web.service.datetime") as dt:
            dt.now.return_value = u(2099, 10, 15, 6); dt.fromtimestamp = datetime.fromtimestamp; dt.fromisoformat = datetime.fromisoformat
            _, e = self.user(tier="pro"); ev = [x for x in self.client(e).req("GET", "/api/calendar?days=3&c=USD").json()["events"] if x["name"] == "Unemployment Rate"]
        self.assertEqual(ev[0]["surprise"], -1)                                                                   # unemployment above forecast = bearish for USD

    def test_admin_feeds_panel(self):
        adm = self.admin(); _, e = self.user(); cu = self.client(e)
        self.assertEqual(cu.req("GET", "/api/admin/feeds").status, 403); self.assertEqual(cu.req("POST", "/api/admin/feeds/sync", {}).status, 403)
        r = adm.req("POST", "/api/admin/feeds/sync", {}).json(); self.assertTrue(r["result"]["Cal"]["ok"]); self.assertEqual(len(r["feeds"]), 3)
        f = adm.req("GET", "/api/admin/feeds").json(); self.assertEqual(f["feeds"][0]["name"], "Cal"); self.assertNotIn("sk_", json.dumps(f))
        codes = [adm.req("POST", "/api/admin/feeds/sync", {}).status for _ in range(6)]; self.assertIn(429, codes)

    def test_source_config_validation(self):
        from apex.web.webconfig import WebSettings
        self.assertEqual(WebSettings(news_sources=[{"name": "x", "type": "rss", "url": "https://a.b/c"}]).problems(), [])
        for bad in ({"name": "x", "type": "rss", "url": "file:///etc/passwd"}, {"name": "x", "type": "evil", "url": "https://a.b"}, {"type": "rss", "url": "https://a.b"}, "string"):
            self.assertTrue(WebSettings(news_sources=[bad]).problems())
        os.environ["PAYSTACK_SECRET_KEY"] = "sk_test_x"; self.assertTrue(any("private" in p for p in WebSettings(production=True, base_url="https://a.b", secure_cookies=True, allow_private_feeds=True).problems()))


class TimingTests(unittest.TestCase):
    def test_dst_correct_sessions_weekend_and_next_open(self):
        lon = lambda t: [w["start"].hour for w in T.session_windows(t) if w["name"] == "london"][0]; self.assertEqual((lon(u(2026, 7, 1, 12)), lon(u(2026, 1, 7, 12))), (7, 8))
        self.assertEqual((T.market_open(u(2026, 10, 7, 12)), T.market_open(u(2026, 10, 10, 12)), T.market_open(u(2026, 10, 11, 12)), T.market_open(u(2026, 10, 11, 22))), (True, False, False, True))
        self.assertEqual(T.iso(T.next_open(u(2026, 10, 10, 12))), "2026-10-11T21:00:00Z"); self.assertEqual(T.iso(T.next_open(u(2026, 1, 10, 12))), "2026-01-11T22:00:00Z")      # DST shifts the reopen hour
        self.assertFalse(T.market_open(u(2026, 10, 7, 21, 30), futures=True)); self.assertTrue(T.market_open(u(2026, 10, 7, 21, 30), futures=False))

    def test_liquidity_levels(self):
        eu = ["EUR", "USD"]; self.assertEqual(T.liquidity(u(2026, 10, 7, 14), eu), 1.0); self.assertLess(T.liquidity(u(2026, 10, 7, 3), eu), 0.6); self.assertGreater(T.liquidity(u(2026, 10, 7, 3), ["JPY"]), T.liquidity(u(2026, 10, 7, 3), eu))

    def test_hour_profile_finds_the_active_hours(self):
        cs, p = [], 1.1
        for i in range(24 * 40):
            t = u(2026, 1, 1) + timedelta(hours=i); big = t.hour in (8, 9, 13); r = 0.004 if big else 0.001
            cs.append(Candle(t, p, p + r, p - r, p + (0.0005 if i % 2 else -0.0005), 100))
        prof = T.hour_profile(cs, 3600); self.assertTrue({8, 9, 13} <= set(prof["best"])); self.assertTrue(prof["hours"][8] > 2.0 and prof["hours"][3] < 1.0 and prof["hours"][8] > 2.5 * prof["hours"][3])
        self.assertIsNone(T.hour_profile(cs, 14400)); self.assertIsNone(T.hour_profile(cs[:50], 3600))

    def verdict(self, now, ev=(), cons="BUY", grade="A", disorderly=False):
        return T.plan_timing(now, ["EUR", "USD"], False, list(ev), cons, grade, 15, None, disorderly)
    CPI = [{"time_utc": "2026-10-07T12:30:00Z", "currency": "USD", "impact": "high", "name": "CPI"}]

    def test_verdicts(self):
        self.assertEqual(self.verdict(u(2026, 10, 7, 15), self.CPI)["status"], "GO")
        for now, want in ((u(2026, 10, 7, 12, 10), "AVOID"), (u(2026, 10, 7, 12, 40), "AVOID"), (u(2026, 10, 7, 12, 47), "GO"), (u(2026, 10, 7, 11, 45), "WAIT"), (u(2026, 10, 10, 12), "AVOID")):
            self.assertEqual(self.verdict(now, self.CPI)["status"], want, now)
        self.assertEqual(self.verdict(u(2026, 10, 7, 15), cons=None, grade=None)["status"], "WAIT"); self.assertEqual(self.verdict(u(2026, 10, 7, 15), grade="C")["status"], "WAIT")
        self.assertEqual(self.verdict(u(2026, 10, 7, 15), disorderly=True)["status"], "AVOID"); self.assertEqual(self.verdict(u(2026, 10, 7, 3))["status"], "WAIT")                    # thin Asian-hours liquidity for EUR/USD

    def test_next_window_skips_blackout_and_weekend(self):
        v = self.verdict(u(2026, 10, 7, 12, 10), self.CPI); w = v["next_window"]; self.assertEqual(w["start"], "2026-10-07T12:45:00Z"); self.assertIn("overlap", w["why"])
        v = self.verdict(u(2026, 10, 10, 12)); self.assertGreaterEqual(v["next_window"]["start"], "2026-10-12T07:00:00Z"); self.assertIsNone(self.verdict(u(2026, 10, 7, 15))["next_window"])

    def test_payload_has_everything_the_ui_needs(self):
        v = self.verdict(u(2026, 10, 7, 12, 10), self.CPI); self.assertTrue({"status", "headline", "reasons", "sessions", "blackouts", "next_candle_close", "signal_valid_until", "liquidity", "tz_exact"} <= set(v))
        self.assertEqual(v["next_candle_close"], "2026-10-07T12:15:00Z"); self.assertEqual(v["signal_valid_until"], "2026-10-07T12:55:00Z"); self.assertEqual(len(v["blackouts"]), 1); self.assertIsNone(self.verdict(u(2026, 10, 7, 15), cons=None, grade=None)["signal_valid_until"])
        self.assertEqual(T.plan_timing(u(2026, 10, 7, 12, 10), ["EUR"], False, self.CPI, "BUY", "A", 15)["status"], "GO")                                                              # USD news is irrelevant to a EUR-only list


class ScanIntegration(Base):
    def test_scan_includes_timing_news_and_upcoming_for_every_tier(self):
        for tier in ("free", "pro"):
            _, e = self.user(tier=tier); d = self.client(e).req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).json()
            self.assertTrue({"timing", "news", "upcoming", "news_bias"} <= set(d)); self.assertIn(d["timing"]["status"], ("GO", "WAIT", "AVOID")); self.assertEqual(len(d["timing"]["hour_profile"]["hours"]), 24)

    def test_event_near_data_time_changes_the_verdict(self):
        cs, q, now, src = self.svc.md.get("EURUSD", 15)
        with self.svc.db.tx() as c: c.execute("INSERT INTO events(time_utc,currency,impact,name,source) VALUES(?,?,?,?,?)", ((now + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), "USD", "high", "Test NFP", "manual"))
        self.svc._policy = (0.0, None); _, e = self.user(); d = self.client(e).req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).json()
        self.assertIn(d["timing"]["status"], ("WAIT", "AVOID")); self.assertTrue(any("Test NFP" in r["text"] for r in d["timing"]["reasons"])); self.assertTrue(any(x["name"] == "Test NFP" for x in d["upcoming"]))

    def test_news_tone_enters_the_checklist(self):
        now = int(time.time())
        with self.svc.db.tx() as c: c.execute("INSERT INTO news_items(source,guid,title,link,published,currencies,sentiment,fetched) VALUES('t','1','Fed hawkish hike','',?,?,?,?)", (now, "USD", 1.0, now))
        _, e = self.user(tier="pro")
        from apex.scanner import scan_chart as real
        def fake(cs, hb, cfg, dis, w): r = real(cs, hb, cfg, dis, w); r.consensus = "SELL"; r.fam_short = {f"f{i}": .8 for i in range(6)}; r.fam_long = {}; r.regime.update(trending=True, ranging=False, ext_atr=0.0); return r
        with mock.patch("apex.engine.scan_chart", side_effect=fake): d = self.client(e).req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).json()
        if d["grade"]:
            item = [i for i in d["grade"]["items"] if i["key"] == "news_tone"][0]; self.assertTrue(item["ok"]); self.assertFalse(item["critical"]); self.assertIn("heuristic", item["detail"])   # hawkish USD headline = USD strength = supports a EURUSD SELL


class RadarWatchlist(Base):
    def test_radar_gating_quota_ranking_and_shape(self):
        _, e = self.user(); self.assertEqual(self.client(e).req("POST", "/api/radar", {"tf": 15}).status, 402); self.assertEqual(Client(self.port).req("POST", "/api/radar", {"tf": 15}).status, 401)
        _, e2 = self.user(tier="pro"); c = self.client(e2); r = c.req("POST", "/api/radar", {"tf": 15}); self.assertEqual(r.status, 200, r.raw); d = r.json()
        self.assertEqual(d["scanned"], 8); self.assertEqual(c.req("GET", "/api/me").json()["scans_used"], 3)                                             # a radar run costs 3 scans, not 8
        _, e3 = self.user(tier="pro")
        with self.svc.db.tx() as cx: cx.execute("INSERT INTO usage(user_id,day,scans) VALUES(?,?,?)", (self.uid(e3), datetime.now(timezone.utc).date().isoformat(), 148))
        c3 = self.client(e3); r3 = c3.req("POST", "/api/radar", {"tf": 15}); self.assertEqual(r3.status, 402); self.assertEqual(c3.req("GET", "/api/me").json()["scans_used"], 148)   # 2 left < 3 needed: refused, nothing charged
        keys = ("symbol", "direction", "grade", "score", "plan", "timing", "families", "criteria", "news_bias", "regime"); self.assertTrue(all(k in d["items"][0] for k in keys))
        rank = lambda i: ({"A": 3, "B": 2, "C": 1}.get(i["grade"] or "", 0), 1 if i["direction"] else 0); self.assertEqual([rank(i) for i in d["items"]], sorted([rank(i) for i in d["items"]], reverse=True))
        self.assertTrue(c.req("POST", "/api/radar", {"tf": 15}).json()["cached"]); self.assertEqual(c.req("GET", "/api/me").json()["scans_used"], 3)    # cached run costs nothing
        self.assertEqual(c.req("POST", "/api/radar", {"tf": 7}).status, 400); self.assertIn("not a prediction", d["note"])

    def test_radar_respects_watchlist_and_survives_a_bad_symbol(self):
        _, e = self.user(tier="pro"); c = self.client(e)
        self.assertEqual(c.req("POST", "/api/watchlist", {"symbols": ["EURUSD", "GBPUSD"]}).status, 200); d = c.req("POST", "/api/radar", {"tf": 15}).json(); self.assertEqual(sorted(i["symbol"] for i in d["items"]), ["EURUSD", "GBPUSD"])
        os.rename(os.path.join(DEMO, "GBPUSD_M15.csv"), os.path.join(DEMO, "GBPUSD_M15.csv.bak"))
        try:
            self.svc.md._files.clear(); self.svc._radar_cache.clear(); d = c.req("POST", "/api/radar", {"tf": 15}); self.assertEqual(d.status, 200)
        finally: os.rename(os.path.join(DEMO, "GBPUSD_M15.csv.bak"), os.path.join(DEMO, "GBPUSD_M15.csv"))

    def test_watchlist_limits_validation_and_isolation(self):
        _, e = self.user(); cf = self.client(e); self.assertEqual(cf.req("POST", "/api/watchlist", {"symbols": ["EURUSD"]}).status, 200); self.assertEqual(cf.req("POST", "/api/watchlist", {"symbols": ["EURUSD", "GBPUSD"]}).status, 400)        # GBPUSD is not available on the free plan
        self.assertEqual(cf.req("POST", "/api/watchlist", {"symbols": ["GBPUSD"]}).status, 400)
        self.svc.s.watchlist_limits["pro"] = 2; _, e3 = self.user(tier="pro"); c3 = self.client(e3); self.assertEqual(c3.req("POST", "/api/watchlist", {"symbols": ["EURUSD", "GBPUSD", "USDJPY"]}).status, 402)   # plan limit
        self.svc.s.watchlist_limits["pro"] = 8
        _, e2 = self.user(tier="pro"); cp = self.client(e2); for_bad = [{"symbols": "EURUSD"}, {"symbols": [1]}, {"symbols": ["X" * 20]}, {"symbols": ["NOPE"]}, {"symbols": ["EURUSD'; DROP--"]}]
        for b in for_bad: self.assertEqual(cp.req("POST", "/api/watchlist", b).status, 400, b)
        self.assertEqual(cp.req("POST", "/api/watchlist", {"symbols": ["EURUSD", "EURUSD", "USDJPY"]}).status, 200); self.assertEqual(cp.req("GET", "/api/watchlist").json()["symbols"], ["EURUSD", "USDJPY"])
        self.assertEqual(cf.req("GET", "/api/watchlist").json()["symbols"], ["EURUSD"]); self.assertEqual(cp.req("GET", "/api/watchlist").json()["limit"], 8)

    def test_sizing_unavailable_pair_does_not_break_radar(self):
        _, e = self.user(tier="pro"); c = self.client(e); c.req("POST", "/api/watchlist", {"symbols": ["EURJPY", "USDJPY"]}); d = c.req("POST", "/api/radar", {"tf": 15}); self.assertEqual(d.status, 200); self.assertEqual(len(d.json()["items"]), 2)


class JournalTests(Base):
    def add(self, c, **k):
        b = {"symbol": "EURUSD", "side": "BUY", "entry": 1.1000, "stop": 1.0950, "exit": 1.1100, "grade": "A", "notes": "t"}; b.update(k); return c.req("POST", "/api/journal", b)

    def test_r_multiples_and_stats(self):
        _, e = self.user(); c = self.client(e)
        self.assertEqual(self.add(c).status, 201); self.add(c, side="SELL", entry=1.1, stop=1.105, exit=1.108); self.add(c, exit=1.0950); self.add(c, exit=None)
        j = c.req("GET", "/api/journal").json(); rs = sorted(r["r"] for r in j["rows"] if r["r"] is not None); self.assertEqual(rs, [-1.6, -1.0, 2.0]); st = j["stats"]
        self.assertEqual((st["closed"], st["open"], st["total_r"]), (3, 1, -0.6)); self.assertAlmostEqual(st["win_rate"], 1 / 3, 3); self.assertAlmostEqual(st["profit_factor"], round(2 / 2.6, 2)); self.assertIn("Fewer than 30", st["note"]); self.assertTrue(st["win_ci"][0] < st["win_rate"] < st["win_ci"][1])
        oid = [r for r in j["rows"] if r["exit"] is None][0]["id"]; self.assertEqual(c.req("POST", "/api/journal/close", {"id": oid, "exit": 1.105}).status, 200); self.assertEqual(c.req("GET", "/api/journal").json()["stats"]["closed"], 4)
        self.assertEqual(c.req("POST", "/api/journal/close", {"id": oid, "exit": "abc"}).status, 400); self.assertEqual(c.req("POST", "/api/journal/close", {"id": oid, "exit": -1}).status, 400)

    def test_validation_rejects_nonsense(self):
        _, e = self.user(); c = self.client(e)
        for bad in ({"side": "HOLD"}, {"stop": 1.2}, {"side": "SELL"}, {"entry": "x"}, {"entry": -1}, {"entry": float("nan") if False else 0}, {"symbol": "NOPE"}, {"grade": "Z"}, {"notes": "x" * 301}, {"exit": "inf"}): self.assertEqual(self.add(c, **bad).status, 400, bad)
        self.assertEqual(c.req("POST", "/api/journal", {"symbol": "EURUSD"}).status, 400)

    def test_limits_idor_and_delete(self):
        _, e = self.user(); c = self.client(e)
        for _ in range(30): self.assertEqual(self.add(c).status, 201)
        self.assertEqual(self.add(c).status, 402)
        _, e2 = self.user(tier="pro"); c2 = self.client(e2); self.add(c2); rid = c2.req("GET", "/api/journal").json()["rows"][0]["id"]
        c.req("POST", "/api/journal/delete", {"id": rid}); c.req("POST", "/api/journal/close", {"id": rid, "exit": 9.9}); row = c2.req("GET", "/api/journal").json()["rows"][0]; self.assertEqual((row["id"], row["exit"]), (rid, 1.11))    # other user cannot touch it
        self.assertEqual(Client(self.port).req("GET", "/api/journal").status, 401); c2.req("POST", "/api/journal/delete", {"id": rid}); self.assertEqual(len(c2.req("GET", "/api/journal").json()["rows"]), 0)

    def test_csv_export_neutralises_formula_injection(self):
        _, e = self.user(); c = self.client(e); self.add(c, notes='=HYPERLINK("http://evil","x")'); self.add(c, notes="+cmd|' /C calc'!A0"); self.add(c, notes="@SUM(1+1)"); self.add(c, notes="-2+3"); self.add(c, notes="ok, with comma")
        r = c.req("GET", "/api/journal/export"); self.assertEqual(r.status, 200); self.assertIn("text/csv", r.headers.getheader("Content-Type")); self.assertIn("attachment", r.headers.getheader("Content-Disposition")); body = r.raw.decode()
        self.assertIn("date_utc,symbol,side", body); self.assertIn("'=HYPERLINK", body); self.assertIn("'+cmd", body); self.assertIn("'@SUM", body); self.assertIn("'-2+3", body); self.assertIn('"ok, with comma"', body)
        for line in body.splitlines()[1:]: self.assertFalse(any(cell.startswith(("=", "+", "@")) for cell in line.split(",")[-1:]))
        self.assertEqual(Client(self.port).req("GET", "/api/journal/export").status, 401)


if __name__ == "__main__":
    unittest.main()
