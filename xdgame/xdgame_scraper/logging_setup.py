from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path


DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
SENSITIVE_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|token|cookie|authorization|x-auth-token)"
    r"(\s*[=:]\s*)([^\s,;]+)"
)
BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


@dataclass(frozen=True)
class LoggingConfig:
    path: Path
    max_bytes: int = DEFAULT_MAX_BYTES
    backup_count: int = DEFAULT_BACKUP_COUNT


class RuntimeFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id
        self.sensitive_values = _environment_secrets()

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        # 日志参数只在过滤器中展开，确保控制台与文件两个出口使用同一套脱敏结果。
        record.msg = redact_message(record.getMessage(), self.sensitive_values)
        record.args = ()
        return True


def _environment_secrets() -> tuple[str, ...]:
    values = (
        os.environ.get("XDGAME_USERNAME"),
        os.environ.get("XDGAME_PASSWORD"),
    )
    return tuple(value for value in values if value)


def redact_message(message: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = SENSITIVE_PATTERN.sub(r"\1\2***", message)
    redacted = BEARER_PATTERN.sub("Bearer ***", redacted)
    for value in sensitive_values:
        redacted = redacted.replace(value, "***")
    return redacted


def configure_logging(config: LoggingConfig) -> str:
    run_id = uuid.uuid4().hex[:12]
    config.path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [run=%(run_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    runtime_filter = RuntimeFilter(run_id)
    console = logging.StreamHandler()
    file_handler = RotatingFileHandler(
        config.path,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    for handler in (console, file_handler):
        handler.setFormatter(formatter)
        handler.addFilter(runtime_filter)

    package_logger = logging.getLogger("xdgame_scraper")
    for handler in package_logger.handlers:
        handler.close()
    package_logger.handlers.clear()
    package_logger.setLevel(logging.INFO)
    package_logger.propagate = False
    package_logger.addHandler(console)
    package_logger.addHandler(file_handler)
    return run_id
