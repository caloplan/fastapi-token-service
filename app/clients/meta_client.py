"""Meta Storage HTTP 客户端（mservice-fastapi-metastorage）。

本服务通过它读写 token_usage / token_quota 两个 type：
- 透传当前请求的用户 JWT（Authorization: Bearer <token>），身份与数据隔离由
  meta 侧 Scope 保证；entity_key = str(user_id)；
- get_entry 对 404 返回 None（首用惰性创建）；其余错误抛 MetaApiError。

数据格式约定：entry.data 使用 snake_case（user_id / updated_at / daily_usage 等）。
"""

from typing import Any

import httpx

from app.utils.logger import get_logger

logger = get_logger("meta_client")


class MetaApiError(Exception):
    """Meta 服务调用失败（已映射为可读消息）。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _extract_detail(payload: dict[str, Any]) -> str:
    """从 FastAPI 错误响应中提取可读 detail（兼容 str / 校验错误列表）。"""
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc", []))
                msg = item.get("msg", "")
                parts.append(f"{loc}: {msg}" if loc else msg)
            else:
                parts.append(str(item))
        return "; ".join(parts) if parts else "请求校验失败"
    return str(detail) if detail else "请求失败"


class MetaClient:
    """meta-service 客户端（单实例复用 httpx 连接池）。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        user_token: str,
        json_body: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> Any:
        """统一请求入口：注入当前用户 JWT、映射错误。

        allow_404=True 时 404 返回 None（用于 get 单实体）；其余状态码一律抛 MetaApiError。
        """
        if not user_token:
            raise MetaApiError("当前请求缺少访问令牌，无法调用 meta-service")
        headers = {"Authorization": f"Bearer {user_token}"}
        try:
            resp = await self._http.request(
                method,
                path,
                headers=headers,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise MetaApiError(f"meta-service 连接失败: {exc}") from exc

        if resp.status_code == 404 and allow_404:
            return None

        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            detail = _extract_detail(payload) if isinstance(payload, dict) else str(payload)
            message = {
                401: f"meta-service 认证失败: {detail}",
                403: f"meta-service 无权限: {detail}",
                404: f"meta-service 资源不存在: {detail}",
                409: f"meta-service 冲突: {detail}",
                422: f"meta-service 校验失败: {detail}",
            }.get(resp.status_code, f"meta-service 错误({resp.status_code}): {detail}")
            raise MetaApiError(message, status_code=resp.status_code)

        if resp.status_code == 204:
            return None
        try:
            return resp.json()
        except Exception as exc:
            raise MetaApiError(f"meta-service 响应解析失败: {exc}") from exc

    # ── 实体 CRUD（/api/v1/entries）──────────────────────────

    async def create_entry(
        self,
        type_name: str,
        entity_key: str,
        data: dict[str, Any],
        *,
        user_token: str,
    ) -> dict[str, Any]:
        """创建实体元数据（201 返回完整 entry）。"""
        body: dict[str, Any] = {"type_name": type_name, "entity_key": entity_key, "data": data}
        return await self._request("POST", "/api/v1/entries", user_token=user_token, json_body=body)

    async def get_entry(self, type_name: str, entity_key: str, *, user_token: str) -> dict[str, Any] | None:
        """获取实体（404 返回 None）。"""
        return await self._request(
            "GET",
            f"/api/v1/entries/{type_name}/{entity_key}",
            user_token=user_token,
            allow_404=True,
        )

    async def update_entry(
        self,
        type_name: str,
        entity_key: str,
        data: dict[str, Any],
        *,
        user_token: str,
    ) -> dict[str, Any]:
        """部分更新实体（data deep merge，版本自增）。"""
        return await self._request(
            "PUT",
            f"/api/v1/entries/{type_name}/{entity_key}",
            user_token=user_token,
            json_body={"data": data},
        )


_client: MetaClient | None = None


def get_meta_client() -> MetaClient:
    """进程级单例 meta 客户端（lazy 创建，连接池复用）。"""
    global _client
    if _client is None:
        from app.core.config import settings

        _client = MetaClient(settings.META_SERVICE_URL, timeout=settings.META_TIMEOUT_SECONDS)
    return _client


async def close_meta_client() -> None:
    """关闭单例连接池（应用退出时调用）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


__all__ = ["MetaApiError", "MetaClient", "get_meta_client", "close_meta_client"]
