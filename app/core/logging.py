import logging
import re
from logging.config import dictConfig

from app.core.config import Settings

SENSITIVE_LOG_KEYS = ("password", "token", "api_key", "apikey", "authorization", "credential", "secret")
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)(password|token|api[_-]?key|authorization|credential|secret)(['\"]?\s*[:=]\s*['\"]?)[^,'\"\s}]+"
)
XML_PAYLOAD_PATTERN = re.compile(r"(?is)<\?xml.*|<Invoice\b.*")


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_text(record.msg)
        for key, value in list(record.__dict__.items()):
            if any(marker in key.lower() for marker in SENSITIVE_LOG_KEYS):
                record.__dict__[key] = "<redacted>"
            elif isinstance(value, str):
                record.__dict__[key] = _redact_text(value)
        return True


def configure_logging(settings: Settings) -> None:
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "redaction": {
                    "()": RedactionFilter,
                }
            },
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["redaction"],
                }
            },
            "root": {
                "handlers": ["console"],
                "level": settings.log_level,
            },
        }
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _redact_text(value: str) -> str:
    redacted = SENSITIVE_VALUE_PATTERN.sub(r"\1\2<redacted>", value)
    if XML_PAYLOAD_PATTERN.search(redacted):
        return "<redacted-payload>"
    return redacted
