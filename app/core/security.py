"""JWT 校验模块：作为 user-service 的兄弟微服务，消费其签发的 JWT 令牌。

对齐 mservice-fastapi-user 的认证协议（RS256 + kid + JWKS 多密钥轮换）：
1. 读取 JWT Header 中的 kid；
2. 本地 JWKS 缓存命中则用对应公钥验证；
3. 未命中或缓存过期则从 user-service 强制刷新 JWKS 后再验证；
4. 兼容 user-service 的密钥轮换（多 kid）。

本服务不签发令牌，无需私钥，只持有公钥（JWKS）。
"""

import time
from typing import Any

import httpx
from jose import JWTError, jwt

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger("security")

# JWKS 缓存：{kid: public_key_pem}，带过期时间
_jwks_cache: dict[str, str] = {}
_jwks_cache_expires_at: float = 0.0


def _jwks_url() -> str:
    """构造 user-service 的 JWKS 端点 URL。"""
    base = settings.USER_SERVICE_URL.rstrip("/")
    return f"{base}/.well-known/jwks.json"


async def _fetch_jwks() -> dict[str, Any]:
    """从 user-service 拉取 JWKS，返回 {kid: public_key_pem} 映射。"""
    url = _jwks_url()
    logger.info("正在从 user-service 拉取 JWKS: %s", url)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    keys = data.get("keys", [])
    jwks_map: dict[str, Any] = {}
    for key in keys:
        kid = key.get("kid")
        if kid:
            jwks_map[kid] = key  # 保留完整 JWK 字典，python-jose 可直接用
    logger.info("JWKS 拉取成功，共 %s 个 kid", len(jwks_map))
    return jwks_map


async def _get_jwks(force_refresh: bool = False) -> dict[str, Any]:
    """获取 JWKS 缓存，过期或强制刷新时重新拉取。"""
    global _jwks_cache, _jwks_cache_expires_at

    now = time.time()
    if not force_refresh and _jwks_cache and now < _jwks_cache_expires_at:
        return _jwks_cache

    try:
        _jwks_cache = await _fetch_jwks()
        _jwks_cache_expires_at = now + settings.JWKS_CACHE_TTL_SECONDS
    except Exception as exc:
        # 拉取失败时，如果有旧缓存则继续使用旧缓存（降级），否则抛出
        if _jwks_cache:
            logger.warning("JWKS 刷新失败，继续使用旧缓存: %s", exc)
        else:
            logger.error("JWKS 拉取失败且无缓存: %s", exc)
            raise
    return _jwks_cache


def _kid_from_token(token: str) -> str | None:
    """读取 JWT Header 中的 kid；解析失败返回 None。"""
    try:
        return jwt.get_unverified_header(token).get("kid")
    except Exception:
        return None


async def decode_token(token: str) -> dict[str, Any]:
    """使用 user-service 对应 kid 的公钥解码并校验 JWT，失败抛出 JWTError。"""
    kid = _kid_from_token(token)

    jwks = await _get_jwks(force_refresh=False)

    public_key: Any = None
    if kid is not None and kid in jwks:
        public_key = jwks[kid]
    else:
        # kid 未命中：强制刷新 JWKS（可能 user-service 刚轮换了密钥）
        logger.info("JWT kid=%s 未命中本地缓存，强制刷新 JWKS", kid)
        jwks = await _get_jwks(force_refresh=True)
        if kid is not None and kid in jwks:
            public_key = jwks[kid]

    if public_key is None:
        raise JWTError(f"未知的 kid: {kid}（user-service JWKS 中不存在该公钥）")

    return jwt.decode(token, public_key, algorithms=[settings.ALGORITHM])


__all__ = ["decode_token", "JWTError"]
