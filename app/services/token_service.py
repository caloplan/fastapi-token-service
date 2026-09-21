"""Token 业务逻辑：使用记录 / 配额解析 / check / consume / 展示查询。

并发与一致性：
- Meta Storage 的 PUT 为 read-modify-write（无 expected_version / CAS），同用户并发
  consume 会丢更新（last-write-wins）；
- v1 用进程内 per-user asyncio.Lock 在锁内完成 read → compute → write，单实例部署下
  保证同用户额度不超卖；
- 多实例横向扩容时进程内锁失效，升级路径：Redis 原子计数（INCRBY + 当日 TTL）或
  Meta 侧增加 expected_version CAS（见 PLAN.md §5.5）。

故障策略：
- check 时 Meta 不可用 → allowed=false（reason=meta_unavailable，fail-closed）；
- consume / 查询时 Meta 不可用 → 抛 MetaApiError，由路由映射 503。
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.clients.meta_client import MetaApiError, MetaClient
from app.core.config import settings
from app.schemas.token import (
    CheckResponse,
    ConsumeResponse,
    QuotaResponse,
    RemainingResponse,
    TokenQuota,
    TokenUsageSnapshot,
    UsageResponse,
)
from app.utils.logger import get_logger

logger = get_logger("token_service")

# Meta 类型名（需先在 metastorage 注册，service_name 与 JWT 一致，默认 default）
TYPE_USAGE = "token_usage"
TYPE_QUOTA = "token_quota"

# per-user 锁注册表（单进程内）
_user_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _user_lock(user_id: int) -> asyncio.Lock:
    """获取（或创建）指定用户的串行锁。"""
    async with _locks_guard:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[user_id] = lock
        return lock


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _month_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _empty_data(user_id: int) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "request_count": 0,
        "daily_usage": {},
        "updated_at": None,
    }


def _today_usage(data: dict[str, Any]) -> dict[str, Any]:
    return (data.get("daily_usage") or {}).get(_today_key()) or {}


def _month_total(data: dict[str, Any]) -> int:
    """由 daily_usage 聚合当月 Token 总量（不单独落库）。"""
    prefix = _month_prefix()
    daily = data.get("daily_usage") or {}
    return sum(int(v.get("total_tokens", 0) or 0) for k, v in daily.items() if str(k).startswith(prefix))


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _snapshot(data: dict[str, Any], user_id: int) -> TokenUsageSnapshot:
    today = _today_usage(data)
    return TokenUsageSnapshot(
        user_id=user_id,
        input_tokens=_int(data.get("input_tokens")),
        output_tokens=_int(data.get("output_tokens")),
        total_tokens=_int(data.get("total_tokens")),
        request_count=_int(data.get("request_count")),
        today_input_tokens=_int(today.get("input_tokens")),
        today_output_tokens=_int(today.get("output_tokens")),
        today_total_tokens=_int(today.get("total_tokens")),
        today_request_count=_int(today.get("request_count")),
        month_total_tokens=_month_total(data),
        updated_at=data.get("updated_at"),
    )


class TokenService:
    """Token 使用 / 配额业务层（Meta 仓储经 MetaClient 访问）。"""

    def __init__(self, meta: MetaClient) -> None:
        self.meta = meta

    # ── 内部读取 ──────────────────────────────────────────

    async def _read_usage_data(self, user_id: int, user_token: str) -> dict[str, Any]:
        entry = await self.meta.get_entry(TYPE_USAGE, str(user_id), user_token=user_token)
        if entry is None:
            return _empty_data(user_id)
        data = entry.get("data") or {}
        data.setdefault("daily_usage", {})
        return data

    def _global_quota(self) -> TokenQuota:
        return TokenQuota(
            per_request_limit=settings.TOKEN_PER_REQUEST_LIMIT,
            daily_limit=settings.TOKEN_DAILY_LIMIT,
            monthly_limit=settings.TOKEN_MONTHLY_LIMIT,
            total_limit=settings.TOKEN_TOTAL_LIMIT,
        )

    async def _read_quota(self, user_id: int, user_token: str) -> TokenQuota:
        """生效配额：全局 settings + per-user 覆盖（token_quota entry）。"""
        quota = self._global_quota()
        try:
            entry = await self.meta.get_entry(TYPE_QUOTA, str(user_id), user_token=user_token)
        except MetaApiError as exc:
            logger.warning("读取 per-user 配额失败，回退全局配置: user=%s err=%s", user_id, exc)
            return quota
        if entry is None:
            return quota
        data = entry.get("data") or {}
        if data.get("per_request_limit") is not None:
            quota.per_request_limit = _int(data["per_request_limit"], quota.per_request_limit)
        if data.get("daily_limit") is not None:
            quota.daily_limit = _int(data["daily_limit"], quota.daily_limit)
        if data.get("total_limit") is not None:
            quota.total_limit = _int(data["total_limit"], quota.total_limit)
        return quota

    # ── check / consume ────────────────────────────────────

    async def check(
        self,
        user_id: int,
        estimated_tokens: int | None,
        user_token: str,
    ) -> CheckResponse:
        """LLM 调用前检查：保守防超卖。

        - estimated_tokens 提供时：单次估算不得超 per_request_limit，且
          当日已用 + 估算不得超 daily_limit；
        - 缺省时：仅当日已用已超 daily_limit 即拒绝；
        - Meta 不可用：fail-closed，allowed=false（reason=meta_unavailable）。
        """
        try:
            async with await _user_lock(user_id):
                data = await self._read_usage_data(user_id, user_token)
                quota = await self._read_quota(user_id, user_token)
        except MetaApiError as exc:
            logger.warning("check Meta 不可用（fail-closed）: user=%s err=%s", user_id, exc)
            return CheckResponse(
                allowed=False,
                reason="meta_unavailable",
                usage=_snapshot(_empty_data(user_id), user_id),
                quota=self._global_quota(),
            )

        snapshot = _snapshot(data, user_id)
        if estimated_tokens is not None:
            if estimated_tokens > quota.per_request_limit:
                reason = "per_request_exceeded"
                return CheckResponse(allowed=False, reason=reason, usage=snapshot, quota=quota)
            if snapshot.today_total_tokens + estimated_tokens > quota.daily_limit:
                reason = "daily_limit_exceeded"
                return CheckResponse(allowed=False, reason=reason, usage=snapshot, quota=quota)
        elif snapshot.today_total_tokens > quota.daily_limit:
            return CheckResponse(
                allowed=False,
                reason="daily_limit_exceeded",
                usage=snapshot,
                quota=quota,
            )

        # 生命周期累计总上限（无 estimated 时也拦截，因为不依赖本次量）
        if quota.total_limit is not None:
            est = estimated_tokens or 0
            if snapshot.total_tokens + est > quota.total_limit:
                return CheckResponse(
                    allowed=False,
                    reason="total_limit_exceeded",
                    usage=snapshot,
                    quota=quota,
                )

        return CheckResponse(allowed=True, reason=None, usage=snapshot, quota=quota)

    async def consume(
        self,
        user_id: int,
        input_tokens: int,
        output_tokens: int,
        model: str | None,
        provider: str | None,
        user_token: str,
    ) -> ConsumeResponse:
        """LLM 完成后记账：以真实 usage 累加（input+output）。

        超当日上限照常记账，响应带 over_limit=true（实际已发生，下一次 check 拦截）。
        """
        async with await _user_lock(user_id):
            data = await self._read_usage_data(user_id, user_token)
            quota = await self._read_quota(user_id, user_token)

            total = input_tokens + output_tokens
            today_key = _today_key()
            daily: dict[str, Any] = data.setdefault("daily_usage", {})
            today: dict[str, Any] = daily.get(today_key) or {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "request_count": 0,
            }
            today["input_tokens"] = _int(today.get("input_tokens")) + input_tokens
            today["output_tokens"] = _int(today.get("output_tokens")) + output_tokens
            today["total_tokens"] = _int(today.get("total_tokens")) + total
            today["request_count"] = _int(today.get("request_count")) + 1
            if model:
                today["model"] = model
            if provider:
                today["provider"] = provider
            daily[today_key] = today

            data["user_id"] = user_id
            data["input_tokens"] = _int(data.get("input_tokens")) + input_tokens
            data["output_tokens"] = _int(data.get("output_tokens")) + output_tokens
            data["total_tokens"] = _int(data.get("total_tokens")) + total
            data["request_count"] = _int(data.get("request_count")) + 1
            data["updated_at"] = datetime.now(timezone.utc).isoformat()

            existed = await self.meta.get_entry(TYPE_USAGE, str(user_id), user_token=user_token) is not None
            if existed:
                await self.meta.update_entry(TYPE_USAGE, str(user_id), data, user_token=user_token)
            else:
                await self.meta.create_entry(TYPE_USAGE, str(user_id), data, user_token=user_token)

            snapshot = _snapshot(data, user_id)
            over_limit = snapshot.today_total_tokens > quota.daily_limit
            if quota.total_limit is not None:
                over_limit = over_limit or snapshot.total_tokens > quota.total_limit
            return ConsumeResponse(usage=snapshot, quota=quota, over_limit=over_limit)

    # ── 展示查询（chat 门户转发，client 只读）────────────────

    async def get_usage(self, user_id: int, user_token: str) -> UsageResponse:
        data = await self._read_usage_data(user_id, user_token)
        return UsageResponse(usage=_snapshot(data, user_id))

    async def get_quota(self, user_id: int, user_token: str) -> QuotaResponse:
        quota = await self._read_quota(user_id, user_token)
        return QuotaResponse(quota=quota)

    async def get_remaining(self, user_id: int, user_token: str) -> RemainingResponse:
        data = await self._read_usage_data(user_id, user_token)
        quota = await self._read_quota(user_id, user_token)
        daily_used = _int(_today_usage(data).get("total_tokens"))
        return RemainingResponse(
            per_request_limit=quota.per_request_limit,
            daily_limit=quota.daily_limit,
            daily_used=daily_used,
            daily_remaining=max(0, quota.daily_limit - daily_used),
        )


__all__ = ["TokenService", "MetaApiError", "TYPE_USAGE", "TYPE_QUOTA"]
