import json, os, tempfile, time, unittest
from datetime import datetime, timedelta, timezone
from apex import scanner as sc, scanner_ext as ext
from apex.config import RiskConfig, ExecConfig, Mode
from apex.data import Candle, Quote, load_csv
from tests.synth import synthetic
from apex.edge import wilson, bootstrap_mean_ci, weights_from, evidence_for_consensus, walk_forward, evaluate_methods
from apex.engine import analyze
from apex.execution import PaperBroker, OrderRequest
from apex.instruments import get_spec
from apex.risk import RiskState, risk_multiplier
from tests.test_web import Base, Client, PW, DEMO, SECRET
from tests.test_apex import mkcsv

NOW = datetime(2024, 3, 5, 10, tzinfo=timezone.utc)


def path_candles(prices, per_leg=15, pad=100, tail=0, tail_step=0.0008, t0=NOW):
    """Piecewise-linear price path -> candles (used to craft exact swing structures)."""
    pts = [prices[0] + 0.02 * (1 - k / pad) for k in range(pad)]
    for a, b in zip(prices, prices[1:]): pts += [a + (b - a) * (k + 1) / per_leg for k in range(per_leg)]
    pts += [prices[-1] + tail_step * (k + 1) for k in range(tail)]
    cs, prev = [], pts[0]
    for i, p in enumerate(pts):
        cs.append(Candle(t0 + timedelta(minutes=15 * i), prev, max(prev, p) + 1e-5, min(prev, p) - 1e-5, p, 100)); prev = p
    return cs


class Library(unittest.TestCase):
    def test_library_size_and_uniqueness(self):
        names = [m[0] for m in sc.METHODS]
        self.assertGreaterEqual(len(names), 50); self.assertEqual(len(names), len(set(names))); self.assertGreaterEqual(len(sc.FAMILIES), 15)
        self.assertEqual(len(ext.EXT), 22)

    def test_all_methods_robust_on_edge_cases(self):
        flat = [Candle(NOW + timedelta(minutes=15 * i), 1.1, 1.1001, 1.0999, 1.1, 0) for i in range(400)]   # zero volume, tiny range
        r = sc.scan_chart(flat, 0.0); self.assertEqual(r.errors, 0)
        spike = synthetic(500, seed=3); spike[-1] = Candle(spike[-1].ts, spike[-1].o, spike[-1].o * 1.05, spike[-1].o * 0.95, spike[-1].o * 1.04, 500)
        self.assertEqual(sc.scan_chart(spike, 0.0).errors, 0)
        for n in (60, 61, 100, 205): self.assertEqual(sc.scan_chart(synthetic(n, seed=1), 0.0).errors, 0, n)

    def test_wma(self):
        self.assertAlmostEqual(ext.wma([1, 2, 3, 4], 3)[-1], (2 * 1 + 3 * 2 + 4 * 3) / 6 - 0 if False else (1 * 2 + 2 * 3 + 3 * 4) / 6)

    def test_harmonic_gartley_detected(self):
        X, A = 1.10, 1.20; B = A - 0.618 * (A - X); C = B + 0.5 * (A - B); D = A - 0.786 * (A - X)
        cs = path_candles([X, A, B, C, D], per_leg=15, tail=6)
        s = ext.m_harmonic(sc.Ctx(cs)); self.assertEqual((s.direction, s.passed), (1, True), s.detail); self.assertIn("Gartley", s.detail)

    def test_multi_candle_three_soldiers(self):
        cs = synthetic(300, seed=1); b = cs[-1].c; a = 0.001
        cs = cs[:-3] + [Candle(NOW, b, b + 1.2 * a, b - 0.1 * a, b + a, 100), Candle(NOW, b + a, b + 2.4 * a, b + 0.9 * a, b + 2.2 * a, 100), Candle(NOW, b + 2.2 * a, b + 3.6 * a, b + 2.1 * a, b + 3.4 * a, 100)]
        s = ext.m_candles2(sc.Ctx(cs)); self.assertEqual(s.direction, 1); self.assertIn("three white soldiers", s.detail)

    def test_regime_penalty_applies_to_wrong_tool_only(self):
        class X: ranging, trending, adx_v = True, False, 15
        t = sc.Signal("EMA trend stack", "trend", 1, 0.8, "d"); m = sc.Signal("Bollinger reversion", "mean_reversion", 1, 0.8, "d")
        self.assertAlmostEqual(sc.regime_adjust(X, t, 0.75).strength, 0.6); self.assertIn("counter-regime", t.detail); self.assertEqual(sc.regime_adjust(X, m, 0.75).strength, 0.8)
        class Y: ranging, trending, adx_v = False, True, 35
        m2 = sc.Signal("RSI(2) pullback", "mean_reversion", -1, 0.8, "d"); self.assertAlmostEqual(sc.regime_adjust(Y, m2, 0.75).strength, 0.6)
        n = sc.Signal("EMA trend stack", "trend", 0, 0.0, "x"); self.assertEqual(sc.regime_adjust(X, n, 0.75).strength, 0.0)

    def test_consensus_filters_and_weights(self):
        mk = lambda n, f, d, s=0.8: sc.Signal(n, f, d, s, "")
        sigs = [mk(f"m{i}", f"f{i % 5}", 1) for i in range(10)]; cfg = sc.ScanConfig(8, 4)
        self.assertEqual(sc.decide(sigs, cfg).consensus, "BUY")
        self.assertIsNone(sc.decide(sigs, cfg, htf_bias=-1.5).consensus)                   # against higher-timeframe trend
        self.assertEqual(sc.decide(sigs, sc.ScanConfig(8, 4, require_htf_alignment=False), htf_bias=-1.5).consensus, "BUY")
        self.assertIsNone(sc.decide(sigs, cfg, ext=3.5).consensus)                         # chasing an extended move
        self.assertEqual(sc.decide(sigs, cfg, ext=-3.5).consensus, "BUY")                  # extended the OTHER way is fine for a long
        w = {f"m{i}": 0.0 for i in range(10)}; self.assertIsNone(sc.decide(sigs, cfg, w).consensus)    # zero weight removes evidence
        self.assertGreater(sum(sc.decide(sigs, cfg, {"m0": 1.3}).fam_long.values()), sum(sc.decide(sigs, cfg).fam_long.values()))

    def test_regime_metrics_present(self):
        cs = synthetic(800, seed=3); r = sc.scan_chart(cs, 0.0); self.assertTrue({"adx", "chop", "vr", "trending", "ranging", "ext_atr"} <= set(r.regime))
        self.assertTrue(0 <= r.regime["chop"] <= 100)


