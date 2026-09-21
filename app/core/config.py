from typing import Annotated, List

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置，从环境变量 / .env 加载。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用
    APP_NAME: str = "Token Service"
    APP_VERSION: str = "1.0.0"
    APP_ENV: str = "development"
    DEBUG: bool = True

    # 服务
    HOST: str = "0.0.0.0"
    PORT: int = 9096

    # user-service 对接（JWT 消费方）
    # 本服务不签发令牌，只消费 user-service 签发的 RS256 JWT（JWKS 多 kid 校验）。
    USER_SERVICE_URL: str = "http://localhost:8000"
    JWKS_CACHE_TTL_SECONDS: int = 3600
    ALGORITHM: str = "RS256"

    # 超级用户白名单（三重 AND 校验：role=superuser + username 匹配 + user_id 匹配）
    # NoDecode：跳过 pydantic-settings 对 env 的自动 JSON 解码，空串/逗号串交给 _parse_list
    SUPERUSER_USERNAMES: Annotated[List[str], NoDecode] = ["superuser"]
    SUPERUSER_USER_IDS: Annotated[List[int], NoDecode] = [1]

    # 服务名白名单：非 superuser 的请求必须携带 service_name 且命中本白名单
    ALLOWED_SERVICE_NAMES: Annotated[List[str], NoDecode] = ["default"]

    # Meta Storage 对接（复用 mservice-fastapi-metastorage，不引入新数据库）
    # 本服务直接 HTTP 调用 meta-service 读写 token_usage / token_quota 两个 type；
    # 透传当前请求 JWT，身份与数据隔离由 meta 侧 Scope 保证。
    META_SERVICE_URL: str = "http://localhost:9093"
    META_TIMEOUT_SECONDS: float = 10.0

    # 配额（额度规则统一由本服务管理，Chat Service 不硬编码）
    TOKEN_PER_REQUEST_LIMIT: int = 15000   # 单次请求 Token 上限
    TOKEN_DAILY_LIMIT: int = 100000        # 每日 Token 上限
    TOKEN_MONTHLY_LIMIT: int | None = None  # 预留，不实现强制
    TOKEN_TOTAL_LIMIT: int | None = None  # 生命周期累计总上限（None=不限）

    # 选择性暴露：写端点（POST /check、POST /consume）的内部访问密钥
    # chat service 持有并随请求头 X-Token-Internal-Key 透传；支持多值（轮换）。
    # 为空时写端点一律 403（安全默认，防误开放）。
    TOKEN_INTERNAL_KEYS: Annotated[List[str], NoDecode] = []

    # 故障策略：Meta 不可用时 check 一律拒绝（fail-closed，承担防滥用职责）
    TOKEN_FAIL_CLOSED: bool = True

    # 日志
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "logs/token_service.log"
    LOG_MAX_BYTES: int = 10 * 1024 * 1024
    LOG_BACKUP_COUNT: int = 5

    # CORS
    ALLOWED_ORIGINS: Annotated[List[str], NoDecode] = ["http://localhost:3000", "http://localhost:9096"]

    @field_validator(
        "ALLOWED_ORIGINS",
        "SUPERUSER_USERNAMES",
        "SUPERUSER_USER_IDS",
        "ALLOWED_SERVICE_NAMES",
        "TOKEN_INTERNAL_KEYS",
        mode="before",
    )
    @classmethod
    def _parse_list(cls, v):
        """支持从 .env 读取 JSON 数组字符串。"""
        if isinstance(v, str):
            import json

            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return [item.strip() for item in v.split(",") if item.strip()]
        return v


settings = Settings()
