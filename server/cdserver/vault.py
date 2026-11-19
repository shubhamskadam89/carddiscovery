"""Envelope encryption for Gmail refresh tokens (and the date of birth for PDFs).

Production: AWS KMS wraps a fresh data key per secret, and IAM lets only the
worker role call Decrypt. Here a local master key file stands in for KMS behind
the same two calls (generate_data_key / decrypt_data_key), and the role check
stands in for IAM: the API process can seal secrets but not open them.
"""

from __future__ import annotations

import os
import secrets
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config

VERSION = 1


class LocalKms:
    """Stand-in for KMS. Swap for a boto3 client with the same two methods."""

    def __init__(self, key_file: Path):
        if not key_file.exists():
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_bytes(secrets.token_bytes(32))
            os.chmod(key_file, 0o600)
        self._key = AESGCM(key_file.read_bytes())

    def generate_data_key(self) -> tuple[bytes, bytes]:
        dek = secrets.token_bytes(32)
        nonce = secrets.token_bytes(12)
        return dek, nonce + self._key.encrypt(nonce, dek, b"dek")

    def decrypt_data_key(self, wrapped: bytes) -> bytes:
        return self._key.decrypt(wrapped[:12], wrapped[12:], b"dek")


class Vault:
    def __init__(self, role: str, kms: LocalKms | None = None):
        if role not in ("api", "worker"):
            raise ValueError("role must be api or worker")
        self.role = role
        self.kms = kms or LocalKms(config.MASTER_KEY_FILE)

    def seal(self, plaintext: bytes, context: str) -> bytes:
        dek, wrapped = self.kms.generate_data_key()
        nonce = secrets.token_bytes(12)
        ct = AESGCM(dek).encrypt(nonce, plaintext, context.encode())
        return struct.pack(">BH", VERSION, len(wrapped)) + wrapped + nonce + ct

    def open(self, blob: bytes, context: str) -> bytes:
        if self.role != "worker":
            raise PermissionError("only the worker role can decrypt secrets")
        version, n = struct.unpack(">BH", blob[:3])
        if version != VERSION:
            raise ValueError("unknown vault blob version")
        wrapped, rest = blob[3:3 + n], blob[3 + n:]
        dek = self.kms.decrypt_data_key(wrapped)
        return AESGCM(dek).decrypt(rest[:12], rest[12:], context.encode())