class Evidence(unittest.TestCase):
    def test_wilson(self):
        self.assertEqual(wilson(0, 0), (0.0, 1.0)); lo, hi = wilson(50, 100); self.assertAlmostEqual(lo, 0.404, 2); self.assertAlmostEqual(hi, 0.596, 2)
        lo2, hi2 = wilson(500, 1000); self.assertLess(hi2 - lo2, hi - lo)                   # more data -> tighter interval

    def test_bootstrap_ci(self):
        lo, hi = bootstrap_mean_ci([0.5] * 5 + [0.4, 0.6, 0.5, 0.45, 0.55] * 6); self.assertGreater(lo, 0)
        lo, hi = bootstrap_mean_ci([1, -1] * 25); self.assertLess(lo, 0); self.assertGreater(hi, 0)
        self.assertEqual(bootstrap_mean_ci([1, 2])[0], float("-inf"))

    def test_weights_and_evidence(self):
        st = {"A": {"out_of_sample": {"n": 200, "mean_R": 0.3, "t": 3}, "verdict": "EDGE-SUPPORTED"}, "B": {"out_of_sample": {"n": 20, "mean_R": 0.9, "t": 3}, "verdict": "INSUFFICIENT DATA"},
              "C": {"out_of_sample": {"n": 300, "mean_R": 5.0, "t": 9}, "verdict": "EDGE-SUPPORTED"}, "CONSENSUS": {"out_of_sample": {"n": 150, "mean_R": 0.12, "hit": 0.55, "hit_ci": [0.47, 0.63], "t": 1.5}, "verdict": "NO SIGNIFICANT EDGE"}}
        w = weights_from(st); self.assertNotIn("B", w); self.assertAlmostEqual(w["A"], 1.3 if 1 + 0.3 / 0.5 > 1.3 else 1 + 0.3 / 0.5); self.assertLessEqual(w["C"], 1.3)
        e = evidence_for_consensus(st); self.assertEqual((e["n"], e["hit_ci"]), (150, [0.47, 0.63])); self.assertIsNone(evidence_for_consensus({}))

    def test_walk_forward_structure_and_honesty(self):
        r = walk_forward(synthetic(1700, seed=2), 0.0001, train=700, test=300, step=12)
        self.assertTrue({"verdict", "oos_trades", "mean_R_ci95", "hit_ci95", "folds", "positive_folds", "note"} <= set(r)); self.assertGreaterEqual(len(r["folds"]), 2)
        self.assertEqual(r["verdict"], "INSUFFICIENT DATA")                              # a few dozen trades can never be called robust
        self.assertEqual(walk_forward(synthetic(500, seed=1), 0.0001)["folds"], [])

    def test_train_test_never_overlap(self):
        r = walk_forward(synthetic(1800, seed=4), 0.0001, train=700, test=300, step=12)
        for f in r["folds"]: self.assertEqual(f["train"][1], f["test"][0]); self.assertLess(f["train"][0], f["train"][1])

    def test_evaluate_methods_calibration_buckets(self):
        r = evaluate_methods(synthetic(2200, seed=5), 0.0001, step=6); self.assertIn("CONSENSUS", r)
        if "by_strength" in r["CONSENSUS"]: self.assertEqual(set(r["CONSENSUS"]["by_strength"]), {"low", "mid", "high"})


