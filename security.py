"""Security primitives: scrypt passwords, session tokens, TOTP (RFC 6238), rate limiting, validation. Standard library only."""
from __future__ import annotations
import base64, hashlib, hmac, os, re, secrets, struct, threading, time
from collections import defaultdict, deque

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False


DUMMY_HASH = hash_password("dummy-password-for-timing-equalisation")   # burn the same CPU for unknown emails

COMMON = {"password", "123456789012", "qwertyuiopas", "administrator", "iloveyou1234", "letmein12345", "welcome12345", "passwordpassword", "123456123456"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")


def valid_email(e: str) -> bool: return isinstance(e, str) and len(e) <= 254 and bool(EMAIL_RE.match(e))


def password_problem(pw: str, email: str = "") -> str | None:
    if not isinstance(pw, str): return "invalid password"
    if len(pw) < 12: return "password must be at least 12 characters"
    if len(pw) > 128: return "password too long (max 128)"
    if pw.lower() in COMMON or len(set(pw)) < 5: return "password is too common or repetitive"
    if email and email.split("@")[0].lower() in pw.lower() and len(email.split("@")[0]) >= 4: return "password must not contain your email name"
    return None


def new_token(n: int = 32) -> str: return secrets.token_urlsafe(n)
def token_hash(t: str) -> str: return hashlib.sha256(t.encode()).hexdigest()
def ct_eq(a: str, b: str) -> bool: return hmac.compare_digest(a.encode(), b.encode())


# ---- TOTP ----
def new_totp_secret() -> str: return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def totp_code(secret: str, step: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    d = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = d[-1] & 15
    return str((struct.unpack(">I", d[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def totp_verify(secret: str, code: str, last_step: int, now: float | None = None, window: int = 1):
    """Returns the matched time-step (> last_step: replay-protected) or None."""
    if not (isinstance(code, str) and code.isdigit() and len(code) == 6): return None
    cur = int((now or time.time()) // 30)
    for s in range(cur - window, cur + window + 1):
        if s > last_step and hmac.compare_digest(totp_code(secret, s), code): return s
    return None


def totp_uri(secret: str, email: str, issuer: str = "APEX") -> str:
    return f"otpauth://totp/{issuer}:{email}?secret={secret}&issuer={issuer}&period=30&digits=6"


class RateLimiter:
    """Sliding-window limiter (in-memory, single process)."""
    def __init__(self): self.h = defaultdict(deque); self.lock = threading.Lock(); self._n = 0

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.h[key]
            while q and q[0] <= now - window: q.popleft()
            self._n += 1
            if self._n % 2000 == 0:
                for k in [k for k, v in self.h.items() if not v]: del self.h[k]
            if len(q) >= limit: return False
            q.append(now); return True
