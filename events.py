"""Economic calendar loader. No live calendar is bundled: supply a CSV/JSON export (time_utc,currency,impact,name)."""
from __future__ import annotations
import csv, json
from typing import List, Tuple


def load_events(path: str, min_impact: str = "high") -> List[Tuple[str, str, str]]:
    rank = {"low": 1, "medium": 2, "high": 3}
    out = []
    if path.endswith(".json"):
        with open(path) as f: rows = json.load(f)
    else:
        with open(path, newline="") as f: rows = list(csv.DictReader(f))
    for r in rows:
        imp = str(r.get("impact", "high")).lower()
        if rank.get(imp, 3) >= rank[min_impact]:
            out.append((r["time_utc"], r["currency"].upper(), r.get("name", "event")))
    return out


def parse_events_csv(text: str, max_rows: int = 2000):
    """Strict parser for admin uploads: header time_utc,currency,impact,name. Returns (rows, errors)."""
    import io
    from datetime import datetime
    rows, errs = [], []
    rd = csv.DictReader(io.StringIO(text))
    if not rd.fieldnames or not {"time_utc", "currency", "impact", "name"} <= {f.strip() for f in rd.fieldnames}: return [], ["header must be: time_utc,currency,impact,name"]
    for n, r in enumerate(rd, 2):
        if len(rows) >= max_rows: errs.append(f"more than {max_rows} rows"); break
        try:
            t = datetime.fromisoformat(r["time_utc"].strip().replace("Z", "+00:00"))
            if t.tzinfo is None: raise ValueError("timezone required")
            cur, imp, name = r["currency"].strip().upper(), r["impact"].strip().lower(), r["name"].strip()[:80]
            if not (len(cur) == 3 and cur.isalpha()) or imp not in ("low", "medium", "high") or not name: raise ValueError("bad currency/impact/name")
            rows.append((t.astimezone().isoformat() if False else r["time_utc"].strip(), cur, imp, name))
        except Exception as e: errs.append(f"line {n}: {e}")
    return rows, errs
