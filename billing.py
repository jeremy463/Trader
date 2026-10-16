"""Tiers, Paystack client, webhook verification and idempotent settlement.
Money rules: amounts live in KOBO (integers). The server decides price and tier; the client/metadata is never trusted.
A payment only credits access if reference, amount, currency and customer email all match what the server created."""
from __future__ import annotations
import hashlib, hmac, json, logging, secrets, time, urllib.error, urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from .db import DB, audit

log = logging.getLogger("apex.billing")
PERIOD = 30 * 86400


@dataclass(frozen=True)
class Tier:
    name: str
    label: str
    price_kobo: int
    scans_per_day: int
    all_methods: bool
    plan: bool          # entry/stop/targets + sizing + full report
    edge: bool          # out-of-sample edge evaluation
    upload: bool
    symbols: Optional[Tuple[str, ...]]   # None = all available
    alerts: int = 0                      # max email alerts


FREE_METHODS = ("EMA trend stack", "Supertrend(10,3)", "ADX/DI", "MACD", "Stochastic cross", "RSI extreme reversal",
                "Donchian 20 breakout", "Bollinger squeeze breakout", "Market structure BOS/CHOCH", "Multi-timeframe confluence")

DEFAULT_TIERS: Dict[str, Tier] = {
    "free":  Tier("free", "Free", 0, 5, False, False, False, False, ("EURUSD",), 0),
    "pro":   Tier("pro", "Pro", 1_500_000, 150, True, True, False, False, None, 3),
    "elite": Tier("elite", "Elite", 4_500_000, 1000, True, True, True, True, None, 10),
}
RANK = {"free": 0, "pro": 1, "elite": 2}


def effective_tier(user, now: int, tiers=DEFAULT_TIERS) -> str:
    """Admin = unrestricted. Paid tiers silently fall back to free when paid_until has passed."""
    if user["role"] == "admin": return "admin"
    return user["tier"] if user["tier"] != "free" and user["paid_until"] > now else "free"


class PaystackError(Exception): pass


class PaystackClient:
    def __init__(self, secret: str, base: str = "https://api.paystack.co", timeout: int = 15):
        if not secret: raise ValueError("paystack secret missing")
        self._secret, self.base, self.timeout = secret, base.rstrip("/"), timeout

    def _req(self, method: str, path: str, body: Optional[dict] = None, retries: int = 1) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        for k in range(retries + 1):
            req = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Authorization": f"Bearer {self._secret}", "Content-Type": "application/json", "User-Agent": "apex-billing/1"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r: out = json.loads(r.read())
                if not out.get("status"): raise PaystackError(f"paystack rejected request: {out.get('message')}")
                return out["data"]
            except urllib.error.HTTPError as e:
                if e.code >= 500 and k < retries: time.sleep(0.5); continue
                raise PaystackError(f"paystack http {e.code}") from None
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                if method == "GET" and k < retries: time.sleep(0.5); continue
                raise PaystackError(f"paystack unreachable: {type(e).__name__}") from None
        raise PaystackError("unreachable")

    def initialize(self, email, amount_kobo, reference, callback_url, currency, metadata) -> dict:
        return self._req("POST", "/transaction/initialize", {"email": email, "amount": int(amount_kobo), "currency": currency,
                                                             "reference": reference, "callback_url": callback_url, "metadata": metadata}, retries=0)

    def balance(self) -> list:
        """Cheap authenticated read used to validate the secret key (never moves money)."""
        return self._req("GET", "/balance")

    def verify(self, reference: str) -> dict:
        from urllib.parse import quote
        return self._req("GET", "/transaction/verify/" + quote(reference, safe=""))


def verify_signature(secret: str, raw: bytes, header: str) -> bool:
    """Paystack signs the raw body with HMAC-SHA512 using your secret key (header x-paystack-signature)."""
    if not secret or not header: return False
    return hmac.compare_digest(hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest(), header.strip().lower())


