import os
import stat

import pytest

from mailhub.crypto import SecretBox


def test_roundtrip_and_key_file_permissions(tmp_path):
    box = SecretBox.load(tmp_path)
    token = box.encrypt("app-password-123")
    assert token != "app-password-123"
    assert box.decrypt(token) == "app-password-123"
    mode = stat.S_IMODE(os.stat(tmp_path / "secret.key").st_mode)
    assert mode == 0o600
    # A second load reuses the same key.
    assert SecretBox.load(tmp_path).decrypt(token) == "app-password-123"


def test_wrong_key_fails_loudly(tmp_path):
    token = SecretBox.load(tmp_path).encrypt("x")
    from cryptography.fernet import Fernet
    other = SecretBox(Fernet.generate_key())
    with pytest.raises(ValueError):
        other.decrypt(token)
