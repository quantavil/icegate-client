from __future__ import annotations

import base64

import pytest

from icegate_client.shared.crypto import encrypt_password


def test_encrypt_matches_known_vector():
    assert encrypt_password("example-password") == "pJu42lN88/oljmNhH8aEu2grHgdAZm8MjJ3l5WchDNA="


def test_deterministic_and_padded():
    first = encrypt_password("secret-value")
    second = encrypt_password("secret-value")
    assert first == second
    raw = base64.b64decode(first)
    assert len(raw) % 16 == 0
    assert len(raw) >= len("secret-value")


def test_empty_password_rejected():
    with pytest.raises(ValueError):
        encrypt_password("")
