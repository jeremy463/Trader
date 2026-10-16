"""Business logic: accounts, sessions, tier gating, usage quotas, scanning, admin operations."""
from __future__ import annotations
import hashlib, logging, os, sqlite3, threading, time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import dataclasses, math
from ..config import ExecConfig, Mode, RiskConfig, EventPolicy
from ..events import parse_events_csv
from ..profiles import PROFILES, DEFAULT_PROFILE
from .. import quality, news as N, timing as T
from ..edge import wilson
from .mailer import Mailer
from ..config import EventPolicy as _EP
from ..data import Quote, load_csv, Candle
from ..edge import disabled_from, load as load_edge, evaluate_methods, weights_from, evidence_for_consensus, walk_forward, compare_profiles
from .. import indicators as ind
from ..engine import analyze
from ..instruments import get_spec
from ..report import format_report
from ..scanner import METHODS, ScanConfig, METHOD_COUNT
from ..app import q2a_for
from . import security as sec
from .billing import Billing, DEFAULT_TIERS, FREE_METHODS, RANK, effective_tier
from .db import DB, audit, verify_audit
from .webconfig import WebSettings

log = logging.getLogger("apex.web")
TFS = (5, 15, 30, 60, 240)
ALL_METHODS = [m[0] for m in METHODS]


def _ts(iso: str) -> float:
    try: return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError: return 0.0


class ApiError(Exception):
    def __init__(self, code: int, msg: str, extra: Optional[dict] = None):
        super().__init__(msg); self.code, self.msg, self.extra = code, msg, extra or {}


class MarketData:
    """Shared READ-ONLY market data for all users (no execution, no credentials exposed)."""
    def __init__(self, s: WebSettings, feed=None, ttl: int = 10):
        self.s, self.feed, self.ttl, self._c, self._files, self.lock = s, feed, ttl, {}, {}, threading.Lock()

    def symbols(self) -> List[str]:
        if self.s.feed_type != "csv": return list(self.s.feed_symbols)
        try: return sorted({f.split("_M")[0] for f in os.listdir(self.s.data_dir) if f.endswith(".csv") and "_M" in f})
        except OSError: return []

    def _csv(self, sym, tf):
        p = os.path.join(self.s.data_dir, f"{sym}_M{tf}.csv"); m = os.path.getmtime(p)
        k = self._files.get(p)
        if not k or k[0] != m: self._files[p] = (m, load_csv(p))
        return self._files[p][1]

    def _need_feed(self):
        if self.feed is None and self.s.feed_type != "csv":
            env = {"oanda": self.s.oanda_token_env, "twelvedata": self.s.twelvedata_key_env, "mt5": "the MetaTrader 5 terminal login"}.get(self.s.feed_type, "credentials")
            raise RuntimeError(f"no {self.s.feed_type} market-data provider available: check {env} (and that the provider is reachable) then restart")

    def get(self, sym: str, tf: int, count: int = 600):
        self._need_feed()
        if self.s.feed_type == "csv":
            cs = self._csv(sym, tf)
            q = Quote(cs[-1].c - get_spec(sym).pip_size / 2, cs[-1].c + get_spec(sym).pip_size / 2, cs[-1].ts, "simulated-csv")
            return cs[-count:], q, cs[-1].ts, "TEST DATA (your own csv files, not live market data)"
        key = (sym, tf); now = time.time()
        with self.lock:
            hit = self._c.get(key)
            if hit and now - hit[0] < self.ttl: return hit[1]
            cs = self.feed.candles(sym, tf * 60, count); out = (cs, self.feed.quote(sym), self.feed.now(), getattr(self.feed, "label", self.s.feed_type) + " (live)")
            self._c[key] = (now, out); return out

    def history(self, sym: str, tf: int, n: int = 2500) -> List[Candle]:
        self._need_feed()
        return self._csv(sym, tf)[-n:] if self.s.feed_type == "csv" else self.feed.candles(sym, tf * 60, n)