class RiskAndManagement(unittest.TestCase):
    def test_throttle_only_reduces(self):
        cfg = RiskConfig(); st = RiskState.fresh(10000); self.assertEqual(risk_multiplier(cfg, st), (1.0, ""))
        st.consecutive_losses = 2; self.assertEqual(risk_multiplier(cfg, st)[0], 0.5); st.consecutive_losses = 5; self.assertEqual(risk_multiplier(cfg, st)[0], 0.25)
        st = RiskState.fresh(10000); st.peak_equity = 10500; st.equity = 10000; self.assertEqual(risk_multiplier(cfg, st)[0], 0.5)    # 4.8% DD >= half of 8%
        for n in range(0, 10):
            st = RiskState.fresh(10000); st.consecutive_losses = n; self.assertLessEqual(risk_multiplier(cfg, st)[0], 1.0)

    def test_engine_sizes_smaller_when_throttled(self):
        d, p = mkcsv(1); cs = load_csv(p)[:800]; q = Quote(cs[-1].c - 5e-5, cs[-1].c + 5e-5, cs[-1].ts, "simulated-x"); ec = ExecConfig(mode=Mode.ANALYSIS_ONLY, allowed_sessions_utc=[(0, 24)], allowed_instruments=["EURUSD"])
        a = analyze("EURUSD", cs, 900, RiskConfig(), ec, RiskState.fresh(10000), q, cs[-1].ts, allow_simulated_quote=True, enforce_session=False)
        st = RiskState.fresh(10000); st.consecutive_losses = 2
        b = analyze("EURUSD", cs, 900, RiskConfig(), ec, st, q, cs[-1].ts, allow_simulated_quote=True, enforce_session=False)
        self.assertIsNotNone(a.size); self.assertLess(b.size.size, a.size.size); self.assertTrue(any("throttled" in e for e in b.evidence_against))

    def test_partial_at_tp1_then_breakeven(self):
        spec = get_spec("EURUSD"); b = PaperBroker(spec, 10000); q = Quote(1.0999, 1.1001, NOW, "t")
        self.assertEqual(b.submit(OrderRequest("m1", "EURUSD", "BUY", "market", 0.10, 1.095, 1.11, None, 50, 1.103, 0.5), q).status, "FILLED")
        b.on_bar(Candle(NOW, 1.1, 1.1035, 1.0995, 1.103), 0.0001); pos = list(b.open.values())[0]
        self.assertEqual((pos["size"], pos["sl"], pos["be"]), (0.05, pos["entry"], True)); self.assertGreater(b.equity, 10000)
        b.on_bar(Candle(NOW, 1.103, 1.104, 1.0999, 1.1), 0.0001); t = b.closed[0]
        self.assertEqual(t["reason"], "BE"); self.assertGreaterEqual(t["pnl"], 0); self.assertGreater(t["r"], 0)       # protected profit, never a loss

    def test_full_target_after_partial(self):
        spec = get_spec("EURUSD"); b = PaperBroker(spec, 10000); q = Quote(1.0999, 1.1001, NOW, "t")
        b.submit(OrderRequest("m2", "EURUSD", "BUY", "market", 0.10, 1.095, 1.108, None, 50, 1.103, 0.5), q)
        b.on_bar(Candle(NOW, 1.1, 1.1035, 1.0995, 1.103), 0.0001); b.on_bar(Candle(NOW, 1.103, 1.109, 1.102, 1.108), 0.0001)
        t = b.closed[0]; self.assertEqual(t["reason"], "TP"); self.assertGreater(t["r"], 0.5)

    def test_backtest_reports_ci_and_cannot_validate_small_sample(self):
        from apex.backtest import run_backtest
        r = run_backtest("EURUSD", synthetic(1100, seed=3), 900, RiskConfig(), ExecConfig(allowed_sessions_utc=[(0, 24)]), slippage=2e-5, step=6, enforce_session=False)
        self.assertIn("oos_expectancy_ci95", r); self.assertEqual(r["status"], "UNVALIDATED"); self.assertIn("breakeven", r["assumptions"])


