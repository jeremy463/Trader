"""SQLite (WAL) with per-thread connections, parameterised SQL only, explicit transactions, hash-chained audit log."""
from __future__ import annotations
import contextlib, hashlib, json, os, sqlite3, threading, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','user')) DEFAULT 'user',
  tier TEXT NOT NULL DEFAULT 'free',
  paid_until INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK(status IN ('active','suspended')) DEFAULT 'active',
  totp_secret TEXT, totp_enabled INTEGER NOT NULL DEFAULT 0, totp_last_step INTEGER NOT NULL DEFAULT 0,
  equity REAL NOT NULL DEFAULT 1000, risk_pct REAL NOT NULL DEFAULT 0.25,
  email_verified INTEGER NOT NULL DEFAULT 1, profile TEXT NOT NULL DEFAULT 'balanced', terms_ts INTEGER NOT NULL DEFAULT 0,
  created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created INTEGER NOT NULL, last_seen INTEGER NOT NULL, expires INTEGER NOT NULL, csrf TEXT NOT NULL, ip TEXT, mfa_ok INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS sess_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS payments(
  reference TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), tier TEXT NOT NULL,
  amount_kobo INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL, paid_at INTEGER, source TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS pay_user ON payments(user_id);
CREATE TABLE IF NOT EXISTS usage(user_id INTEGER NOT NULL, day TEXT NOT NULL, scans INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS login_fail(key TEXT PRIMARY KEY, n INTEGER NOT NULL, locked_until INTEGER NOT NULL DEFAULT 0, last INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reset_tokens(hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, time_utc TEXT NOT NULL, currency TEXT NOT NULL, impact TEXT NOT NULL, name TEXT NOT NULL,
  forecast TEXT NOT NULL DEFAULT '', previous TEXT NOT NULL DEFAULT '', actual TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual');
CREATE TABLE IF NOT EXISTS email_tokens(hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, purpose TEXT NOT NULL, expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, symbol TEXT NOT NULL, tf INTEGER NOT NULL,
  min_grade TEXT NOT NULL DEFAULT 'A', active INTEGER NOT NULL DEFAULT 1, last_candle INTEGER NOT NULL DEFAULT 0, last_dir TEXT NOT NULL DEFAULT '', last_sent INTEGER NOT NULL DEFAULT 0,
  created INTEGER NOT NULL, UNIQUE(user_id, symbol, tf));
CREATE TABLE IF NOT EXISTS news_items(id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, guid TEXT NOT NULL, title TEXT NOT NULL, link TEXT NOT NULL DEFAULT '',
  published INTEGER NOT NULL, currencies TEXT NOT NULL DEFAULT '', sentiment REAL NOT NULL DEFAULT 0, fetched INTEGER NOT NULL, UNIQUE(source, guid));
CREATE INDEX IF NOT EXISTS news_pub ON news_items(published);
CREATE TABLE IF NOT EXISTS feed_status(source TEXT PRIMARY KEY, last_ok INTEGER NOT NULL DEFAULT 0, last_try INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', items INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS watchlist(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, symbol TEXT NOT NULL, PRIMARY KEY(user_id, symbol));
CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, ts INTEGER NOT NULL, symbol TEXT NOT NULL,
  side TEXT NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, exit REAL, grade TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS journal_user ON journal(user_id);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail TEXT, prev TEXT NOT NULL, hash TEXT NOT NULL);
"""


class DB:
    def __init__(self, path: str):
        self.path, self._local = path, threading.local()
        first = not os.path.exists(path)
        c = self.conn(); c.executescript(SCHEMA); self._migrate(c)
        if first:
            try: os.chmod(path, 0o600)
            except OSError: pass

    SCHEMA_VERSION = 4

    def _migrate(self, c):
        """Forward-only, idempotent migrations for databases created by older versions (legacy users are treated as verified)."""
        def ensure(table, col, ddl):
            if col not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}: c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        for col in ("forecast", "previous", "actual"): ensure("events", col, "TEXT NOT NULL DEFAULT ''")
        ensure("events", "source", "TEXT NOT NULL DEFAULT 'manual'")
        ensure("users", "email_verified", "INTEGER NOT NULL DEFAULT 1"); ensure("users", "profile", "TEXT NOT NULL DEFAULT 'balanced'"); ensure("users", "terms_ts", "INTEGER NOT NULL DEFAULT 0")
        c.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    def backup(self, dest: str) -> None:
        """Consistent online backup (safe while the server runs); result is chmod 600."""
        dst = sqlite3.connect(dest)
        try: self.conn().backup(dst)
        finally: dst.close()
        try: os.chmod(dest, 0o600)
        except OSError: pass

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, timeout=10)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=8000")
            self._local.c = c
        return c

    @contextlib.contextmanager
    def tx(self):
        c = self.conn(); c.execute("BEGIN IMMEDIATE")
        try:
            yield c; c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK"); raise

    def one(self, sql, args=()): return self.conn().execute(sql, args).fetchone()
    def all(self, sql, args=()): return self.conn().execute(sql, args).fetchall()


def audit(c: sqlite3.Connection, actor: str, action: str, target: str = "", detail=None) -> None:
    """Append to the tamper-evident chain. Call inside a transaction."""
    prev = c.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    prev = prev["hash"] if prev else "GENESIS"
    ts, d = int(time.time()), json.dumps(detail, default=str, sort_keys=True) if detail is not None else ""
    h = hashlib.sha256(f"{prev}|{ts}|{actor}|{action}|{target}|{d}".encode()).hexdigest()
    c.execute("INSERT INTO audit(ts,actor,action,target,detail,prev,hash) VALUES(?,?,?,?,?,?,?)", (ts, actor, action, target, d, prev, h))


def verify_audit(db: DB) -> bool:
    prev = "GENESIS"
    for r in db.all("SELECT * FROM audit ORDER BY id"):
        h = hashlib.sha256(f"{prev}|{r['ts']}|{r['actor']}|{r['action']}|{r['target']}|{r['detail'] or ''}".encode()).hexdigest()
        if r["prev"] != prev or r["hash"] != h: return False
        prev = h
    return True
