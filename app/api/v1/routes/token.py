"""Token 路由：check / consume（内部）+ usage / quota / remaining（查询，经 chat 门户转发）。

认证与访问控制：
- 全部端点先经 get_current_user（RS256 + JWKS），user_id 唯一来自 JWT，
  请求体 / 查询参数不接受 user_id（防伪造）；
- 写端点（POST /check、POST /consume）追加内部密钥校验（require_internal_token，
  X-Token-Internal-Key），client 无法触达；
- 查询端点由 chat service 门户转发（同构响应），client 不直连本服务。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.clients.meta_client import MetaApiError
from app.clients.meta_client import get_meta_client
from app.core.dependencies import (
    CurrentUser,
    get_current_user,
    require_internal_token,
)
from app.schemas.token import (
    CheckRequest,
    CheckResponse,
    ConsumeRequest,
    ConsumeResponse,
    QuotaResponse,
    RemainingResponse,
    UsageResponse,
)
from app.services.token_service import TokenService

router = APIRouter(prefix="/token", tags=["Token"])


def _token_service() -> TokenService:
    return TokenService(get_meta_client())


def _extract_bearer(request: Request) -> str | None:
    """从 Authorization 头提取原始 JWT（透传给 meta-service）。"""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _user_token_or_503(request: Request) -> str:
    token = _extract_bearer(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无法验证凭据",
        )
    return token


@router.post(
    "/check",
    response_model=CheckResponse,
    summary="LLM 调用前检查（内部：需内部密钥）",
    description="受信服务（chat service）调用；client 不可访问。Meta 不可用时 fail-closed（allowed=false）。",
)
async def check(
    request: Request,
    body: CheckRequest,
    current_user: Annotated[CurrentUser, Depends(require_internal_token)],
) -> CheckResponse:
    token = _user_token_or_503(request)
    return await _token_service().check(current_user.user_id, body.estimated_tokens, token)


@router.post(
    "/consume",
    response_model=ConsumeResponse,
    summary="LLM 完成后记账（内部：需内部密钥）",
    description="受信服务（chat service）调用；usage 以 Server 获得的真实值为准，不接受客户端伪造。",
)
async def consume(
    request: Request,
    body: ConsumeRequest,
    current_user: Annotated[CurrentUser, Depends(require_internal_token)],
) -> ConsumeResponse:
    token = _user_token_or_503(request)
    try:
        return await _token_service().consume(
            current_user.user_id,
            body.input_tokens,
            body.output_tokens,
            body.model,
            body.provider,
            token,
        )
    except MetaApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Meta 服务暂不可用: {exc.message}",
        ) from exc


@router.get(
    "/usage",
    response_model=UsageResponse,
    summary="查询当前用户 Token 使用（经 chat 门户转发，client 只读）",
)
async def get_usage(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> UsageResponse:
    token = _user_token_or_503(request)
    try:
        return await _token_service().get_usage(current_user.user_id, token)
    except MetaApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Meta 服务暂不可用: {exc.message}",
        ) from exc


@router.get(
    "/quota",
    response_model=QuotaResponse,
    summary="查询当前用户生效配额（经 chat 门户转发，client 只读）",
)
async def get_quota(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> QuotaResponse:
    token = _user_token_or_503(request)
    try:
        return await _token_service().get_quota(current_user.user_id, token)
    except MetaApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Meta 服务暂不可用: {exc.message}",
        ) from exc


@router.get(
    "/remaining",
    response_model=RemainingResponse,
    summary="查询当前用户剩余额度（经 chat 门户转发，client 只读）",
)
async def get_remaining(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> RemainingResponse:
    token = _user_token_or_503(request)
    try:
        return await _token_service().get_remaining(current_user.user_id, token)
    except MetaApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Meta 服务暂不可用: {exc.message}",
        ) from exc
