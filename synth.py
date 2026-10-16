"""Deterministic SYNTHETIC candles for tests ONLY. This module is not part of the product package and must never feed a customer-facing path."""
import random
from datetime import datetime, timedelta, timezone
from typing import List
from apex.data import Candle


def synthetic(n=3000, seed=7, start=1.1000, tf_seconds=900, vol=0.0004) -> List[Candle]:
    """SYNTHETIC random-regime data for demos/tests ONLY - never real market data."""
    import random
    rnd = random.Random(seed)
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    p, drift, out = start, 0.0, []
    for i in range(n):
        if i % 300 == 0: drift = rnd.choice([-1, 0, 1]) * vol * 0.12
        o = p
        c = o + drift + rnd.gauss(0, vol)
        h = max(o, c) + abs(rnd.gauss(0, vol * 0.5))
        l = min(o, c) - abs(rnd.gauss(0, vol * 0.5))
        out.append(Candle(t, round(o, 5), round(h, 5), round(l, 5), round(c, 5), rnd.randint(100, 1000)))
        p = c
        t += timedelta(seconds=tf_seconds)
    return out
