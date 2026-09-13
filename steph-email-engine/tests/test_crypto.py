import os
import stat

import pytest

from steph_email.crypto import SecretBox


def test_roundtrip_and_key_file_permissions(tmp_path):
    box = SecretBox.load(tmp_path)
    token = box.encrypt("app-password-123")
    assert token != "app-password-123"
    assert box.decrypt(token) == "app-password-123"
    assert stat.S_IMODE(os.stat(tmp_path / "secret.key").st_mode) == 0o600
    assert SecretBox.load(tmp_path).decrypt(token) == "app-password-123"


def test_wrong_key_fails_loudly(tmp_path):
    token = SecretBox.load(tmp_path).encrypt("x")
    from cryptography.fernet import Fernet
    with pytest.raises(ValueError):
        SecretBox(Fernet.generate_key()).decrypt(token)
