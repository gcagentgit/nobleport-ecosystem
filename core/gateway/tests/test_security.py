"""Unit tests for the five security controls + the sanitizer (no HTTP)."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from core.gateway import security
from core.gateway.envelope import McpEnvelope
from core.gateway.governance import GovernanceGate
from core.gateway.security import (SCOPE_INVOKE, SCOPE_READ, AuthError, SecurityConfigError,
                                   TokenVerifier, mint_token)

from .fakes import TEST_AUDIENCE, TEST_ISSUER, strong_secret, make_settings


def verifier(cfg=None) -> TokenVerifier:
    return TokenVerifier.from_settings(cfg or make_settings())


def token(cfg, **kw) -> str:
    base = dict(key=cfg.jwt_secret, algorithm="HS256", audience=cfg.jwt_audience,
                issuer=cfg.jwt_issuer, subject="stephanie", agent="Stephanie.ai",
                scopes=[SCOPE_INVOKE])
    base.update(kw)
    return mint_token(**base)


# --- Control 1: no hardcoded / insecure signing key -------------------------
def test_no_key_refuses_boot():
    with pytest.raises(SecurityConfigError, match="No JWT signing key"):
        security.validate(make_settings(jwt_secret=""))


def test_insecure_dev_key_placeholder_rejected():
    with pytest.raises(SecurityConfigError, match="placeholder"):
        security.validate(make_settings(jwt_secret="insecure-dev-key"))


def test_short_secret_rejected():
    with pytest.raises(SecurityConfigError, match="at least 32"):
        security.validate(make_settings(jwt_secret="tooshort-tooshort-1"))


@pytest.mark.parametrize("weak", [
    "a" * 64,                                   # one character
    "12345678" * 4,                             # short block repeated
    "abcdefghijklmnopqrstuvwxyz012345",         # alphabet walk
    "qwertyuiop" * 4,                           # repeated keyboard row
])
def test_low_variety_secret_rejected(weak):
    with pytest.raises(SecurityConfigError, match="variety"):
        security.validate(make_settings(jwt_secret=weak))


def test_real_secrets_pass_entropy_check():
    import secrets as _s
    for _ in range(50):
        assert not security.is_low_entropy_secret(_s.token_urlsafe(48))
        assert not security.is_low_entropy_secret(_s.token_hex(16))   # 32 hex chars


def test_both_key_kinds_rejected():
    cfg = make_settings(jwt_public_key="-----BEGIN PUBLIC KEY-----\nxx\n-----END PUBLIC KEY-----")
    with pytest.raises(SecurityConfigError, match="exactly one"):
        security.validate(cfg)


def test_alg_none_never_permitted():
    with pytest.raises(SecurityConfigError, match="never permitted"):
        security.validate(make_settings(jwt_algorithms="HS256,none"))


def test_hmac_secret_cannot_enable_asymmetric_algs():
    with pytest.raises(SecurityConfigError, match="not HMAC"):
        security.validate(make_settings(jwt_algorithms="RS256"))


def test_no_default_key_anywhere_in_module():
    src = open(security.__file__, encoding="utf-8").read()
    assert 'os.getenv("OIDC_JWK"' not in src
    assert "verify_aud\": False" not in src and "'verify_aud': False" not in src
    # The only place the placeholder appears is the denylist.
    assert src.count("insecure-dev-key") == 1


def test_rsa_public_key_pem_and_jwk():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    cfg = make_settings(jwt_secret="", jwt_public_key=pem)
    summary = security.validate(cfg)
    assert summary["key_kind"] == "asymmetric" and summary["algorithms"] == ["RS256"]
    tok = mint_token(key=priv, algorithm="RS256", audience=cfg.jwt_audience, issuer=cfg.jwt_issuer,
                     subject="svc", agent="Borg.ai", scopes=[SCOPE_READ])
    assert verifier(cfg).verify(tok).agent == "Borg.ai"

    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key()))
    cfg2 = make_settings(jwt_secret="", jwt_public_key=json.dumps(jwk))
    assert verifier(cfg2).verify(tok).subject == "svc"


def test_private_jwk_rejected_as_verification_key():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(priv)  # contains private members
    with pytest.raises(SecurityConfigError, match="PUBLIC"):
        security.validate(make_settings(jwt_secret="", jwt_public_key=jwk))


def test_ec_key_family_enforced():
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    with pytest.raises(SecurityConfigError, match="do not match"):
        security.validate(make_settings(jwt_secret="", jwt_public_key=pem, jwt_algorithms="RS256"))
    # A P-256 key cannot be allowed to verify ES384 either.
    with pytest.raises(SecurityConfigError, match="do not match"):
        security.validate(make_settings(jwt_secret="", jwt_public_key=pem, jwt_algorithms="ES384"))
    assert security.validate(make_settings(jwt_secret="", jwt_public_key=pem))["algorithms"] == ["ES256"]


@pytest.mark.parametrize("curve,alg", [
    (ec.SECP256R1(), "ES256"), (ec.SECP384R1(), "ES384"),
    (ec.SECP521R1(), "ES512"), (ec.SECP256K1(), "ES256K"),
])
def test_ec_default_alg_follows_curve(curve, alg):
    priv = ec.generate_private_key(curve)
    pem = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    cfg = make_settings(jwt_secret="", jwt_public_key=pem)
    assert security.validate(cfg)["algorithms"] == [alg]
    tok = mint_token(key=priv, algorithm=alg, audience=cfg.jwt_audience, issuer=cfg.jwt_issuer,
                     subject="svc", agent="Borg.ai", scopes=[SCOPE_READ])
    assert verifier(cfg).verify(tok).subject == "svc"
    # The same key as a JWK derives the same algorithm.
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(priv.public_key())
    assert security.validate(make_settings(jwt_secret="", jwt_public_key=jwk))["algorithms"] == [alg]


def test_wrong_key_signature_rejected():
    cfg = make_settings()
    forged = token(cfg, key=strong_secret())
    with pytest.raises(AuthError, match="invalid_signature"):
        verifier(cfg).verify(forged)


def test_alg_none_token_rejected():
    cfg = make_settings()
    now = int(time.time())
    unsigned = jwt.encode({"sub": "x", "aud": cfg.jwt_audience, "iss": cfg.jwt_issuer,
                           "iat": now, "exp": now + 60}, key=None, algorithm="none")
    with pytest.raises(AuthError):
        verifier(cfg).verify(unsigned)


def test_hs256_token_rejected_when_rsa_configured():
    """Algorithm confusion: an HMAC token signed with the public key bytes."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    cfg = make_settings(jwt_secret="", jwt_public_key=pem)
    now = int(time.time())
    # PyJWT itself refuses to HMAC-sign with a PEM key, so build the token by hand.
    import base64, hmac, hashlib
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"sub": "x", "aud": cfg.jwt_audience, "iss": cfg.jwt_issuer,
                           "iat": now, "exp": now + 60, "scope": SCOPE_INVOKE}).encode())
    sig = b64(hmac.new(pem.encode(), header + b"." + body, hashlib.sha256).digest())
    confused = (header + b"." + body + b"." + sig).decode()
    with pytest.raises(AuthError):
        verifier(cfg).verify(confused)


