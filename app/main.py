from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import api_router
from app.clients.meta_client import close_meta_client
from app.core.config import settings
from app.utils.logger import setup_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：初始化日志，关闭时释放 meta 连接池。"""
    logger = setup_logger()
    logger.info("正在启动 %s v%s (env=%s)", settings.APP_NAME, settings.APP_VERSION, settings.APP_ENV)
    logger.info("user-service 对接地址: %s", settings.USER_SERVICE_URL)
    logger.info("meta-service 对接地址: %s", settings.META_SERVICE_URL)
    try:
        yield
    finally:
        await close_meta_client()
        logger.info("应用已关闭")


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="CaloPlan LLM Token 使用记录 / 配额管理 / 防滥用微服务："
        "JWT 认证消费 user-service RS256 令牌；写端点（check/consume）内部密钥保护，"
        "查询端点经 chat service 门户转发。不单独引入数据库，复用 Meta Storage。",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 业务路由
    app.include_router(api_router)

    # 健康检查
    @app.get("/health", tags=["系统"], summary="健康检查")
    async def health_check() -> dict[str, str]:
        return {"status": "healthy", "service": settings.APP_NAME, "version": settings.APP_VERSION}

    @app.get("/", tags=["系统"], summary="服务信息")
    async def root() -> dict[str, str]:
        return {
            "name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "docs": "/docs",
            "health": "/health",
            "user_service": settings.USER_SERVICE_URL,
            "meta_service": settings.META_SERVICE_URL,
        }

    return app


app = create_app()
