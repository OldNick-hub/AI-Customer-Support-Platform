"""Safe Logging Infrastructure with Automatic PII Sanitization.

Provides filters and formatters that ensure no customer PII (credit cards,
emails, phone numbers, SSNs, CVVs) or authentication secrets leak into
application log streams.
"""

import logging
from typing import Optional

from app.services.pii_masker import PIIMasker, get_pii_masker


class SafeLoggingFilter(logging.Filter):
    """Logging filter that scrubs PII from log record messages and arguments.

    Prevents credentials, credit cards, SSNs, phone numbers, and emails from leaking
    into log aggregators, console streams, or persistent log files.
    """

    def __init__(self, name: str = "", masker: Optional[PIIMasker] = None) -> None:
        super().__init__(name)
        self.masker = masker or get_pii_masker()

    def filter(self, record: logging.LogRecord) -> bool:
        """Sanitizes record.msg and record.args before emission."""
        # Sanitize record.msg
        if isinstance(record.msg, str):
            record.msg = self.masker.mask_text(record.msg)
        elif isinstance(record.msg, dict):
            record.msg = self.masker.sanitize_dict(record.msg)

        # Sanitize record.args if present
        if record.args:
            if isinstance(record.args, tuple):
                sanitized_args = []
                for arg in record.args:
                    if isinstance(arg, str):
                        sanitized_args.append(self.masker.mask_text(arg))
                    elif isinstance(arg, dict):
                        sanitized_args.append(self.masker.sanitize_dict(arg))
                    else:
                        sanitized_args.append(arg)
                record.args = tuple(sanitized_args)
            elif isinstance(record.args, dict):
                record.args = self.masker.sanitize_dict(record.args)

        return True


class PIISanitizingFormatter(logging.Formatter):
    """Logging formatter that ensures the final rendered log string contains no PII."""

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        style: str = "%",
        validate: bool = True,
        masker: Optional[PIIMasker] = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt, style=style, validate=validate)
        self.masker = masker or get_pii_masker()

    def format(self, record: logging.LogRecord) -> str:
        """Formats the record and applies final PII masking to the resulting string."""
        formatted = super().format(record)
        return self.masker.mask_text(formatted)


def configure_safe_logger(
    logger: Optional[logging.Logger] = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configures a logger with the SafeLoggingFilter and PIISanitizingFormatter."""
    target_logger = logger or logging.getLogger("app")
    target_logger.setLevel(level)

    # Attach filter if not already attached
    has_filter = any(isinstance(f, SafeLoggingFilter) for f in target_logger.filters)
    if not has_filter:
        target_logger.addFilter(SafeLoggingFilter())

    return target_logger

