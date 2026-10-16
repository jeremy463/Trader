"""Test market-data directory, generated on the fly (nothing synthetic ships with the product)."""
import atexit, os, shutil, sys, tempfile
from tests.synth import synthetic

SPECS = [("EURUSD", 21, 1.10, 0.0004), ("GBPUSD", 4, 1.27, 0.0004), ("USDJPY", 31, 150.0, 0.05), ("AUDUSD", 32, 0.66, 0.0003), ("USDCAD", 33, 1.36, 0.0004),
         ("NZDUSD", 34, 0.60, 0.0003), ("USDCHF", 35, 0.88, 0.0003), ("EURJPY", 36, 163.0, 0.06), ("GBPJPY", 37, 190.0, 0.08)]


def write_dir(path: str, n: int = 2500) -> str:
    os.makedirs(path, exist_ok=True)
    for sym, seed, start, vol in SPECS:
        with open(os.path.join(path, f"{sym}_M15.csv"), "w") as f:
            f.write("time,open,high,low,close,volume\n")
            for c in synthetic(n, seed=seed, start=start, vol=vol): f.write(f"{c.ts.isoformat()},{round(c.o, 5)},{round(c.h, 5)},{round(c.l, 5)},{round(c.c, 5)},{c.v}\n")
    return path


TESTDATA = write_dir(tempfile.mkdtemp(prefix="apex-testdata-"))
atexit.register(shutil.rmtree, TESTDATA, ignore_errors=True)
DEMO = TESTDATA   # legacy alias used by older tests

if __name__ == "__main__":   # python -m tests.fixtures OUTDIR  -> writes the files for manual UI smoke runs
    print(write_dir(sys.argv[1]) if len(sys.argv) > 1 else TESTDATA)
