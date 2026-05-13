"""Telegram bot command handlers."""
import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .auth import restricted
from .config import Config
from .db import DB
from .files import FileManager, PathEscapeError
from .runner import make_runner
from .shell import ShellRunner
from .trading import register_trading
from .scheduler import register_scheduler

logger = logging.getLogger(__name__)

# /config conversation states
CFG_SELECT, CFG_START_CMD, CFG_STOP_CMD, CFG_ENTRY_FILE = range(4)
# add-project conversation states (from inline button)
ADD_NAME, ADD_PATH = range(4, 6)
# add-action conversation states
ADD_A_NAME, ADD_A_COMMAND, ADD_A_CWD, ADD_A_MODE, ADD_A_CONFIRM = range(6, 11)
PROJ_SHELL_CMD = 700

# Telegram message limit is ~4096; leave headroom for markdown fences
INLINE_TEXT_LIMIT = 3500
PAGE_SIZE = 10


HELP_TEXT = """*Telegram Project Manager*

*Projects*
`/projects` — list with running status
`/add <name> <path>` — register a folder
`/config <name>` — set start command + entry file
`/remove <name>` — unregister (files untouched)

*Running*
`/run <name>` — start the project
`/stop <name>` — kill the tmux session
`/restart <name>` — stop + start
`/status <name>` — show config + status
`/logs <name> [lines]` — recent output

*Files*
`/ls <name> [subpath]` — list directory
`/get <name> <path>` — download file as document
To upload: send a document with caption `/put <name> <path>`

*Shell*
`/shell <name> <command...>` — run inside project dir

*Actions*
`/actions` — list saved actions
`/addaction` — create a new action (interactive)
`/runaction <name>` — execute an action by name
`/delaction <name>` — delete an action

*Trading* (if enabled)
`/watch <addr> <chain> [label]` — track a wallet
`/unwatch <addr> [chain]` — stop tracking
`/wallets` — list watched wallets
`/alert <token> <chain> <mc> [--above|--below] [--persistent]` — MC alert
`/alerts` — list alerts
`/unalert <id>` — delete alert
`/holdings <wallet> <chain>` — snapshot positions + total USD

`/cancel` exits /config flow."""


def _md_code_block(text: str) -> str:
    """Wrap text in a markdown code fence."""
    return f"```\n{text}\n```"


async def _send_text_or_file(update: Update, text: str, filename: str,
                              header: str | None = None) -> None:
    """Send `text` inline if short, otherwise as a document.

    Uses ``effective_message`` so this works both for direct commands
    (``update.message``) and inline-button callbacks (``update.callback_query``).
    """
    message = update.effective_message
    if len(text) <= INLINE_TEXT_LIMIT:
        prefix = f"`{header}`\n" if header else ""
        await message.reply_text(
            prefix + _md_code_block(text), parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.reply_document(
            document=BytesIO(text.encode()),
            filename=filename,
            caption=header,
        )


# callback_data uses ":" as separator — project names with ":" would break parsing.
# In practice names are alphanum/_/- so we accept that limitation.
def _main_menu_markup(trading_enabled: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("📂 Projets", callback_data="menu:projects"),
        InlineKeyboardButton("🚀 Actions", callback_data="menu:actions"),
    ]]
    rows.append([InlineKeyboardButton("⏰ Planifié", callback_data="sched:list")])
    if trading_enabled:
        rows.append([InlineKeyboardButton("📈 Trading", callback_data="trd:home")])
    rows.append([
        InlineKeyboardButton("⚙️ Admin", callback_data="menu:admin"),
        InlineKeyboardButton("❓ Aide", callback_data="menu:help"),
    ])
    return InlineKeyboardMarkup(rows)


def _admin_menu_markup(notifications_enabled: bool = True) -> InlineKeyboardMarkup:
    notifs_label = (
        "🔔 Notifs : ON" if notifications_enabled else "🔕 Notifs : OFF"
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(notifs_label, callback_data="bot:notifs")],
        [InlineKeyboardButton("🔄 Redémarrer le bot", callback_data="bot:restart")],
        [InlineKeyboardButton("📥 Update bot", callback_data="bot:update")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")],
    ])


def _bot_restart_confirm_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Oui, redémarrer", callback_data="bot:restart_do"),
            InlineKeyboardButton("❌ Annuler", callback_data="menu:admin"),
        ],
    ])


def _bot_update_confirm_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Oui, update + restart", callback_data="bot:update_do"),
            InlineKeyboardButton("❌ Annuler", callback_data="menu:admin"),
        ],
    ])


