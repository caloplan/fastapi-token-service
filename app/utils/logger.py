import logging
import os
from logging.handlers import RotatingFileHandler

from app.core.config import settings

_LOGGER_NAME = "token_service"
_initialized = False


def setup_logger() -> logging.Logger:
    """初始化并返回全局日志器（支持控制台 + 文件轮转）。"""
    global _initialized
    logger = logging.getLogger(_LOGGER_NAME)
    if _initialized:
        return logger
    logger.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # 控制台
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    # 文件轮转
    log_dir = os.path.dirname(settings.LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        settings.LOG_FILE,
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    _initialized = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """获取子日志器。"""
    base = setup_logger()
    if name:
        return base.getChild(name)
    return base
