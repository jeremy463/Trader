"""Backtest with costs, no look-ahead, IS/OOS split, folds, cost stress, Monte Carlo (spec 19)."""
from __future__ import annotations
import random, statistics, uuid
from datetime import datetime
from typing import List, Optional
from .config import RiskConfig, ExecConfig
from .data import Candle, Quote
from .engine import analyze
from .execution import PaperBroker, OrderRequest
from .instruments import get_spec
from .risk import RiskState, OpenPosition, risk_multiplier
from .edge import bootstrap_mean_ci


def metrics(rs: List[float], pnls: List[float], start_eq: float) -> dict:
    n = len(rs)
    if n == 0: return {"trades": 0}
    wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x <= 0]
    eq, peak, mdd = start_eq, start_eq, 0.0
    for p in pnls:
        eq += p; peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak * 100)
    sd = statistics.pstdev(rs) if n > 1 else 0
    return {"trades": n, "win_rate": len(wins) / n, "expectancy_R": sum(rs) / n,
            "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else float("inf") if wins else 0.0,
            "avg_win": statistics.mean(wins) if wins else 0.0, "avg_loss": statistics.mean(losses) if losses else 0.0,
            "max_drawdown_pct": mdd, "sharpe_per_trade": (sum(rs) / n / sd) if sd else 0.0, "net_pnl": sum(pnls)}


def monte_carlo(pnls: List[float], start_eq: float, runs=500, seed=1) -> dict:
    rnd, dds = random.Random(seed), []
    for _ in range(runs):
        s = pnls[:]; rnd.shuffle(s)
        eq, pk, md = start_eq, start_eq, 0.0
        for p in s: eq += p; pk = max(pk, eq); md = max(md, (pk - eq) / pk * 100)
        dds.append(md)
    dds.sort()
    return {"mc_dd_p50": dds[len(dds) // 2], "mc_dd_p95": dds[int(len(dds) * 0.95)]}


def run_backtest(symbol: str, candles: List[Candle], tf_seconds: int, risk_cfg: Optional[RiskConfig] = None,
                 exec_cfg: Optional[ExecConfig] = None, spread: Optional[float] = None, slippage: float = 0.0,
                 commission_per_lot: float = 0.0, window: int = 320, step: int = 1, enforce_session=True,
                 progress=False, scan_cfg=None, disabled=()) -> dict:
    rc = risk_cfg or RiskConfig(); ec = exec_cfg or ExecConfig()
    spec = get_spec(symbol)
    spread = spread if spread is not None else spec.pip_size * 1.0
    rc.assumed_slippage = slippage
    br = PaperBroker(spec, rc.equity, commission_per_lot, slippage)
    st = RiskState.fresh(rc.equity)
    pending_sig, day = None, None
    for i in range(window, len(candles)):
        c = candles[i]
        if pending_sig:  # fill at THIS bar's open (signal formed on prior close)
            req = pending_sig; pending_sig = None
            q = Quote(c.o - spread / 2, c.o + spread / 2, c.ts, "simulated-backtest")
            r = br.submit(req, q)
            if r.status == "FILLED": st.order_times.append(c.ts)
        n_closed = len(br.closed)
        br.on_bar(c, spread)
        for t in br.closed[n_closed:]:
            st.consecutive_losses = st.consecutive_losses + 1 if t["pnl"] < 0 else 0
        st.equity = br.equity; st.peak_equity = max(st.peak_equity, st.equity)
        st.free_margin = st.equity
        d = c.ts.date()
        if d != day:
            st.day_start_equity = st.equity; day = d
            if c.ts.weekday() == 0: st.week_start_equity = st.equity
        st.positions = []
        for p in br.open.values():
            st.positions.append(OpenPosition(
                p["symbol"], p["side"], p["size"], p["entry"], p["sl"], p["risk"], spec.base_ccy, spec.quote_ccy,
                spec.notional_per_lot(p["entry"]) * p["size"]))
        if br.open or br.pending or (i - window) % step: continue
        w = candles[i - window + 1:i + 1]
        quote = Quote(c.c - spread / 2, c.c + spread / 2, c.ts, "simulated-backtest")
        a = analyze(symbol, w, tf_seconds, rc, ec, st, quote, None, None, 1.0, commission_per_lot, None,
                    enforce_session=enforce_session, min_bars=200, scan_cfg=scan_cfg, disabled_methods=disabled,
                    allow_simulated_quote=True)
        if a.decision in ("BUY", "SELL") and a.plan and a.size and not a.size.rejected and a.gate and a.gate.approved:
            pending_sig = OrderRequest(uuid.uuid4().hex, symbol, a.decision, "market", a.size.size, a.plan.stop.price,
                                       a.plan.targets.tp2, None, a.size.risk_amount, a.plan.targets.tp1, 0.5)
    tr = br.closed
    rs, pn = [t["r"] for t in tr], [t["pnl"] for t in tr]
    cut = int(len(tr) * 0.7)
    allm, ism, oosm = metrics(rs, pn, rc.equity), metrics(rs[:cut], pn[:cut], rc.equity), metrics(rs[cut:], pn[cut:], rc.equity)
    fs = max(1, len(rs) // 5)
    folds = [metrics(rs[j:j + fs], pn[j:j + fs], rc.equity).get("expectancy_R", 0) for j in range(0, len(rs), fs)][:5] if rs else []
    # stress: double friction (extra spread-sized cost per trade) expressed in R
    extra = [(spread + 2 * slippage) * spec.value_per_price_unit * t["size"] / t["risk"] if t["risk"] else 0 for t in tr]
    stressed = [r - e for r, e in zip(rs, extra)]
    mc = monte_carlo(pn, rc.equity) if pn else {}
    status, why = "UNVALIDATED", []
    if oosm.get("trades", 0) < 100: why.append(f"OOS trades {oosm.get('trades',0)} < 100")
    if oosm.get("expectancy_R", 0) <= 0: why.append("OOS expectancy <= 0")
    lo, hi = bootstrap_mean_ci(rs[cut:]) if len(rs[cut:]) >= 5 else (float("-inf"), float("inf"))
    if lo <= 0: why.append(f"bootstrap 95% CI of OOS expectancy includes zero ({lo:.2f}..{hi:.2f} R)")
    if stressed and sum(stressed[cut:]) <= 0: why.append("not profitable under doubled friction")
    if allm.get("profit_factor", 0) < 1.1: why.append("profit factor < 1.1")
    if mc and mc["mc_dd_p95"] > rc.max_drawdown_pct: why.append("Monte-Carlo p95 drawdown exceeds limit")
    if not why: status = "VALIDATED"
    return {"status": status, "reasons": why, "all": allm, "in_sample": ism, "out_of_sample": oosm, "fold_expectancy_R": folds,
            "oos_expectancy_ci95": [lo, hi], "stressed_expectancy_R": (sum(stressed) / len(stressed)) if stressed else 0, **mc,
            "oos_win_rate": oosm.get("win_rate"), "oos_trades": oosm.get("trades", 0),
            "assumptions": f"spread={spread}, slippage={slippage}, commission/lot={commission_per_lot}, signals at close, fills next open, SL-first on ambiguous bars, one position at a time, partial 50% at TP1 then stop to breakeven, runner to TP2"}