def _projects_list_markup(projects: list[dict], statuses: dict[str, bool]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ Ajouter un projet", callback_data="menu:add")]]
    for p in projects:
        icon = "🟢" if statuses.get(p["name"]) else "⚪"
        rows.append([
            InlineKeyboardButton(f"{icon} {p['name']}", callback_data=f"proj:{p['name']}"),
            InlineKeyboardButton("🗑", callback_data=f"act:del:{p['name']}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def _project_actions_markup(name: str, running: bool) -> InlineKeyboardMarkup:
    run_btn = (
        InlineKeyboardButton("⏹ Stop", callback_data=f"act:stop:{name}")
        if running
        else InlineKeyboardButton("▶️ Run", callback_data=f"act:run:{name}")
    )
    return InlineKeyboardMarkup([
        [run_btn, InlineKeyboardButton("🔄 Restart", callback_data=f"act:restart:{name}")],
        [
            InlineKeyboardButton("📄 Logs", callback_data=f"act:logs:{name}"),
            InlineKeyboardButton("ℹ️ Status", callback_data=f"act:status:{name}"),
        ],
        [
            InlineKeyboardButton("⚙️ Config", callback_data=f"proj:cfg:{name}"),
            InlineKeyboardButton("📁 Fichiers", callback_data=f"proj:files:{name}"),
        ],
        [
            InlineKeyboardButton("💻 Shell", callback_data=f"proj:shell:{name}"),
            InlineKeyboardButton("⬅️ Retour", callback_data="menu:projects"),
        ],
    ])


def _confirm_markup(action: str, name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirmer", callback_data=f"cfm:{action}:{name}"),
            InlineKeyboardButton("❌ Annuler", callback_data=f"proj:{name}"),
        ],
    ])


def _actions_list_markup(actions: list[dict], statuses: dict[str, bool]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ Nouvelle action", callback_data="actions:new")]]
    for a in actions:
        mode = a.get("mode", "oneshot")
        if mode == "managed":
            icon = "🟢" if statuses.get(a["name"]) else "🔁"
        else:
            icon = "⚡"
        rows.append([
            InlineKeyboardButton(f"{icon} {a['name']}", callback_data=f"actions:{a['name']}"),
            InlineKeyboardButton("🗑", callback_data=f"act_a:del:{a['name']}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def _action_card_markup(name: str, mode: str, running: bool) -> InlineKeyboardMarkup:
    if mode == "managed":
        run_btn = (
            InlineKeyboardButton("⏹ Stop", callback_data=f"act_a:stop:{name}")
            if running
            else InlineKeyboardButton("▶️ Démarrer", callback_data=f"act_a:run:{name}")
        )
        rows = [
            [run_btn, InlineKeyboardButton("🔄 Redémarrer", callback_data=f"act_a:restart:{name}")],
            [
                InlineKeyboardButton("📄 Logs", callback_data=f"act_a:logs:{name}"),
                InlineKeyboardButton("🗑 Supprimer", callback_data=f"act_a:del:{name}"),
            ],
            [InlineKeyboardButton("⬅️ Retour", callback_data="menu:actions")],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton("▶️ Exécuter", callback_data=f"act_a:run:{name}"),
                InlineKeyboardButton("🗑 Supprimer", callback_data=f"act_a:del:{name}"),
            ],
            [InlineKeyboardButton("⬅️ Retour", callback_data="menu:actions")],
        ]
    return InlineKeyboardMarkup(rows)


def _action_confirm_markup(verb: str, name: str) -> InlineKeyboardMarkup:
    """Yes/No confirmation for an action (verb is 'run' or 'del')."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirmer", callback_data=f"cfm_a:{verb}:{name}"),
            InlineKeyboardButton("❌ Annuler", callback_data=f"cfm_a:no:{name}"),
        ],
    ])



def _exec_restart() -> None:
    """Replace the current process image with a fresh `python -m tgbot …` run.

    Works under systemd (PID is preserved, the supervisor sees no death) and
    in dev (terminal/foreground). On Windows ``os.execv`` does not truly replace
    the parent, so we spawn detached and force-exit instead.
    """
    args = [sys.executable, "-m", "tgbot", *sys.argv[1:]]
    logger.warning("Re-executing bot: %s", args)
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, creationflags=creationflags, close_fds=True)
        os._exit(0)
    os.execv(sys.executable, args)


def _action_runner_key(name: str) -> str:
    """Prefix action names when handed to the runner so they don't collide with projects."""
    return f"action_{name}"


def _files_slug(rel_path: str) -> str:
    """8-char base32 hash of a relative path, callback_data-safe."""
    if rel_path in ("", "."):
        return "_"
    digest = hashlib.sha1(rel_path.encode("utf-8")).digest()[:5]
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def _files_resolve(ctx: ContextTypes.DEFAULT_TYPE, slug: str) -> str | None:
    """Return the rel path mapped to slug, or None if expired/unknown."""
    if slug == "_":
        return "."
    mapping = ctx.chat_data.get("files_path_map") or {}
    return mapping.get(slug)


def _files_remember(ctx: ContextTypes.DEFAULT_TYPE, rel_path: str) -> str:
    """Compute slug for rel_path and store mapping in chat_data."""
    slug = _files_slug(rel_path)
    mapping = ctx.chat_data.setdefault("files_path_map", {})
    mapping[slug] = rel_path
    return slug


def build_app(cfg: Config) -> Application:
    db = DB(cfg.data_dir / "projects.db")
    runner = make_runner(cfg.data_dir / "logs")
    files_mgr = FileManager(cfg.data_dir / "backups")
    shell = ShellRunner(timeout=cfg.shell_timeout)
    auth = restricted(cfg.allowed_user_ids)
    trading_enabled = bool(cfg.trading and cfg.trading.enabled)

    # Populated at the end of build_app once register_scheduler runs.
    # Wrapped in a dict so closures can read the latest value after init.
    scheduler_db_holder: dict = {}

    # ─── /help ────────────────────────────────────────────────────────────
    @auth
    async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    # ─── /start (inline menu) ─────────────────────────────────────────────
    @auth
    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "*Menu principal*\nChoisis une action :",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_main_menu_markup(trading_enabled),
        )

    async def _stop_project(name: str) -> bool:
        """Stop a project: run its stop_command (if any) then kill the tracked process."""
        proj = db.get_project(name)
        ran_custom = False
        if proj and proj.get("stop_command"):
            try:
                await shell.run(proj["stop_command"], proj["path"])
                ran_custom = True
            except Exception as e:
                logger.warning("stop_command failed for %s: %s", name, e)
        killed = await runner.stop(name)
        return ran_custom or killed

    async def _restart_project(name: str) -> tuple[bool, str]:
        proj = db.get_project(name)
        if not proj or not proj.get("start_command"):
            return False, "Not configured."
        await _stop_project(name)
        await asyncio.sleep(0.3)
        return await runner.start(name, proj["start_command"], proj["path"])

    async def _project_card_text(name: str) -> tuple[str, bool] | None:
        proj = db.get_project(name)
        if not proj:
            return None
        running = await runner.is_running(name)
        text = (
            f"*{name}*\n"
            f"Path: `{proj['path']}`\n"
            f"Start: `{proj.get('start_command') or '(unset)'}`\n"
            f"Stop: `{proj.get('stop_command') or '(default kill)'}`\n"
            f"Entry: `{proj.get('entry_file') or '(unset)'}`\n"
            f"Status: {'🟢 running' if running else '⚪ stopped'}"
        )
        return text, running

    async def _render_project_card(query, name: str) -> None:
        result = await _project_card_text(name)
        if result is None:
            await query.edit_message_text(
                f"Projet `{name}` introuvable.", parse_mode=ParseMode.MARKDOWN,
            )
            return
        text, running = result
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_project_actions_markup(name, running),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def _render_projects_list(query) -> None:
        projs = db.list_projects()
        statuses = {p["name"]: await runner.is_running(p["name"]) for p in projs}
        text = "*Projets*" if projs else "*Projets*\nAucun projet enregistré."
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_projects_list_markup(projs, statuses),
        )

    async def _render_files(
        query, ctx: ContextTypes.DEFAULT_TYPE, name: str, rel: str,
    ) -> None:
        proj = db.get_project(name)
        if not proj:
            await query.answer(text=f"Projet {name} introuvable", show_alert=True)
            return
        try:
            entries = files_mgr.list_dir(proj["path"], rel)
        except (PathEscapeError, FileNotFoundError, NotADirectoryError) as e:
            await query.answer(text=str(e), show_alert=True)
            return

        dirs, files = [], []
        for e in entries:
            if e.endswith("/"):
                dirs.append(e.rstrip("/"))
            else:
                files.append(e)
        dirs.sort()
        files.sort()
        items = [(d, True) for d in dirs] + [(f, False) for f in files]

        page_key = f"files_page:{name}:{_files_slug(rel)}"
        page = ctx.chat_data.get(page_key, 0)
        total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        ctx.chat_data[page_key] = page
        slice_ = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

        rows: list[list[InlineKeyboardButton]] = []
        for entry_name, is_dir in slice_:
            child_rel = f"{rel}/{entry_name}" if rel not in ("", ".") else entry_name
            child_slug = _files_remember(ctx, child_rel)
            if is_dir:
                rows.append([InlineKeyboardButton(
                    f"📁 {entry_name}/",
                    callback_data=f"proj:files:{name}:{child_slug}",
                )])
            else:
                rows.append([InlineKeyboardButton(
                    f"📄 {entry_name}",
                    callback_data=f"proj:fget:{name}:{child_slug}",
                )])

        cur_slug = _files_slug(rel)
        if total_pages > 1:
            rows.append([
                InlineKeyboardButton("◀️", callback_data=f"proj:fpg:{name}:{cur_slug}:prev"),
                InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="wiz:noop"),
                InlineKeyboardButton("▶️", callback_data=f"proj:fpg:{name}:{cur_slug}:next"),
            ])

        if rel not in ("", "."):
            parent = "/".join(rel.split("/")[:-1]) or "."
            parent_slug = _files_remember(ctx, parent)
            rows.append([InlineKeyboardButton(
                "⬆️ Parent", callback_data=f"proj:files:{name}:{parent_slug}",
            )])

        rows.append([InlineKeyboardButton("⬅️ Retour", callback_data=f"proj:{name}")])

        text = f"*📁 Fichiers — {name}*\nChemin : `{rel}`"
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    # ─── action helpers ──────────────────────────────────────────────────
    async def _format_action_card(action: dict) -> tuple[str, bool]:
        mode = action.get("mode", "oneshot")
        running = (
            await runner.is_running(_action_runner_key(action["name"]))
            if mode == "managed"
            else False
        )
        status = (
            f"Statut : {'🟢 running' if running else '⚪ stopped'}"
            if mode == "managed"
            else "Statut : ⚡ oneshot (déclencher pour exécuter)"
        )
        confirm = "🛡 confirmation requise" if action.get("require_confirm") else "déclenchement direct"
        text = (
            f"*{action['name']}*\n"
            f"Commande : `{action['command']}`\n"
            f"Dossier : `{action.get('cwd') or '(héritage du bot)'}`\n"
            f"Mode : `{mode}` — {confirm}\n"
            f"{status}"
        )
        return text, running

    async def _render_action_card(query, name: str) -> None:
        action = db.get_action(name)
        if not action:
            await query.edit_message_text(
                f"Action `{name}` introuvable.", parse_mode=ParseMode.MARKDOWN,
            )
            return
        text, running = await _format_action_card(action)
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_action_card_markup(name, action.get("mode", "oneshot"), running),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def _render_actions_list(query) -> None:
        actions = db.list_actions()
        statuses = {
            a["name"]: await runner.is_running(_action_runner_key(a["name"]))
            for a in actions
            if a.get("mode") == "managed"
        }
        text = "*Actions*" if actions else "*Actions*\nAucune action enregistrée."
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_actions_list_markup(actions, statuses),
        )

    def _wizard_markup(extra_rows: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
        """Optional extra rows + Cancel row.

        Main-menu buttons are intentionally omitted while a wizard is awaiting input —
        clicking e.g. *Projets* mid-wizard would be nonsensical. Callers pass step-specific
        buttons (project picker, ⏭️ Passer, etc.) via ``extra_rows``.
        """
        rows: list[list[InlineKeyboardButton]] = []
        if extra_rows:
            rows.extend(extra_rows)
        rows.append([InlineKeyboardButton("❌ Annuler", callback_data="wiz:cancel")])
        return InlineKeyboardMarkup(rows)

    async def _wizard_step(
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        text: str,
        extra_rows: list[list[InlineKeyboardButton]] | None = None,
    ) -> None:
        """Edit (or create) the single wizard message with the current question."""
        markup = _wizard_markup(extra_rows)
        msg_id = ctx.user_data.get("wizard_msg_id")
        chat_id = ctx.user_data.get("wizard_chat_id")
        if msg_id is None and update.callback_query is not None:
            msg_id = update.callback_query.message.message_id
            chat_id = update.callback_query.message.chat_id
            ctx.user_data["wizard_msg_id"] = msg_id
            ctx.user_data["wizard_chat_id"] = chat_id
        if msg_id is not None and chat_id is not None:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup,
                )
                return
            except BadRequest:
                ctx.user_data.pop("wizard_msg_id", None)
                ctx.user_data.pop("wizard_chat_id", None)
        # Fallback path (existing message edit failed or no message yet)
        if update.effective_message is None:
            return
        sent = await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
        )
        ctx.user_data["wizard_msg_id"] = sent.message_id
        ctx.user_data["wizard_chat_id"] = sent.chat_id

    async def _wizard_finish(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Replace the wizard message with the clean main menu. Clean user_data."""
        msg_id = ctx.user_data.get("wizard_msg_id")
        chat_id = ctx.user_data.get("wizard_chat_id")
        for k in (
            "wizard_msg_id", "wizard_chat_id",
            "add_name",
            "addact_name", "addact_command", "addact_cwd", "addact_mode",
            "cfg_project",
            "shell_project",
            "sched",
        ):
            ctx.user_data.pop(k, None)
        text = "*Menu principal*\nChoisis une action :"
        markup = _main_menu_markup(trading_enabled)
        if msg_id is not None and chat_id is not None:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup,
                )
                return
            except BadRequest:
                pass
        if update.effective_message is None:
            return
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup,
        )

    async def _execute_action(update: Update, query, action: dict) -> None:
        """Run an action — oneshot via shell, managed via runner."""
        name = action["name"]
        mode = action.get("mode", "oneshot")
        cwd = action.get("cwd") or None
        if mode == "managed":
            ok, msg = await runner.start(
                _action_runner_key(name),
                action["command"],
                cwd or str(Path.cwd()),
            )
            prefix = "▶️" if ok else "⚠️"
            await query.edit_message_text(
                f"{prefix} `{name}` : {msg}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_action_card_markup(name, mode, ok),
            )
            return
        # oneshot
        await query.edit_message_text(
            f"⏳ Exécution de `{name}`…", parse_mode=ParseMode.MARKDOWN,
        )
        rc, out = await shell.run(action["command"], cwd)
        body = out or "(aucune sortie)"
        # send result; keep the action card available as a follow-up
        await _send_text_or_file(
            update, body, f"{name}-output.txt", header=f"{name} — exit {rc}",
        )
        # restore the card so the user can re-run
        text, running = await _format_action_card(action)
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_action_card_markup(name, mode, running),
        )

    @auth
    async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data or ""

        if data == "wiz:cancel":
            await _wizard_finish(update, ctx)
            return

        parts = data.split(":", 2)
        ns = parts[0] if parts else ""

        if ns == "menu":
            target = parts[1] if len(parts) > 1 else "home"
            if target == "home":
                await query.edit_message_text(
                    "*Menu principal*\nChoisis une action :",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_main_menu_markup(trading_enabled),
                )
            elif target == "projects":
                await _render_projects_list(query)
            elif target == "actions":
                await _render_actions_list(query)
            elif target == "help":
                await query.edit_message_text(
                    HELP_TEXT,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")]]
                    ),
                )
            elif target == "admin":
                await query.edit_message_text(
                    "*⚙️ Admin*\nOpérations sur le bot lui-même :",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_admin_menu_markup(
                        scheduler_db_holder["sdb"].get_notifications_enabled()
                        if scheduler_db_holder.get("sdb")
                        else True
                    ),
                )
            return

        if ns == "bot":
            target = parts[1] if len(parts) > 1 else ""
            if target == "notifs":
                sdb = scheduler_db_holder.get("sdb")
                if sdb is not None:
                    sdb.set_notifications_enabled(not sdb.get_notifications_enabled())
                await query.edit_message_text(
                    "*⚙️ Admin*\nOpérations sur le bot lui-même :",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_admin_menu_markup(
                        sdb.get_notifications_enabled() if sdb else True
                    ),
                )
                return
            if target == "restart":
                await query.edit_message_text(
                    "⚠️ *Redémarrer le bot ?*\n"
                    "Le processus va se relancer. Les projets gérés par tmux/runner "
                    "continuent de tourner indépendamment.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_bot_restart_confirm_markup(),
                )
            elif target == "restart_do":
                await query.edit_message_text(
                    "🔄 *Redémarrage en cours…*\nLe bot sera de nouveau joignable dans quelques secondes.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                # Persist the notice coordinates so the next process can delete
                # this message and post a fresh main menu on startup.
                try:
                    notice_path = cfg.data_dir / ".restart_notice.json"
                    notice_path.write_text(json.dumps({
                        "chat_id": query.message.chat_id,
                        "message_id": query.message.message_id,
                    }))
                except Exception as e:
                    logger.warning("Could not write restart notice: %s", e)
                logger.warning("Bot restart requested by user %s", update.effective_user.id)
                # Schedule the re-exec after the current callback finishes so the
                # confirmation message has time to be flushed to Telegram.
                asyncio.get_running_loop().call_later(0.8, _exec_restart)
            elif target == "update":
                await query.edit_message_text(
                    "⚠️ *Mettre à jour le bot ?*\n"
                    "`git pull` sera exécuté dans le dossier du bot, puis le "
                    "processus redémarrera.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_bot_update_confirm_markup(),
                )
            elif target == "update_do":
                repo_dir = Path(__file__).resolve().parent.parent
                await query.edit_message_text(
                    f"📥 *Mise à jour…*\n`git pull` dans `{repo_dir}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
                logger.warning("Bot update requested by user %s", update.effective_user.id)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "git", "pull", "--ff-only",
                        cwd=str(repo_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    raw, _ = await proc.communicate()
                    out = (raw.decode("utf-8", errors="replace") or "").strip() or "(aucune sortie)"
                    rc = proc.returncode
                except Exception as e:
                    out, rc = f"Exception: {e}", -1
                # Truncate so Telegram (4096-char limit) is happy even with backticks.
                if len(out) > 3500:
                    out = out[:3500] + "\n…(tronqué)"
                if rc != 0:
                    await query.edit_message_text(
                        f"❌ *Échec du git pull* (code {rc})\n```\n{out}\n```\n"
                        "Le bot n'a pas été redémarré.",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_admin_menu_markup(
                            scheduler_db_holder["sdb"].get_notifications_enabled()
                            if scheduler_db_holder.get("sdb")
                            else True
                        ),
                    )
                    return
                await query.edit_message_text(
                    f"✅ *git pull OK*\n```\n{out}\n```\n🔄 *Redémarrage en cours…*",
                    parse_mode=ParseMode.MARKDOWN,
                )
                try:
                    notice_path = cfg.data_dir / ".restart_notice.json"
                    notice_path.write_text(json.dumps({
                        "chat_id": query.message.chat_id,
                        "message_id": query.message.message_id,
                    }))
                except Exception as e:
                    logger.warning("Could not write restart notice: %s", e)
                asyncio.get_running_loop().call_later(0.8, _exec_restart)
            return

        if ns == "proj" and len(parts) >= 2 and parts[1] in ("files", "fget", "fpg"):
            sub = parts[1]
            rest = parts[2] if len(parts) > 2 else ""
            rest_parts = rest.split(":")
            name = rest_parts[0] if rest_parts else ""
            if not name or not db.get_project(name):
                await query.answer(text=f"Projet {name} introuvable", show_alert=True)
                return

            if sub == "files":
                slug = rest_parts[1] if len(rest_parts) > 1 else "_"
                rel = _files_resolve(ctx, slug)
                if rel is None:
                    await query.answer(text="Chemin expiré, réouvre Fichiers", show_alert=True)
                    return
                await _render_files(query, ctx, name, rel)
                return

            if sub == "fget":
                slug = rest_parts[1] if len(rest_parts) > 1 else None
                rel = _files_resolve(ctx, slug) if slug else None
                if rel is None:
                    await query.answer(text="Fichier expiré, réouvre Fichiers", show_alert=True)
                    return
                proj = db.get_project(name)
                try:
                    target = files_mgr.get_file(proj["path"], rel)
                except (PathEscapeError, FileNotFoundError, IsADirectoryError) as e:
                    await query.answer(text=str(e), show_alert=True)
                    return
                with target.open("rb") as f:
                    await query.message.reply_document(
                        document=f, filename=target.name,
                        caption=f"/put {name} {rel}",
                    )
                return

            if sub == "fpg":
                if len(rest_parts) < 3:
                    return
                slug, direction = rest_parts[1], rest_parts[2]
                rel = _files_resolve(ctx, slug)
                if rel is None:
                    await query.answer(text="Page expirée", show_alert=True)
                    return
                page_key = f"files_page:{name}:{slug}"
                cur = ctx.chat_data.get(page_key, 0)
                ctx.chat_data[page_key] = cur + (1 if direction == "next" else -1)
                await _render_files(query, ctx, name, rel)
                return

        if ns == "proj" and len(parts) == 2:
            await _render_project_card(query, parts[1])
            return

        if ns == "act" and len(parts) == 3:
            action, name = parts[1], parts[2]
            proj = db.get_project(name)
            if not proj:
                await query.edit_message_text(
                    f"Projet `{name}` introuvable.", parse_mode=ParseMode.MARKDOWN,
                )
                return
            if action == "run":
                if not proj.get("start_command"):
                    await query.edit_message_text(
                        f"Pas de start command. Utilise `/config {name}`.",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_project_actions_markup(name, await runner.is_running(name)),
                    )
                    return
                await runner.start(name, proj["start_command"], proj["path"])
                await _render_project_card(query, name)
            elif action == "status":
                await _render_project_card(query, name)
            elif action == "logs":
                out = await runner.get_logs(name, cfg.default_log_lines)
                await _send_text_or_file(
                    update, out, f"{name}-logs.txt",
                    header=f"{name} (last {cfg.default_log_lines})",
                )
            elif action in ("stop", "restart", "del"):
                verb = {"stop": "arrêter", "restart": "redémarrer", "del": "supprimer"}[action]
                await query.edit_message_text(
                    f"Confirmer : {verb} `{name}` ?",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_confirm_markup(action, name),
                )
            return

        if ns == "cfm" and len(parts) == 3:
            action, name = parts[1], parts[2]
            if action == "del":
                await _stop_project(name)
                db.remove_project(name)
                await _render_projects_list(query)
                return
            proj = db.get_project(name)
            if not proj:
                await query.edit_message_text(
                    f"Projet `{name}` introuvable.", parse_mode=ParseMode.MARKDOWN,
                )
                return
            if action == "stop":
                await _stop_project(name)
            elif action == "restart":
                if proj.get("start_command"):
                    await _restart_project(name)
            await _render_project_card(query, name)
            return

        # ─── Actions namespaces ──────────────────────────────────────────
        if ns == "actions" and len(parts) >= 2:
            target = parts[1]
            if target == "new":
                # handled by ConversationHandler entry — nothing to do here
                return
            await _render_action_card(query, target)
            return

        if ns == "act_a" and len(parts) == 3:
            verb, name = parts[1], parts[2]
            action = db.get_action(name)
            if not action:
                await query.edit_message_text(
                    f"Action `{name}` introuvable.", parse_mode=ParseMode.MARKDOWN,
                )
                return
            if verb == "run":
                if action.get("require_confirm"):
                    await query.edit_message_text(
                        f"Confirmer : exécuter `{name}` ?",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_action_confirm_markup("run", name),
                    )
                    return
                await _execute_action(update, query, action)
                return
            if verb == "del":
                await query.edit_message_text(
                    f"Confirmer : supprimer l'action `{name}` ?",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_action_confirm_markup("del", name),
                )
                return
            if action.get("mode") != "managed":
                await _render_action_card(query, name)
                return
            key = _action_runner_key(name)
            if verb == "stop":
                await runner.stop(key)
            elif verb == "restart":
                await runner.restart(key, action["command"], action["cwd"] or str(Path.cwd()))
            elif verb == "logs":
                out = await runner.get_logs(key, cfg.default_log_lines)
                await _send_text_or_file(
                    update, out, f"{name}-logs.txt",
                    header=f"{name} (last {cfg.default_log_lines})",
                )
                return
            await _render_action_card(query, name)
            return

        if ns == "cfm_a" and len(parts) == 3:
            verb, name = parts[1], parts[2]
            if verb == "no":
                await _render_action_card(query, name)
                return
            action = db.get_action(name)
            if not action:
                await query.edit_message_text(
                    f"Action `{name}` introuvable.", parse_mode=ParseMode.MARKDOWN,
                )
                return
            if verb == "del":
                if action.get("mode") == "managed":
                    await runner.stop(_action_runner_key(name))
                db.remove_action(name)
                await _render_actions_list(query)
                return
            if verb == "run":
                await _execute_action(update, query, action)
                return
            return

    # ─── add-project flow (triggered by inline button menu:add) ──────────
    @auth
    async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.callback_query is not None:
            await update.callback_query.answer()
        await _wizard_step(update, ctx, "📂 *Nouveau projet*\n\nEnvoie un nom court (pas d'espace ni de `:`).")
        return ADD_NAME

    async def add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = update.message.text.strip()
        if not name or ":" in name or " " in name:
            await _wizard_step(update, ctx, "⚠️ Nom invalide (pas d'espace ni de `:`).\n\n📂 Envoie un nom court.")
            return ADD_NAME
        if db.get_project(name):
            await _wizard_step(update, ctx, f"⚠️ Le projet `{name}` existe déjà.\n\n📂 Choisis un autre nom.")
            return ADD_NAME
        ctx.user_data["add_name"] = name
        await _wizard_step(update, ctx, f"📂 Projet : *{name}*\n\n📄 Envoie le chemin absolu du dossier.")
        return ADD_PATH

    async def add_path(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        path_obj = Path(update.message.text.strip()).expanduser().resolve()
        if not path_obj.is_dir():
            await _wizard_step(update, ctx, f"⚠️ Pas un dossier : `{path_obj}`.\n\n📄 Envoie un chemin valide.")
            return ADD_PATH
        name = ctx.user_data.get("add_name")
        if not name:
            await _wizard_step(update, ctx, "⚠️ État perdu, recommence depuis le menu.")
            ctx.user_data.pop("add_name", None)
            return ConversationHandler.END
        if not db.add_project(name, str(path_obj)):
            await _wizard_step(update, ctx, f"⚠️ Le projet `{name}` existe déjà.")
            ctx.user_data.pop("add_name", None)
            return ConversationHandler.END
        await _wizard_finish(update, ctx)
        return ConversationHandler.END

    # ─── add-action flow (triggered by /addaction or inline button) ──────
    @auth
    async def action_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.callback_query is not None:
            await update.callback_query.answer()
        await _wizard_step(update, ctx, "🚀 *Nouvelle action*\n\nEnvoie un nom court (pas d'espace ni de `:`).")
        return ADD_A_NAME

    async def action_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = update.message.text.strip()
        if not name or ":" in name or " " in name:
            await _wizard_step(update, ctx, "⚠️ Nom invalide (pas d'espace ni de `:`).\n\n🚀 Envoie un nom court.")
            return ADD_A_NAME
        if db.get_action(name):
            await _wizard_step(update, ctx, f"⚠️ L'action `{name}` existe déjà.\n\n🚀 Choisis un autre nom.")
            return ADD_A_NAME
        ctx.user_data["addact_name"] = name
        await _wizard_step(
            update, ctx,
            f"🚀 Action : *{name}*\n\nMode d'exécution ?",
            extra_rows=[[
                InlineKeyboardButton("⚡ Oneshot", callback_data="addact:mode:oneshot"),
                InlineKeyboardButton("🔁 Long-running", callback_data="addact:mode:managed"),
            ]],
        )
        return ADD_A_MODE

    async def action_add_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        command = update.message.text.strip()
        if not command:
            await _wizard_step(update, ctx, "⚠️ Commande vide.\n\n💻 Envoie la commande shell à exécuter.")
            return ADD_A_COMMAND
        ctx.user_data["addact_command"] = command
        await _wizard_step(
            update, ctx,
            "📁 Répertoire de travail (optionnel) ?\n\nEnvoie un chemin absolu, ou clique *Passer*.",
            extra_rows=[[InlineKeyboardButton("⏭️ Passer", callback_data="addact:cwd:skip")]],
        )
        return ADD_A_CWD

    async def action_add_cwd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        cwd = None
        if update.callback_query is not None:
            query = update.callback_query
            await query.answer()
            if query.data != "addact:cwd:skip":
                return ADD_A_CWD
            cwd = None
        else:
            try:
                await update.message.delete()
            except Exception:
                pass
            text = update.message.text.strip()
            if text == "" or text == "-":
                cwd = None
            else:
                cwd_path = Path(text).expanduser()
                if not cwd_path.is_dir():
                    await _wizard_step(
                        update, ctx,
                        f"⚠️ Pas un dossier : `{cwd_path}`.\n\n📁 Envoie un chemin valide, ou *Passer*.",
                        extra_rows=[[InlineKeyboardButton("⏭️ Passer", callback_data="addact:cwd:skip")]],
                    )
                    return ADD_A_CWD
                cwd = str(cwd_path.resolve())
        name = ctx.user_data.get("addact_name")
        command = ctx.user_data.get("addact_command")
        mode = ctx.user_data.get("addact_mode", "oneshot")
        require_confirm = ctx.user_data.get("addact_confirm", False)
        if not name or not command:
            await _wizard_step(update, ctx, "⚠️ État perdu, recommence depuis le menu.")
            ctx.user_data.pop("addact_name", None)
            ctx.user_data.pop("addact_command", None)
            ctx.user_data.pop("addact_cwd", None)
            ctx.user_data.pop("addact_mode", None)
            ctx.user_data.pop("addact_confirm", None)
            return ConversationHandler.END
        db.add_action(name, command, cwd, mode, require_confirm)
        await _wizard_finish(update, ctx)
        return ConversationHandler.END

    async def action_add_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[2] not in ("oneshot", "managed"):
            return ADD_A_MODE
        ctx.user_data["addact_mode"] = parts[2]
        await _wizard_step(
            update, ctx,
            "Demander une confirmation avant chaque exécution ?",
            extra_rows=[[
                InlineKeyboardButton("✅ Oui", callback_data="addact:cfm:yes"),
                InlineKeyboardButton("❌ Non", callback_data="addact:cfm:no"),
            ]],
        )
        return ADD_A_CONFIRM

    async def action_add_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[2] not in ("yes", "no"):
            return ADD_A_CONFIRM
        ctx.user_data["addact_confirm"] = (parts[2] == "yes")
        await _wizard_step(update, ctx, "💻 Commande shell à exécuter ?")
        return ADD_A_COMMAND

    async def action_add_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await _wizard_finish(update, ctx)
        return ConversationHandler.END

    # ─── /actions /runaction /delaction ──────────────────────────────────
    @auth
    async def cmd_actions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        actions = db.list_actions()
        if not actions:
            await update.message.reply_text(
                "Aucune action. Utilise `/addaction` pour en créer une.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        lines = []
        for a in actions:
            mode = a.get("mode", "oneshot")
            if mode == "managed":
                running = await runner.is_running(_action_runner_key(a["name"]))
                icon = "🟢" if running else "🔁"
            else:
                icon = "⚡"
            lines.append(f"{icon} *{a['name']}* — `{a['command']}`")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def _run_action_by_name(name: str) -> tuple[int, str]:
        """Execute a saved Action by name. Returns (rc, output).

        - oneshot mode: runs via shell, returns (returncode, merged stdout/stderr).
        - managed mode: runs via runner.start; returns (0, msg) on success,
          (1, msg) on failure. There is no captured output for managed Actions.
        - Unknown action: returns (1, "action not found: <name>").

        Notes:
          * Does NOT honour require_confirm — confirmation is a UI concern; the
            scheduler always proceeds.
        """
        action = db.get_action(name)
        if not action:
            return 1, f"action not found: {name}"
        mode = action.get("mode", "oneshot")
        cwd = action.get("cwd") or None
        if mode == "managed":
            ok, msg = await runner.start(
                _action_runner_key(name), action["command"], cwd or str(Path.cwd()),
            )
            return (0 if ok else 1), msg
        rc, out = await shell.run(action["command"], cwd)
        return rc, out

    @auth
    async def cmd_runaction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text(
                "Usage: `/runaction <name>`", parse_mode=ParseMode.MARKDOWN,
            )
            return
        name = ctx.args[0]
        action = db.get_action(name)
        if not action:
            await update.message.reply_text(
                f"Action `{name}` introuvable.", parse_mode=ParseMode.MARKDOWN,
            )
            return
        if action.get("require_confirm"):
            await update.message.reply_text(
                f"Confirmer : exécuter `{name}` ?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_action_confirm_markup("run", name),
            )
            return
        mode = action.get("mode", "oneshot")
        if mode == "managed":
            rc, msg = await _run_action_by_name(name)
            prefix = "▶️" if rc == 0 else "⚠️"
            await update.message.reply_text(
                f"{prefix} `{name}` : {msg}", parse_mode=ParseMode.MARKDOWN,
            )
            return
        await update.message.reply_text(
            f"⏳ Exécution de `{name}`…", parse_mode=ParseMode.MARKDOWN,
        )
        rc, out = await _run_action_by_name(name)
        await _send_text_or_file(
            update, out or "(aucune sortie)", f"{name}-output.txt",
            header=f"{name} — exit {rc}",
        )

    @auth
    async def cmd_delaction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text(
                "Usage: `/delaction <name>`", parse_mode=ParseMode.MARKDOWN,
            )
            return
        name = ctx.args[0]
        action = db.get_action(name)
        if not action:
            await update.message.reply_text("Pas trouvée.")
            return
        if action.get("mode") == "managed":
            await runner.stop(_action_runner_key(name))
        ok = db.remove_action(name)
        await update.message.reply_text("Supprimée." if ok else "Pas trouvée.")

    # ─── /projects ────────────────────────────────────────────────────────
    @auth
    async def cmd_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        projs = db.list_projects()
        if not projs:
            await update.message.reply_text(
                "No projects yet. Use `/add <name> <path>`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        lines = []
        for p in projs:
            running = await runner.is_running(p["name"])
            icon = "🟢" if running else "⚪"
            lines.append(f"{icon} *{p['name']}* — `{p['path']}`")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    # ─── /add ─────────────────────────────────────────────────────────────
    @auth
    async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if len(ctx.args) < 2:
            await update.message.reply_text("Usage: `/add <name> <path>`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        name = ctx.args[0]
        path = " ".join(ctx.args[1:])
        path_obj = Path(path).expanduser().resolve()
        if not path_obj.is_dir():
            await update.message.reply_text(f"Not a directory: `{path_obj}`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        if db.add_project(name, str(path_obj)):
            await update.message.reply_text(
                f"Added *{name}* → `{path_obj}`\n"
                f"Run `/config {name}` to set the start command.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(f"Project `{name}` already exists.",
                                             parse_mode=ParseMode.MARKDOWN)

    # ─── /remove ──────────────────────────────────────────────────────────
    @auth
    async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: `/remove <name>`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        name = ctx.args[0]
        await _stop_project(name)
        ok = db.remove_project(name)
        await update.message.reply_text("Removed." if ok else "Not found.")

    # ─── /config (conversation) ───────────────────────────────────────────
    @auth
    async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if ctx.args:
            name = ctx.args[0]
            proj = db.get_project(name)
            if not proj:
                await _wizard_step(update, ctx, f"⚠️ Pas de projet `{name}`.")
                ctx.user_data.pop("cfg_project", None)
                return ConversationHandler.END
            ctx.user_data["cfg_project"] = name
            current = proj.get("start_command") or "(none)"
            await _wizard_step(
                update, ctx,
                f"⚙️ Configurer *{name}*\n\nCommande actuelle : `{current}`\n\n💻 Envoie la commande de démarrage.",
            )
            return CFG_START_CMD
        projects = db.list_projects()
        if not projects:
            await _wizard_step(update, ctx, "Aucun projet. Crée-en un d'abord via *📂 Projets*.")
            return ConversationHandler.END
        rows = [[InlineKeyboardButton(p["name"], callback_data=f"cfgsel:{p['name']}")] for p in projects]
        await _wizard_step(update, ctx, "⚙️ Sélectionne un projet à configurer.", extra_rows=rows)
        return CFG_SELECT

    async def cfg_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":", 1)
        if len(parts) != 2:
            return CFG_SELECT
        name = parts[1]
        proj = db.get_project(name)
        if not proj:
            await _wizard_step(update, ctx, f"⚠️ Pas de projet `{name}`.")
            return ConversationHandler.END
        ctx.user_data["cfg_project"] = name
        current = proj.get("start_command") or "(none)"
        await _wizard_step(
            update, ctx,
            f"⚙️ Configurer *{name}*\n\nCommande actuelle : `{current}`\n\n💻 Envoie la commande de démarrage.",
        )
        return CFG_START_CMD

    async def proj_shell_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":", 2)
        if len(parts) != 3:
            return ConversationHandler.END
        name = parts[2]
        proj = db.get_project(name)
        if not proj:
            await _wizard_step(update, ctx, f"⚠️ Pas de projet `{name}`.")
            return ConversationHandler.END
        ctx.user_data["shell_project"] = name
        await _wizard_step(
            update, ctx,
            f"💻 Shell pour *{name}*\n\nEnvoie la commande à exécuter :",
        )
        return PROJ_SHELL_CMD

    async def proj_shell_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = ctx.user_data.get("shell_project")
        if not name:
            await _wizard_step(update, ctx, "⚠️ État perdu, recommence depuis le menu.")
            return ConversationHandler.END
        proj = db.get_project(name)
        if not proj:
            await _wizard_step(update, ctx, f"⚠️ Projet `{name}` disparu.")
            return ConversationHandler.END
        cmd = update.message.text.strip()
        await _wizard_step(update, ctx, f"⏳ `{cmd}` …")
        rc, out = await shell.run(cmd, proj["path"])
        await _send_text_or_file(
            update, out or "(no output)", f"{name}-shell.txt",
            header=f"exit {rc}",
        )
        await _wizard_finish(update, ctx)
        return ConversationHandler.END

    async def proj_shell_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data.pop("shell_project", None)
        await _wizard_finish(update, ctx)
        return ConversationHandler.END

    async def proj_cfg_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":", 2)
        if len(parts) != 3:
            return ConversationHandler.END
        name = parts[2]
        proj = db.get_project(name)
        if not proj:
            await _wizard_step(update, ctx, f"⚠️ Pas de projet `{name}`.")
            return ConversationHandler.END
        ctx.user_data["cfg_project"] = name
        current = proj.get("start_command") or "(none)"
        await _wizard_step(
            update, ctx,
            f"🛠️ Configurer *{name}*\n\nCommande actuelle : `{current}`\n\n"
            f"💬 Envoie la commande de démarrage.",
        )
        return CFG_START_CMD

    async def cfg_start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = ctx.user_data.get("cfg_project")
        if not name:
            await _wizard_step(update, ctx, "⚠️ État perdu, recommence depuis le menu.")
            return ConversationHandler.END
        cmd = update.message.text.strip()
        db.update_project(name, start_command=cmd)
        proj = db.get_project(name)
        current = proj.get("stop_command") or "(none)"
        await _wizard_step(
            update, ctx,
            f"✅ Start command enregistrée pour *{name}*.\n\n"
            f"🛑 Commande d'arrêt (actuelle : `{current}`) ?\n"
            f"Envoie une commande ou `skip` (taskkill/tmux kill par défaut).",
        )
        return CFG_STOP_CMD

    async def cfg_stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = ctx.user_data.get("cfg_project")
        if not name:
            await _wizard_step(update, ctx, "⚠️ État perdu, recommence depuis le menu.")
            return ConversationHandler.END
        text = update.message.text.strip()
        if text.lower() == "clear":
            db.update_project(name, stop_command=None)
        elif text.lower() != "skip":
            db.update_project(name, stop_command=text)
        proj = db.get_project(name)
        current = proj.get("entry_file") or "(none)"
        await _wizard_step(
            update, ctx,
            f"📄 Fichier de log d'entrée (actuel : `{current}`) ?\n"
            f"Envoie un nom de fichier ou `skip`.",
        )
        return CFG_ENTRY_FILE

    async def cfg_entry_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = ctx.user_data.get("cfg_project")
        if not name:
            await _wizard_step(update, ctx, "⚠️ État perdu, recommence depuis le menu.")
            return ConversationHandler.END
        text = update.message.text.strip()
        if text.lower() != "skip":
            db.update_project(name, entry_file=text)
        await _wizard_finish(update, ctx)
        return ConversationHandler.END

    async def cfg_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await _wizard_finish(update, ctx)
        return ConversationHandler.END

    # ─── /run /stop /restart /status ──────────────────────────────────────
    @auth
    async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: `/run <name>`", parse_mode=ParseMode.MARKDOWN)
            return
        name = ctx.args[0]
        proj = db.get_project(name)
        if not proj:
            await update.message.reply_text(f"No project `{name}`.", parse_mode=ParseMode.MARKDOWN)
            return
        if not proj.get("start_command"):
            await update.message.reply_text(
                f"No start command set. Use `/config {name}`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        ok, msg = await runner.start(name, proj["start_command"], proj["path"])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    @auth
    async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: `/stop <name>`", parse_mode=ParseMode.MARKDOWN)
            return
        name = ctx.args[0]
        ok = await _stop_project(name)
        await update.message.reply_text("Stopped." if ok else "Not running.")

    @auth
    async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: `/restart <name>`", parse_mode=ParseMode.MARKDOWN)
            return
        ok, msg = await _restart_project(ctx.args[0])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    @auth
    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: `/status <name>`", parse_mode=ParseMode.MARKDOWN)
            return
        name = ctx.args[0]
        proj = db.get_project(name)
        if not proj:
            await update.message.reply_text(f"No project `{name}`.", parse_mode=ParseMode.MARKDOWN)
            return
        running = await runner.is_running(name)
        await update.message.reply_text(
            f"*{name}*\n"
            f"Path: `{proj['path']}`\n"
            f"Start: `{proj.get('start_command') or '(unset)'}`\n"
            f"Stop: `{proj.get('stop_command') or '(default kill)'}`\n"
            f"Entry: `{proj.get('entry_file') or '(unset)'}`\n"
            f"Status: {'🟢 running' if running else '⚪ stopped'}",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ─── /logs ────────────────────────────────────────────────────────────
    @auth
    async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: `/logs <name> [lines]`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        name = ctx.args[0]
        try:
            lines = int(ctx.args[1]) if len(ctx.args) > 1 else cfg.default_log_lines
        except ValueError:
            lines = cfg.default_log_lines
        out = await runner.get_logs(name, lines)
        await _send_text_or_file(update, out, f"{name}-logs.txt", header=f"{name} (last {lines})")

    # ─── /ls /get ─────────────────────────────────────────────────────────
    @auth
    async def cmd_ls(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: `/ls <name> [subpath]`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        name = ctx.args[0]
        rel = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else "."
        proj = db.get_project(name)
        if not proj:
            await update.message.reply_text(f"No project `{name}`.", parse_mode=ParseMode.MARKDOWN)
            return
        try:
            entries = files_mgr.list_dir(proj["path"], rel)
        except (PathEscapeError, FileNotFoundError, NotADirectoryError) as e:
            await update.message.reply_text(f"Error: {e}")
            return
        body = "\n".join(entries) if entries else "(empty)"
        await _send_text_or_file(update, body, f"{name}-ls.txt", header=f"{name}/{rel}")

    @auth
    async def cmd_get(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if len(ctx.args) < 2:
            await update.message.reply_text("Usage: `/get <name> <path>`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        name = ctx.args[0]
        rel = " ".join(ctx.args[1:])
        proj = db.get_project(name)
        if not proj:
            await update.message.reply_text(f"No project `{name}`.", parse_mode=ParseMode.MARKDOWN)
            return
        try:
            target = files_mgr.get_file(proj["path"], rel)
        except (PathEscapeError, FileNotFoundError, IsADirectoryError) as e:
            await update.message.reply_text(f"Error: {e}")
            return
        # The caption pre-fills the upload command if user wants to replace it
        with target.open("rb") as f:
            await update.message.reply_document(
                document=f,
                filename=target.name,
                caption=f"/put {name} {rel}",
            )

    # ─── /put (document upload with caption) ─────────────────────────────
    @auth
    async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        caption = (msg.caption or "").strip()
        if not caption.startswith("/put"):
            await msg.reply_text(
                "To save a file, set the caption to: `/put <project> <relative-path>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        parts = caption.split(maxsplit=2)
        if len(parts) < 3:
            await msg.reply_text(
                "Caption usage: `/put <name> <path>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        _, name, rel = parts
        proj = db.get_project(name)
        if not proj:
            await msg.reply_text(f"No project `{name}`.", parse_mode=ParseMode.MARKDOWN)
            return
        tg_file = await msg.document.get_file()
        content = bytes(await tg_file.download_as_bytearray())
        try:
            files_mgr.put_file(name, proj["path"], rel, content)
        except PathEscapeError as e:
            await msg.reply_text(f"Error: {e}")
            return
        await msg.reply_text(
            f"✅ Wrote `{rel}` ({len(content)} bytes). Previous version backed up.",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ─── /shell ───────────────────────────────────────────────────────────
    @auth
    async def cmd_shell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if len(ctx.args) < 2:
            await update.message.reply_text(
                "Usage: `/shell <name> <command...>`", parse_mode=ParseMode.MARKDOWN
            )
            return
        name = ctx.args[0]
        cmd = " ".join(ctx.args[1:])
        proj = db.get_project(name)
        if not proj:
            await update.message.reply_text(f"No project `{name}`.", parse_mode=ParseMode.MARKDOWN)
            return
        await update.message.reply_text(
            f"⏳ `{cmd}`", parse_mode=ParseMode.MARKDOWN
        )
        rc, out = await shell.run(cmd, proj["path"])
        await _send_text_or_file(
            update, out or "(no output)", f"{name}-shell.txt", header=f"exit {rc}"
        )

    # ─── error handler ────────────────────────────────────────────────────
    async def on_error(update, ctx):
        logger.exception("Handler error", exc_info=ctx.error)
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                f"⚠️ Internal error: `{type(ctx.error).__name__}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    # ─── build Application ────────────────────────────────────────────────
    app = Application.builder().token(cfg.bot_token).build()

    async def _wizard_escape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Fallback when user clicks a non-wizard button (e.g. main menu) during a wizard.
        Cleans state and lets on_callback handle the navigation by editing the same message."""
        ctx.user_data.pop("wizard_msg_id", None)
        ctx.user_data.pop("wizard_chat_id", None)
        for k in ("add_name", "addact_name", "addact_command", "addact_cwd", "addact_mode", "cfg_project", "shell_project", "sched"):
            ctx.user_data.pop(k, None)
        await on_callback(update, ctx)
        return ConversationHandler.END

    config_conv = ConversationHandler(
        entry_points=[
            CommandHandler("config", cmd_config),
            CallbackQueryHandler(proj_cfg_entry, pattern=r"^proj:cfg:"),
        ],
        states={
            CFG_SELECT: [CallbackQueryHandler(cfg_select, pattern=r"^cfgsel:")],
            CFG_START_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_start_cmd)],
            CFG_STOP_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_stop_cmd)],
            CFG_ENTRY_FILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_entry_file)],
        },
        fallbacks=[
            CommandHandler("cancel", cfg_cancel),
            CallbackQueryHandler(_wizard_escape),
        ],
        conversation_timeout=300,
    )

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern=r"^menu:add$")],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_path)],
        },
        fallbacks=[
            CommandHandler("cancel", cfg_cancel),
            CallbackQueryHandler(_wizard_escape),
        ],
        conversation_timeout=300,
    )

    action_add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addaction", action_add_start),
            CallbackQueryHandler(action_add_start, pattern=r"^actions:new$"),
        ],
        states={
            ADD_A_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, action_add_name)],
            ADD_A_MODE: [CallbackQueryHandler(action_add_mode, pattern=r"^addact:mode:")],
            ADD_A_CONFIRM: [CallbackQueryHandler(action_add_confirm, pattern=r"^addact:cfm:")],
            ADD_A_COMMAND: [MessageHandler(filters.TEXT & ~filters.COMMAND, action_add_command)],
            ADD_A_CWD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, action_add_cwd),
                CallbackQueryHandler(action_add_cwd, pattern=r"^addact:cwd:skip$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", action_add_cancel),
            CallbackQueryHandler(_wizard_escape),
        ],
        conversation_timeout=300,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(config_conv)
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("ls", cmd_ls))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("shell", cmd_shell))
    app.add_handler(CommandHandler("actions", cmd_actions))
    app.add_handler(CommandHandler("runaction", cmd_runaction))
    app.add_handler(CommandHandler("delaction", cmd_delaction))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(add_conv)
    app.add_handler(action_add_conv)
    proj_shell_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(proj_shell_start, pattern=r"^proj:shell:")],
        states={
            PROJ_SHELL_CMD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, proj_shell_run),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", proj_shell_cancel),
            CallbackQueryHandler(_wizard_escape),
        ],
    )
    app.add_handler(proj_shell_conv)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    async def post_init(application):
        commands = [
            BotCommand("start", "Menu principal"),
            BotCommand("projects", "List all projects"),
            BotCommand("add", "Add project: <name> <path>"),
            BotCommand("config", "Configure project"),
            BotCommand("run", "Start project"),
            BotCommand("stop", "Stop project"),
            BotCommand("restart", "Restart project"),
            BotCommand("status", "Show project status"),
            BotCommand("logs", "Recent logs"),
            BotCommand("ls", "List files"),
            BotCommand("get", "Download a file"),
            BotCommand("shell", "Run shell command"),
            BotCommand("actions", "List saved actions"),
            BotCommand("addaction", "Create a new action"),
            BotCommand("runaction", "Run an action by name"),
            BotCommand("delaction", "Delete an action"),
            BotCommand("scheduled", "List/manage scheduled tasks"),
            BotCommand("remove", "Remove project"),
            BotCommand("help", "Show help"),
        ]
        if trading_enabled:
            commands.extend([
                BotCommand("watch", "Watch wallet: <addr> <chain>"),
                BotCommand("unwatch", "Unwatch wallet"),
                BotCommand("wallets", "List watched wallets"),
                BotCommand("alert", "MC alert: <token> <chain> <mc>"),
                BotCommand("alerts", "List MC alerts"),
                BotCommand("unalert", "Delete MC alert"),
                BotCommand("holdings", "Wallet holdings snapshot"),
            ])
        await application.bot.set_my_commands(commands)

        # If we just restarted via the inline button, delete the "Redémarrage
        # en cours…" message and post a fresh main menu in the same chat.
        notice_path = cfg.data_dir / ".restart_notice.json"
        if notice_path.exists():
            try:
                data = json.loads(notice_path.read_text())
                chat_id = data["chat_id"]
                message_id = data["message_id"]
                try:
                    await application.bot.delete_message(chat_id=chat_id, message_id=message_id)
                except BadRequest as e:
                    logger.warning("Could not delete restart notice message: %s", e)
                await application.bot.send_message(
                    chat_id=chat_id,
                    text="*Menu principal*\nChoisis une action :",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_main_menu_markup(trading_enabled),
                )
            except Exception as e:
                logger.warning("Failed to handle restart notice: %s", e)
            finally:
                try:
                    notice_path.unlink()
                except OSError:
                    pass

    app.post_init = post_init

    # Wire the optional trading module — no-op if [trading] is missing/disabled.
    register_trading(
        app, cfg,
        wizard_step=_wizard_step,
        wizard_finish=_wizard_finish,
        wizard_escape=_wizard_escape,
    )

    class _ProjectOps:
        @staticmethod
        async def start(name: str) -> tuple[bool, str]:
            proj = db.get_project(name)
            if not proj or not proj.get("start_command"):
                return False, "project not configured"
            return await runner.start(name, proj["start_command"], proj["path"])

        @staticmethod
        async def stop(name: str) -> bool:
            return await _stop_project(name)

        @staticmethod
        async def restart(name: str) -> tuple[bool, str]:
            return await _restart_project(name)

    scheduler_db_holder["sdb"] = register_scheduler(
        app, cfg, db,
        wizard_step=_wizard_step,
        wizard_finish=_wizard_finish,
        wizard_escape=_wizard_escape,
        run_action=_run_action_by_name,
        project_ops=_ProjectOps,
    )

    return app