from mailhub.providers import PROVIDERS, detect_provider, get_provider
import pytest


def test_detects_common_domains():
    assert detect_provider("a@gmail.com").key == "gmail"
    assert detect_provider("a@Hotmail.com").key == "outlook"
    assert detect_provider("a@ymail.com").key == "yahoo"
    assert detect_provider("a@me.com").key == "icloud"
    assert detect_provider("a@zoho.com").key == "zoho"


def test_unknown_domain_is_generic():
    assert detect_provider("ops@noblepordev.com").key == "generic"


def test_presets_are_complete():
    for key, p in PROVIDERS.items():
        if key == "generic":
            continue
        assert p.imap_host and p.smtp_host, key
        assert p.imap_port == 993
        assert p.auth_methods


def test_get_provider_rejects_unknown():
    with pytest.raises(ValueError):
        get_provider("aol")
