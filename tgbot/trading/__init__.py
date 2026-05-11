"""Trading domain: on-chain wallet activity monitoring + MC alerts.

Single integration point with the bot: ``register_trading(app, cfg)``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application
    from ..config import Config

logger = logging.getLogger(__name__)


def register_trading(app: "Application", cfg: "Config") -> None:
    """Wire trading handlers + monitor into the Telegram Application.

    No-op when ``cfg.trading`` is missing or disabled — keeping the bot
    100% functional without any trading API keys configured.
    """
    if cfg.trading is None or not cfg.trading.enabled:
        logger.debug("Trading module disabled (no [trading] config or enabled=false).")
        return

    # Local imports: optional deps (aiohttp/websockets) only loaded when enabled.
    from .db import TradingDB
    from .handlers import register_handlers
    from .monitor import TradingMonitor, install_monitor_lifecycle
    from .prices import PriceClient

    db = TradingDB(cfg.data_dir / "trading.db")
    price_client = PriceClient()
    monitor = TradingMonitor(app, cfg, db, price_client)

    register_handlers(app, cfg, db, monitor)
    install_monitor_lifecycle(app, monitor)

    logger.info("Trading module registered (chains: sol + %s).",
                ", ".join(cfg.trading.evm_chains))
