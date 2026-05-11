"""Telegram handlers for the trading module.

Step 4 of the plan: command handlers backed by the trading DB.
Monitor + WSS integration is added in later steps.

Registered via ``register_trading`` (see ``__init__.py``). Adding handlers
here does NOT touch the existing ``bot.on_callback`` — namespaced
callbacks are routed via ``CallbackQueryHandler(pattern=r"^trd:")`` added
in step 9.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from ..auth import restricted
from .db import TradingDB

if TYPE_CHECKING:
    from telegram.ext import Application
    from ..config import Config
    from .monitor import TradingMonitor

logger = logging.getLogger(__name__)

SUPPORTED_CHAINS = ("sol", "eth", "base", "bsc")

_SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def validate_address(address: str, chain: str) -> bool:
    if chain == "sol":
        return bool(_SOL_ADDR_RE.match(address))
    if chain in ("eth", "base", "bsc"):
        return bool(_EVM_ADDR_RE.match(address))
    return False


def _normalize_address(address: str, chain: str) -> str:
    """EVM addresses are case-insensitive: lowercase for canonical storage."""
    return address.lower() if chain in ("eth", "base", "bsc") else address


def _parse_mc(raw: str) -> float | None:
    """Accept '1000000', '1m', '1.5k', '2.5B' etc."""
    s = raw.strip().lower().replace(",", "").replace("_", "").replace("$", "")
    if not s:
        return None
    mult = 1.0
    if s[-1] in "kmbt":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[s[-1]]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _fmt_mc(mc: float) -> str:
    if mc >= 1e9:
        return f"{mc / 1e9:.2f}B"
    if mc >= 1e6:
        return f"{mc / 1e6:.2f}M"
    if mc >= 1e3:
        return f"{mc / 1e3:.2f}K"
    return f"{mc:.2f}"


def register_handlers(
    app: "Application",
    cfg: "Config",
    db: TradingDB,
    monitor: "TradingMonitor",
) -> None:
    """Register trading command handlers on the Application."""
    auth = restricted(cfg.allowed_user_ids)

    # ── /watch <addr> <chain> [label] ──────────────────────────────────
    @auth
    async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if len(ctx.args) < 2:
            await update.message.reply_text(
                "Usage: `/watch <address> <chain> [label]`\n"
                f"chain ∈ {{ {', '.join(SUPPORTED_CHAINS)} }}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        address = ctx.args[0]
        chain = ctx.args[1].lower()
        label = " ".join(ctx.args[2:]) or None
        if chain not in SUPPORTED_CHAINS:
            await update.message.reply_text(
                f"Unknown chain `{chain}`. Supported: {', '.join(SUPPORTED_CHAINS)}.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if not validate_address(address, chain):
            await update.message.reply_text(
                f"Invalid {chain.upper()} address: `{address}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        norm = _normalize_address(address, chain)
        if db.add_wallet(norm, chain, label):
            monitor.notify_wallets_changed(chain)
            tag = f" ({label})" if label else ""
            await update.message.reply_text(
                f"✅ Watching `{norm}` on *{chain.upper()}*{tag}",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                f"Already watching `{norm}` on *{chain.upper()}*.",
                parse_mode=ParseMode.MARKDOWN,
            )

    # ── /unwatch <addr> [chain] ────────────────────────────────────────
    @auth
    async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text(
                "Usage: `/unwatch <address> [chain]`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        address = ctx.args[0]
        chain = ctx.args[1].lower() if len(ctx.args) > 1 else None
        norm = _normalize_address(address, chain) if chain else address.lower()
        # Try both raw + lowered if chain is unknown — best effort
        removed = db.remove_wallet(norm, chain)
        if removed == 0 and chain is None:
            removed = db.remove_wallet(address)
        if removed:
            monitor.notify_wallets_changed(chain)
        await update.message.reply_text(
            f"Removed {removed} wallet entr{'y' if removed == 1 else 'ies'}."
        )

    # ── /wallets ───────────────────────────────────────────────────────
    @auth
    async def cmd_wallets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        wallets = db.list_wallets()
        if not wallets:
            await update.message.reply_text(
                "No wallets watched. Use `/watch <addr> <chain>`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        lines = ["*Watched wallets*"]
        for w in wallets:
            label = f" — _{w['label']}_" if w["label"] else ""
            lines.append(f"`{w['address']}` *{w['chain'].upper()}*{label}")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )

    # ── /alert <token_addr> <chain> <mc> [--above|--below] [--persistent] [label...] ─
    @auth
    async def cmd_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        args = list(ctx.args)
        if len(args) < 3:
            await update.message.reply_text(
                "Usage: `/alert <token> <chain> <mc> [--above|--below] [--persistent] [label...]`\n"
                "mc supports k/m/b suffix (e.g. `1m`, `500k`).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # Strip flags
        direction = "above"
        persistent = False
        rest = []
        for a in args:
            al = a.lower()
            if al == "--above":
                direction = "above"
            elif al == "--below":
                direction = "below"
            elif al == "--persistent":
                persistent = True
            else:
                rest.append(a)
        if len(rest) < 3:
            await update.message.reply_text(
                "Need at least `<token> <chain> <mc>`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        token, chain_raw, mc_raw, *label_parts = rest
        chain = chain_raw.lower()
        if chain not in SUPPORTED_CHAINS:
            await update.message.reply_text(
                f"Unknown chain `{chain}`. Supported: {', '.join(SUPPORTED_CHAINS)}.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if not validate_address(token, chain):
            await update.message.reply_text(
                f"Invalid {chain.upper()} token address: `{token}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        mc = _parse_mc(mc_raw)
        if mc is None or mc <= 0:
            await update.message.reply_text(
                f"Invalid market cap: `{mc_raw}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        label = " ".join(label_parts) or None
        norm = _normalize_address(token, chain)
        aid = db.add_alert(
            token_address=norm, chain=chain, mc_target=mc,
            direction=direction, persistent=persistent, label=label,
        )
        arrow = "≥" if direction == "above" else "≤"
        kind = "persistent" if persistent else "one-shot"
        tag = f" — _{label}_" if label else ""
        await update.message.reply_text(
            f"🔔 Alert *#{aid}* armed: `{norm}` MC {arrow} *${_fmt_mc(mc)}* "
            f"({chain.upper()}, {kind}){tag}",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── /alerts ────────────────────────────────────────────────────────
    @auth
    async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        alerts = db.list_alerts()
        if not alerts:
            await update.message.reply_text(
                "No alerts configured. Use `/alert <token> <chain> <mc>`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        lines = ["*MC alerts*"]
        for a in alerts:
            arrow = "≥" if a["direction"] == "above" else "≤"
            state = "🟢" if a["armed"] else "⚫"
            kind = " (persistent)" if a["persistent"] else ""
            label = f" — _{a['label']}_" if a["label"] else ""
            lines.append(
                f"{state} *#{a['id']}* `{a['token_address']}` "
                f"*{a['chain'].upper()}* {arrow} ${_fmt_mc(a['mc_target'])}{kind}{label}"
            )
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )

    # ── /unalert <id> ──────────────────────────────────────────────────
    @auth
    async def cmd_unalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text(
                "Usage: `/unalert <id>`", parse_mode=ParseMode.MARKDOWN,
            )
            return
        try:
            aid = int(ctx.args[0])
        except ValueError:
            await update.message.reply_text(f"Invalid id: `{ctx.args[0]}`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        ok = db.remove_alert(aid)
        await update.message.reply_text(
            f"Deleted alert #{aid}." if ok else f"No alert #{aid}."
        )

    # ── /holdings <wallet> <chain> ─────────────────────────────────────
    @auth
    async def cmd_holdings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if len(ctx.args) < 2:
            await update.message.reply_text(
                "Usage: `/holdings <wallet_address> <chain>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        wallet = ctx.args[0]
        chain = ctx.args[1].lower()
        if chain not in SUPPORTED_CHAINS:
            await update.message.reply_text(
                f"Unknown chain `{chain}`.", parse_mode=ParseMode.MARKDOWN,
            )
            return
        if not validate_address(wallet, chain):
            await update.message.reply_text(
                f"Invalid {chain.upper()} address.", parse_mode=ParseMode.MARKDOWN,
            )
            return
        norm = _normalize_address(wallet, chain)
        await update.message.reply_text("⏳ Fetching holdings…")
        try:
            if chain == "sol":
                from .solana import fetch_solana_holdings
                holdings, _ = await fetch_solana_holdings(
                    monitor.helius_api_key, norm
                )
            else:
                from .evm import fetch_evm_holdings
                holdings, native_value = await fetch_evm_holdings(
                    chain, monitor.alchemy_api_key, norm,
                    price_client=monitor.price_client,
                )
        except Exception as e:
            logger.exception("holdings fetch failed")
            await update.message.reply_text(
                f"Error fetching holdings: `{type(e).__name__}: {e}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        total = sum((h.value_usd or 0) for h in holdings) or None
        from .formatters import holdings_message
        text = holdings_message(chain, norm, holdings, total)
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
        )

    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("unalert", cmd_unalert))
    app.add_handler(CommandHandler("holdings", cmd_holdings))