class Service:
    def __init__(self, s: WebSettings, db: DB, billing: Billing, md: MarketData, mailer: Optional[Mailer] = None):
        self.s, self.db, self.billing, self.md = s, db, billing, md
        self.mailer = mailer or Mailer(); self._policy = (0.0, None)
        self.news_sources = s.news_sources or N.DEFAULT_SOURCES; self.fetcher = N.fetch; self._hp = {}; self._radar_cache = {}
        self.rl = sec.RateLimiter(); self.trading = None
        self._edge_lock, self._edge_cache = threading.Lock(), {}
        self.tiers = billing.tiers

    # ---------------- accounts ----------------
    def register(self, email: str, pw: str, ip: str, accept_terms: bool = True) -> int:
        email = (email or "").strip()
        if not self.rl.allow("reg:" + ip, 5, 3600): raise ApiError(429, "too many registrations from this address")
        if accept_terms is not True: raise ApiError(400, "you must accept the terms and risk disclosure")
        if not sec.valid_email(email): raise ApiError(400, "invalid email")
        prob = sec.password_problem(pw, email)
        if prob: raise ApiError(400, prob)
        h = sec.hash_password(pw)
        try:
            with self.db.tx() as c:
                cur = c.execute("INSERT INTO users(email,pw_hash,created,email_verified,terms_ts) VALUES(?,?,?,?,?)", (email, h, int(time.time()), 0 if self.mailer.enabled else 1, int(time.time())))
                audit(c, "system", "register", str(cur.lastrowid)); uid = cur.lastrowid
        except sqlite3.IntegrityError: raise ApiError(409, "could not register with that email")
        if self.mailer.enabled: self._send_verification(uid, email)
        return uid

    # ---- email flows ----
    def _issue_token(self, c, uid: int, purpose: str, ttl: int) -> str:
        tok = sec.new_token(); c.execute("INSERT INTO email_tokens(hash,user_id,purpose,expires) VALUES(?,?,?,?)", (sec.token_hash(tok), uid, purpose, int(time.time()) + ttl)); return tok

    def _send_verification(self, uid: int, email: str):
        with self.db.tx() as c: tok = self._issue_token(c, uid, "verify", 86400)
        self.mailer.send_async(email, f"Confirm your {self.s.brand_name} email", f"Welcome to {self.s.brand_name}.\n\nConfirm your email (valid 24 hours):\n{self.s.base_url}/?verify={tok}\n\nIf you did not sign up, ignore this message.")

    def verify_email(self, token: str, ip: str) -> None:
        if not self.rl.allow("verify:" + ip, 20, 3600): raise ApiError(429, "too many attempts")
        h, now = sec.token_hash(token or ""), int(time.time())
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM email_tokens WHERE hash=? AND purpose='verify'", (h,)).fetchone()
            if not r or r["used"] or r["expires"] < now: raise ApiError(400, "invalid or expired link")
            c.execute("UPDATE email_tokens SET used=1 WHERE hash=?", (h,)); c.execute("UPDATE users SET email_verified=1 WHERE id=?", (r["user_id"],)); audit(c, "email-link", "email_verified", str(r["user_id"]))

    def resend_verification(self, u):
        if u["email_verified"]: return
        if not self.mailer.enabled: raise ApiError(503, "email delivery is not configured")
        if not self.rl.allow(f"resend:{u['id']}", 3, 3600): raise ApiError(429, "too many requests")
        self._send_verification(u["id"], u["email"])

    def forgot(self, email: str, ip: str) -> None:
        """Always succeeds from the caller's view (no account enumeration)."""
        email = (email or "").strip()
        if not self.rl.allow("forgot-ip:" + ip, 5, 3600) or not self.rl.allow("forgot:" + self._fail_key(email), 3, 3600): raise ApiError(429, "too many requests, try again later")
        if not self.mailer.enabled: raise ApiError(503, "email delivery is not configured - ask the admin for a reset token")
        u = self.db.one("SELECT * FROM users WHERE email=? AND status='active'", (email,)) if sec.valid_email(email) else None
        if u:
            tok = sec.new_token()
            with self.db.tx() as c: c.execute("INSERT INTO reset_tokens(hash,user_id,expires) VALUES(?,?,?)", (sec.token_hash(tok), u["id"], int(time.time()) + 3600))
            self.mailer.send_async(u["email"], f"Reset your {self.s.brand_name} password", f"Use this link within 1 hour to set a new password:\n{self.s.base_url}/?reset={tok}\n\nIf you did not request this, ignore this message; your password is unchanged.")

    def _fail_key(self, email): return hashlib.sha256(email.strip().lower().encode()).hexdigest()

    def _record_fail(self, key: str):
        now = int(time.time())
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM login_fail WHERE key=?", (key,)).fetchone()
            n = 1 if (not r or now - r["last"] > 3600) else r["n"] + 1
            lock = now + min(900, 30 * 2 ** (n - 5)) if n >= 5 else 0
            c.execute("INSERT INTO login_fail(key,n,locked_until,last) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET n=?,locked_until=?,last=?", (key, n, lock, now, n, lock, now))

    def login(self, email: str, pw: str, totp: Optional[str], ip: str) -> dict:
        email = (email or "").strip()
        if not self.rl.allow("login-ip:" + ip, 20, 300): raise ApiError(429, "too many attempts, slow down")
        if not isinstance(pw, str) or len(pw) > 128 or not isinstance(email, str) or len(email) > 254: raise ApiError(400, "invalid request")
        key, now = self._fail_key(email), int(time.time())
        f = self.db.one("SELECT * FROM login_fail WHERE key=?", (key,))
        if f and f["locked_until"] > now: raise ApiError(429, "too many failed attempts; try again later", {"retry_after": f["locked_until"] - now})
        u = self.db.one("SELECT * FROM users WHERE email=?", (email,))
        ok = sec.verify_password(pw, u["pw_hash"] if u else sec.DUMMY_HASH) and u is not None   # same CPU cost either way
        if not ok:
            self._record_fail(key); raise ApiError(401, "invalid email or password")
        if u["status"] != "active": raise ApiError(403, "account suspended; contact support")
        mfa = 0
        if u["totp_enabled"]:
            if not totp: return {"need_totp": True}
            step = sec.totp_verify(u["totp_secret"], str(totp), u["totp_last_step"])
            if step is None:
                self._record_fail(key); raise ApiError(401, "invalid email, password or code")
            with self.db.tx() as c: c.execute("UPDATE users SET totp_last_step=? WHERE id=?", (step, u["id"]))
            mfa = 1
        elif u["role"] == "admin" and self.s.production: raise ApiError(403, "admin 2FA is not enabled; run create-admin")
        with self.db.tx() as c:
            c.execute("DELETE FROM login_fail WHERE key=?", (key,))
            tok, csrf = self._new_session(c, u, ip, mfa)
            audit(c, u["email"], "login", str(u["id"]), {"ip": ip})
        return {"token": tok, "csrf": csrf, "role": u["role"]}

    def _new_session(self, c, u, ip, mfa):
        tok, csrf, now = sec.new_token(), sec.new_token(18), int(time.time())
        ttl = (self.s.session_hours_admin if u["role"] == "admin" else self.s.session_hours_user) * 3600
        c.execute("INSERT INTO sessions(id,user_id,created,last_seen,expires,csrf,ip,mfa_ok) VALUES(?,?,?,?,?,?,?,?)", (sec.token_hash(tok), u["id"], now, now, now + ttl, csrf, ip, mfa))
        return tok, csrf

    def session(self, token: Optional[str]):
        if not token or len(token) > 100: return None
        h, now = sec.token_hash(token), int(time.time())
        r = self.db.one("SELECT s.*, u.role AS role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?", (h,))
        if not r: return None
        idle = (self.s.idle_minutes_admin if r["role"] == "admin" else self.s.idle_minutes_user) * 60
        if r["expires"] <= now or now - r["last_seen"] > idle:
            with self.db.tx() as c: c.execute("DELETE FROM sessions WHERE id=?", (h,))
            return None
        u = self.db.one("SELECT * FROM users WHERE id=?", (r["user_id"],))
        if not u or u["status"] != "active": return None
        if u["role"] == "admin" and u["totp_enabled"] and not r["mfa_ok"]: return None
        if now - r["last_seen"] > 60:
            with self.db.tx() as c: c.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now, h))
        return u, r

    def logout(self, token: str):
        with self.db.tx() as c: c.execute("DELETE FROM sessions WHERE id=?", (sec.token_hash(token),))

    def change_password(self, u, old: str, new: str, keep_token: str):
        if not sec.verify_password(old, u["pw_hash"]): raise ApiError(403, "current password is incorrect")
        prob = sec.password_problem(new, u["email"])
        if prob: raise ApiError(400, prob)
        with self.db.tx() as c:
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (sec.hash_password(new), u["id"]))
            c.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (u["id"], sec.token_hash(keep_token)))
            audit(c, u["email"], "password_changed", str(u["id"]))

    def reset_password(self, token: str, new: str, ip: str):
        if not self.rl.allow("reset:" + ip, 10, 3600): raise ApiError(429, "too many attempts")
        r = self.db.one("SELECT * FROM reset_tokens WHERE hash=?", (sec.token_hash(token or ""),))
        if not r or r["used"] or r["expires"] < time.time(): raise ApiError(400, "invalid or expired reset token")
        u = self.db.one("SELECT * FROM users WHERE id=?", (r["user_id"],))
        prob = sec.password_problem(new, u["email"])
        if prob: raise ApiError(400, prob)
        with self.db.tx() as c:
            c.execute("UPDATE reset_tokens SET used=1 WHERE hash=?", (r["hash"],))
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (sec.hash_password(new), u["id"]))
            c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
            c.execute("DELETE FROM login_fail WHERE key=?", (self._fail_key(u["email"]),))
            audit(c, "reset-token", "password_reset", str(u["id"]))

    def set_risk(self, u, equity, risk_pct, profile=None):
        try: equity, risk_pct = float(equity), float(risk_pct)
        except (TypeError, ValueError): raise ApiError(400, "invalid numbers")
        if not (100 <= equity <= 10_000_000) or not (0.05 <= risk_pct <= 2.0): raise ApiError(400, "equity must be 100..10,000,000 and risk 0.05..2.0 %")
        if profile is not None:
            if profile not in PROFILES: raise ApiError(400, "unknown profile")
            if profile != DEFAULT_PROFILE and not self.limits(u)["plan"]: raise ApiError(402, "selectivity profiles require Pro", {"upgrade": True})
        with self.db.tx() as c: c.execute("UPDATE users SET equity=?, risk_pct=?, profile=COALESCE(?, profile) WHERE id=?", (equity, risk_pct, profile, u["id"]))

    def delete_account(self, u, pw: str):
        if u["role"] == "admin": raise ApiError(403, "admin accounts are removed via CLI")
        if not sec.verify_password(pw, u["pw_hash"]): raise ApiError(403, "password incorrect")
        with self.db.tx() as c:   # keep payment rows for accounting; remove identity
            c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
            c.execute("UPDATE users SET email=?, pw_hash=?, status='suspended', totp_secret=NULL, tier='free', paid_until=0 WHERE id=?",
                      (f"deleted+{u['id']}@invalid.local", sec.hash_password(sec.new_token()), u["id"]))
            audit(c, u["email"], "account_deleted", str(u["id"]))

    # ---------------- entitlements & usage ----------------
    def tier_of(self, u) -> str: return effective_tier(u, int(time.time()), self.tiers)

    def limits(self, u) -> dict:
        t = self.tier_of(u)
        if t == "admin": return dict(name="admin", label="Admin", scans=10 ** 6, all_methods=True, plan=True, edge=True, upload=True, symbols=None, alerts=20)
        x = self.tiers[t]; return dict(name=x.name, label=x.label, scans=x.scans_per_day, all_methods=x.all_methods, plan=x.plan, edge=x.edge, upload=x.upload, symbols=x.symbols, alerts=x.alerts)

    def used_today(self, u) -> int:
        r = self.db.one("SELECT scans FROM usage WHERE user_id=? AND day=?", (u["id"], datetime.now(timezone.utc).date().isoformat()))
        return r["scans"] if r else 0

    def _consume(self, u, limit: int, cost: int = 1):
        """Atomically spend `cost` scans from today's allowance (refuses when fewer than `cost` remain)."""
        day = datetime.now(timezone.utc).date().isoformat()
        with self.db.tx() as c:
            r = c.execute("SELECT scans FROM usage WHERE user_id=? AND day=?", (u["id"], day)).fetchone()
            if (r["scans"] if r else 0) + cost > limit: raise ApiError(402, "daily scan limit reached - upgrade for more" if cost == 1 else f"not enough scans left today ({cost} needed for this action)", {"upgrade": True})
            c.execute("INSERT INTO usage(user_id,day,scans) VALUES(?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET scans=scans+?", (u["id"], day, cost, cost))

    def me(self, u, sess) -> dict:
        L = self.limits(u)
        return {"email": u["email"], "role": u["role"], "tier": L["name"], "tier_label": L["label"], "stored_tier": u["tier"], "paid_until": u["paid_until"],
                "scans_used": self.used_today(u), "scans_limit": L["scans"] if u["role"] != "admin" else None, "features": {k: L[k] for k in ("all_methods", "plan", "edge", "upload")},
                "equity": u["equity"], "risk_pct": u["risk_pct"], "csrf": sess["csrf"], "methods_total": METHOD_COUNT, "methods_free": len(FREE_METHODS), "email_verified": bool(u["email_verified"]), "mail": self.mailer.enabled, "profile": u["profile"] if L["plan"] else DEFAULT_PROFILE,
                "profiles": [{"name": p.name, "label": p.label, "desc": p.desc} for p in PROFILES.values()], "alerts_limit": L["alerts"], "news": self.s.news_enabled, "radar": bool(L["plan"]), "journal_limit": self.JOURNAL_LIMIT[self.tier_of(u)], "watch_limit": self.s.watchlist_limits.get("admin" if u["role"] == "admin" else self.tier_of(u), 1), "billing": self.billing.enabled, "symbols": self.available_symbols(u),
                "tiers": [{"name": t.name, "label": t.label, "price": t.price_kobo / 100, "currency": self.billing.currency, "scans": t.scans_per_day,
                           "all_methods": t.all_methods, "plan": t.plan, "edge": t.edge, "upload": t.upload, "alerts": t.alerts} for t in self.tiers.values()], "trading": self.trading is not None and u["role"] == "admin"}

    def available_symbols(self, u) -> List[str]:
        L = self.limits(u); av = self.md.symbols()
        return [s for s in av if L["symbols"] is None or s in L["symbols"]]

    # ---------------- scanning ----------------
    def event_policy(self):
        """High-impact calendar from the DB (admin-uploaded). Cached 60s. None = no calendar loaded."""
        ts, pol = self._policy
        if time.time() - ts < 60: return pol
        cut = (datetime.now(timezone.utc).timestamp() - 2 * 86400)
        rows = [(r["time_utc"], r["currency"], r["name"]) for r in self.db.all("SELECT * FROM events WHERE impact='high'")]
        rows = [r for r in rows if _ts(r[0]) > cut]
        pol = EventPolicy(events=rows, block_weekend=False) if rows else None
        self._policy = (time.time(), pol); return pol

    def scan(self, u, symbol: str, tf: int) -> dict:
        L = self.limits(u)
        if not isinstance(symbol, str) or symbol not in self.available_symbols(u):
            raise ApiError(403, "symbol not available on your plan" if symbol in self.md.symbols() else "unknown symbol")
        if tf not in TFS: raise ApiError(400, "unsupported timeframe")
        if not self.rl.allow(f"scan:{u['id']}", 30, 60): raise ApiError(429, "slow down")
        self._consume(u, L["scans"])
        try: cs, quote, now, source = self.md.get(symbol, tf)
        except Exception: log.exception("market data failure"); raise ApiError(503, "market data temporarily unavailable")
        return self._build(u, L, symbol, tf, cs, quote, now, source)[1]

    def _build(self, u, L, symbol, tf, cs, quote, now, source):
        """Shared by interactive scans and the alert worker. Returns (Analysis, response dict)."""
        spec = get_spec(symbol)
        prof = PROFILES.get(u["profile"], PROFILES[DEFAULT_PROFILE]) if L["plan"] else PROFILES[DEFAULT_PROFILE]
        rc = RiskConfig(equity=u["equity"], risk_per_trade_pct=min(u["risk_pct"], 2.0), max_daily_loss_pct=100, max_weekly_loss_pct=100, max_drawdown_pct=100,
                        max_total_open_risk_pct=5, max_leverage=500, max_position_size=1000, min_free_margin_pct=0, max_spread_atr_frac=1.0, min_net_rr=prof.min_net_rr)
        locked = [] if L["all_methods"] else [m for m in ALL_METHODS if m not in FREE_METHODS]
        estats = load_edge(self.s.edge_path); edge_off = disabled_from(estats); wts = weights_from(estats)
        scfg = prof.scan if L["all_methods"] else ScanConfig(min_methods=4, min_families=3)
        try: q2a, sizing_ok = q2a_for(spec, "USD", cs[-1].c), True
        except RuntimeError: q2a, sizing_ok = 1.0, False
        a = analyze(symbol, cs, tf * 60, rc, ExecConfig(mode=Mode.ANALYSIS_ONLY, allowed_instruments=[symbol], allowed_sessions_utc=[(0, 24)]), None, quote, now, self.event_policy(), q2a, 0.0,
                    None, enforce_session=False, scan_cfg=scfg, disabled_methods=list(set(locked) | set(edge_off)), allow_simulated_quote=True, spec=spec, method_weights=wts, market_closed=not T.market_open(now, spec.asset_class == "futures"))
        out = self._present(a, L, source, locked, sizing_ok)
        ev = evidence_for_consensus(estats) if L["plan"] else None
        out["evidence"], out["chart"], out["tf"], out["profile"] = ev, self._chart(cs), tf, prof.name
        g = quality.add_evidence(a.grade, ev) if L["plan"] else None
        curs = self.currencies_of(spec); ts_now = now.timestamp(); sg = 1 if a.scan.consensus == "BUY" else -1
        sents = N.currency_sentiment(self.news_rows(curs, ts_now, 24), ts_now); bias = N.pair_bias(spec, sents)
        if g is not None and a.scan.consensus: g = quality.add_item(g, quality.news_item(bias, sg))
        a.grade = g; out["grade"] = g
        evs = self.events_for(curs, now, 8, 48)
        prof_h = self.hour_profile(symbol, tf)
        out["timing"] = T.plan_timing(now, curs, spec.asset_class == "futures", evs, a.scan.consensus, (g or {}).get("grade"), tf, prof_h, any("disorderly" in r for r in a.regime.labels))
        out["timing"]["hour_profile"] = prof_h
        out["news"] = [{k: r[k] for k in ("title", "link", "source", "published", "currencies", "sentiment")} for r in self.news_rows(curs, ts_now, 48)[:8]]
        out["upcoming"] = [e for e in evs if e["impact"] in ("high", "medium")][:10]; out["news_bias"] = bias
        return a, out

    @staticmethod
    def _chart(cs, n: int = 120) -> dict:
        e21, e50 = ind.ema([c.c for c in cs], 21), ind.ema([c.c for c in cs], 50); k = len(cs) - n if len(cs) > n else 0
        r = lambda v: None if v != v else round(v, 6)
        return {"t": [int(c.ts.timestamp()) for c in cs[k:]], "o": [c.o for c in cs[k:]], "h": [c.h for c in cs[k:]], "l": [c.l for c in cs[k:]], "c": [c.c for c in cs[k:]],
                "e21": [r(v) for v in e21[k:]], "e50": [r(v) for v in e50[k:]]}

    def _present(self, a, L, source, locked, sizing_ok) -> dict:
        sc, rows, cnt = a.scan, [], dict(long=0, short=0, neutral=0, na=0, err=0)
        for s in sc.signals:
            lk = s.name in locked
            rows.append({"name": s.name, "family": s.family, "dir": s.direction, "strength": s.strength, "passed": s.passed, "locked": lk,
                         "applicable": s.applicable and not lk, "detail": "locked - upgrade to unlock" if lk else s.detail})
            if lk: continue
            if s.error: cnt["err"] += 1
            elif not s.applicable: cnt["na"] += 1
            elif s.passed: cnt["long" if s.direction > 0 else "short"] += 1
            else: cnt["neutral"] += 1
        scanned = len(rows) - len(locked)
        out = {"symbol": a.symbol, "source": source, "data_ts": a.data_ts.isoformat(), "scanned": scanned, "total_methods": len(rows), "counts": cnt,
               "passed": cnt["long"] + cnt["short"], "families_long": len(sc.fam_long), "families_short": len(sc.fam_short), "consensus": sc.consensus,
               "why_not": sc.why_not[:3], "methods": rows, "decision": a.decision if L["plan"] else (sc.consensus or "NONE"), "classification": a.classification if L["plan"] else None,
               "regime": a.regime.labels, "regime_detail": sc.regime, "plan": None, "report": None, "informational": True}
        if L["plan"] and a.plan and not a.plan.rejected:
            p, z = a.plan, a.size
            out["plan"] = {"side": p.direction, "entry": p.entry, "stop": p.stop.price, "tp1": p.targets.tp1, "tp2": p.targets.tp2, "tp3": p.targets.tp3, "net_rr": p.net_rr[1],
                           "size": z.size if (z and sizing_ok and not z.rejected) else None, "risk_amount": round(z.risk_amount, 2) if (z and sizing_ok and not z.rejected) else None,
                           "sizing_note": None if sizing_ok else "position sizing unavailable for this pair"}
        if L["plan"]: out["report"] = format_report(a)
        if not L["plan"]: out["upgrade_hint"] = f"Upgrade to Pro for all {len(rows)} methods, entry/stop/targets and position sizing."
        return out

    def edge(self, u, symbol: str, tf: int) -> dict:
        L = self.limits(u)
        if not L["edge"]: raise ApiError(402, "Elite plan required", {"upgrade": True})
        if symbol not in self.available_symbols(u) or tf not in TFS: raise ApiError(400, "invalid symbol/timeframe")
        key, now = (symbol, tf), time.time()
        hit = self._edge_cache.get(key)
        if hit and now - hit[0] < 3600: return hit[1]
        if not self._edge_lock.acquire(blocking=False): raise ApiError(429, "an evaluation is already running; retry shortly")
        try:
            cs = self.md.history(symbol, tf)
            if len(cs) < 600: raise ApiError(400, "not enough history for edge evaluation (need 600+ candles)")
            res = evaluate_methods(cs, get_spec(symbol).pip_size, step=8)
            out = {"symbol": symbol, "bars": len(cs), "note": "Out-of-sample forward returns in ATR units after spread. Evidence, not a guarantee.",
                   "rows": [{"method": k, "oos_n": v["out_of_sample"]["n"], "oos_mean_r": round(v["out_of_sample"]["mean_R"], 3), "t": round(v["out_of_sample"]["t"], 2), "verdict": v["verdict"]} for k, v in res.items()]}
            self._edge_cache[key] = (now, out); return out
        finally: self._edge_lock.release()

    def validate(self, u, symbol: str, tf: int) -> dict:
        """Elite: walk-forward validation (methods chosen on training windows only, judged on unseen windows)."""
        L = self.limits(u)
        if not L["edge"]: raise ApiError(402, "Elite plan required", {"upgrade": True})
        if symbol not in self.available_symbols(u) or tf not in TFS: raise ApiError(400, "invalid symbol/timeframe")
        key, now = ("wf", symbol, tf), time.time()
        hit = self._edge_cache.get(key)
        if hit and now - hit[0] < 3600: return hit[1]
        if not self._edge_lock.acquire(blocking=False): raise ApiError(429, "an evaluation is already running; retry shortly")
        try:
            cs = self.md.history(symbol, tf, 4000)
            if len(cs) < 2000: raise ApiError(400, "not enough history for walk-forward (need 2000+ candles)")
            out = walk_forward(cs, get_spec(symbol).pip_size, train=1000, test=400, step=8); out["symbol"], out["bars"] = symbol, len(cs)
            self._edge_cache[key] = (now, out); return out
        finally: self._edge_lock.release()

    # ---------------- profiles comparison (Elite) ----------------
    def compare(self, u, symbol: str, tf: int) -> dict:
        L = self.limits(u)
        if not L["edge"]: raise ApiError(402, "Elite plan required", {"upgrade": True})
        if symbol not in self.available_symbols(u) or tf not in TFS: raise ApiError(400, "invalid symbol/timeframe")
        key, now = ("pc", symbol, tf), time.time(); hit = self._edge_cache.get(key)
        if hit and now - hit[0] < 3600: return hit[1]
        if not self._edge_lock.acquire(blocking=False): raise ApiError(429, "an evaluation is already running; retry shortly")
        try:
            cs = self.md.history(symbol, tf, 3000)
            if len(cs) < 1200: raise ApiError(400, "not enough history (need 1200+ candles)")
            out = compare_profiles(cs, get_spec(symbol).pip_size); out["symbol"] = symbol; self._edge_cache[key] = (now, out); return out
        finally: self._edge_lock.release()

    # ---------------- alerts ----------------
    def _alert_rows(self, u): return [dict(r) for r in self.db.all("SELECT id,symbol,tf,min_grade,active,last_sent FROM alerts WHERE user_id=? ORDER BY id", (u["id"],))]

    def alert_list(self, u) -> dict:
        L = self.limits(u); return {"alerts": self._alert_rows(u), "limit": L["alerts"], "mail": self.mailer.enabled, "verified": bool(u["email_verified"])}

    def alert_add(self, u, symbol: str, tf: int, min_grade: str):
        L = self.limits(u)
        if not L["alerts"]: raise ApiError(402, "email alerts require Pro", {"upgrade": True})
        if not self.mailer.enabled: raise ApiError(503, "email delivery is not configured on this server")
        if not u["email_verified"]: raise ApiError(403, "verify your email address first")
        if symbol not in self.available_symbols(u) or tf not in TFS or min_grade not in ("A", "B"): raise ApiError(400, "invalid alert")
        if self.db.one("SELECT COUNT(*) AS n FROM alerts WHERE user_id=?", (u["id"],))["n"] >= L["alerts"]: raise ApiError(402, f"alert limit reached ({L['alerts']})", {"upgrade": True})
        try:
            with self.db.tx() as c: c.execute("INSERT INTO alerts(user_id,symbol,tf,min_grade,created) VALUES(?,?,?,?,?)", (u["id"], symbol, tf, min_grade, int(time.time())))
        except sqlite3.IntegrityError: raise ApiError(409, "you already have an alert for that symbol and timeframe")

    def alert_delete(self, u, aid: int):
        with self.db.tx() as c: c.execute("DELETE FROM alerts WHERE id=? AND user_id=?", (aid, u["id"]))

    def alert_toggle(self, u, aid: int, active: bool):
        with self.db.tx() as c: c.execute("UPDATE alerts SET active=? WHERE id=? AND user_id=?", (1 if active else 0, aid, u["id"]))

    def alerts_run_once(self, now: Optional[float] = None) -> int:
        """Evaluate every active alert once per new candle; email only when the setup grade meets the user's bar. Returns emails sent."""
        now = now or time.time(); sent, data = 0, {}
        rows = self.db.all("SELECT a.id AS aid, a.symbol, a.tf, a.min_grade, a.last_candle, a.last_dir, a.last_sent, u.* FROM alerts a JOIN users u ON u.id=a.user_id WHERE a.active=1")
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        for r in rows:
            if r["status"] != "active" or not r["email_verified"] or not self.mailer.enabled: continue
            L = self.limits(r)
            if not L["alerts"] or not L["plan"]: continue
            key = (r["symbol"], r["tf"])
            if key not in data:
                try: data[key] = self.md.get(r["symbol"], r["tf"])
                except Exception: log.exception("alert data failure"); data[key] = None
            if data[key] is None: continue
            cs, quote, mnow, source = data[key]; cts = int(cs[-1].ts.timestamp())
            if cts <= r["last_candle"]: continue
            a, out = self._build(r, L, r["symbol"], r["tf"], cs, quote, mnow, source)
            g, fire = out.get("grade"), False
            if g and g["grade"] and quality.GRADE_RANK[g["grade"]] >= quality.GRADE_RANK[r["min_grade"]] and a.scan.consensus and out["plan"]:
                d = a.scan.consensus
                fire = not (d == r["last_dir"] and now - r["last_sent"] < 4 * r["tf"] * 60)
                if fire:
                    used = self.db.one("SELECT scans FROM usage WHERE user_id=? AND day=?", (r["id"], "al:" + day)); n = used["scans"] if used else 0
                    if n >= self.s.alerts_max_per_user_per_day: fire = False
            if fire:
                p = out["plan"]
                body = (f"{self.s.brand_name} alert: {a.scan.consensus} {r['symbol']} ({r['tf']}m) - setup grade {g['grade']} ({g['passed']}/{g['total']} criteria)\n\n"
                        f"Entry ~{p['entry']}  Stop {p['stop']}  TP1 {p['tp1']}  TP2 {p['tp2']}  TP3 {p['tp3']}  net R:R {p['net_rr']}\n"
                        f"Size for your account: {p['size']} lots (risk {p['risk_amount']})\n\nFailed criteria: " + (", ".join(i["label"] for i in g["items"] if i["ok"] is False) or "none") +
                        f"\n\nThis is analysis, not advice or a prediction. Nothing was traded. Review the chart before acting: {self.s.base_url}")
                ok = self.mailer.send(r["email"], f"[{self.s.brand_name}] {a.scan.consensus} {r['symbol']} {r['tf']}m - grade {g['grade']}", body)
                if ok:
                    sent += 1
                    with self.db.tx() as c:
                        c.execute("INSERT INTO usage(user_id,day,scans) VALUES(?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET scans=scans+1", (r["id"], "al:" + day))
                        c.execute("UPDATE alerts SET last_dir=?, last_sent=? WHERE id=?", (a.scan.consensus, int(now), r["aid"]))
            with self.db.tx() as c: c.execute("UPDATE alerts SET last_candle=? WHERE id=?", (cts, r["aid"]))
        return sent

    # ---------------- news, calendar, timing ----------------
    @staticmethod
    def currencies_of(spec) -> List[str]: return [c for c in (spec.base_ccy, spec.quote_ccy) if c] or ["USD"]

    def hour_profile(self, symbol: str, tf: int):
        key, now = (symbol, tf), time.time(); hit = self._hp.get(key)
        if hit and now - hit[0] < 6 * 3600: return hit[1]
        try: prof = T.hour_profile(self.md.history(symbol, tf, 3000), tf * 60)
        except Exception: log.exception("hour profile failed"); prof = None
        self._hp[key] = (now, prof); return prof

    def events_for(self, currs: List[str], now: datetime, back_h: int, ahead_h: int, impacts=("high", "medium", "low", "holiday")) -> List[dict]:
        lo, hi = now.timestamp() - back_h * 3600, now.timestamp() + ahead_h * 3600; out = []
        for r in self.db.all("SELECT * FROM events"):
            t = _ts(r["time_utc"])
            if lo <= t <= hi and r["currency"] in currs and r["impact"] in impacts:
                out.append({"time_utc": r["time_utc"], "currency": r["currency"], "impact": r["impact"], "name": r["name"], "forecast": r["forecast"], "previous": r["previous"], "actual": r["actual"],
                            "minutes": int((t - now.timestamp()) // 60), "surprise": N.surprise(r["name"], r["actual"], r["forecast"])})
        return sorted(out, key=lambda e: e["time_utc"])

    def news_rows(self, currs: List[str], now_ts: float, hours: int) -> List[dict]:
        rows = [dict(r) for r in self.db.all("SELECT * FROM news_items WHERE published>? ORDER BY published DESC LIMIT 400", (int(now_ts - hours * 3600),))]
        return [r for r in rows if any(c in r["currencies"].split(",") for c in currs)]

    def news_sync(self, now: Optional[float] = None) -> dict:
        """Pull every configured source. One failing source never blocks the others; each source's status is recorded."""
        now = int(now or time.time()); res = {}
        for src in self.news_sources:
            name = src["name"]; err, count = "", 0
            try:
                data = self.fetcher(src["url"], 10, self.s.allow_private_feeds)
                if src["type"] == "ff_json":
                    rows = N.parse_ff_json(data, name)
                    with self.db.tx() as c:
                        c.execute("DELETE FROM events WHERE source=?", (name,))
                        c.executemany("INSERT INTO events(time_utc,currency,impact,name,forecast,previous,actual,source) VALUES(?,?,?,?,?,?,?,?)",
                                      [(r["time_utc"], r["currency"], r["impact"], r["name"], r["forecast"], r["previous"], r["actual"], name) for r in rows])
                    count = len(rows); self._policy = (0.0, None)
                else:
                    items = N.parse_feed(data)
                    with self.db.tx() as c:
                        for it in items:
                            curs = N.tag_currencies(it["title"], src.get("currencies"))
                            cur = c.execute("INSERT OR IGNORE INTO news_items(source,guid,title,link,published,currencies,sentiment,fetched) VALUES(?,?,?,?,?,?,?,?)",
                                            (name, it["guid"], it["title"], it["link"], it["published"], ",".join(curs), N.headline_sentiment(it["title"]), now)); count += cur.rowcount
                        c.execute("DELETE FROM news_items WHERE published<?", (now - 14 * 86400,))
            except N.FeedError as e: err = str(e)
            except Exception as e: log.exception("feed sync failed"); err = f"unexpected {type(e).__name__}"
            with self.db.tx() as c:
                c.execute("INSERT INTO feed_status(source,last_ok,last_try,last_error,items) VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET last_try=excluded.last_try, "
                          "last_error=excluded.last_error, last_ok=CASE WHEN excluded.last_error='' THEN excluded.last_try ELSE feed_status.last_ok END, items=CASE WHEN excluded.last_error='' THEN excluded.items ELSE feed_status.items END",
                          (name, now if not err else 0, now, err, count))
            res[name] = {"ok": not err, "items": count, "error": err}
        return res

    def feeds_status(self) -> list:
        st = {r["source"]: dict(r) for r in self.db.all("SELECT * FROM feed_status")}
        return [{"name": s_["name"], "type": s_["type"], "url": s_["url"], **{k: st.get(s_["name"], {}).get(k, v) for k, v in (("last_ok", 0), ("last_try", 0), ("last_error", ""), ("items", 0))}} for s_ in self.news_sources]

    def calendar(self, u, days: int, currs: Optional[List[str]]) -> dict:
        L = self.limits(u); days = max(1, min(int(days), 14 if L["plan"] else 3)); now = datetime.now(timezone.utc)
        cs = [c for c in (currs or []) if c in ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD")] or ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"]
        sents = N.currency_sentiment(self.news_rows(cs, now.timestamp(), 24), now.timestamp())
        return {"events": self.events_for(cs, now, 6, days * 24), "sentiment": sents, "days": days, "now": T.iso(now), "enabled": self.s.news_enabled or bool(self.db.one("SELECT 1 FROM events")), "last_sync": max([f["last_ok"] for f in self.feeds_status()] or [0])}

    def news(self, u, currs: Optional[List[str]], hours: int = 48) -> dict:
        now = time.time(); cs = [c for c in (currs or []) if c in N.CUR_RE] or list(N.CUR_RE)
        rows = self.news_rows(cs, now, max(1, min(int(hours), 96)))[: (40 if self.limits(u)["plan"] else 10)]
        return {"items": [{k: r[k] for k in ("title", "link", "source", "published", "currencies", "sentiment")} for r in rows], "sentiment": N.currency_sentiment(rows, now),
                "note": "Headline tone is a keyword heuristic: context only, low reliability."}

    # ---------------- opportunity radar ----------------
    def watch_get(self, u) -> dict:
        L = self.limits(u); lim = self.s.watchlist_limits.get("admin" if u["role"] == "admin" else self.tier_of(u), 1)
        return {"symbols": [r["symbol"] for r in self.db.all("SELECT symbol FROM watchlist WHERE user_id=? ORDER BY symbol", (u["id"],))], "limit": lim, "available": self.available_symbols(u)}

    def watch_set(self, u, symbols: List[str]):
        lim = self.watch_get(u)["limit"]; av = set(self.available_symbols(u)); symbols = list(dict.fromkeys(symbols))
        if any(s not in av for s in symbols): raise ApiError(400, "unknown or unavailable symbol in watchlist")
        if len(symbols) > lim: raise ApiError(402, f"your plan allows {lim} watchlist symbols", {"upgrade": True})
        with self.db.tx() as c:
            c.execute("DELETE FROM watchlist WHERE user_id=?", (u["id"],)); c.executemany("INSERT INTO watchlist(user_id,symbol) VALUES(?,?)", [(u["id"], s_) for s_ in symbols])

    def radar(self, u, tf: int) -> dict:
        """WHAT to trade: rank the watchlist (or all available symbols) by setup grade, consensus, timing and reward:risk."""
        L = self.limits(u)
        if not L["plan"]: raise ApiError(402, "the opportunity radar requires Pro", {"upgrade": True})
        if tf not in TFS: raise ApiError(400, "unsupported timeframe")
        if not self.rl.allow(f"radar:{u['id']}", 6, 60): raise ApiError(429, "slow down")
        hit = self._radar_cache.get((u["id"], tf))
        if hit and time.time() - hit[0] < 45: return {**hit[1], "cached": True}
        wl = self.watch_get(u); syms = wl["symbols"] or wl["available"][:wl["limit"]]
        self._consume(u, L["scans"], cost=3)
        items = []
        for sym in syms:
            try:
                cs, quote, now, source = self.md.get(sym, tf); a, out = self._build(u, L, sym, tf, cs, quote, now, source)
            except Exception: log.exception("radar failed for %s", sym); continue
            g, p, tm = out.get("grade"), out.get("plan"), out["timing"]
            items.append({"symbol": sym, "tf": tf, "direction": a.scan.consensus, "decision": out["decision"], "grade": (g or {}).get("grade"), "criteria": [(g or {}).get("passed", 0), (g or {}).get("total", 0)],
                          "score": a.score, "passed": out["passed"], "scanned": out["scanned"], "families": [out["families_long"], out["families_short"]], "plan": p, "timing": {k: tm[k] for k in ("status", "headline", "next_window", "signal_valid_until", "next_candle_close", "liquidity")},
                          "news_bias": out["news_bias"], "regime": out["regime"], "source": source, "data_ts": out["data_ts"]})
        rank = lambda i: (quality.GRADE_RANK.get(i["grade"] or "", 0), 1 if i["direction"] else 0, {"GO": 2, "WAIT": 1, "AVOID": 0}[i["timing"]["status"]], (i["plan"] or {}).get("net_rr", 0), i["score"])
        items.sort(key=rank, reverse=True); res = {"generated": T.iso(datetime.now(timezone.utc)), "tf": tf, "items": items, "scanned": len(items), "cached": False,
                                                    "note": "Ranked by grade, consensus, timing and net R:R. A ranking is a to-do list for your own analysis, not a prediction."}
        self._radar_cache[(u["id"], tf)] = (time.time(), res); return res

    # ---------------- trade journal ----------------
    JOURNAL_LIMIT = {"free": 30, "pro": 500, "elite": 2000, "admin": 2000}

    def journal_add(self, u, symbol, side, entry, stop, exit_, grade, notes):
        lim = self.JOURNAL_LIMIT[self.tier_of(u)]
        if symbol not in self.md.symbols() and symbol not in {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURJPY", "GBPJPY"}: raise ApiError(400, "unknown symbol")
        try: entry, stop = float(entry), float(stop); exit_ = None if exit_ in (None, "") else float(exit_)
        except (TypeError, ValueError): raise ApiError(400, "entry, stop and exit must be numbers")
        vals = [entry, stop] + ([exit_] if exit_ is not None else [])
        if side not in ("BUY", "SELL") or any(not math.isfinite(v) or v <= 0 for v in vals): raise ApiError(400, "invalid trade values")
        if (side == "BUY" and not stop < entry) or (side == "SELL" and not stop > entry): raise ApiError(400, "stop must be on the losing side of entry")
        if grade not in ("", "A", "B", "C") or len(notes) > 300: raise ApiError(400, "invalid grade or notes too long")
        if self.db.one("SELECT COUNT(*) AS n FROM journal WHERE user_id=?", (u["id"],))["n"] >= lim: raise ApiError(402, f"journal limit reached ({lim})", {"upgrade": True})
        with self.db.tx() as c: c.execute("INSERT INTO journal(user_id,ts,symbol,side,entry,stop,exit,grade,notes) VALUES(?,?,?,?,?,?,?,?,?)", (u["id"], int(time.time()), symbol, side, entry, stop, exit_, grade, notes))

    def journal_close(self, u, jid: int, exit_):
        try: exit_ = float(exit_)
        except (TypeError, ValueError): raise ApiError(400, "exit must be a number")
        if not math.isfinite(exit_) or exit_ <= 0: raise ApiError(400, "invalid exit")
        with self.db.tx() as c: c.execute("UPDATE journal SET exit=? WHERE id=? AND user_id=?", (exit_, jid, u["id"]))

    def journal_delete(self, u, jid: int):
        with self.db.tx() as c: c.execute("DELETE FROM journal WHERE id=? AND user_id=?", (jid, u["id"]))

    @staticmethod
    def _r(row) -> Optional[float]:
        if row["exit"] is None: return None
        risk = abs(row["entry"] - row["stop"]); return None if risk == 0 else round(((row["exit"] - row["entry"]) if row["side"] == "BUY" else (row["entry"] - row["exit"])) / risk, 3)

    def journal(self, u) -> dict:
        rows = [dict(r) for r in self.db.all("SELECT * FROM journal WHERE user_id=? ORDER BY ts DESC, id DESC", (u["id"],))]
        for r in rows: r["r"] = self._r(r)
        cl = [r["r"] for r in reversed(rows) if r["r"] is not None]; n = len(cl); wins = sum(1 for x in cl if x > 0); losses = [x for x in cl if x <= 0]
        eq, curve, peak, mdd = 0.0, [], 0.0, 0.0
        for x in cl: eq += x; peak = max(peak, eq); mdd = max(mdd, peak - eq); curve.append(round(eq, 2))
        lo, hi = wilson(wins, n)
        st = {"closed": n, "open": len(rows) - n, "win_rate": round(wins / n, 3) if n else None, "win_ci": [round(lo, 3), round(hi, 3)] if n else None, "avg_r": round(sum(cl) / n, 3) if n else None,
              "profit_factor": round(sum(x for x in cl if x > 0) / abs(sum(losses)), 2) if losses and sum(losses) else None, "max_dd_r": round(mdd, 2), "total_r": round(eq, 2), "curve": curve[-200:],
              "note": "Fewer than 30 closed trades says almost nothing about skill or edge." if n < 30 else "Compare with the app's validation results; luck alone can explain a lot over small samples."}
        return {"rows": rows, "stats": st, "limit": self.JOURNAL_LIMIT[self.tier_of(u)]}

    def journal_csv(self, u) -> str:
        def cell(v):
            s_ = "" if v is None else str(v)
            return "'" + s_ if s_[:1] in ("=", "+", "-", "@", "\t", "\r") else s_
        import csv, io
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(["date_utc", "symbol", "side", "entry", "stop", "exit", "r_multiple", "grade", "notes"])
        for r in self.journal(u)["rows"]: w.writerow([cell(datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M")), cell(r["symbol"]), cell(r["side"]), r["entry"], r["stop"], "" if r["exit"] is None else r["exit"], "" if r["r"] is None else r["r"], cell(r["grade"]), cell(r["notes"])])
        return buf.getvalue()

    # ---------------- account: data export and sessions ----------------
    def export_account(self, u) -> dict:
        """Everything we hold about the user (NDPR/GDPR-style access). Never includes password hash, TOTP secret or session tokens."""
        q = lambda sql: [dict(r) for r in self.db.all(sql, (u["id"],))]
        return {"exported_at": T.iso(datetime.now(timezone.utc)), "account": {"email": u["email"], "role": u["role"], "tier": u["tier"], "paid_until": u["paid_until"], "created": u["created"], "terms_accepted_at": u["terms_ts"],
                "email_verified": bool(u["email_verified"]), "equity": u["equity"], "risk_pct": u["risk_pct"], "profile": u["profile"]},
                "watchlist": [r["symbol"] for r in q("SELECT symbol FROM watchlist WHERE user_id=?")], "alerts": q("SELECT symbol,tf,min_grade,active,created FROM alerts WHERE user_id=?"),
                "journal": q("SELECT ts,symbol,side,entry,stop,exit,grade,notes FROM journal WHERE user_id=?"), "payments": q("SELECT reference,tier,amount_kobo,currency,status,created,paid_at FROM payments WHERE user_id=?"),
                "daily_usage": q("SELECT day,scans FROM usage WHERE user_id=? AND day NOT LIKE 'al:%'")}

    def sessions(self, u, token: str) -> list:
        cur = sec.token_hash(token or ""); out = []
        for r in self.db.all("SELECT id,created,last_seen,expires,ip FROM sessions WHERE user_id=? ORDER BY last_seen DESC", (u["id"],)):
            ip = r["ip"] or ""; ip = (".".join(ip.split(".")[:3] + ["x"]) if "." in ip else ip[:12] + "...") if ip else ""
            out.append({"created": r["created"], "last_seen": r["last_seen"], "expires": r["expires"], "ip": ip, "current": r["id"] == cur})
        return out

    def revoke_other_sessions(self, u, token: str) -> int:
        with self.db.tx() as c:
            n = c.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (u["id"], sec.token_hash(token or ""))).rowcount; audit(c, u["email"], "revoke_other_sessions", str(u["id"]), {"n": n}); return n

    def public_config(self) -> dict:
        from ..scanner import FAMILIES
        s = self.s
        return {"mail": self.mailer.enabled, "billing": self.billing.enabled, "brand": s.brand_name, "company": s.company_name, "support_email": s.support_email, "data_mode": s.data_mode,
                "methods": METHOD_COUNT, "families": len(FAMILIES), "legal_updated": s.legal_updated}

    # ---------------- admin: go-live readiness ----------------
    def readiness(self, force: bool = False) -> dict:
        """Live checklist of everything a real launch needs. Cached 60s (provider and API calls are throttled); `force` re-runs it."""
        hit = getattr(self, "_ready", None)
        if hit and not force and time.time() - hit[0] < 60: return hit[1]
        s, rows = self.s, []
        def add(key, label, status, detail): rows.append({"key": key, "label": label, "status": status, "detail": detail})
        add("https", "HTTPS and secure cookies", "ok" if (s.base_url.startswith("https://") and s.secure_cookies) else ("fail" if s.production else "warn"), s.base_url + (" (secure cookies on)" if s.secure_cookies else " (secure cookies OFF)"))
        add("mode", "Production mode", "ok" if s.production else "warn", "production" if s.production else "development: unsafe settings are tolerated; do not take customers like this")
        # market data
        if s.feed_type == "csv": add("data", "Market data provider", "fail" if s.production else "warn", "TEST DATA from csv files: not real market data")
        else:
            try:
                sym = s.feed_symbols[0]; cs, q, now, src = self.md.get(sym, 15); closed = not T.market_open(now); age = (now - cs[-1].ts).total_seconds() / 60
                add("data", "Market data provider", "ok" if (closed or age < 45) else "fail", f"{src}: {sym} last candle {age:.0f} min old" + (" (market closed)" if closed else ""))
            except Exception as e: add("data", "Market data provider", "fail", f"{self.s.feed_type}: {str(e)[:120] or 'not reachable / credentials missing'}")
        add("failover", "Backup data provider", "ok" if s.feed_fallback_type else "warn", s.feed_fallback_type or "none configured: one provider outage stops scans (set feed_fallback_type)")
        # payments
        key = s.paystack_secret()
        if not key: add("paystack", "Paystack secret key", "warn", f"env {s.paystack_secret_env} is not set: checkout is disabled until you add it")
        else:
            mode = "LIVE" if key.startswith("sk_live_") else "TEST" if key.startswith("sk_test_") else "unrecognised"; ok_api, err = False, "client unavailable"
            try:
                if self.billing.client: self.billing.client.balance(); ok_api = True
            except Exception as e: err = str(e)
            add("paystack", "Paystack secret key", ("ok" if mode == "LIVE" else "warn") if ok_api else "fail",
                f"{mode} key; " + ("accepted by Paystack" if ok_api else f"Paystack rejected it or could not be reached ({err})") + ("" if mode == "LIVE" else "; TEST mode moves no real money"))
        add("webhook", "Paystack webhook URL", "warn", f"register {s.base_url}/webhooks/paystack in Paystack Dashboard > Settings > API Keys & Webhooks (test and live are separate); cannot be verified from here")
        add("prices", "Plan prices", "ok" if all(t in s.price_kobo for t in ("pro", "elite")) else ("fail" if s.production else "warn"), ", ".join(f"{t}: {self.billing.tiers[t].price_kobo / 100:,.2f} {s.currency}" for t in ("pro", "elite")) + ("" if all(t in s.price_kobo for t in ("pro", "elite")) else " (library defaults: set price_kobo explicitly)"))
        # email
        if self.mailer.enabled:
            ok, msg = self.mailer.check(); add("smtp", "Email delivery (SMTP)", "ok" if ok else "fail", msg)
        else: add("smtp", "Email delivery (SMTP)", "warn", "not configured: no verification emails, password-reset emails, receipts or alerts")
        # news
        fs = self.feeds_status(); healthy = sum(1 for f in fs if f["last_ok"] and not f["last_error"])
        add("news", "News & calendar feeds", "ok" if (s.news_enabled and healthy == len(fs) and fs) else "warn", f"{'enabled' if s.news_enabled else 'DISABLED'}; {healthy}/{len(fs)} sources healthy")
        ups = self.db.one("SELECT COUNT(*) AS n FROM events WHERE impact='high' AND time_utc>?", (T.iso(datetime.now(timezone.utc)),))["n"]
        add("calendar", "Upcoming high-impact events loaded", "ok" if ups else "warn", f"{ups} upcoming" if ups else "none: news-blackout protection is inactive")
        # security & ops
        add("admin2fa", "Admin account with 2FA", "ok" if self.db.one("SELECT 1 FROM users WHERE role='admin' AND totp_enabled=1") else "fail", "present" if self.db.one("SELECT 1 FROM users WHERE role='admin' AND totp_enabled=1") else "create one: python -m apex.cli --config web.json web-create-admin --email you@...")
        add("audit", "Audit log integrity", "ok" if verify_audit(self.db) else "fail", "hash chain intact" if verify_audit(self.db) else "CHAIN BROKEN: investigate")
        if s.backup_dir:
            try:
                files = [os.path.getmtime(os.path.join(s.backup_dir, f)) for f in os.listdir(s.backup_dir) if f.endswith(".db")]; age = (time.time() - max(files)) / 3600 if files else None
            except OSError: age = None
            add("backup", "Recent database backup", "ok" if (age is not None and age < 36) else "fail", f"newest backup {age:.1f} h old" if age is not None else f"no backups found in {s.backup_dir}")
        else: add("backup", "Recent database backup", "warn", "backup_dir not set: schedule deploy/backup.sh and set backup_dir")
        add("legal", "Legal text reviewed", "ok" if (s.legal_reviewed and s.company_name and s.support_email) else ("fail" if s.production else "warn"), f"{s.company_name or 'company name missing'}, {s.legal_jurisdiction or 'jurisdiction missing'}, updated {s.legal_updated or 'n/a'}" + ("" if s.legal_reviewed else "; NOT marked as lawyer-reviewed"))
        add("timezones", "Timezone database (DST-correct sessions)", "ok" if T.TZ_OK else "warn", "available" if T.TZ_OK else "missing: pip install tzdata (sessions fall back to fixed winter hours)")
        res = {"ready": not any(r["status"] == "fail" for r in rows), "rows": rows, "checked": T.iso(datetime.now(timezone.utc)), "counts": {k: sum(1 for r in rows if r["status"] == k) for k in ("ok", "warn", "fail")}}
        self._ready = (time.time(), res); return res

    # ---------------- admin: economic calendar ----------------
    def admin_events_set(self, admin, text: str) -> dict:
        if len(text) > 200_000: raise ApiError(413, "calendar too large")
        rows, errs = parse_events_csv(text)
        if errs and not rows: raise ApiError(400, "; ".join(errs[:3]))
        with self.db.tx() as c:
            c.execute("DELETE FROM events WHERE source='manual'"); c.executemany("INSERT INTO events(time_utc,currency,impact,name,source) VALUES(?,?,?,?,'manual')", rows)
            audit(c, admin["email"], "calendar_upload", "", {"rows": len(rows), "errors": len(errs)})
        self._policy = (0.0, None); return {"saved": len(rows), "errors": errs[:5]}

    def admin_events_list(self) -> list:
        now = datetime.now(timezone.utc).timestamp() - 86400
        return [dict(r) for r in self.db.all("SELECT time_utc,currency,impact,name FROM events ORDER BY time_utc") if _ts(r["time_utc"]) > now][:200]

    def health(self) -> dict:
        try: self.db.one("SELECT 1"); db_ok = True
        except Exception: db_ok = False
        fresh = None
        try:
            sym = (self.md.symbols() or [None])[0]
            if sym:
                cs, q, now, src = self.md.get(sym, 15)
                fresh = True if (self.s.feed_type == "csv" or not T.market_open(now)) else (now - cs[-1].ts).total_seconds() < 3 * 900
        except Exception: fresh = False
        return {"ok": db_ok and fresh is not False, "db": db_ok, "data_fresh": fresh, "billing": self.billing.enabled, "mail": self.mailer.enabled}

    # ---------------- admin ----------------
    def admin_stats(self) -> dict:
        now = int(time.time()); day = datetime.now(timezone.utc).date().isoformat(); users = self.db.all("SELECT * FROM users")
        tiers = {"free": 0, "pro": 0, "elite": 0, "admin": 0}
        for r in users: tiers[effective_tier(r, now)] = tiers.get(effective_tier(r, now), 0) + 1
        rev = {}
        for r in self.db.all("SELECT currency, SUM(amount_kobo) AS s FROM payments WHERE status='success' AND paid_at>? GROUP BY currency", (now - 30 * 86400,)): rev[r["currency"]] = r["s"] / 100
        return {"users": len(users), "new_7d": sum(1 for r in users if r["created"] > now - 7 * 86400), "tiers": tiers, "revenue_30d": rev,
                "pending_payments": self.db.one("SELECT COUNT(*) AS n FROM payments WHERE status='pending'")["n"],
                "scans_today": (self.db.one("SELECT COALESCE(SUM(scans),0) AS n FROM usage WHERE day=?", (day,)))["n"], "suspended": sum(1 for r in users if r["status"] != "active")}

    def _target(self, uid) -> sqlite3.Row:
        t = self.db.one("SELECT * FROM users WHERE id=?", (int(uid),))
        if not t: raise ApiError(404, "no such user")
        if t["role"] == "admin": raise ApiError(403, "admin accounts cannot be modified through the web console")
        return t

    def admin_users(self) -> list:
        day = datetime.now(timezone.utc).date().isoformat(); now = int(time.time())
        return [{"id": r["id"], "email": r["email"], "role": r["role"], "tier": r["tier"], "effective": effective_tier(r, now), "paid_until": r["paid_until"], "status": r["status"],
                 "created": r["created"], "scans_today": (self.db.one("SELECT scans FROM usage WHERE user_id=? AND day=?", (r["id"], day)) or {"scans": 0})["scans"]}
                for r in self.db.all("SELECT * FROM users ORDER BY id")]

    def admin_grant(self, admin, uid, tier: str, days: int):
        t = self._target(uid)
        if tier not in ("free", "pro", "elite"): raise ApiError(400, "bad tier")
        days = int(days)
        if not (0 <= days <= 3650): raise ApiError(400, "days out of range")
        until = 0 if tier == "free" else int(time.time()) + days * 86400
        with self.db.tx() as c:
            c.execute("UPDATE users SET tier=?, paid_until=? WHERE id=?", (tier, until, t["id"]))
            audit(c, admin["email"], "grant_tier", str(t["id"]), {"tier": tier, "days": days})

    def admin_status(self, admin, uid, status: str):
        t = self._target(uid)
        if status not in ("active", "suspended"): raise ApiError(400, "bad status")
        with self.db.tx() as c:
            c.execute("UPDATE users SET status=? WHERE id=?", (status, t["id"]))
            if status == "suspended": c.execute("DELETE FROM sessions WHERE user_id=?", (t["id"],))
            audit(c, admin["email"], "set_status", str(t["id"]), {"status": status})

    def admin_revoke(self, admin, uid):
        t = self._target(uid)
        with self.db.tx() as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (t["id"],)); audit(c, admin["email"], "revoke_sessions", str(t["id"]))

    def admin_issue_reset(self, admin, uid) -> str:
        t = self._target(uid); tok = sec.new_token()
        with self.db.tx() as c:
            c.execute("INSERT INTO reset_tokens(hash,user_id,expires) VALUES(?,?,?)", (sec.token_hash(tok), t["id"], int(time.time()) + 3600))
            audit(c, admin["email"], "issue_reset_token", str(t["id"]))
        return tok

    def payments(self, u, all_: bool) -> list:
        rows = self.db.all("SELECT p.*, u.email FROM payments p JOIN users u ON u.id=p.user_id " + ("" if all_ else "WHERE p.user_id=? ") + "ORDER BY p.created DESC LIMIT 200", () if all_ else (u["id"],))
        return [{"reference": r["reference"], "email": r["email"] if all_ else None, "tier": r["tier"], "amount": r["amount_kobo"] / 100, "currency": r["currency"], "status": r["status"], "created": r["created"], "paid_at": r["paid_at"]} for r in rows]

    def audit_tail(self, n=100) -> dict:
        return {"chain_ok": verify_audit(self.db), "rows": [dict(r) for r in self.db.all("SELECT id,ts,actor,action,target,detail FROM audit ORDER BY id DESC LIMIT ?", (int(n),))]}
