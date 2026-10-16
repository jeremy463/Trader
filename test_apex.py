import json, os, tempfile, threading, unittest, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from apex.config import *
from apex.data import *
from tests.synth import synthetic
from apex.instruments import get_spec
from apex import indicators as ind
from apex.risk import *
from apex.engine import analyze
from apex.execution import *
from apex.journal import Journal
from apex.scoring import WEIGHTS
from apex import scanner as sc
from apex.settings import load_settings, Settings
from apex.app import App
from apex.edge import evaluate_methods

NOW = datetime(2024, 3, 5, 10, 0, tzinfo=timezone.utc)
SESSIONS_ALL = [(0, 24)]


def mkcsv(seed, n=2200):
    d = tempfile.mkdtemp(); p = os.path.join(d, "d.csv")
    with open(p, "w") as f:
        f.write("time,open,high,low,close,volume\n")
        for c in synthetic(n, seed=seed): f.write(f"{c.ts.isoformat()},{c.o},{c.h},{c.l},{c.c},{c.v}\n")
    return d, p


def mkapp(seed=1, cursor=800, **over):
    d, p = mkcsv(seed)
    s = Settings(); s.feed.path = p
    s.exec = ExecConfig(mode=Mode.APPROVAL_REQUIRED, allowed_sessions_utc=SESSIONS_ALL, allowed_instruments=["EURUSD"])
    s.server.state_path, s.server.journal_path, s.server.log_path = (os.path.join(d, x) for x in ("st.json", "j.jsonl", "l.log"))
    for k, v in over.items(): setattr(s.risk, k, v)
    app = App(s); app.feed.cursor = cursor
    return app


class Sizing(unittest.TestCase):
    def test_forex(self):
        r = size_position(get_spec("EURUSD"), RiskConfig(equity=10000, risk_per_trade_pct=0.5), 10000, 1.1, 1.095)
        self.assertAlmostEqual(r.size, 0.10, 2); self.assertLessEqual(r.risk_amount, 50.0001)
    def test_futures_ticks(self):
        r = size_position(get_spec("MES"), RiskConfig(equity=10000, risk_per_trade_pct=0.5), 10000, 5000.0, 4990.0)
        self.assertEqual(r.size, 1.0); self.assertAlmostEqual(r.stop_ticks, 40)
    def test_min_size_rejected(self):
        self.assertIsNotNone(size_position(get_spec("ES"), RiskConfig(equity=5000), 5000, 5000, 4980).rejected)
    def test_never_exceeds_budget_fuzz(self):
        import random; rnd = random.Random(3)
        for _ in range(300):
            eq = rnd.uniform(1000, 200000); stop = rnd.uniform(0.0003, 0.02); cfg = RiskConfig(equity=eq, risk_per_trade_pct=rnd.uniform(0.05, 1.0))
            r = size_position(get_spec("EURUSD"), cfg, eq, 1.1, 1.1 - stop, 1.0, rnd.uniform(0, 8), rnd.uniform(0, 0.0001))
            if not r.rejected: self.assertLessEqual(r.risk_amount, r.risk_budget + 1e-9)


class Data(unittest.TestCase):
    def test_validation(self):
        cs = synthetic(300)
        self.assertTrue(validate_candles(cs, 900).ok)
        self.assertFalse(validate_candles(cs[:100] + [cs[100]] + cs[100:], 900).ok)
        self.assertFalse(validate_candles(cs, 900, now=cs[-1].ts + timedelta(hours=5)).ok)
        self.assertFalse(validate_candles(cs[:50], 900).ok)
    def test_indicators(self):
        cs = synthetic(400)
        self.assertGreater(ind.last(ind.atr(cs)), 0); self.assertTrue(0 <= ind.last(ind.rsi([c.c for c in cs])) <= 100)
        self.assertAlmostEqual(ind.sma([1, 2, 3, 4], 2)[-1], 3.5)


