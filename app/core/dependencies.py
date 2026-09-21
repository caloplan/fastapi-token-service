"""认证与访问控制依赖。

- get_current_user：从 JWT 解析当前用户（对齐 mservice-fastapi-user 认证协议）；
- require_internal_token：写端点（check/consume）访问控制——仅持内部密钥的
  受信服务（chat service）可调，client 无法触达写接口（选择性暴露）。
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import decode_token
from app.utils.logger import get_logger

logger = get_logger("dependencies")

# tokenUrl 指向 user-service 的登录端点（OAuth2 交互文档用；本服务自身无登录路由）
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

INTERNAL_KEY_HEADER = "x-token-internal-key"


class CurrentUser(BaseModel):
    """当前登录用户（从 JWT payload 解析，不落库）。"""

    sub: str
    user_id: int
    service_name: str
    role: str
    type: str


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
) -> CurrentUser:
    """从 JWT 解析当前用户信息（user-service 签发）。

    认证成功后执行服务名白名单校验：
    - superuser（三重 AND 判定通过）直接放行；
    - 其余用户要求 JWT 中的 service_name 命中 ALLOWED_SERVICE_NAMES，否则 403。
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = await decode_token(token)
        if payload.get("type") != "access":
            raise credentials_exception
        user_id = payload.get("user_id")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    except HTTPException:
        raise
    except Exception:
        raise credentials_exception

    user = CurrentUser(
        sub=payload.get("sub", ""),
        user_id=user_id,
        service_name=payload.get("service_name", "default"),
        role=payload.get("role", "user"),
        type=payload.get("type", "access"),
    )

    # 服务名白名单：非 superuser 必须命中 ALLOWED_SERVICE_NAMES
    if not is_superuser(user) and user.service_name not in settings.ALLOWED_SERVICE_NAMES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"服务 {user.service_name} 不在访问白名单中",
        )

    return user


def is_superuser(user: CurrentUser) -> bool:
    """判定是否为超级用户（三重 AND 校验，缺一不可）。"""
    if user.role != "superuser":
        return False
    if user.sub not in settings.SUPERUSER_USERNAMES:
        return False
    if user.user_id not in settings.SUPERUSER_USER_IDS:
        return False
    return True


async def require_internal_token(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """写端点访问控制：内部密钥校验（选择性暴露）。

    仅持 X-Token-Internal-Key（命中 TOKEN_INTERNAL_KEYS）的受信服务可调用
    check / consume。配置为空时一律 403（安全默认，防误开放）。
    """
    provided = request.headers.get(INTERNAL_KEY_HEADER, "")
    keys = settings.TOKEN_INTERNAL_KEYS
    if not keys:
        logger.warning("写端点被调用但 TOKEN_INTERNAL_KEYS 未配置，拒绝")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="写端点仅限内部服务调用（未配置内部密钥）",
        )
    if provided not in keys:
        logger.warning("写端点内部密钥校验失败: user=%s", current_user.user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="写端点仅限内部服务调用",
        )
    return current_user


async def require_superuser(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """管理接口依赖：非超级用户统一返回 403。"""
    if not is_superuser(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅超级用户可执行此操作",
        )
    return current_user


__all__ = [
    "CurrentUser",
    "get_current_user",
    "is_superuser",
    "require_internal_token",
    "require_superuser",
    "INTERNAL_KEY_HEADER",
]
