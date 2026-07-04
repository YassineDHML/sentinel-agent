"""Stdlib logging configuration for Sentinel.

All Sentinel modules log under the ``sentinel`` logger namespace. Call
:func:`setup_logging` once at process start (entrypoints / ``__main__`` blocks);
library modules should just use :func:`get_logger` and never configure handlers
themselves.

Level resolution order (highest priority first):
    1. explicit ``level`` argument to :func:`setup_logging`
    2. ``LOG_LEVEL`` environment variable
    3. the module default (``INFO``)
"""

from __future__ import annotations

import logging
import os
import sys

DEFAULT_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

ROOT_LOGGER_NAME = "sentinel"

_configured = False


def setup_logging(level: str | None = None) -> logging.Logger:
    """Configure the ``sentinel`` logger with a single stdout handler.

    Idempotent: repeated calls only update the level, never stack handlers.

    Args:
        level: An explicit level name (e.g. ``"DEBUG"``). If ``None``, falls back
            to the ``LOG_LEVEL`` env var, then to :data:`DEFAULT_LEVEL`.

    Returns:
        The configured ``sentinel`` root logger.
    """
    global _configured

    resolved = (level or os.environ.get("LOG_LEVEL") or DEFAULT_LEVEL).upper()

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(resolved)

    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        logger.handlers.clear()
        logger.addHandler(handler)
        # Don't propagate to the root logger — avoids duplicate lines if the
        # host process also configured the root logger.
        logger.propagate = False
        _configured = True

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child logger, e.g. ``sentinel.collect.rss``.

    Args:
        name: Short module name (``"config"``, ``"collect.rss"``, ...).
    """
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