class Scanner(unittest.TestCase):
    def test_many_methods_many_families_no_errors(self):
        self.assertGreaterEqual(sc.METHOD_COUNT, 50); self.assertGreaterEqual(len(sc.FAMILIES), 15)
        for seed in range(1, 25):
            cs = synthetic(1200, seed=seed); r = sc.scan_chart(cs, sc.compute_htf_bias(cs)[0])
            self.assertEqual(r.errors, 0); self.assertEqual(r.n_total, sc.METHOD_COUNT)
            self.assertEqual(r.long_passed + r.short_passed + r.neutral + r.not_applicable + r.errors, r.n_total)
    def test_candle_patterns(self):
        C = lambda o, h, l, c: Candle(NOW, o, h, l, c)
        base = [C(1, 1.1, 0.9, 1)] * 3
        self.assertEqual(sc.detect_candle(base[:1] * 1 + [C(1.10, 1.11, 1.0, 1.01), C(1.0, 1.15, 0.99, 1.12)])[1], "bullish engulfing")
        self.assertEqual(sc.detect_candle([C(1, 1.1, 0.9, 1), C(1.1, 1.11, 1.0, 1.05), C(1.05, 1.0555, 0.80, 1.055)])[1], "hammer/bullish pin bar")
        self.assertEqual(sc.detect_candle([C(1.3, 1.31, 1.09, 1.1), C(1.08, 1.09, 1.07, 1.075), C(1.09, 1.25, 1.08, 1.24)])[1], "morning star")
        self.assertEqual(sc.detect_candle([C(1, 1.3, 0.9, 1.0), C(1.0, 1.2, 1.0, 1.1), C(1.1, 1.4, 1.1, 1.35)])[1], "inside-bar breakout up")
    def test_donchian_breakout(self):
        cs = synthetic(300, seed=1); last = cs[-1]
        hi = max(c.h for c in cs[-21:-1])
        cs = cs[:-1] + [Candle(last.ts, last.o, hi + 0.003, last.l, hi + 0.002, last.v)]
        s = sc.m_donchian(sc.Ctx(cs)); self.assertEqual(s.direction, 1); self.assertTrue(s.passed)
    def test_session_breakout(self):
        t0 = datetime(2024, 3, 5, 0, 0, tzinfo=timezone.utc)
        pre = [Candle(t0 - timedelta(minutes=15 * (300 - k)), 1.1, 1.1003, 1.0997, 1.1, 100) for k in range(300)]
        cs = []
        for i in range(10 * 4 + 1):                       # 00:00 .. 10:00, flat Asian range then quiet London open
            p = 1.1000 + 0.0002 * ((i % 4) - 2) if i < 28 else 1.1
            cs.append(Candle(t0 + timedelta(minutes=15 * i), p, p + 0.0003, p - 0.0003, p, 100))
        cs.append(Candle(cs[-1].ts + timedelta(minutes=15), 1.1, 1.1030, 1.1, 1.1028, 300))   # 10:15 break
        s = sc.m_session(sc.Ctx(pre + cs)); self.assertEqual(s.direction, 1, s.detail)
    def test_consensus_counts_independent_families(self):
        mk = lambda n, f, d, s=0.8: sc.Signal(n, f, d, s, "")
        trend5 = [mk(f"t{i}", "trend", 1) for i in range(5)] + [mk("m1", "momentum", 1), mk("m2", "momentum", 1), mk("b", "breakout", 1)]
        r = sc.decide(trend5, sc.ScanConfig(min_methods=8, min_families=4))
        self.assertIsNone(r.consensus); self.assertEqual(len(r.fam_long), 3)         # 8 methods but only 3 families
        more = trend5 + [mk("s", "structure", 1), mk("z", "zones", 1)]
        self.assertEqual(sc.decide(more, sc.ScanConfig(8, 4)).consensus, "BUY")
        opp = more + [mk(f"x{i}", f"f{i}", -1) for i in range(3)]
        self.assertIsNone(sc.decide(opp, sc.ScanConfig(8, 4)).consensus)               # too many opposing families
    def test_failing_method_is_isolated(self):
        old = sc.METHODS[0]
        sc.METHODS[0] = ("EMA trend stack", "trend", lambda x: 1 / 0)
        try:
            r = sc.scan_chart(synthetic(400), 0.0); self.assertEqual(r.errors, 1)
        finally: sc.METHODS[0] = old


