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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..auth import restricted
from .db import TradingDB

if TYPE_CHECKING:
    from telegram.ext import Application
    from ..config import Config
    from .monitor import TradingMonitor

logger = logging.getLogger(__name__)

SUPPORTED_CHAINS = ("sol", "eth", "base", "bsc")

TRD_WADD_CHAIN = 800
TRD_WADD_ADDR = 801
TRD_WADD_LABEL = 802

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
    *,
    wizard_step=None,
    wizard_finish=None,
    wizard_escape=None,
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

    # ── inline UI (callback_data namespace "trd:") ─────────────────────
    def _home_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👛 Wallets surveillés", callback_data="trd:wallets")],
            [InlineKeyboardButton("🔔 Alertes MC", callback_data="trd:alerts")],
            [InlineKeyboardButton("💰 Holdings", callback_data="trd:hold")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")],
        ])

    def _wallets_markup(wallets: list[dict]) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton("➕ Ajouter wallet", callback_data="trd:wadd")],
        ]
        for w in wallets:
            tag = f" — {w['label']}" if w["label"] else ""
            short = f"{w['address'][:4]}…{w['address'][-4:]}"
            rows.append([
                InlineKeyboardButton(
                    f"{w['chain'].upper()} {short}{tag}",
                    callback_data=f"trd:whold:{w['chain']}:{w['address']}",
                ),
                InlineKeyboardButton(
                    "🗑", callback_data=f"trd:wdel:{w['chain']}:{w['address']}"
                ),
            ])
        rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="trd:home")])
        return InlineKeyboardMarkup(rows)

    def _alerts_markup(alerts: list[dict]) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton("➕ Créer alerte", callback_data="trd:aadd")],
        ]
        for a in alerts:
            arrow = "≥" if a["direction"] == "above" else "≤"
            state = "🟢" if a["armed"] else "⚫"
            rows.append([
                InlineKeyboardButton(
                    f"{state} #{a['id']} {a['chain'].upper()} {arrow}${_fmt_mc(a['mc_target'])}",
                    callback_data=f"trd:anoop:{a['id']}",
                ),
                InlineKeyboardButton("🗑", callback_data=f"trd:adel:{a['id']}"),
            ])
        rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="trd:home")])
        return InlineKeyboardMarkup(rows)

    async def _render_home(query) -> None:
        text = (
            "*📈 Trading*\n"
            "Surveille les wallets on-chain et reçois des alertes de marketcap."
        )
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=_home_markup(),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def _render_wallets(query) -> None:
        wallets = db.list_wallets()
        if not wallets:
            text = "*Wallets surveillés*\nAucun. Utilise `/watch <addr> <chain>`."
        else:
            lines = ["*Wallets surveillés*"]
            for w in wallets:
                label = f" — _{w['label']}_" if w["label"] else ""
                lines.append(f"`{w['address']}` *{w['chain'].upper()}*{label}")
            text = "\n".join(lines)
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=_wallets_markup(wallets),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def _render_alerts(query) -> None:
        alerts = db.list_alerts()
        if not alerts:
            text = "*Alertes MC*\nAucune. Utilise `/alert <token> <chain> <mc>`."
        else:
            lines = ["*Alertes MC*"]
            for a in alerts:
                arrow = "≥" if a["direction"] == "above" else "≤"
                state = "🟢" if a["armed"] else "⚫"
                label = f" — _{a['label']}_" if a["label"] else ""
                lines.append(
                    f"{state} *#{a['id']}* `{a['token_address']}` "
                    f"{a['chain'].upper()} {arrow}${_fmt_mc(a['mc_target'])}{label}"
                )
            text = "\n".join(lines)
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=_alerts_markup(alerts),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def _render_holdings_picker(query) -> None:
        wallets = db.list_wallets()
        if not wallets:
            rows = [[
                InlineKeyboardButton("👛 Aller aux Wallets", callback_data="trd:wallets"),
            ], [
                InlineKeyboardButton("⬅️ Retour", callback_data="trd:home"),
            ]]
            await query.edit_message_text(
                "*💰 Holdings*\nAucun wallet surveillé. Ajoute-en un d'abord.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        rows: list[list[InlineKeyboardButton]] = []
        for w in wallets:
            tag = f" — {w['label']}" if w["label"] else ""
            short = f"{w['address'][:4]}…{w['address'][-4:]}"
            rows.append([InlineKeyboardButton(
                f"{w['chain'].upper()} {short}{tag}",
                callback_data=f"trd:hget:{w['chain']}:{w['address']}",
            )])
        rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="trd:home")])
        await query.edit_message_text(
            "*💰 Holdings*\nChoisis un wallet :",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _render_holdings_for(query, chain: str, addr: str) -> None:
        await query.edit_message_text(
            f"⏳ Fetching holdings for `{addr[:6]}…{addr[-4:]}`…",
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            if chain == "sol":
                from .solana import fetch_solana_holdings
                holdings, _ = await fetch_solana_holdings(monitor.helius_api_key, addr)
            else:
                from .evm import fetch_evm_holdings
                holdings, _ = await fetch_evm_holdings(
                    chain, monitor.alchemy_api_key, addr,
                    price_client=monitor.price_client,
                )
        except Exception as e:
            logger.exception("inline holdings failed")
            await query.edit_message_text(
                f"Error: `{type(e).__name__}: {e}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Retour", callback_data="trd:hold")]]
                ),
            )
            return
        total = sum((h.value_usd or 0) for h in holdings) or None
        from .formatters import holdings_message
        text = holdings_message(chain, addr, holdings, total)
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Retour", callback_data="trd:hold")]]
            ),
        )

    @auth
    async def on_trading_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        parts = data.split(":")
        ns = parts[0]
        if ns != "trd":
            return  # safety: shouldn't reach here due to pattern filter
        action = parts[1] if len(parts) > 1 else "home"

        if action == "home":
            await _render_home(query)
            return
        if action == "wallets":
            await _render_wallets(query)
            return
        if action == "alerts":
            await _render_alerts(query)
            return
        if action == "wdel" and len(parts) >= 4:
            chain, addr = parts[2], parts[3]
            if db.remove_wallet(addr, chain):
                monitor.notify_wallets_changed(chain)
            await _render_wallets(query)
            return
        if action == "adel" and len(parts) >= 3:
            try:
                aid = int(parts[2])
            except ValueError:
                return
            db.remove_alert(aid)
            await _render_alerts(query)
            return
        if action == "whold" and len(parts) >= 4:
            chain, addr = parts[2], parts[3]
            await query.edit_message_text(
                f"⏳ Fetching holdings for `{addr[:6]}…{addr[-4:]}`…",
                parse_mode=ParseMode.MARKDOWN,
            )
            try:
                if chain == "sol":
                    from .solana import fetch_solana_holdings
                    holdings, _ = await fetch_solana_holdings(monitor.helius_api_key, addr)
                else:
                    from .evm import fetch_evm_holdings
                    holdings, _ = await fetch_evm_holdings(
                        chain, monitor.alchemy_api_key, addr,
                        price_client=monitor.price_client,
                    )
            except Exception as e:
                logger.exception("inline holdings failed")
                await query.edit_message_text(
                    f"Error: `{type(e).__name__}: {e}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Retour", callback_data="trd:wallets")]]
                    ),
                )
                return
            total = sum((h.value_usd or 0) for h in holdings) or None
            from .formatters import holdings_message
            text = holdings_message(chain, addr, holdings, total)
            await query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Retour", callback_data="trd:wallets")]]
                ),
            )
            return
        if action == "hold":
            await _render_holdings_picker(query)
            return
        if action == "hget" and len(parts) >= 4:
            chain, addr = parts[2], parts[3]
            if not chain or not addr:
                return
            await _render_holdings_for(query, chain, addr)
            return
        if action == "anoop":
            # No-op: row labels (no state change needed)
            return

    # ── Add Wallet wizard ──────────────────────────────────────────────────
    async def wadd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        rows = [[
            InlineKeyboardButton(c.upper(), callback_data=f"trd:wadd:chain:{c}")
            for c in SUPPORTED_CHAINS
        ]]
        await wizard_step(update, ctx, "➕ Ajouter wallet\n\nChoisis la chaîne :", extra_rows=rows)
        return TRD_WADD_CHAIN

    async def wadd_chain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 4:
            return TRD_WADD_CHAIN
        chain = parts[3]
        if chain not in SUPPORTED_CHAINS:
            return TRD_WADD_CHAIN
        ctx.user_data["wadd_chain"] = chain
        await wizard_step(update, ctx, f"Chaîne : *{chain.upper()}*\n\nEnvoie l'adresse du wallet :")
        return TRD_WADD_ADDR

    async def wadd_addr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        chain = ctx.user_data.get("wadd_chain")
        if not chain:
            await wizard_step(update, ctx, "⚠️ État perdu.")
            return ConversationHandler.END
        addr = update.message.text.strip()
        if not validate_address(addr, chain):
            await wizard_step(
                update, ctx,
                f"❌ Adresse invalide pour *{chain.upper()}*. Réessaie :",
            )
            return TRD_WADD_ADDR
        ctx.user_data["wadd_addr"] = _normalize_address(addr, chain)
        await wizard_step(update, ctx, "Label optionnel (ou tape `skip`) :")
        return TRD_WADD_LABEL

    async def wadd_label(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        text = update.message.text.strip()
        label = None if text.lower() == "skip" else text
        chain = ctx.user_data.get("wadd_chain")
        addr = ctx.user_data.get("wadd_addr")
        if not chain or not addr:
            await wizard_step(update, ctx, "⚠️ État perdu.")
            return ConversationHandler.END
        if db.add_wallet(addr, chain, label):
            monitor.notify_wallets_changed(chain)
            msg = f"✅ Watching `{addr}` sur *{chain.upper()}*"
        else:
            msg = f"ℹ️ Déjà surveillé : `{addr}` sur *{chain.upper()}*"
        await wizard_step(update, ctx, msg)
        for k in ("wadd_chain", "wadd_addr"):
            ctx.user_data.pop(k, None)
        await wizard_finish(update, ctx)
        return ConversationHandler.END

    async def wadd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        for k in ("wadd_chain", "wadd_addr"):
            ctx.user_data.pop(k, None)
        await wizard_finish(update, ctx)
        return ConversationHandler.END

    wadd_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(wadd_start, pattern=r"^trd:wadd$")],
        states={
            TRD_WADD_CHAIN: [CallbackQueryHandler(wadd_chain, pattern=r"^trd:wadd:chain:")],
            TRD_WADD_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, wadd_addr)],
            TRD_WADD_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, wadd_label)],
        },
        fallbacks=[
            CallbackQueryHandler(wadd_cancel, pattern=r"^wiz:cancel$"),
            CallbackQueryHandler(wizard_escape),
        ],
    )
    app.add_handler(wadd_conv, group=-1)

    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("unalert", cmd_unalert))
    app.add_handler(CommandHandler("holdings", cmd_holdings))
    # Must be registered BEFORE the catch-all bot.on_callback in bot.py;
    # PTB matches handlers in registration order and stops at first hit.
    # Since register_trading() runs after build_app() finishes adding the
    # catch-all, we add it into group=-1 so it has higher priority.
    app.add_handler(
        CallbackQueryHandler(on_trading_callback, pattern=r"^trd:"),
        group=-1,
    )
