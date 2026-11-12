"""PII protection at rest.

Two keys are derived from one master key:
- a *blind-index* key: HMAC-SHA256(canonical phone) is stored so events can be joined to leads
  by phone without the phone ever being stored in plaintext. HMAC (not a bare hash) matters:
  Indian mobiles are only ~4e9 values, so an unkeyed SHA-256 could be brute-forced in minutes.
- an *encryption* key: AES-256-GCM for the PII values themselves, so an authorised process
  can still recover them (for example to actually dial the customer).

The master key comes from `AU_PII_KEY` (64 hex chars) or a key file created on first use.
A fingerprint of it is stored in the database so a run with the wrong key fails loudly instead
of silently producing hashes that never match.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class PiiVault:
    def __init__(self, master_key: bytes):
        if len(master_key) != 32:
            raise ValueError("PII master key must be 32 bytes")
        self._index_key = hmac.new(master_key, b"au-ledger/blind-index/v1", hashlib.sha256).digest()
        self._aead = AESGCM(hmac.new(master_key, b"au-ledger/encryption/v1", hashlib.sha256).digest())
        self.fingerprint = hmac.new(master_key, b"au-ledger/fingerprint/v1", hashlib.sha256).hexdigest()[:16]

    @classmethod
    def load(cls, key_file: Path) -> "PiiVault":
        env = os.environ.get("AU_PII_KEY")
        if env:
            return cls(_parse_key(env, "AU_PII_KEY"))
        if not key_file.exists():
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_text(secrets.token_hex(32))
            key_file.chmod(0o600)
        return cls(_parse_key(key_file.read_text(), f"key file {key_file}"))

    def blind_index(self, canonical_value: str) -> str:
        return hmac.new(self._index_key, canonical_value.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, value: str) -> str:
        nonce = secrets.token_bytes(12)
        return base64.b64encode(nonce + self._aead.encrypt(nonce, value.encode(), None)).decode()

    def decrypt(self, token: str) -> str:
        raw = base64.b64decode(token)
        return self._aead.decrypt(raw[:12], raw[12:], None).decode()


def _parse_key(text: str, source: str) -> bytes:
    try:
        key = bytes.fromhex(text.strip())
    except ValueError:
        key = b""
    if len(key) != 32:
        raise SystemExit(f"{source} must be exactly 64 hex characters (a 32-byte key)")
    return key
