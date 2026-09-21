"""TokenService 核心逻辑单测（stub MetaClient，不发真实 HTTP）。"""

import asyncio

import pytest

from app.clients.meta_client import MetaApiError
from app.core.config import settings
from app.services.token_service import TYPE_QUOTA, TokenService

from tests.conftest import FakeMetaClient

TOKEN = "fake-jwt"


@pytest.fixture(autouse=True)
def _patch_quota(monkeypatch):
    """每个用例前还原全局配额默认值。"""
    monkeypatch.setattr(settings, "TOKEN_PER_REQUEST_LIMIT", 15000)
    monkeypatch.setattr(settings, "TOKEN_DAILY_LIMIT", 100000)
    yield


async def test_check_allowed_for_fresh_user():
    svc = TokenService(FakeMetaClient())
    resp = await svc.check(1, estimated_tokens=100, user_token=TOKEN)
    assert resp.allowed is True
    assert resp.reason is None
    assert resp.quota.per_request_limit == 15000
    assert resp.usage.total_tokens == 0


async def test_check_per_request_exceeded():
    svc = TokenService(FakeMetaClient())
    resp = await svc.check(1, estimated_tokens=20000, user_token=TOKEN)
    assert resp.allowed is False
    assert resp.reason == "per_request_exceeded"


async def test_check_daily_exceeded():
    fake = FakeMetaClient()
    svc = TokenService(fake)
    # 先 consume 掉 90000
    await svc.consume(1, 60000, 30000, "m", "p", TOKEN)
    # 再估 11000：当日已用 90000 + 11000 > 100000（且 11000 ≤ 单次上限 15000）
    resp = await svc.check(1, estimated_tokens=11000, user_token=TOKEN)
    assert resp.allowed is False
    assert resp.reason == "daily_limit_exceeded"


async def test_consume_accumulates():
    fake = FakeMetaClient()
    svc = TokenService(fake)
    r1 = await svc.consume(1, 8000, 2000, "deepseek", "ds", TOKEN)
    assert r1.usage.total_tokens == 10000
    assert r1.usage.today_total_tokens == 10000
    assert r1.usage.request_count == 1
    assert r1.over_limit is False

    r2 = await svc.consume(1, 1000, 500, "deepseek", "ds", TOKEN)
    assert r2.usage.total_tokens == 11500
    assert r2.usage.today_total_tokens == 11500
    assert r2.usage.request_count == 2

    # meta 落库结构正确
    saved = fake.store["token_usage"]["1"]["data"]
    assert saved["input_tokens"] == 9000
    assert saved["output_tokens"] == 2500
    assert saved["daily_usage"]  # 有当日键


async def test_consume_over_limit_flag():
    fake = FakeMetaClient()
    svc = TokenService(fake)
    resp = await svc.consume(1, 80000, 30000, None, None, TOKEN)
    assert resp.over_limit is True
    assert resp.usage.today_total_tokens == 110000


async def test_per_user_quota_override():
    fake = FakeMetaClient()
    # per-user 覆盖：单日 5000
    await fake.create_entry(TYPE_QUOTA, "1", {"user_id": 1, "daily_limit": 5000}, user_token=TOKEN)
    svc = TokenService(fake)
    resp = await svc.check(1, estimated_tokens=3000, user_token=TOKEN)
    assert resp.allowed is True
    resp = await svc.check(1, estimated_tokens=6000, user_token=TOKEN)
    assert resp.allowed is False
    assert resp.reason == "daily_limit_exceeded"


async def test_fail_closed_when_meta_broken():
    svc = TokenService(FakeMetaClient(broken=True))
    resp = await svc.check(1, estimated_tokens=100, user_token=TOKEN)
    assert resp.allowed is False
    assert resp.reason == "meta_unavailable"

    with pytest.raises(MetaApiError):
        await svc.consume(1, 100, 50, None, None, TOKEN)


async def test_concurrent_consume_no_overlap():
    """同用户并发 consume：锁内串行，总量精确（无丢更新）。"""
    fake = FakeMetaClient()
    svc = TokenService(fake)

    async def one():
        await svc.consume(1, 1000, 500, None, None, TOKEN)

    await asyncio.gather(*[one() for _ in range(20)])

    usage = await svc.get_usage(1, TOKEN)
    assert usage.usage.total_tokens == 20 * 1500
    assert usage.usage.request_count == 20
    assert usage.usage.today_total_tokens == 20 * 1500


async def test_get_remaining():
    fake = FakeMetaClient()
    svc = TokenService(fake)
    await svc.consume(1, 30000, 12381, None, None, TOKEN)
    rem = await svc.get_remaining(1, TOKEN)
    assert rem.daily_used == 42381
    assert rem.daily_remaining == 100000 - 42381
    assert rem.per_request_limit == 15000


async def test_total_limit_global(monkeypatch):
    """全局 TOKEN_TOTAL_LIMIT 拦截累计消耗。"""
    monkeypatch.setattr(settings, "TOKEN_TOTAL_LIMIT", 50000)
    fake = FakeMetaClient()
    svc = TokenService(fake)
    await svc.consume(1, 35000, 10000, None, None, TOKEN)  # 累计 45000
    # 估 10000：45000+10000=55000 > 50000（且 ≤ 单次 15000、当日 100000）
    resp = await svc.check(1, estimated_tokens=10000, user_token=TOKEN)
    assert resp.allowed is False
    assert resp.reason == "total_limit_exceeded"


async def test_total_limit_per_user_override():
    """per-user total_limit 覆盖全局（全局不限）。"""
    fake = FakeMetaClient()
    await fake.create_entry(TYPE_QUOTA, "1", {"user_id": 1, "total_limit": 10000}, user_token=TOKEN)
    svc = TokenService(fake)
    await svc.consume(1, 8000, 1000, None, None, TOKEN)
    resp = await svc.check(1, estimated_tokens=2000, user_token=TOKEN)
    assert resp.allowed is False
    assert resp.reason == "total_limit_exceeded"