# --- Control 2: audience --------------------------------------------------
def test_audience_required_in_config():
    with pytest.raises(SecurityConfigError, match="JWT_AUDIENCE"):
        security.validate(make_settings(jwt_audience=""))


def test_wrong_audience_rejected():
    cfg = make_settings()
    with pytest.raises(AuthError, match="invalid_audience"):
        verifier(cfg).verify(token(cfg, audience="some-other-api"))


def test_missing_audience_rejected():
    cfg = make_settings()
    now = int(time.time())
    tok = jwt.encode({"sub": "x", "iss": cfg.jwt_issuer, "iat": now, "exp": now + 60},
                     cfg.jwt_secret, algorithm="HS256")
    with pytest.raises(AuthError, match="aud"):
        verifier(cfg).verify(tok)


def test_audience_list_must_contain_ours():
    cfg = make_settings()
    now = int(time.time())
    ok = jwt.encode({"sub": "x", "aud": ["other", TEST_AUDIENCE], "iss": TEST_ISSUER,
                     "iat": now, "exp": now + 60, "scope": SCOPE_READ},
                    cfg.jwt_secret, algorithm="HS256")
    assert verifier(cfg).verify(ok).subject == "x"
    bad = jwt.encode({"sub": "x", "aud": ["other", "another"], "iss": TEST_ISSUER,
                      "iat": now, "exp": now + 60}, cfg.jwt_secret, algorithm="HS256")
    with pytest.raises(AuthError, match="invalid_audience"):
        verifier(cfg).verify(bad)


