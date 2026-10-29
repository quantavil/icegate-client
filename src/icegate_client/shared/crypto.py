"""AES-128-ECB password obfuscation used by the ICEGATE login form.

This is not encryption with a secret: the key ships in the public JavaScript
bundle. It only prevents the password from appearing as plain text in the
request body, so treat it exactly like a plain password in logs.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# "icegatecbicdelhi" as published in the SPA bundle.
LOGIN_KEY_B64 = "aWNlZ2F0ZWNiaWNkZWxoaQ=="
_BLOCK_BITS = 128


def _key() -> bytes:
    return base64.b64decode(LOGIN_KEY_B64)


def encrypt_password(password: str) -> str:
    """Return base64(AES-128-ECB/PKCS7(password))."""
    if not password:
        raise ValueError("password must not be empty")
    padder = padding.PKCS7(_BLOCK_BITS).padder()
    padded = padder.update(password.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_key()), modes.ECB()).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")