class Engine(unittest.TestCase):
    def test_no_executable_signal_without_live_quote(self):
        for seed in (1, 2, 4):
            a = analyze("EURUSD", synthetic(1500, seed=seed), 900, enforce_session=False)
            self.assertNotIn(a.decision, ("BUY", "SELL")); self.assertIsNone(a.prob.value)
    def test_simulated_quote_blocked_unless_paper(self):
        cs = synthetic(1000, seed=1)[:800]; q = Quote(cs[-1].c - 5e-5, cs[-1].c + 5e-5, cs[-1].ts, "simulated-x")
        a = analyze("EURUSD", cs, 900, quote=q, now=cs[-1].ts, enforce_session=False)
        self.assertNotIn(a.decision, ("BUY", "SELL"))
        b = analyze("EURUSD", cs, 900, quote=q, now=cs[-1].ts, enforce_session=False, allow_simulated_quote=True)
        self.assertEqual(b.decision, "SELL")
    def test_insufficient_data(self): self.assertEqual(analyze("EURUSD", synthetic(80), 900).classification, "DATA INSUFFICIENT")
    def test_weights(self): self.assertAlmostEqual(sum(WEIGHTS.values()), 1.0)


class Risk(unittest.TestCase):
    def setUp(self): self.s = get_spec("EURUSD"); self.cfg = RiskConfig(); self.sz = size_position(self.s, self.cfg, 10000, 1.1, 1.095)
    def gate(self, st=None, spread=0.00005, pol=None, cfg=None, spec=None, sz=None):
        return risk_gate(spec or self.s, "BUY", sz or self.sz, cfg or self.cfg, st or RiskState.fresh(10000), NOW, spread, 0.0006, pol, [(7, 20)])
    def test_ok(self): self.assertTrue(self.gate().approved)
    def test_vetoes(self):
        st = RiskState.fresh(10000); st.equity = 9850; self.assertFalse(self.gate(st).approved)
        st = RiskState.fresh(10000); st.kill_switch = True; self.assertFalse(self.gate(st).approved)
        self.assertFalse(self.gate(spread=0.0005).approved)
        self.assertFalse(self.gate(pol=EventPolicy(events=[("2024-03-05T10:10:00Z", "USD", "NFP")])).approved)
        st = RiskState.fresh(10000); st.consecutive_losses = 4; self.assertFalse(self.gate(st).approved)
        st = RiskState.fresh(10000); st.peak_equity = 11000; self.assertFalse(self.gate(st).approved)
    def test_futures_continuous_blocked(self):
        s = get_spec("MES"); sz = size_position(s, RiskConfig(equity=50000), 50000, 5000, 4990)
        self.assertFalse(risk_gate(s, "BUY", sz, RiskConfig(equity=50000), RiskState.fresh(50000), NOW, 0.25, 5.0).approved)
    def test_correlated_exposure(self):
        st = RiskState.fresh(10000); st.positions.append(OpenPosition("GBPUSD", "BUY", .1, 1.27, 1.26, 10, "GBP", "USD", 12700))
        cfg = RiskConfig(max_total_open_risk_pct=2.0)
        self.assertTrue(any("USD" in r for r in risk_gate(self.s, "BUY", self.sz, cfg, st, NOW, 5e-5, 6e-4).reasons))
    def test_roll_and_persist(self):
        st = RiskState.fresh(10000); st.roll(NOW); st.equity = 9900; st.roll(NOW + timedelta(days=1))
        self.assertEqual(st.day_start_equity, 9900)
        p = os.path.join(tempfile.mkdtemp(), "s.json"); st.kill_switch = True; st.order_times.append(NOW); st.save(p)
        l = RiskState.load(p, 1); self.assertTrue(l.kill_switch); self.assertEqual(l.equity, 9900); self.assertEqual(l.order_times[0], NOW)


