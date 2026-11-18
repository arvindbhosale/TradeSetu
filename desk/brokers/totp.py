"""Time-based one-time codes (RFC 6238), for brokers whose API sign-in asks for one."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def totp(secret: str, at: float | None = None, digits: int = 6, step: int = 30) -> str:
    """The current code for a base32 ``secret`` (the text under the QR code the broker shows)."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(code).zfill(digits)