# --- Control 3: issuer ----------------------------------------------------
def test_issuer_required_in_config():
    with pytest.raises(SecurityConfigError, match="JWT_ISSUER"):
        security.validate(make_settings(jwt_issuer=""))


def test_wrong_issuer_rejected():
    cfg = make_settings()
    with pytest.raises(AuthError, match="invalid_issuer"):
        verifier(cfg).verify(token(cfg, issuer="https://evil.example"))


def test_missing_issuer_rejected():
    cfg = make_settings()
    now = int(time.time())
    tok = jwt.encode({"sub": "x", "aud": cfg.jwt_audience, "iat": now, "exp": now + 60},
                     cfg.jwt_secret, algorithm="HS256")
    with pytest.raises(AuthError, match="iss"):
        verifier(cfg).verify(tok)


# --- Lifetime / claims ----------------------------------------------------
def test_expired_rejected():
    cfg = make_settings()
    with pytest.raises(AuthError, match="token_expired"):
        verifier(cfg).verify(token(cfg, issued_at=int(time.time()) - 7200, ttl_s=3600))


def test_not_yet_valid_rejected():
    cfg = make_settings()
    with pytest.raises(AuthError, match="not_yet_valid"):
        verifier(cfg).verify(token(cfg, issued_at=int(time.time()) + 3600))


def test_excessive_ttl_rejected():
    cfg = make_settings()
    with pytest.raises(AuthError, match="ttl_too_long"):
        verifier(cfg).verify(token(cfg, ttl_s=30 * 24 * 3600))


def test_missing_subject_rejected():
    cfg = make_settings()
    now = int(time.time())
    tok = jwt.encode({"aud": cfg.jwt_audience, "iss": cfg.jwt_issuer, "iat": now, "exp": now + 60},
                     cfg.jwt_secret, algorithm="HS256")
    with pytest.raises(AuthError, match="sub"):
        verifier(cfg).verify(tok)


def test_unknown_scope_rejected():
    cfg = make_settings()
    with pytest.raises(AuthError, match="unknown_scope"):
        verifier(cfg).verify(token(cfg, scopes=["gateway:root"]))


def test_malformed_token_rejected():
    with pytest.raises(AuthError, match="malformed"):
        verifier().verify("not-a-jwt")


def test_valid_token_yields_principal():
    cfg = make_settings()
    p = verifier(cfg).verify(token(cfg, scopes=[SCOPE_INVOKE, SCOPE_READ]))
    assert p.subject == "stephanie" and p.agent == "Stephanie.ai"
    assert p.has(SCOPE_INVOKE) and p.has(SCOPE_READ) and not p.has(security.SCOPE_ADMIN)


