"""Tamper-evident audit journal (spec 20): JSONL with hash chain."""
from __future__ import annotations
import hashlib, json, os
from datetime import datetime, timezone


class Journal:
    def __init__(self, path: str = "apex_journal.jsonl"):
        self.path, self.prev = path, "GENESIS"
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    if line.strip(): self.prev = json.loads(line)["hash"]

    def log(self, event: str, **fields) -> dict:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields, "prev": self.prev}
        rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()
        with open(self.path, "a") as f: f.write(json.dumps(rec, default=str) + "\n")
        self.prev = rec["hash"]
        return rec

    @staticmethod
    def verify(path: str) -> bool:
        prev = "GENESIS"
        with open(path) as f:
            for line in f:
                if not line.strip(): continue
                r = json.loads(line); h = r.pop("hash")
                if r["prev"] != prev or hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest() != h:
                    return False
                prev = h
        return True
