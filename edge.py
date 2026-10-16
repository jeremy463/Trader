"""Out-of-sample evidence engine (spec 5G, 19): per-method edge, consensus calibration, walk-forward validation.
Forward outcome = ATR-normalised return after `horizon` bars minus spread. Overlap is removed by a per-method cooldown.
Everything here measures what happened on the data you supply. None of it predicts or guarantees future profit."""
from __future__ import annotations
import json, math, random, statistics
from typing import Dict, List, Optional, Sequence, Tuple
from .data import Candle
from .scanner import Ctx, run_methods, decide, ScanConfig, compute_htf_bias, METHODS


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0: return 0.0, 1.0
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - h) / d), min(1.0, (c + h) / d)


def bootstrap_mean_ci(xs: Sequence[float], runs: int = 1000, seed: int = 7) -> Tuple[float, float]:
    if len(xs) < 5: return float("-inf"), float("inf")
    rnd, n = random.Random(seed), len(xs)
    ms = sorted(sum(xs[rnd.randrange(n)] for _ in range(n)) / n for _ in range(runs))
    return ms[int(0.025 * runs)], ms[int(0.975 * runs)]


def _stats(rs: List[float]) -> dict:
    n = len(rs)
    if n < 2: return {"n": n, "mean_R": 0.0, "hit": 0.0, "t": 0.0, "hit_ci": [0.0, 1.0]}
    m, sd = statistics.mean(rs), statistics.pstdev(rs) or 1e-12
    k = sum(1 for r in rs if r > 0)
    return {"n": n, "mean_R": m, "hit": k / n, "t": m / (sd / math.sqrt(n)), "hit_ci": list(wilson(k, n))}


def collect(candles: List[Candle], spread: float, i_start: int, i_end: int, horizon: int = 12, window: int = 320, step: int = 4,
            cfg: Optional[ScanConfig] = None, disabled: Sequence[str] = (), weights: Optional[Dict[str, float]] = None, progress=None):
    """Forward outcomes for every method and for the consensus rule, for decision points in [i_start, i_end)."""
    cfg = cfg or ScanConfig()
    names = [m[0] for m in METHODS] + ["CONSENSUS"]
    obs: Dict[str, List[tuple]] = {n: [] for n in names}
    cool = {n: -10 ** 9 for n in names}
    hi = min(i_end, len(candles) - horizon)
    for i in range(max(i_start, window), hi, step):
        w = candles[i - window + 1:i + 1]
        bias = compute_htf_bias(w)[0]; x = Ctx(w, bias)
        sigs = run_methods(x, disabled, cfg.counter_regime_penalty)
        ext = (x.c[-1] - x.e21[-1]) / x.a
        res = decide(sigs, cfg, weights, bias, ext)
        fwd = lambda d: ((candles[i + horizon].c - candles[i].c) * d - spread) / x.a
        for s in sigs:
            if s.passed and i - cool[s.name] >= horizon: obs[s.name].append((i, fwd(s.direction))); cool[s.name] = i
        if res.consensus and i - cool["CONSENSUS"] >= horizon:
            obs["CONSENSUS"].append((i, fwd(1 if res.consensus == "BUY" else -1), res.consensus_strength)); cool["CONSENSUS"] = i
        if progress and (i - i_start) % (step * 200) == 0: progress(i, hi)
    return obs


def verdict_for(is_: dict, oos: dict, min_n: int) -> str:
    if oos["n"] < min_n: return "INSUFFICIENT DATA"
    if oos["mean_R"] > 0 and oos["t"] >= 2 and is_["mean_R"] > 0: return "EDGE-SUPPORTED"
    if oos["mean_R"] < 0 and oos["t"] <= -2: return "NEGATIVE EDGE"
    return "NO SIGNIFICANT EDGE"


