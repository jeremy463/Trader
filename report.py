"""Section A-I response format (spec 21). Unavailable fields are marked, never fabricated."""
from __future__ import annotations
from .engine import Analysis

NA = "UNAVAILABLE"


def _f(x, nd=5):
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def format_report(a: Analysis) -> str:
    L = []
    P = L.append
    p, s, g = a.plan, a.size, a.gate
    if a.scan:
        sc = a.scan
        P("S. CHART SCAN (all methods)")
        P(f"- {sc.summary()}")
        P(f"- Consensus: {sc.consensus or 'NONE'} (strength {sc.consensus_strength:.2f}); " + ("; ".join(sc.why_not[:3]) if not sc.consensus else "all consensus checks met"))
        P("- Method | Family | Result | Strength | Detail")
        for sig in sc.signals:
            res = "n/a" if not sig.applicable else ("LONG pass" if sig.passed and sig.direction > 0 else "SHORT pass" if sig.passed else "neutral")
            P(f"  {sig.name} | {sig.family} | {res} | {sig.strength:.2f} | {sig.detail}")
        P("")
    P("A. MARKET SNAPSHOT")
    P(f"- Instrument: {a.symbol}\n- Asset class: {a.spec.asset_class}{' (spec UNVERIFIED vs broker)' if not a.spec.verified else ''}")
    P(f"- Data timestamp: {a.data_ts.isoformat()}\n- Current bid/ask or last price: {a.price_text}")
    P(f"- Timeframe: {a.timeframe_s//60} min\n- Trading session: {'UTC hour '+str(a.data_ts.hour)}")
    P(f"- Market regime: {', '.join(a.regime.labels)}")
    P(f"- Data limitations: {'; '.join(a.quality.blocking + a.quality.warnings) or 'none detected'}")
    P("\nB. MULTI-TIMEFRAME BIAS")
    for v in a.tfs: P(f"- {v.name}: trend {v.trend}; {v.ema_state}; structure {v.structure_event}")
    P(f"- Key levels: {a.strat_notes.get('liquidity', NA)}")
    P(f"- Conflicting evidence: {'; '.join(a.evidence_against) or 'none identified'}")
    P("\nC. STRATEGY CONFLUENCE")
    for k, lab in (("price_action", "Price action"), ("liquidity", "Liquidity"), ("indicators", "Indicators"),
                   ("volume", "Volume/order flow"), ("fundamental", "Fundamental/event context"), ("quant", "Quantitative evidence")):
        P(f"- {lab}: {a.strat_notes.get(k, NA)}")
    P(f"- Hypotheses: LONG {a.hypotheses.get('long')} | SHORT {a.hypotheses.get('short')} | NO-TRADE {a.hypotheses.get('no-trade') or 'not selected'}")
    P(f"- Counterarguments: {'; '.join(a.evidence_against) or 'none identified'}")
    P("\nD. TRADE DECISION")
    P(f"- Decision: {a.decision}\n- Setup classification: {a.classification}\n- Proposed order type: {a.order_type}")
    P(f"- Entry price or zone: {_f(p.entry) if p else NA}\n- Entry trigger: {a.trigger}\n- Setup expiration: {a.expiry}")
    P(f"- Thesis invalidation: {('close beyond ' + _f(p.stop.price)) if p and not p.rejected else NA}")
    P("\nE. STOP-LOSS ANALYSIS")
    if p and p.stops:
        P(f"- Preferred SL: {_f(p.stop.price)} ({p.stop.method})\n- Technical reason: {p.stop.note}")
        P(f"- Distance from entry: {_f(p.stop.distance)} ({p.stop.atr_mult:.2f} ATR)")
        P("- Alternative SL methods: " + " | ".join(f"{o.method} {_f(o.price)} ({o.atr_mult:.2f}ATR) {'OK' if o.valid else 'rejected'}" for o in p.stops))
        P("- Why preferred: structural invalidation within volatility bounds is chosen over arbitrary distance; falls back to ATR only if structure invalid")
        P("- Estimated execution risk: stops are not guaranteed; gaps/slippage can exceed risk budget")
    else: P(f"- {NA} (no setup)")
    P("\nF. TAKE-PROFIT ANALYSIS")
    if p and p.targets.method not in ("n/a",) and p.gross_rr:
        t = p.targets
        for i, (n, v) in enumerate((("TP1", t.tp1), ("TP2", t.tp2), ("TP3", t.tp3))):
            P(f"- {n}: {_f(v)}  gross R:R {p.gross_rr[i]}  net R:R {p.net_rr[i]}")
        P(f"- Technical justification: {t.method}; {t.notes}")
        P("- Management: partial at TP1 then trail by structure/ATR per approved policy; targets are not assumed to be reached")
        if p.comparison:
            P("- Comparison: Method | Entry | SL | TP1 | TP2 | TP3 | Gross R:R | Main weakness")
            for r in p.comparison: P("  " + " | ".join(str(_f(r[k]) if isinstance(r[k], float) else r[k]) for k in ("Method", "Entry", "Stop-loss", "TP1", "TP2", "TP3", "Gross R:R (TP2)", "Main weakness")))
        if p.rejected: P(f"- SETUP REJECTED: {p.rejected}")
    else: P(f"- {NA}")
    P("\nG. RISK AND POSITION SIZE")
    P(f"- Account equity: {a.equity}\n- Risk percentage: {a.risk_cfg.risk_per_trade_pct if a.risk_cfg else NA}%")
    if s:
        P(f"- Maximum permitted loss: {s.risk_budget:.2f}\n- Instrument-specific calculation: {s.formula}")
        P(f"- Stop distance: {s.stop_distance:.6g} ({s.stop_ticks:.1f} ticks)")
        P(f"- Position size: {s.size if not s.rejected else 'REJECTED - ' + s.rejected}\n- Risk at stop: {s.risk_amount:.2f}\n- Estimated transaction costs: {s.est_costs:.2f} (+ spread in R:R)")
        P(f"- Margin requirement: {s.margin:.2f} (margin is NOT maximum loss)\n- Notional: {s.notional:.2f}")
    else: P(f"- {NA}")
    P(f"- Risk-gate decision: {('APPROVED' if g.approved else 'VETOED: ' + '; '.join(g.reasons)) if g else NA}")
    P("\nH. EVIDENCE AND UNCERTAINTY")
    P(f"- Setup score: {a.score}/100 (NOT a probability) components {({k: round(v,2) for k,v in a.components.items()})}")
    P(f"- Probability estimate: {a.prob.value if a.prob.value is not None else 'none'} - {a.prob.basis}")
    P(f"- Evidence supporting: {'; '.join(a.evidence_for) or 'none'}\n- Evidence against: {'; '.join(a.evidence_against) or 'none'}")
    P(f"- Missing information: {'; '.join(a.missing) or 'none'}\n- Conditions that cancel the setup: {'; '.join(a.cancel_conditions) or NA}")
    P("\nI. EXECUTION STATUS")
    P(f"- Mode: {a.mode} - {a.exec_status}\n- Order identifier: {NA}\n- Broker-confirmed status: {NA}\n- Actual fill: {NA}\n- Verified SL/TP: {NA}\n- Next permitted management action: none")
    return "\n".join(L)
