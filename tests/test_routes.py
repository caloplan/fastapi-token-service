"""路由层单测：健康检查 + 写端点内部密钥访问控制。

认证依赖（get_current_user）与 meta 客户端均被 stub；require_internal_token
保留真实实现（纯逻辑，不发网络），用于验证内部密钥访问控制。
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.dependencies import CurrentUser, get_current_user
from app.main import app

from tests.conftest import FakeMetaClient

FAKE_USER = CurrentUser(sub="u1", user_id=1, service_name="default", role="user", type="access")

# 路由内每次 get_meta_client() 都返回同一个内存实例，保证 consume→remaining 同库
_SHARED_META = FakeMetaClient()


@pytest.fixture(autouse=True)
def _stub_deps(monkeypatch):
    async def fake_user():
        return FAKE_USER

    app.dependency_overrides[get_current_user] = fake_user
    monkeypatch.setattr(
        "app.api.v1.routes.token.get_meta_client",
        lambda: _SHARED_META,
    )
    yield
    app.dependency_overrides.clear()


def test_health():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_check_requires_internal_key_when_configured(monkeypatch):
    """配置了内部密钥时，缺头/错头 → 403；带正确头 → 200。"""
    monkeypatch.setattr(settings, "TOKEN_INTERNAL_KEYS", ["sek"])
    client = TestClient(app)

    # 无密钥头
    r = client.post("/api/v1/token/check", json={"estimated_tokens": 100}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 403

    # 错误密钥
    r = client.post(
        "/api/v1/token/check",
        json={"estimated_tokens": 100},
        headers={"Authorization": "Bearer x", "X-Token-Internal-Key": "wrong"},
    )
    assert r.status_code == 403

    # 正确密钥
    r = client.post(
        "/api/v1/token/check",
        json={"estimated_tokens": 100},
        headers={"Authorization": "Bearer x", "X-Token-Internal-Key": "sek"},
    )
    assert r.status_code == 200
    assert r.json()["allowed"] is True


def test_check_denied_when_internal_keys_empty(monkeypatch):
    """安全默认：未配置内部密钥时写端点一律 403。"""
    monkeypatch.setattr(settings, "TOKEN_INTERNAL_KEYS", [])
    client = TestClient(app)
    r = client.post(
        "/api/v1/token/check",
        json={"estimated_tokens": 100},
        headers={"Authorization": "Bearer x", "X-Token-Internal-Key": "sek"},
    )
    assert r.status_code == 403


def test_consume_then_remaining_roundtrip(monkeypatch):
    monkeypatch.setattr(settings, "TOKEN_INTERNAL_KEYS", ["sek"])
    client = TestClient(app)
    headers = {"Authorization": "Bearer x", "X-Token-Internal-Key": "sek"}

    r = client.post("/api/v1/token/consume", json={"input_tokens": 8000, "output_tokens": 2000}, headers=headers)
    assert r.status_code == 200
    assert r.json()["usage"]["total_tokens"] == 10000

    r = client.get("/api/v1/token/remaining", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    body = r.json()
    assert body["daily_used"] == 10000
    assert body["daily_remaining"] == 90000
