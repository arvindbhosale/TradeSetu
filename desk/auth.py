"""Sign-in for the hosted desk: one account, cookie sessions, login throttling.

The account comes from the environment, so the panel that deploys the desk is
where it is set and changed:

    DESK_ADMIN_USER           default "admin"
    DESK_ADMIN_PASSWORD       the password, or
    DESK_ADMIN_PASSWORD_HASH  its hash, from ``python -m desk.auth``

A session is a random id held server-side and sent as an HttpOnly, SameSite
cookie. Each session carries a CSRF token the page must echo in a header on
every /api call, so another site cannot act through a signed-in browser.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

logger = logging.getLogger(__name__)

COOKIE = "desk_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
MIN_PASSWORD = 10
_N, _R, _P = 2 ** 14, 8, 1


class AuthNotConfigured(Exception):
    """The hosted desk was started without a usable admin password."""


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    # Colons, not the usual "$": Compose and .env files expand "$" in a value.
    return "scrypt:" + base64.b64encode(salt).decode() + ":" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.replace("$", ":").split(":")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=_N, r=_R, p=_P, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def totp(secret: str, moment: float | None = None, step: int = 30, digits: int = 6) -> str:
    """The time-based one-time code (RFC 6238, SHA-1) an authenticator app shows for ``secret``."""
    import struct

    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((time.time() if moment is None else moment) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** digits).zfill(digits)


class TwoFactor:
    """A second sign-in step: a code from an authenticator app, or a one-time recovery code.

    State lives in a file beside the settings. Lost the phone and the recovery
    codes? Start the desk once with ``DESK_2FA_RESET=1`` to switch it off.
    """

    def __init__(self, path, wall=time.time):
        import json

        self._json, self.path, self._wall = json, path, wall
        self._state = {"enabled": False, "secret": None, "pending": None, "recovery": [], "last_step": 0}
        with contextlib.suppress(OSError, ValueError):
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._state.update(stored)
        if os.environ.get("DESK_2FA_RESET") == "1" and self._state["enabled"]:
            logger.warning("DESK_2FA_RESET=1: two-factor sign-in has been switched off. Remove the variable now.")
            self._state.update(enabled=False, secret=None, pending=None, recovery=[])
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(self._json.dumps(self._state), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def enabled(self) -> bool:
        return bool(self._state["enabled"])

    def begin(self, account: str) -> dict:
        """Start setting up: a new secret, not active until a code from it is confirmed."""
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        self._state["pending"] = secret
        self._save()
        from urllib.parse import quote

        uri = f"otpauth://totp/{quote('TradeSetu')}:{quote(account)}?secret={secret}&issuer={quote('TradeSetu')}"
        return {"secret": secret, "uri": uri}

    def _matches(self, secret: str, code: str) -> int | None:
        """The time step ``code`` is valid for (now, or one step either side for clock drift), else None."""
        code = "".join(ch for ch in str(code) if ch.isdigit())
        if len(code) != 6:
            return None
        now = self._wall()
        for drift in (0, -1, 1):
            moment = now + drift * 30
            if hmac.compare_digest(totp(secret, moment), code):
                return int(moment // 30)
        return None

    def confirm(self, code: str) -> list[str]:
        """Switch two-factor on once a code from the new secret checks out. Returns the recovery codes, shown once."""
        pending = self._state.get("pending")
        step = self._matches(pending, code) if pending else None
        if step is None:
            raise ValueError("That code does not match. Check the time on your phone and enter the current 6 digits.")
        codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(8)]
        self._state.update(enabled=True, secret=pending, pending=None, last_step=step,
                           recovery=[hashlib.sha256(c.encode()).hexdigest() for c in codes])
        self._save()
        return codes

    def verify(self, code: str) -> bool:
        """Check a sign-in code. An app code works once; a recovery code is used up."""
        if not self.enabled:
            return True
        step = self._matches(self._state["secret"], code)
        if step is not None:
            if step <= self._state["last_step"]:
                return False                  # the same code again: someone may have watched it being typed
            self._state["last_step"] = step
            self._save()
            return True
        digest = hashlib.sha256(str(code).strip().lower().encode()).hexdigest()
        if digest in self._state["recovery"]:
            self._state["recovery"].remove(digest)
            self._save()
            return True
        return False

    def disable(self) -> None:
        self._state.update(enabled=False, secret=None, pending=None, recovery=[], last_step=0)
        self._save()

    def recovery_left(self) -> int:
        return len(self._state["recovery"])


class Auth:
    def __init__(self, username: str, password_hash: str, clock=time.monotonic, two_factor: TwoFactor | None = None):
        self.username, self._hash, self._clock = username, password_hash, clock
        self.two_factor = two_factor
        self._sessions: dict[str, dict] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env=os.environ) -> Auth:
        username = (env.get("DESK_ADMIN_USER") or "admin").strip()
        stored = (env.get("DESK_ADMIN_PASSWORD_HASH") or "").strip().strip("'\"")
        # Surrounding spaces and a pasted line ending are never part of a password;
        # the same trim is applied to what is typed at sign-in.
        password = (env.get("DESK_ADMIN_PASSWORD") or "").strip()
        if stored:
            parts = stored.replace("$", ":").split(":")
            if len(parts) != 3 or parts[0] != "scrypt" or not parts[1] or not parts[2]:
                raise AuthNotConfigured(
                    "DESK_ADMIN_PASSWORD_HASH is not a hash from `python -m desk.auth`. If it was pasted "
                    "into an env file, check that nothing was cut out of it.")
        elif password:
            if len(password) < MIN_PASSWORD:
                raise AuthNotConfigured(f"DESK_ADMIN_PASSWORD must be at least {MIN_PASSWORD} characters.")
            if len(password) >= 2 and password[0] == password[-1] and password[0] in "'\"":
                logger.warning("DESK_ADMIN_PASSWORD starts and ends with a quote character. The quotes are "
                               "part of the password as the desk received it; type them when signing in, "
                               "or remove them from the environment.")
            stored = hash_password(password)
            logger.info("Admin password set from DESK_ADMIN_PASSWORD: %d characters as received.", len(password))
        else:
            raise AuthNotConfigured(
                "Set DESK_ADMIN_PASSWORD (or DESK_ADMIN_PASSWORD_HASH) before starting the hosted desk. "
                "It will not serve a trading panel without a password.")
        return cls(username, stored)

    # -- throttling ---------------------------------------------------------

    def locked_for(self, client: str) -> int:
        """Seconds until ``client`` may try again; 0 when it may."""
        with self._lock:
            now = self._clock()
            recent = [t for t in self._failures.get(client, []) if now - t < LOCK_SECONDS]
            self._failures[client] = recent
            if len(recent) >= MAX_FAILURES:
                return int(LOCK_SECONDS - (now - recent[0])) + 1
            return 0

    # -- sessions -----------------------------------------------------------

    def check_password(self, password: str) -> bool:
        return verify_password(str(password).strip(), self._hash)

    def login(self, username: str, password: str, client: str, code: str = "") -> dict | None:
        """A new session for correct credentials, else None. Raises PermissionError when locked.

        With two-factor on, a correct password with no code raises ``LookupError``
        so the page can ask for the code; a wrong code counts as a failed sign-in.
        """
        wait = self.locked_for(client)
        if wait:
            logger.warning("Sign-in from %s refused: locked for %d more seconds.", client, wait)
            raise PermissionError(f"Too many failed sign-ins. Try again in {wait // 60 + 1} minutes.")
        # Both checks always run, so timing does not reveal which one failed.
        user_ok = hmac.compare_digest(username.strip().encode(), self.username.encode())
        pass_ok = verify_password(password.strip(), self._hash)
        second = self.two_factor
        if user_ok and pass_ok and second is not None and second.enabled:
            if not str(code).strip():
                raise LookupError("Enter the 6-digit code from your authenticator app.")
            if not second.verify(code):
                pass_ok = False
                logger.warning("Failed sign-in from %s: wrong two-factor code.", client)
        with self._lock:
            now = self._clock()
            if not (user_ok and pass_ok):
                self._failures.setdefault(client, []).append(now)
                # Which half was wrong goes to the server log only, never to the browser.
                logger.warning("Failed sign-in from %s: %s (typed password was %d characters).", client,
                               "unknown username" if not user_ok else "wrong password", len(password.strip()))
                return None
            self._failures.pop(client, None)
            return self._new_session(now)

    def _new_session(self, now: float) -> dict:
        """Called with the lock held."""
        self._sessions = {k: v for k, v in self._sessions.items() if v["expires"] > now}
        session_id = secrets.token_urlsafe(32)
        session = {"id": session_id, "csrf": secrets.token_urlsafe(24), "expires": now + SESSION_SECONDS}
        self._sessions[session_id] = session
        return dict(session)

    def failed(self, client: str, reason: str) -> None:
        """Count a failed sign-in that did not go through ``login`` (a passkey that did not verify)."""
        with self._lock:
            self._failures.setdefault(client, []).append(self._clock())
        logger.warning("Failed sign-in from %s: %s.", client, reason)

    def passkey_session(self, client: str) -> dict:
        """A session after a verified passkey (it is both factors: device plus fingerprint, face or PIN)."""
        with self._lock:
            self._failures.pop(client, None)
            return self._new_session(self._clock())

    def session(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or session["expires"] <= self._clock():
                self._sessions.pop(session_id, None)
                return None
            return dict(session)

    def logout(self, session_id: str | None) -> None:
        with self._lock:
            self._sessions.pop(session_id or "", None)


if __name__ == "__main__":
    import getpass
    import sys

    if sys.argv[1:] == ["check"]:
        # Does a password match what this container was started with?
        try:
            configured = Auth.from_env()
        except AuthNotConfigured as exc:
            raise SystemExit(f"Not configured: {exc}") from None
        print(f"Username the desk expects: {configured.username!r}")
        typed = getpass.getpass("Password to test: ")
        print("MATCH: this password signs in." if verify_password(typed.strip(), configured._hash)
              else "NO MATCH: the desk was started with a different password than the one you typed.")
        raise SystemExit(0)

    first = getpass.getpass("Password: ")
    if len(first) < MIN_PASSWORD:
        raise SystemExit(f"Use at least {MIN_PASSWORD} characters.")
    if first != getpass.getpass("Again: "):
        raise SystemExit("The two entries differ.")
    print(hash_password(first))
