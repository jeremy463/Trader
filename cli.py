"""CLI: scan | analyze | backtest | edge | serve | reset-kill | verify-journal | selftest."""
from __future__ import annotations
import argparse, json, sys
from datetime import timezone
from .config import RiskConfig, ExecConfig, validate_config
from .data import load_csv, Quote
from .engine import analyze
from .report import format_report
from .settings import load_settings, Settings


def _data(a, s: Settings):
    if getattr(a, "csv", None): return load_csv(a.csv)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser("apex")
    ap.add_argument("--config", help="settings JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("scan", "analyze", "backtest", "edge", "walkforward"):
        p = sub.add_parser(n)
        p.add_argument("--symbol"); p.add_argument("--csv"); p.add_argument("--tf-min", type=int)
        p.add_argument("--any-session", action="store_true"); p.add_argument("--spread", type=float)
        p.add_argument("--step", type=int, default=4)
        if n == "edge": p.add_argument("--out", default="apex_edge.json")
        if n == "walkforward": p.add_argument("--train", type=int, default=1200); p.add_argument("--test", type=int, default=400)
    sub.add_parser("serve"); sub.add_parser("verify-journal"); sub.add_parser("selftest")
    sub.add_parser("web-serve"); sub.add_parser("news-check"); sub.add_parser("paystack-check")
    wc = sub.add_parser("web-check"); wc.add_argument("--strict", action="store_true", help="treat warnings as failures")
    wi = sub.add_parser("web-init"); wi.add_argument("--out", default="web.json"); wi.add_argument("--domain", required=True); wi.add_argument("--company", required=True); wi.add_argument("--support-email", required=True)
    wi.add_argument("--jurisdiction", required=True); wi.add_argument("--feed", default="oanda", choices=["oanda", "twelvedata", "mt5"]); wi.add_argument("--oanda-account", default=""); wi.add_argument("--fallback", default="", choices=["", "oanda", "twelvedata"])
    wi.add_argument("--symbols", default="EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF,NZDUSD"); wi.add_argument("--pro-price", type=float, required=True); wi.add_argument("--elite-price", type=float, required=True); wi.add_argument("--currency", default="NGN"); wi.add_argument("--force", action="store_true")
    wb = sub.add_parser("web-backup"); wb.add_argument("--out", required=True)
    wa = sub.add_parser("web-create-admin"); wa.add_argument("--email", required=True)
    w2 = sub.add_parser("web-reset-admin-2fa"); w2.add_argument("--email", required=True)
    r = sub.add_parser("reset-kill"); r.add_argument("--confirm", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        import unittest
        res = unittest.main(module=None, argv=["x", "tests.test_apex", "tests.test_web", "tests.test_v4", "tests.test_v5", "tests.test_v6", "tests.test_v7"], exit=False).result
        return 0 if res.wasSuccessful() else 1
    if a.cmd == "web-init":
        from .web import main as W
        return W.web_init(a.out, a.domain, a.company, a.support_email, a.jurisdiction, a.feed, a.oanda_account, a.fallback, a.symbols, a.pro_price, a.elite_price, a.currency, a.force)
    if a.cmd.startswith("web-") or a.cmd in ("news-check", "paystack-check"):
        from .web import main as W
        return {"web-serve": lambda: W.run(a.config), "web-check": lambda: W.check(a.config, a.strict), "paystack-check": lambda: W.paystack_check(a.config),
                "web-create-admin": lambda: W.create_admin(a.config, a.email), "web-reset-admin-2fa": lambda: W.reset_admin_2fa(a.config, a.email), "web-backup": lambda: W.backup(a.config, a.out), "news-check": lambda: W.news_check(a.config)}[a.cmd]()
    s = load_settings(a.config)
    if a.cmd == "serve":
        from .app import App
        from .server import serve
        app = App(s); serve(app, s.server.host, s.server.port); return 0
    if a.cmd == "verify-journal":
        from .journal import Journal
        ok = Journal.verify(s.server.journal_path); print("journal chain OK" if ok else "JOURNAL TAMPERED OR CORRUPT"); return 0 if ok else 1
    if a.cmd == "reset-kill":
        from .risk import RiskState
        if not a.confirm: print("Add --confirm after you have manually checked the broker account."); return 2
        st = RiskState.load(s.server.state_path, s.risk.equity); st.kill_switch = False; st.save(s.server.state_path); print("kill switch cleared"); return 0
    cs = _data(a, s)
    if cs is None: print("Provide --csv FILE (your own real OHLCV history: time,open,high,low,close,volume)"); return 2
    sym = a.symbol or s.feed.symbol; tf = (a.tf_min or s.feed.tf_minutes) * 60
    spread = a.spread if a.spread is not None else s.feed.spread
    if a.cmd in ("scan", "analyze"):
        q = Quote(cs[-1].c - spread / 2, cs[-1].c + spread / 2, cs[-1].ts, "simulated-cli (assumed spread from --spread)")
        an = analyze(sym, cs, tf, s.risk, s.exec, None, q, cs[-1].ts, None, 1.0, s.broker.commission_per_lot, None,
                     enforce_session=not a.any_session, scan_cfg=s.scan, allow_simulated_quote=True)
        if a.cmd == "scan":
            sc = an.scan
            print(f"\n{sc.passed_total} of {sc.n_total} methods passed  ({sc.summary()})")
            print(f"Consensus: {sc.consensus or 'NONE'}  | Decision: {an.decision} ({an.classification})\n")
            for g in sc.signals:
                res = "n/a" if not g.applicable else ("LONG " if g.passed and g.direction > 0 else "SHORT" if g.passed else "-    ")
                print(f"  {res} {g.strength:4.2f}  {g.name:30s} [{g.family}] {g.detail}")
            if not sc.consensus: print("\nNo consensus:", "; ".join(sc.why_not[:3]))
        else: print(format_report(an))
    elif a.cmd == "backtest":
        from .backtest import run_backtest
        print(json.dumps(run_backtest(sym, cs, tf, s.risk, s.exec, spread, s.broker.slippage, s.broker.commission_per_lot,
                                      step=a.step, enforce_session=not a.any_session, scan_cfg=s.scan), indent=2, default=str))
    elif a.cmd == "walkforward":
        from .edge import walk_forward
        r = walk_forward(cs, spread, a.train, a.test, step=a.step, cfg=s.scan, progress=lambda k: print(f"  fold {k} done", file=sys.stderr))
        print(json.dumps({k: v for k, v in r.items() if k != "folds"}, indent=1)); print("per-fold:", [(f["n"], f["mean_R"]) for f in r["folds"]])
    elif a.cmd == "edge":
        from .edge import evaluate_methods, save
        res = evaluate_methods(cs, spread, step=a.step, cfg=s.scan, progress=lambda i, n: print(f"  {i}/{n}", file=sys.stderr))
        save(res, a.out)
        for k, v in res.items(): print(f"{k:30s} OOS n={v['out_of_sample']['n']:4d} meanR={v['out_of_sample']['mean_R']:+.3f} t={v['out_of_sample']['t']:+.1f}  {v['verdict']}")
        print(f"saved -> {a.out} (methods with NEGATIVE EDGE are auto-disabled by the dashboard)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
