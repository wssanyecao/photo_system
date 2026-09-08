"""security 模块单测：hash 生成/校验/验证一致，token store/session。"""

from __future__ import annotations

from photo_gateway.security import (
    SessionStore,
    get_bearer_token,
    hash_password,
    verify_password,
)


def test_hash_verify_roundtrip():
    h = hash_password("secret-密码")
    assert h.startswith("scrypt$")
    assert verify_password("secret-密码", h)
    assert not verify_password("wrong", h)


def test_verify_against_bad_format_returns_false():
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "")


def test_session_store_roundtrip():
    s = SessionStore(ttl_hours=1)
    tok = s.create()
    assert s.verify(tok)
    s.revoke(tok)
    assert not s.verify(tok)


def test_bearer_extract():
    assert get_bearer_token("Bearer abc123") == "abc123"
    assert get_bearer_token("bearer xyz") == "xyz"      # scheme case-insensitive
    assert get_bearer_token("Basic abc") is None
    assert get_bearer_token(None) is None
    assert get_bearer_token("Bearer   ") is None
