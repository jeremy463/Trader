"""SMTP mailer (stdlib). EmailMessage rejects header injection; failures never raise secrets into logs or requests."""
from __future__ import annotations
import logging, queue, smtplib, ssl, threading
from email.message import EmailMessage
from . import security as sec

log = logging.getLogger("apex.mail")


class Mailer:
    def __init__(self, host: str = "", port: int = 587, user: str = "", password: str = "", sender: str = "", security: str = "starttls", timeout: int = 15):
        self.host, self.port, self.user, self._pw, self.sender, self.security, self.timeout = host, port, user, password, sender, security, timeout
        self.enabled = bool(host and sender); self._q: "queue.Queue" = queue.Queue(maxsize=500); self._t = None

    def send(self, to: str, subject: str, body: str) -> bool:
        if not self.enabled: return False
        if not sec.valid_email(to) or any(c in subject for c in "\r\n"): log.warning("mail refused: bad recipient/subject"); return False
        msg = EmailMessage(); msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject; msg.set_content(body)
        try:
            if self.security == "ssl": s = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ssl.create_default_context())
            else:
                s = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                if self.security == "starttls": s.starttls(context=ssl.create_default_context())
            try:
                if self.user: s.login(self.user, self._pw)
                s.send_message(msg)
            finally:
                try: s.quit()
                except Exception: pass
            return True
        except Exception as e:
            log.error("mail send failed (%s): %s", type(e).__name__, str(e)[:120]); return False

    def check(self):
        """Connect, negotiate TLS and authenticate WITHOUT sending anything. Returns (ok, message)."""
        if not self.enabled: return False, "SMTP not configured"
        try:
            if self.security == "ssl": s = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ssl.create_default_context())
            else:
                s = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                if self.security == "starttls": s.starttls(context=ssl.create_default_context())
            try:
                if self.user: s.login(self.user, self._pw)
            finally:
                try: s.quit()
                except Exception: pass
            return True, f"connected to {self.host}:{self.port} ({self.security})" + (" and authenticated" if self.user else "")
        except Exception as e: return False, f"{type(e).__name__}: {str(e)[:100]}".replace(self._pw, "***") if self._pw else f"{type(e).__name__}: {str(e)[:100]}"

    def send_async(self, to: str, subject: str, body: str) -> None:
        if not self.enabled: return
        if self._t is None:
            self._t = threading.Thread(target=self._loop, daemon=True); self._t.start()
        try: self._q.put_nowait((to, subject, body))
        except queue.Full: log.error("mail queue full; dropping message")

    def _loop(self):
        while True:
            to, subject, body = self._q.get()
            self.send(to, subject, body)
