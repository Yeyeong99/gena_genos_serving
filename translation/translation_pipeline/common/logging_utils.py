"""Small logging helpers for pipeline progress output."""

from __future__ import annotations

import logging
from typing import Any


_logger = logging.getLogger("uvicorn.error")


def log_info(*args: Any, sep: str = " ", end: str = "\n", flush: bool = False) -> None:
    """Log print-style progress messages through the app logger."""

    message = sep.join(str(arg) for arg in args)
    if end and end != "\n":
        message += end.rstrip("\n")
    _logger.info(message)


__all__ = ["log_info"]