class WebV4(Base):
    def test_scan_payload_has_chart_and_evidence_rules(self):
        _, e = self.user(); c = self.client(e); d = c.req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).json()
        ch = d["chart"]; self.assertEqual(len(ch["t"]), 120); self.assertTrue(all(len(ch[k]) == 120 for k in ("o", "h", "l", "c", "e21", "e50"))); self.assertIsNone(d["evidence"]); self.assertEqual(d["tf"], 15)
        self.assertIn("regime_detail", d); self.assertIsNone(d["plan"])
        _, e2 = self.user(tier="pro")
        with open(self.s.edge_path, "w") as f: json.dump({"CONSENSUS": {"in_sample": {"n": 1, "mean_R": 0, "hit": 0, "t": 0, "hit_ci": [0, 1]}, "out_of_sample": {"n": 140, "mean_R": 0.1, "hit": 0.54, "hit_ci": [0.46, 0.62], "t": 1.2}, "verdict": "NO SIGNIFICANT EDGE"}}, f)
        d2 = self.client(e2).req("POST", "/api/scan", {"symbol": "EURUSD", "tf": 15}).json(); self.assertEqual(d2["evidence"]["n"], 140); self.assertEqual(d2["evidence"]["verdict"], "NO SIGNIFICANT EDGE")
        self.assertIsNone(d["evidence"])    # free tier never gets the evidence panel

    def test_validate_endpoint_gating_and_elite_run(self):
        _, e = self.user(); self.assertEqual(self.client(e).req("POST", "/api/validate", {"symbol": "EURUSD", "tf": 15}).status, 402)
        _, e2 = self.user(tier="pro"); self.assertEqual(self.client(e2).req("POST", "/api/validate", {"symbol": "EURUSD", "tf": 15}).status, 402)
        _, e3 = self.user(tier="elite"); c = self.client(e3); r = c.req("POST", "/api/validate", {"symbol": "EURUSD", "tf": 15})
        self.assertEqual(r.status, 200, r.raw); j = r.json(); self.assertIn(j["verdict"], ("INSUFFICIENT DATA", "NO EDGE FOUND", "WEAK / UNPROVEN EDGE", "ROBUST EDGE")); self.assertTrue(j["folds"])
        self.assertEqual(c.req("POST", "/api/validate", {"symbol": "EURUSD", "tf": 7}).status, 400)

    def test_admin_stats(self):
        self.user(tier="pro"); self.user(tier="elite"); self.user(); adm = self.admin(); s = adm.req("GET", "/api/admin/stats").json()
        self.assertEqual((s["users"], s["tiers"]["pro"], s["tiers"]["elite"], s["tiers"]["free"], s["tiers"]["admin"]), (4, 1, 1, 1, 1))
        _, e = self.user(); self.assertEqual(self.client(e).req("GET", "/api/admin/stats").status, 403); self.assertEqual(Client(self.port).req("GET", "/api/admin/stats").status, 401)

    def test_static_assets_fonts_and_traversal(self):
        c = Client(self.port); r = c.req("GET", "/static/fonts/inter-latin-wght-normal.woff2")
        self.assertEqual(r.status, 200); self.assertEqual(r.headers.getheader("Content-Type"), "font/woff2"); self.assertIn("immutable", r.headers.getheader("Cache-Control")); self.assertEqual(len(r.headers.msg.get_all("Cache-Control")), 1)
        self.assertGreater(len(r.raw), 20000); self.assertTrue(r.raw[:4] == b"wOF2")
        self.assertEqual(c.req("GET", "/static/fonts/jetbrains-mono-latin-wght-normal.woff2").status, 200); self.assertEqual(c.req("GET", "/static/favicon.svg").headers.getheader("Content-Type"), "image/svg+xml")
        for bad in ("/static/../apex/web/server.py", "/static/..%2f..%2fetc/passwd", "/static/fonts/", "/static/index.html", "/static/%2e%2e/service.py"): self.assertEqual(c.req("GET", bad).status, 404, bad)
        self.assertIn("font-src 'self'", c.req("GET", "/").headers.getheader("Content-Security-Policy"))

    def test_design_system_has_fonts_themes_and_animations(self):
        d = os.path.join(os.path.dirname(__file__), "..", "apex", "web", "static"); css = open(os.path.join(d, "app.css")).read(); js = open(os.path.join(d, "app.js")).read()
        self.assertIn('font-family:"Inter"', css); self.assertIn("JetBrains Mono", css); self.assertIn("[data-theme=light]", css)
        self.assertGreaterEqual(css.count("@keyframes"), 8); self.assertIn("prefers-reduced-motion", css); self.assertIn("focus-visible", css)
        self.assertIn("matchMedia(\"(prefers-reduced-motion: reduce)\")", js); self.assertIn("requestAnimationFrame", js)
        for bad in ("innerHTML", "eval(", "document.write", "outerHTML", "insertAdjacentHTML", "style:"): self.assertNotIn(bad, js)


if __name__ == "__main__":
    unittest.main()
