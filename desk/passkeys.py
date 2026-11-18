"""Passkeys (WebAuthn) for the hosted desk: sign in with the phone's or laptop's fingerprint, face or PIN.

A passkey is a key pair made by the device; the desk keeps only the public
half. Signing in proves the device holds the private half for this site's
domain, so a passkey cannot be phished onto another site or replayed. With
user verification (fingerprint, face or device PIN) required, a passkey
counts as both factors: it skips the password and the authenticator code.

Adding a passkey needs the current password, so a stolen session alone
cannot plant a permanent way in. Passkeys work only over HTTPS (or on
localhost), and only on the domain they were made on.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

CHALLENGE_SECONDS = 300
MAX_PASSKEYS = 20


class PasskeyError(ValueError):
    pass


def _b64(data: bytes) -> str:
    from webauthn.helpers import bytes_to_base64url
    return bytes_to_base64url(data)


def _unb64(text: str) -> bytes:
    from webauthn import base64url_to_bytes
    return base64url_to_bytes(text)


class Passkeys:
    def __init__(self, path: Path, username: str, clock=time.time):
        self.path, self.username, self._clock = path, username, clock
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}            # challenge token -> {challenge, kind, expires}
        self._state = {"user_id": _b64(secrets.token_bytes(16)), "keys": []}
        with contextlib.suppress(OSError, json.JSONDecodeError):
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and isinstance(stored.get("keys"), list):
                self._state = {**self._state, **stored}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # -- bookkeeping ----------------------------------------------------------

    def listing(self) -> list[dict]:
        return [{"id": k["id"], "name": k["name"], "created": k["created"], "last_used": k.get("last_used"),
                 "rp_id": k["rp_id"]} for k in self._state["keys"]]

    def count(self) -> int:
        return len(self._state["keys"])

    def remove(self, key_id: str) -> bool:
        with self._lock:
            before = len(self._state["keys"])
            self._state["keys"] = [k for k in self._state["keys"] if k["id"] != key_id]
            if len(self._state["keys"]) == before:
                return False
            self._save()
            return True

    def _remember(self, kind: str, challenge: bytes, rp_id: str, origin: str) -> str:
        token = secrets.token_urlsafe(24)
        now = self._clock()
        with self._lock:
            self._pending = {t: p for t, p in self._pending.items() if p["expires"] > now}
            if len(self._pending) > 200:                    # a flood of unfinished sign-ins: drop the oldest
                for old in sorted(self._pending, key=lambda t: self._pending[t]["expires"])[:100]:
                    self._pending.pop(old, None)
            self._pending[token] = {"kind": kind, "challenge": challenge, "rp_id": rp_id, "origin": origin,
                                    "expires": now + CHALLENGE_SECONDS}
        return token

    def _take(self, token: str, kind: str) -> dict:
        with self._lock:
            pending = self._pending.pop(str(token), None)
        if not pending or pending["kind"] != kind or pending["expires"] <= self._clock():
            raise PasskeyError("This passkey request has expired. Try again.")
        return pending

    # -- adding a passkey -------------------------------------------------------

    def registration_options(self, rp_id: str, origin: str) -> dict:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )

        if self.count() >= MAX_PASSKEYS:
            raise PasskeyError(f"At most {MAX_PASSKEYS} passkeys. Remove one first.")
        options = generate_registration_options(
            rp_id=rp_id, rp_name="TradeSetu", user_name=self.username, user_display_name=self.username,
            user_id=_unb64(self._state["user_id"]),
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=_unb64(k["id"])) for k in self._state["keys"]
                                 if k["rp_id"] == rp_id])
        token = self._remember("register", options.challenge, rp_id, origin)
        return {"token": token, "options": json.loads(options_to_json(options))}

    def register(self, token: str, credential: dict, name: str) -> dict:
        from webauthn import verify_registration_response

        pending = self._take(token, "register")
        try:
            verified = verify_registration_response(
                credential=credential, expected_challenge=pending["challenge"], expected_rp_id=pending["rp_id"],
                expected_origin=pending["origin"], require_user_verification=True)
        except Exception as exc:  # noqa: BLE001 - the library raises several types; all mean "not accepted"
            raise PasskeyError(f"The passkey was not accepted: {exc}") from exc
        key_id = _b64(verified.credential_id)
        entry = {"id": key_id, "public_key": _b64(verified.credential_public_key), "sign_count": verified.sign_count,
                 "name": (str(name).strip() or "Passkey")[:60], "rp_id": pending["rp_id"],
                 "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(self._clock())), "last_used": None}
        with self._lock:
            if any(k["id"] == key_id for k in self._state["keys"]):
                raise PasskeyError("This passkey is already added.")
            self._state["keys"].append(entry)
            self._save()
        return {k: entry[k] for k in ("id", "name", "created", "rp_id")}

    # -- signing in -------------------------------------------------------------

    def authentication_options(self, rp_id: str, origin: str) -> dict:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import UserVerificationRequirement

        # No list of keys: the browser offers this site's passkeys (discoverable credentials), so the
        # page does not reveal whether any exist.
        options = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
        token = self._remember("login", options.challenge, rp_id, origin)
        return {"token": token, "options": json.loads(options_to_json(options))}

    def authenticate(self, token: str, credential: dict) -> dict:
        from webauthn import verify_authentication_response

        pending = self._take(token, "login")
        key_id = str((credential or {}).get("id") or (credential or {}).get("rawId") or "")
        with self._lock:
            entry = next((k for k in self._state["keys"] if k["id"] == key_id and k["rp_id"] == pending["rp_id"]), None)
        if entry is None:
            raise PasskeyError("This passkey is not registered with this desk.")
        try:
            verified = verify_authentication_response(
                credential=credential, expected_challenge=pending["challenge"], expected_rp_id=pending["rp_id"],
                expected_origin=pending["origin"], credential_public_key=_unb64(entry["public_key"]),
                credential_current_sign_count=entry["sign_count"], require_user_verification=True)
        except Exception as exc:  # noqa: BLE001
            raise PasskeyError("The passkey could not be verified.") from exc
        with self._lock:
            entry["sign_count"] = verified.new_sign_count
            entry["last_used"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(self._clock()))
            self._save()
        return {"id": entry["id"], "name": entry["name"]}


def relying_party(request, public_url: str = "") -> tuple[str, str]:
    """The passkey domain (RP ID) and origin for this request. ``public_url`` (DESK_PUBLIC_URL) wins when set."""
    from urllib.parse import urlsplit

    if public_url:
        parts = urlsplit(public_url.strip())
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise PasskeyError("DESK_PUBLIC_URL must look like https://desk.example.com")
        host = parts.hostname
        origin = f"{parts.scheme}://{parts.netloc}"
    else:
        raw = request.headers.get("host") or ""
        host = raw.rsplit(":", 1)[0].strip("[]").lower() if not raw.startswith("[") else raw[1:raw.index("]")]
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
        origin = f"{scheme}://{raw}"
    if not origin.startswith("https://") and host not in ("localhost", "127.0.0.1"):
        raise PasskeyError("Passkeys need the desk to be opened over HTTPS.")
    return host, origin