class Exec(unittest.TestCase):
    def mk(self, mode=Mode.APPROVAL_REQUIRED, approve=lambda r: True, auth=None):
        spec = get_spec("EURUSD"); br = PaperBroker(spec, 10000); st = RiskState.fresh(10000)
        ec = ExecConfig(mode=mode, allowed_sessions_utc=SESSIONS_ALL, allowed_instruments=["EURUSD"])
        return br, st, ExecutionManager(br, ec, RiskConfig(), st, approve=approve, auth=auth), spec
    def req(self): return OrderRequest("c1", "EURUSD", "BUY", "market", 0.1, 1.095, 1.11, None, 50)
    def sz(self, spec): return size_position(spec, RiskConfig(), 10000, 1.1, 1.095)
    Q = Quote(1.0999, 1.1001, NOW, "t")
    def test_approval_required(self):
        br, st, m, spec = self.mk(approve=lambda r: False)
        self.assertEqual(m.execute_request(self.req(), self.sz(spec), spec, self.Q, 1.5e-3, NOW).status, "REJECTED"); self.assertEqual(len(br.open), 0)
    def test_approved_fills_and_reconciles(self):
        br, st, m, spec = self.mk()
        r = m.execute_request(self.req(), self.sz(spec), spec, self.Q, 1.5e-3, NOW)
        self.assertEqual(r.status, "FILLED"); self.assertEqual(len(st.positions), 1); self.assertEqual(len(br.open), 1)
        self.assertEqual(m.execute_request(self.req(), self.sz(spec), spec, self.Q, 1.5e-3, NOW).status, "REJECTED")  # open-risk / duplicate protections
    def test_autonomy_locked(self):
        br, st, m, spec = self.mk(Mode.AUTONOMOUS)
        self.assertIn("autonomy", m.execute_request(self.req(), self.sz(spec), spec, self.Q, 1.5e-3, NOW).detail)
    def test_kill_switch_closes_and_blocks(self):
        br, st, m, spec = self.mk(); m.execute_request(self.req(), self.sz(spec), spec, self.Q, 1.5e-3, NOW)
        m.kill(self.Q); self.assertEqual(len(br.open), 0); self.assertTrue(st.kill_switch)
        r = OrderRequest("c2", "EURUSD", "BUY", "market", 0.1, 1.095, 1.11, None, 50)
        self.assertEqual(m.execute_request(r, self.sz(spec), spec, self.Q, 1.5e-3, NOW).status, "REJECTED")
    def test_unprotected_position_triggers_kill(self):
        br, st, m, spec = self.mk(); br.open["x"] = dict(id="x", side="BUY", size=0.1, entry=1.1, sl=0, tp=1.11, risk=0, ts=NOW, symbol="EURUSD")
        self.assertTrue(any("UNPROTECTED" in i for i in m.reconcile(NOW))); self.assertTrue(st.kill_switch)
    def test_unknown_result_halts(self):
        class Bad(PaperBroker):
            def submit(self, req, quote): return OrderResult("UNKNOWN", req.client_id, detail="timeout")
        spec = get_spec("EURUSD"); st = RiskState.fresh(10000)
        m = ExecutionManager(Bad(spec, 10000), ExecConfig(mode=Mode.APPROVAL_REQUIRED, allowed_sessions_utc=SESSIONS_ALL, allowed_instruments=["EURUSD"]), RiskConfig(), st, approve=lambda r: True)
        self.assertEqual(m.execute_request(self.req(), self.sz(spec), spec, self.Q, 1.5e-3, NOW).status, "UNKNOWN"); self.assertTrue(st.kill_switch)
    def test_paper_sl_and_idempotency(self):
        spec = get_spec("EURUSD"); b = PaperBroker(spec, 10000); q = self.Q
        self.assertEqual(b.submit(self.req(), q).status, "FILLED"); self.assertEqual(b.submit(self.req(), q).status, "DUPLICATE")
        b.on_bar(Candle(NOW, 1.1, 1.101, 1.094, 1.095), 0.0002); self.assertEqual(b.closed[0]["reason"], "SL"); self.assertLess(b.equity, 10000)
    def test_request_validation(self):
        s = get_spec("EURUSD")
        self.assertTrue(OrderRequest("x", "EURUSD", "BUY", "market", 0.1, 0, 1.2).validate(s))
        self.assertTrue(OrderRequest("x", "EURUSD", "BUY", "market", 0.105, 1.0, 1.2).validate(s))


