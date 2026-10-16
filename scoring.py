"""Transparent configurable setup score; separate from probability (spec 14)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Optional

WEIGHTS: Dict[str, float] = {
    "htf_alignment": 0.20, "structure_quality": 0.12, "trigger_quality": 0.12, "location": 0.12,
    "liquidity": 0.06, "momentum": 0.08, "volatility_suitability": 0.08, "fundamental_context": 0.04,
    "net_reward_risk": 0.12, "data_and_costs": 0.06,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def score(components: Dict[str, float], weights: Dict[str, float] = WEIGHTS) -> float:
    return round(100 * sum(weights[k] * max(0.0, min(1.0, components.get(k, 0.0))) for k in weights), 1)


def classify(score_v: float, data_ok: bool, plan_ok: bool, trigger_met: bool, risk_ok: bool) -> str:
    if not data_ok: return "DATA INSUFFICIENT"
    if not plan_ok: return "NO TRADE"
    if score_v >= 75 and trigger_met and risk_ok: return "HIGH-QUALITY SETUP"
    if score_v >= 60: return "CONDITIONAL SETUP"
    if score_v >= 45: return "LOW-QUALITY SETUP"
    return "NO TRADE"


@dataclass
class ProbabilityEstimate:
    value: Optional[float]
    basis: str


def probability(backtest: Optional[dict]) -> ProbabilityEstimate:
    """Only report a probability from a defined, sufficiently tested model (spec 5G, 14)."""
    if not backtest or backtest.get("status") != "VALIDATED":
        return ProbabilityEstimate(None, "UNVALIDATED - no sufficiently tested out-of-sample model; no win probability reported")
    return ProbabilityEstimate(backtest["oos_win_rate"],
                               f"out-of-sample win rate over {backtest['oos_trades']} trades (not calibrated, not a guarantee)")
