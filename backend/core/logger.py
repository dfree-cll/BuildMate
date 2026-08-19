"""结构化日志（对标 EduAgent 3.3：logger.info("事件", key=value)）"""
import logging
import sys
from backend.config import get_settings


class _Logger:
    def __init__(self, name: str):
        self._log = logging.getLogger(name)

    @staticmethod
    def _fmt(event: str, **kw) -> str:
        if kw:
            return event + " | " + " ".join(f"{k}={v!r}" for k, v in kw.items())
        return event

    def debug(self, event: str, **kw):
        self._log.debug(self._fmt(event, **kw))

    def info(self, event: str, *args, **kw):
        if args:
            self._log.info(event, *args)
        else:
            self._log.info(self._fmt(event, **kw))

    def warning(self, event: str, *args, **kw):
        if args:
            self._log.warning(event, *args)
        else:
            self._log.warning(self._fmt(event, **kw))

    def error(self, event: str, **kw):
        exc_info = kw.pop("exc_info", False)
        self._log.error(self._fmt(event, **kw), exc_info=exc_info)

    def critical(self, event: str, **kw):
        self._log.critical(self._fmt(event, **kw))


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )
    for noisy in ("sqlalchemy.engine", "sqlalchemy.pool", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> _Logger:
    return _Logger(name)