# --- Control 4: CORS fail-closed ------------------------------------------
def test_wildcard_origin_rejected_everywhere():
    for env in ("production", "development"):
        with pytest.raises(SecurityConfigError, match="wildcard"):
            security.validate(make_settings(environment=env, frontend_origin="*"))
    with pytest.raises(SecurityConfigError, match="wildcard"):
        security.parse_origins("https://*.nobleport.ai", production=False)


def test_production_requires_origin():
    with pytest.raises(SecurityConfigError, match="required in production"):
        security.validate(make_settings(frontend_origin=""))


def test_production_requires_https_origin():
    with pytest.raises(SecurityConfigError, match="https"):
        security.validate(make_settings(frontend_origin="http://ops.nobleport.ai"))


def test_development_without_origin_denies_cross_origin():
    cfg = make_settings(environment="development", frontend_origin="")
    assert security.validate(cfg)["cors_origins"] == []


def test_origin_must_be_bare():
    for bad in ("https://ops.nobleport.ai/", "https://ops.nobleport.ai/app",
                "ops.nobleport.ai", "https://user:pw@ops.nobleport.ai", "ftp://x"):
        with pytest.raises(SecurityConfigError):
            security.parse_origins(bad, production=True)


def test_origin_list_parsed_and_deduplicated():
    got = security.parse_origins("https://a.nobleport.ai, https://b.nobleport.ai,https://a.nobleport.ai",
                                 production=True)
    assert got == ["https://a.nobleport.ai", "https://b.nobleport.ai"]


def test_production_requires_admin_token():
    with pytest.raises(SecurityConfigError, match="ADMIN_TOKEN"):
        security.validate(make_settings(admin_token=""))
    with pytest.raises(SecurityConfigError, match="placeholder"):
        security.validate(make_settings(admin_token="change-me"))


# --- Sanitizer ------------------------------------------------------------
def env(**kw):
    base = dict(requesting_agent="Stephanie.ai", target_agent="GCagent.ai",
                module="Estimate Engine", action="gcagent.price_estimate")
    base.update(kw)
    return McpEnvelope(**base)


def test_sanitizer_strips_zero_width_and_bidi():
    assert security.sanitize_text("guar​anteed‍ ret⁦urns‮") == "guaranteed returns"


def test_sanitizer_normalizes_fullwidth_and_ligatures():
    assert security.sanitize_text("ｇｕａｒａｎｔｅｅｄ") == "guaranteed"
    assert security.sanitize_text("ﬁnance") == "finance"


def test_sanitizer_keeps_newline_tab_drops_other_controls():
    assert security.sanitize_text("a\nb\tc\x00d\x1be\r") == "a\nb\tcde"


def test_sanitizer_caps_depth_and_size():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": {"j": 1}}}}}}}}}}
    with pytest.raises(security.SanitizeError, match="too_deep"):
        security.sanitize_value(deep)
    with pytest.raises(security.SanitizeError, match="too_large"):
        security.sanitize_value({str(i): i for i in range(600)})
    with pytest.raises(security.SanitizeError, match="too_long"):
        security.sanitize_text("x" * 20_000)


def test_sanitized_envelope_cannot_evade_claim_screen():
    gate = GovernanceGate(make_settings().launch_gates_path)
    evasive = env(message="we offer guar​anteed​ returns on every lot")
    assert gate.evaluate(evasive).decision == "PERMIT"          # raw text slips past
    clean = security.sanitize_envelope(evasive)
    out = gate.evaluate(clean)
    assert out.decision == "BLOCK" and out.reason == "prohibited_claim:guaranteed returns"


def test_sanitized_envelope_preserves_signature_and_payload_types():
    e = env(human_signature="sig​-opaque", payload={"n": 3, "ok": True, "s": "a\x00b",
                                                         "list": ["x​", None, 1.5]})
    clean = security.sanitize_envelope(e)
    assert clean.human_signature == "sig​-opaque"
    assert clean.payload == {"n": 3, "ok": True, "s": "ab", "list": ["x", None, 1.5]}
    assert clean.run_id == e.run_id
