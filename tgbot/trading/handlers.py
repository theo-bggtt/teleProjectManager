"""Telegram handlers for the trading module.

Registered via ``register_trading`` (see ``__init__.py``). Adding handlers
here does NOT touch the existing ``bot.on_callback`` — we install a
namespaced ``CallbackQueryHandler(pattern=r"^trd:")`` instead.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application
    from ..config import Config

logger = logging.getLogger(__name__)


def register_handlers(app: "Application", cfg: "Config") -> None:
    """Register trading command + callback handlers on the Application."""
    # Implemented incrementally — step 4 fills this in.
    logger.debug("register_handlers: stub (trading commands not yet wired).")