class FakeMT5:
    """Minimal MT5 stand-in used to exercise adapter LOGIC. It is not the real terminal."""
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6; ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1; ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN = 1, 0, 2
    TRADE_RETCODE_DONE, TRADE_RETCODE_REJECT, TRADE_RETCODE_INVALID, TRADE_RETCODE_NO_MONEY = 10009, 10006, 10013, 10019
    TRADE_RETCODE_INVALID_VOLUME, TRADE_RETCODE_INVALID_STOPS, TRADE_RETCODE_MARKET_CLOSED = 10014, 10016, 10018
    ACCOUNT_TRADE_MODE_REAL, SYMBOL_TRADE_MODE_FULL, DEAL_ENTRY_OUT = 2, 4, 1
    TIMEFRAME_M15 = 15
    def __init__(self, real=False, mode="ok", stops_level=10):
        self.real, self.mode, self.stops, self.pos, self.t, self._tk = real, mode, stops_level, [], 1000, 0
    def initialize(self, **k): return True
    def last_error(self): return (0, "")
    def account_info(self): return NS(trade_allowed=True, trade_mode=2 if self.real else 0, currency="USD", equity=10000., balance=10000., margin_free=9000.)
    def terminal_info(self): return NS(trade_allowed=True)
    def symbol_select(self, s, e): return True
    def symbol_info(self, s): return NS(trade_mode=4, digits=5, point=1e-5, trade_tick_size=1e-5, trade_contract_size=100000., trade_tick_value=1.0,
                                       volume_step=.01, volume_min=.01, volume_max=100., currency_base="EUR", currency_profit="USD", trade_stops_level=self.stops, filling_mode=3)
    def symbol_info_tick(self, s): return NS(bid=1.0999, ask=1.1001, time=int(__import__("time").time()) + 3 * 3600)
    def order_check(self, r): return NS(retcode=0, comment="")
    def order_send(self, r):
        if r["action"] == self.TRADE_ACTION_SLTP:
            for p in self.pos:
                if p.ticket == r["position"]: p.sl, p.tp = r["sl"], r["tp"]
            return NS(retcode=self.TRADE_RETCODE_DONE)
        if r.get("position"):
            self.pos = [p for p in self.pos if p.ticket != r["position"]]; return NS(retcode=self.TRADE_RETCODE_DONE)
        if self.mode == "ambiguous": return None
        self.t += 1
        sl = 0.0 if self.mode == "nosl" else r["sl"]
        self.pos.append(NS(ticket=self.t, symbol=r["symbol"], magic=r["magic"], comment=r["comment"], type=r["type"], volume=r["volume"], price_open=r["price"], sl=sl, tp=r["tp"]))
        return NS(retcode=self.TRADE_RETCODE_DONE)
    def positions_get(self, symbol=None): return [p for p in self.pos if symbol in (None, p.symbol)]
    def history_deals_get(self, a, b): return []
    def copy_rates_from_pos(self, s, tf, start, n):
        t0 = int(__import__("time").time()) + 3 * 3600 - n * 900
        return [{"time": t0 + 900 * i, "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1, "tick_volume": 10} for i in range(n)]


class MT5(unittest.TestCase):
    def setUp(self):
        from apex import mt5_adapter as M; self.M = M; os.environ.pop("APEX_I_UNDERSTAND_LIVE_RISK", None)
    def broker(self, **kw):
        f = FakeMT5(**kw); spec = self.M.spec_from_mt5(f, "EURUSD"); return f, spec, self.M.MT5Broker(f, spec)
    def req(self, spec, cid="abc123"): return OrderRequest(cid, "EURUSD", "BUY", "market", 0.1, 1.0950, 1.1100)
    Q = Quote(1.0999, 1.1001, NOW, "mt5")
    def test_spec_is_broker_verified(self):
        f = FakeMT5(); s = self.M.spec_from_mt5(f, "EURUSD"); self.assertTrue(s.verified); self.assertAlmostEqual(s.pip_size, 1e-4)
        self.assertAlmostEqual(s.value_per_price_unit, 100000.0)
    def test_feed_utc_conversion_and_closed_bars(self):
        f = FakeMT5(); feed = self.M.MT5Feed(f); off = feed.detect_offset("EURUSD"); self.assertEqual(off, 3)
        cs = feed.candles("EURUSD", 900, 50); self.assertEqual(len(cs), 50)
        self.assertLess(abs((datetime.now(timezone.utc) - cs[-1].ts).total_seconds()), 900 + 60)
    def test_fill_verified_and_duplicate(self):
        f, spec, b = self.broker(); r = b.submit(self.req(spec), self.Q)
        self.assertEqual(r.status, "FILLED"); self.assertEqual(b.submit(self.req(spec), self.Q).status, "DUPLICATE"); self.assertEqual(len(f.pos), 1)
        self.assertEqual(b.positions()[0]["sl"], 1.0950)
    def test_missing_sl_is_repaired(self):
        f, spec, b = self.broker(mode="nosl"); r = b.submit(self.req(spec), self.Q)
        self.assertEqual(r.status, "FILLED"); self.assertEqual(f.pos[0].sl, 1.0950)
    def test_ambiguous_response_is_unknown_and_manager_halts(self):
        f, spec, b = self.broker(mode="ambiguous"); st = RiskState.fresh(10000)
        m = ExecutionManager(b, ExecConfig(mode=Mode.APPROVAL_REQUIRED, allowed_sessions_utc=SESSIONS_ALL, allowed_instruments=["EURUSD"]), RiskConfig(), st, approve=lambda r: True, spec_lookup=lambda s: spec)
        sz = size_position(spec, RiskConfig(), 10000, 1.1, 1.095)
        self.assertEqual(m.execute_request(self.req(spec), sz, spec, self.Q, 1.5e-3, NOW).status, "UNKNOWN"); self.assertTrue(st.kill_switch)
    def test_stops_level_rejected(self):
        f, spec, b = self.broker(stops_level=5000); self.assertEqual(b.submit(self.req(spec), self.Q).status, "REJECTED")
    def test_real_account_refused_without_double_opt_in(self):
        f = FakeMT5(real=True); s = self.M.spec_from_mt5(f, "EURUSD")
        with self.assertRaises(RuntimeError): self.M.MT5Broker(f, s)
        with self.assertRaises(RuntimeError): self.M.MT5Broker(f, s, allow_live=True)
        os.environ["APEX_I_UNDERSTAND_LIVE_RISK"] = "yes"; self.M.MT5Broker(f, s, allow_live=True); os.environ.pop("APEX_I_UNDERSTAND_LIVE_RISK")
    def test_close_all_and_account(self):
        f, spec, b = self.broker(); b.submit(self.req(spec), self.Q); b.close_all(); self.assertEqual(len(f.pos), 0); self.assertEqual(b.account()["equity"], 10000.0)


class AppFlow(unittest.TestCase):
    def test_scan_allow_fill(self):
        app = mkapp(1, 800); r = app.scan(); p = r["proposal"]
        self.assertIsNotNone(p); self.assertEqual(r["scan"]["n_total"], sc.METHOD_COUNT)
        self.assertEqual(r["scan"]["long"] + r["scan"]["short"] + r["scan"]["neutral"] + r["scan"]["na"] + r["scan"]["errors"], sc.METHOD_COUNT)
        out = app.allow(p["id"]); self.assertTrue(out["ok"], out); self.assertEqual(len(app.broker.positions()), 1)
        pos = app.broker.positions()[0]; self.assertEqual(pos["sl"], p["sl"]); self.assertLessEqual(pos["risk"], app.s.risk.equity * app.s.risk.risk_per_trade_pct / 100 + 1e-6)
        self.assertFalse(app.allow(p["id"])["ok"])    # a click can never execute twice
        self.assertTrue(Journal.verify(app.s.server.journal_path))
    def test_nothing_sent_without_allow(self):
        app = mkapp(4, 525); self.assertIsNotNone(app.scan()["proposal"]); self.assertEqual(len(app.broker.positions()), 0)
    def test_price_drift_rejects(self):
        app = mkapp(1, 800); pid = app.scan()["proposal"]["id"]; app.advance(30)
        out = app.allow(pid); self.assertFalse(out["ok"]); self.assertEqual(len(app.broker.positions()), 0)
    def test_expiry(self):
        app = mkapp(4, 525); pid = app.scan()["proposal"]["id"]; app.feed.cursor += 5
        out = app.allow(pid); self.assertFalse(out["ok"]); self.assertIn("expired", out["error"])
    def test_kill_blocks_everything(self):
        app = mkapp(1, 800); pid = app.scan()["proposal"]["id"]; app.kill()
        self.assertFalse(app.allow(pid)["ok"]); self.assertIsNone(app.scan()["proposal"]); self.assertTrue(app.status()["kill"])
    def test_kill_persists_across_restart(self):
        app = mkapp(1, 800); app.kill(); s = app.s; app2 = App(s); self.assertTrue(app2.state.kill_switch)
    def test_risk_veto_blocks_allow(self):
        app = mkapp(1, 800); pid = app.scan()["proposal"]["id"]
        app.state.consecutive_losses = 99; app.mgr._last_reconcile = datetime.now(timezone.utc) + timedelta(days=1)
        self.assertFalse(app.allow(pid)["ok"]); self.assertEqual(len(app.broker.positions()), 0)
    def test_feed_failure_is_safe(self):
        app = mkapp(1, 800)
        def boom(*a, **k): raise RuntimeError("down")
        app.feed.candles = boom; r = app.scan(); self.assertIn("error", r); self.assertEqual(r["decision"], "NO TRADE")
    def test_analysis_only_mode_creates_no_proposal(self):
        app = mkapp(1, 800); app.s.exec.mode = Mode.ANALYSIS_ONLY; app.mgr.cfg.mode = Mode.ANALYSIS_ONLY
        self.assertIsNone(app.scan()["proposal"])


class Server(unittest.TestCase):
    def test_http_api(self):
        from apex.server import make_handler
        from http.server import ThreadingHTTPServer
        app = mkapp(1, 800); srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, "tok")); port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        def call(path, body=None, token="tok", host=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST")
            if token: req.add_header("X-APEX-Token", token)
            if host: req.add_header("Host", host)
            try:
                with urllib.request.urlopen(req, timeout=20) as r: return r.status, r.read().decode()
            except urllib.error.HTTPError as e: return e.code, e.read().decode()
        try:
            self.assertEqual(call("/api/status", token=None)[0], 401)
            self.assertEqual(call("/api/scan", {}, token="wrong")[0], 401)
            self.assertEqual(call("/", host="evil.com")[0], 403)
            code, html = call("/"); self.assertEqual(code, 200); self.assertIn("ALLOW trade", html)
            code, b = call("/api/scan", {}); d = json.loads(b); self.assertEqual(code, 200); pid = d["proposal"]["id"]
            code, b = call("/api/allow", {"id": pid}); self.assertTrue(json.loads(b)["ok"])
            self.assertEqual(json.loads(call("/api/status")[1])["positions"].__len__(), 1)
            self.assertEqual(call("/api/kill", {})[0], 200); self.assertTrue(json.loads(call("/api/status")[1])["kill"])
        finally: srv.shutdown(); srv.server_close()
    def test_refuses_non_local_bind(self):
        from apex.server import serve
        with self.assertRaises(RuntimeError): serve(mkapp(), host="0.0.0.0")


class SettingsAndMisc(unittest.TestCase):
    def w(self, d):
        p = os.path.join(tempfile.mkdtemp(), "c.json"); 
        with open(p, "w") as f: json.dump(d, f)
        return p
    def test_settings_validation(self):
        p0 = self.w({"risk": {"equity": 5000}, "feed": {"type": "csv", "path": "x.csv"}}); self.assertIsInstance(load_settings(p0), Settings)
        with self.assertRaises(ValueError): load_settings(self.w({"risk": {"typo_key": 1}}))
        with self.assertRaises(ValueError): load_settings(self.w({"exec": {"mode": "AUTONOMOUS"}}))
        with self.assertRaises(ValueError): load_settings(self.w({"risk": {"risk_per_trade_pct": 9}}))
        with self.assertRaises(ValueError): load_settings(self.w({"broker": {"type": "mt5"}, "feed": {"type": "csv"}}))
    def test_autonomy_flags(self):
        self.assertFalse(Authorization().autonomous_ok()[0]); self.assertTrue(Authorization(True, True, True, True, True).autonomous_ok()[0])
    def test_journal_tamper_detected(self):
        p = os.path.join(tempfile.mkdtemp(), "j.jsonl"); j = Journal(p); j.log("A", x=1); j.log("B", y=2); self.assertTrue(Journal.verify(p))
        
        with open(p) as f: t = f.read().replace('"y": 2', '"y": 3')
        with open(p, "w") as f: f.write(t)
        self.assertFalse(Journal.verify(p))
    def test_events_loader(self):
        from apex.events import load_events
        p = os.path.join(tempfile.mkdtemp(), "e.csv"); 
        with open(p, "w") as f: f.write("time_utc,currency,impact,name\n2024-03-05T13:30:00Z,usd,high,NFP\n2024-03-05T14:00:00Z,eur,low,minor\n")
        self.assertEqual(load_events(p), [("2024-03-05T13:30:00Z", "USD", "NFP")])
    def test_edge_evaluator_and_verdict_rules(self):
        r = evaluate_methods(synthetic(1200, seed=2), 0.0001, step=8)
        self.assertIn("CONSENSUS", r); self.assertTrue(all(v["verdict"] == "INSUFFICIENT DATA" for v in r.values()))
    def test_backtest_runs_and_cannot_self_validate_on_tiny_sample(self):
        from apex.backtest import run_backtest
        r = run_backtest("EURUSD", synthetic(1100, seed=3), 900, RiskConfig(), ExecConfig(allowed_sessions_utc=SESSIONS_ALL), slippage=2e-5, step=6, enforce_session=False)
        self.assertEqual(r["status"], "UNVALIDATED"); self.assertIn("assumptions", r)


if __name__ == "__main__":
    unittest.main()
