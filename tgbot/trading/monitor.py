"""Trading domain orchestrator.

Owns the long-lived asyncio tasks (WSS sessions per chain, MC alert
polling) and a single ``dispatch`` funnel that:
 1. dedups via ``seen_tx``;
 2. formats the event for Telegram;
 3. fans out to every ``allowed_user_ids`` chat.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from telegram.constants import ParseMode
from telegram.error import TelegramError

from . import formatters
from .db import TradingDB
from .evm import EvmMonitor
from .prices import PriceClient
from .solana import SolanaMonitor, WalletEvent

if TYPE_CHECKING:
    from telegram.ext import Application
    from ..config import Config

logger = logging.getLogger(__name__)


class TradingMonitor:
    """Single entry point that supervises chain monitors and dispatch."""

    def __init__(
        self,
        app: "Application",
        cfg: "Config",
        db: TradingDB,
        price_client: PriceClient,
    ):
        self._app = app
        self._cfg = cfg
        self._db = db
        self._prices = price_client
        self._chat_ids: list[int] = sorted(cfg.allowed_user_ids)
        assert cfg.trading is not None  # invariant: only built when enabled
        self._solana: Optional[SolanaMonitor] = (
            SolanaMonitor(
                api_key=cfg.trading.helius_api_key,
                db=db,
                on_event=self.dispatch,
            )
            if cfg.trading.helius_api_key
            else None
        )
        self._mc_alert_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._mc_poll_interval = int(cfg.trading.mc_poll_interval)
        self._evms: dict[str, EvmMonitor] = {}
        if cfg.trading.alchemy_api_key:
            for chain in cfg.trading.evm_chains:
                try:
                    self._evms[chain] = EvmMonitor(
                        chain=chain,
                        api_key=cfg.trading.alchemy_api_key,
                        db=db,
                        on_event=self.dispatch,
                    )
                except ValueError as e:
                    logger.warning("Skipping EVM chain %s: %s", chain, e)

    # ── lifecycle ─────────────────────────────────────────────────────
    async def start(self) -> None:
        chains = []
        if self._solana is not None:
            await self._solana.start()
            chains.append("sol")
        for chain, mon in self._evms.items():
            await mon.start()
            chains.append(chain)
        self._mc_alert_task = asyncio.create_task(
            self._mc_alert_loop(), name="mc-alert-loop"
        )
        logger.info(
            "Trading monitor started (%s; MC poll every %ds).",
            ", ".join(chains) or "no chains",
            self._mc_poll_interval,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._mc_alert_task is not None:
            try:
                await asyncio.wait_for(self._mc_alert_task, timeout=5)
            except asyncio.TimeoutError:
                self._mc_alert_task.cancel()
            finally:
                self._mc_alert_task = None
        if self._solana is not None:
            await self._solana.stop()
        for mon in self._evms.values():
            await mon.stop()
        await self._prices.close()
        logger.info("Trading monitor stopped.")

    # ── notifications from handlers (wallet add/remove) ───────────────
    def notify_wallets_changed(self, chain: Optional[str] = None) -> None:
        if (chain is None or chain == "sol") and self._solana is not None:
            self._solana.notify_wallets_changed()
        if chain is None:
            for mon in self._evms.values():
                mon.notify_wallets_changed()
        elif chain in self._evms:
            self._evms[chain].notify_wallets_changed()

    # ── event funnel ──────────────────────────────────────────────────
    async def dispatch(self, event: WalletEvent) -> None:
        """Dedup + format + fan-out for one normalized event."""
        if not self._db.mark_seen(event.chain, event.sig_or_hash):
            return  # already pushed

        text = self._render(event)
        await self._fanout(text)

    def _render(self, event: WalletEvent) -> str:
        if event.kind == "swap":
            return formatters.swap_message(
                chain=event.chain,
                wallet=event.wallet,
                wallet_label=event.wallet_label,
                sig_or_hash=event.sig_or_hash,
                side=event.side or "buy",
                token_symbol=event.token_symbol or "?",
                token_address=event.token_address or "?",
                amount=event.amount,
            )
        if event.kind == "transfer":
            return formatters.transfer_message(
                chain=event.chain,
                wallet=event.wallet,
                wallet_label=event.wallet_label,
                sig_or_hash=event.sig_or_hash,
                direction=event.direction or "in",
                counterparty=event.counterparty,
                token_symbol=event.token_symbol or "?",
                amount=event.amount,
            )
        return formatters.activity_message(
            chain=event.chain,
            wallet=event.wallet,
            wallet_label=event.wallet_label,
            sig_or_hash=event.sig_or_hash,
        )

    # ── MC alert polling ─────────────────────────────────────────────
    async def _mc_alert_loop(self) -> None:
        """Periodically poll Dexscreener for every armed alert and fire crossings."""
        while not self._stop_event.is_set():
            try:
                await self._mc_alert_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MC alert tick crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._mc_poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def _mc_alert_tick(self) -> None:
        alerts = self._db.list_alerts(armed_only=True)
        if not alerts:
            return
        for alert in alerts:
            info = await self._prices.get_token(alert["token_address"], alert["chain"])
            if info is None or info.mc_usd is None:
                continue
            crossed = (
                (alert["direction"] == "above" and info.mc_usd >= alert["mc_target"])
                or (alert["direction"] == "below" and info.mc_usd <= alert["mc_target"])
            )
            if not crossed:
                continue
            if not self._is_cooldown_clear(alert):
                continue
            await self._fire_alert(alert, info.mc_usd, info.symbol, info.pair_url)

    @staticmethod
    def _is_cooldown_clear(alert: dict) -> bool:
        last = alert.get("last_triggered_at")
        if not last:
            return True
        try:
            # SQLite CURRENT_TIMESTAMP is "YYYY-MM-DD HH:MM:SS" in UTC.
            last_dt = datetime.fromisoformat(str(last)).replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        cooldown_s = int(alert.get("cooldown_min", 60)) * 60
        return (datetime.now(timezone.utc) - last_dt).total_seconds() >= cooldown_s

    async def _fire_alert(
        self, alert: dict, mc_current: float, symbol: str, pair_url: Optional[str]
    ) -> None:
        persistent = bool(alert["persistent"])
        self._db.mark_alert_triggered(alert["id"], disarm=not persistent)
        text = formatters.mc_alert_message(
            alert_id=alert["id"],
            chain=alert["chain"],
            token_symbol=symbol or "?",
            token_address=alert["token_address"],
            direction=alert["direction"],
            mc_target=alert["mc_target"],
            mc_current=mc_current,
            pair_url=pair_url,
            label=alert.get("label"),
            persistent=persistent,
        )
        await self._fanout(text)

    async def _fanout(self, text: str) -> None:
        for chat_id in self._chat_ids:
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
            except TelegramError as e:
                logger.warning("Failed to send to %s: %s", chat_id, e)
            except Exception:
                logger.exception("Unexpected error sending to %s", chat_id)


# Convenience for hooking lifecycle into PTB's post_init/post_shutdown.
def install_monitor_lifecycle(app: "Application", monitor: TradingMonitor) -> None:
    """Chain post_init and post_shutdown so monitor.start/stop run with PTB."""
    prev_init = app.post_init
    prev_shutdown = app.post_shutdown

    async def post_init(application):
        if prev_init is not None:
            await prev_init(application)
        await monitor.start()

    async def post_shutdown(application):
        try:
            await monitor.stop()
        finally:
            if prev_shutdown is not None:
                await prev_shutdown(application)

    app.post_init = post_init
    app.post_shutdown = post_shutdown