def evaluate_methods(candles: List[Candle], spread: float, horizon: int = 12, window: int = 320, step: int = 4, min_n: int = 100,
                     cfg: Optional[ScanConfig] = None, progress=None) -> Dict[str, dict]:
    obs = collect(candles, spread, window, len(candles), horizon, window, step, cfg, progress=progress)
    out = {}
    for n, o in obs.items():
        cut = int(len(o) * 0.7)
        is_, oos = _stats([r[1] for r in o[:cut]]), _stats([r[1] for r in o[cut:]])
        out[n] = {"in_sample": is_, "out_of_sample": oos, "verdict": verdict_for(is_, oos, min_n)}
        if n == "CONSENSUS" and o:   # calibration by consensus strength tercile
            ss = sorted(r[2] for r in o); t1, t2 = ss[len(ss) // 3], ss[2 * len(ss) // 3]
            out[n]["by_strength"] = {name: _stats([r[1] for r in o if lo <= r[2] < hi]) for name, lo, hi in (("low", 0, t1), ("mid", t1, t2), ("high", t2, 9))}
    return out


def disabled_from(stats: Dict[str, dict]) -> List[str]:
    return [n for n, s in stats.items() if n != "CONSENSUS" and s.get("verdict") == "NEGATIVE EDGE"]


def weights_from(stats: Dict[str, dict], min_n: int = 100) -> Dict[str, float]:
    """Shrunk, bounded weights: proven-useful methods count a bit more, unproven ones stay at 1.0, proven-bad ones are disabled elsewhere."""
    w = {}
    for n, s in stats.items():
        if n == "CONSENSUS": continue
        o = s.get("out_of_sample", {})
        if o.get("n", 0) >= min_n and o.get("t", 0) >= 1.0: w[n] = min(1.3, 1.0 + max(0.0, o["mean_R"]) / 0.5)
    return w


def evidence_for_consensus(stats: Dict[str, dict]) -> Optional[dict]:
    c = stats.get("CONSENSUS") if stats else None
    if not c: return None
    o = c["out_of_sample"]
    return {"n": o["n"], "mean_R": round(o["mean_R"], 3), "hit_rate": round(o["hit"], 3), "hit_ci": [round(v, 3) for v in o["hit_ci"]], "t": round(o["t"], 2),
            "verdict": c["verdict"], "note": "Historical out-of-sample frequency of this signal type on your data. Not a forecast."}


def save(stats, path):
    with open(path, "w") as f: json.dump(stats, f, indent=1)


def load(path):
    try:
        with open(path) as f: return json.load(f)
    except (OSError, ValueError): return {}


# ---------------- walk-forward ----------------
def walk_forward(candles: List[Candle], spread: float, train: int = 1200, test: int = 400, horizon: int = 12, step: int = 4, min_is_n: int = 15,
                 cfg: Optional[ScanConfig] = None, progress=None) -> dict:
    """Roll forward: choose methods from the TRAIN window only (drop negative-edge ones, weight proven ones), then judge the
    consensus on the unseen TEST window. Aggregates all unseen windows; reports bootstrap CI and fold consistency."""
    cfg = cfg or ScanConfig()
    folds, all_oos, start = [], [], 0
    while start + train + test <= len(candles) - horizon:
        tr_end, te_end = start + train, start + train + test
        o_in = collect(candles, spread, start, tr_end, horizon, 320, step, cfg)
        stats = {}
        for n, o in o_in.items():
            if n == "CONSENSUS": continue
            r = [x[1] for x in o]; st = _stats(r)
            stats[n] = {"verdict": "NEGATIVE EDGE" if (st["n"] >= min_is_n and st["mean_R"] < 0 and st["t"] <= -1.5) else "OK",
                        "out_of_sample": {"n": st["n"], "mean_R": st["mean_R"], "t": st["t"]}}
        dis = disabled_from(stats); wts = weights_from(stats, min_n=min_is_n)
        o_out = collect(candles, spread, tr_end, te_end, horizon, 320, step, cfg, dis, wts)
        r = [x[1] for x in o_out["CONSENSUS"]]; fs = _stats(r)
        folds.append({"train": [start, tr_end], "test": [tr_end, te_end], "disabled": dis, "n": fs["n"], "mean_R": round(fs["mean_R"], 3), "hit": round(fs["hit"], 3)})
        all_oos += r; start += test
        if progress: progress(len(folds))
    s = _stats(all_oos); lo, hi = bootstrap_mean_ci(all_oos)
    pos = sum(1 for f in folds if f["n"] and f["mean_R"] > 0); act = sum(1 for f in folds if f["n"])
    if s["n"] < 100 or len(folds) < 3: verdict = "INSUFFICIENT DATA"
    elif s["mean_R"] > 0 and lo > 0 and act and pos / act >= 0.6: verdict = "ROBUST EDGE"
    elif s["mean_R"] > 0: verdict = "WEAK / UNPROVEN EDGE"
    else: verdict = "NO EDGE FOUND"
    return {"verdict": verdict, "oos_trades": s["n"], "mean_R": round(s["mean_R"], 3), "mean_R_ci95": [round(lo, 3), round(hi, 3)], "hit_rate": round(s["hit"], 3),
            "hit_ci95": [round(v, 3) for v in s["hit_ci"]], "positive_folds": f"{pos}/{act}", "folds": folds,
            "note": "Measured on data you supplied, with methods chosen only from each fold's training window. A past result is not a guarantee."}


def compare_profiles(candles: List[Candle], spread: float, horizon: int = 12, step: int = 6, progress=None) -> dict:
    """Same data, three selectivity levels. Shows the real trade-off (signal count vs out-of-sample result) so you can see whether being pickier helped."""
    from .profiles import PROFILES
    rows = []
    for name, p in PROFILES.items():
        o = collect(candles, spread, 320, len(candles), horizon, 320, step, p.scan)["CONSENSUS"]
        cut = int(len(o) * 0.5); is_, oos = _stats([r[1] for r in o[:cut]]), _stats([r[1] for r in o[cut:]])
        rows.append({"profile": name, "signals": len(o), "oos_n": oos["n"], "oos_mean_R": round(oos["mean_R"], 3), "oos_hit": round(oos["hit"], 3), "oos_hit_ci": [round(v, 3) for v in oos["hit_ci"]],
                     "is_mean_R": round(is_["mean_R"], 3), "verdict": verdict_for(is_, oos, 60)})
        if progress: progress(name)
    return {"rows": rows, "note": "Out-of-sample = second half of signals. A stricter profile is only better if its OOS result is better AND the sample is big enough. Past results are not guarantees."}