class Billing:
    def __init__(self, db: DB, client: Optional[PaystackClient], secret: str, currency: str, base_url: str,
                 tiers: Dict[str, Tier] = DEFAULT_TIERS, plan_codes: Optional[Dict[str, str]] = None):
        self.db, self.client, self.secret, self.currency, self.base_url = db, client, secret, currency, base_url.rstrip("/")
        self.tiers, self.plan_codes = tiers, plan_codes or {}

    @property
    def enabled(self) -> bool: return self.client is not None

    # ---- checkout ----
    def checkout(self, user, tier_name: str, now: Optional[int] = None) -> str:
        now = now or int(time.time())
        if not self.enabled: raise PaystackError("billing not configured")
        if tier_name not in ("pro", "elite"): raise ValueError("unknown tier")
        cur = effective_tier(user, now)
        if cur != "admin" and RANK[tier_name] < RANK.get(cur, 0): raise ValueError("cannot buy a lower tier while a higher one is active")
        t = self.tiers[tier_name]
        ref = "apx_" + secrets.token_hex(12)
        with self.db.tx() as c:
            c.execute("INSERT INTO payments(reference,user_id,tier,amount_kobo,currency,status,created,source) VALUES(?,?,?,?,?,'pending',?,'checkout')",
                      (ref, user["id"], tier_name, t.price_kobo, self.currency, now))
        d = self.client.initialize(user["email"], t.price_kobo, ref, self.base_url + "/billing/return", self.currency,
                                   {"user_id": user["id"], "tier": tier_name})
        return d["authorization_url"]

    # ---- settlement (shared by redirect-verify and webhook) ----
    def _credit(self, c, user, tier_name: str, now: int) -> int:
        """New paid_until. Upgrading converts the unused value of the old tier into extra time (pro-rata); same tier extends."""
        t = self.tiers[tier_name]
        if user["tier"] == tier_name and user["paid_until"] > now: return user["paid_until"] + PERIOD
        credit = 0
        if user["tier"] in RANK and user["tier"] != "free" and user["paid_until"] > now and RANK[tier_name] > RANK[user["tier"]]:
            old = self.tiers[user["tier"]]
            credit = int((user["paid_until"] - now) * old.price_kobo / t.price_kobo)
        return now + PERIOD + credit

    on_credit = None   # optional callback(user_row, tier, paid_until, reference) fired once after a successful credit

    def settle(self, reference: str, data: dict, source: str, now: Optional[int] = None) -> str:
        res = self._settle(reference, data, source, now)
        if res == "credited" and self.on_credit:
            try:
                row = self.db.one("SELECT p.tier, u.* FROM payments p JOIN users u ON u.id=p.user_id WHERE p.reference=?", (reference,))
                self.on_credit(row, row["tier"], row["paid_until"], reference)
            except Exception: log.exception("receipt hook failed")
        return res

    def _settle(self, reference: str, data: dict, source: str, now: Optional[int] = None) -> str:
        """Returns: credited | already | unknown | pending | failed | mismatch. Idempotent and atomic."""
        now = now or int(time.time())
        status = str(data.get("status", ""))
        amount, currency = data.get("amount"), str(data.get("currency", "")).upper()
        email = str((data.get("customer") or {}).get("email", "")).lower()
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM payments WHERE reference=?", (reference,)).fetchone()
            if row is None:   # possible subscription renewal charge created by Paystack (not by checkout)
                plan = data.get("plan") or {}
                code = plan.get("plan_code") if isinstance(plan, dict) else None
                tier_name = self.plan_codes.get(code) if code else None
                user = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone() if tier_name else None
                if not (tier_name and user and status == "success"): return "unknown"
                t = self.tiers[tier_name]
                if amount != t.price_kobo or currency != self.currency:
                    audit(c, "billing", "renewal_mismatch", reference, {"amount": amount, "currency": currency}); return "mismatch"
                c.execute("INSERT INTO payments(reference,user_id,tier,amount_kobo,currency,status,created,source) VALUES(?,?,?,?,?,'pending',?,?)",
                          (reference, user["id"], tier_name, t.price_kobo, self.currency, now, "renewal:" + source))
                row = c.execute("SELECT * FROM payments WHERE reference=?", (reference,)).fetchone()
            if row["status"] == "success": return "already"
            if status in ("failed", "abandoned", "reversed"):
                c.execute("UPDATE payments SET status=?, note=? WHERE reference=? AND status='pending'", (status, source, reference)); return "failed"
            if status != "success": return "pending"
            user = c.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
            if amount != row["amount_kobo"] or currency != row["currency"] or (email and email != user["email"].lower()):
                c.execute("UPDATE payments SET status='mismatch', note=? WHERE reference=?", (f"{source}: amount/currency/email mismatch", reference))
                audit(c, "billing", "payment_mismatch", reference, {"paid": amount, "expected": row["amount_kobo"], "cur": currency}); return "mismatch"
            until = self._credit(c, user, row["tier"], now)
            c.execute("UPDATE users SET tier=?, paid_until=? WHERE id=?", (row["tier"], until, user["id"]))
            c.execute("UPDATE payments SET status='success', paid_at=?, source=? WHERE reference=?", (now, source, reference))
            audit(c, "billing", "payment_credited", reference, {"user": user["id"], "tier": row["tier"], "until": until, "via": source})
            return "credited"

    def verify_and_settle(self, reference: str) -> str:
        """Redirect-return path: never trust the browser; ask Paystack."""
        if not self.enabled: return "disabled"
        try: data = self.client.verify(reference)
        except PaystackError as e:
            log.warning("verify failed for %s: %s", reference, e); return "unverified"
        if data.get("reference") != reference: return "mismatch"
        return self.settle(reference, data, "verify")

    def handle_webhook(self, raw: bytes, signature: str) -> Tuple[int, str]:
        if not verify_signature(self.secret, raw, signature):
            log.warning("webhook with bad signature rejected"); return 401, "bad signature"
        try: ev = json.loads(raw)
        except ValueError: return 400, "bad json"
        if not isinstance(ev, dict) or not isinstance(ev.get("data"), dict): return 200, "ignored"
        if ev.get("event") == "charge.success":
            ref = str(ev["data"].get("reference", ""))
            if ref: return 200, self.settle(ref, ev["data"], "webhook")
        return 200, "ignored"
