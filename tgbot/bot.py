"""Telegram bot command handlers."""
import logging
from io import BytesIO
from pathlib import Path

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
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
from .runner import TmuxRunner
from .shell import ShellRunner

logger = logging.getLogger(__name__)

# /config conversation states
CFG_START_CMD, CFG_ENTRY_FILE = range(2)

# Telegram message limit is ~4096; leave headroom for markdown fences
INLINE_TEXT_LIMIT = 3500


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

`/cancel` exits /config flow."""


def _md_code_block(text: str) -> str:
    """Wrap text in a markdown code fence."""
    return f"```\n{text}\n```"


async def _send_text_or_file(update: Update, text: str, filename: str,
                              header: str | None = None) -> None:
    """Send `text` inline if short, otherwise as a document."""
    if len(text) <= INLINE_TEXT_LIMIT:
        prefix = f"`{header}`\n" if header else ""
        await update.message.reply_text(
            prefix + _md_code_block(text), parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_document(
            document=BytesIO(text.encode()),
            filename=filename,
            caption=header,
        )


def build_app(cfg: Config) -> Application:
    db = DB(cfg.data_dir / "projects.db")
    runner = TmuxRunner(cfg.data_dir / "logs")
    files_mgr = FileManager(cfg.data_dir / "backups")
    shell = ShellRunner(timeout=cfg.shell_timeout)
    auth = restricted(cfg.allowed_user_ids)

    # ─── /help ────────────────────────────────────────────────────────────
    @auth
    async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

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
        if await runner.is_running(name):
            await runner.stop(name)
        ok = db.remove_project(name)
        await update.message.reply_text("Removed." if ok else "Not found.")

    # ─── /config (conversation) ───────────────────────────────────────────
    @auth
    async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: `/config <name>`",
                                             parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END
        name = ctx.args[0]
        proj = db.get_project(name)
        if not proj:
            await update.message.reply_text(f"No project `{name}`.",
                                             parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END
        ctx.user_data["cfg_project"] = name
        current = proj.get("start_command") or "(none)"
        await update.message.reply_text(
            f"Configuring *{name}*.\n\n"
            f"Current start command: `{current}`\n\n"
            f"Send the new start command (e.g. `python main.py` or `npm run dev`), "
            f"or `/cancel`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return CFG_START_CMD

    async def cfg_start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        name = ctx.user_data["cfg_project"]
        cmd = update.message.text.strip()
        db.update_project(name, start_command=cmd)
        proj = db.get_project(name)
        current = proj.get("entry_file") or "(none)"
        await update.message.reply_text(
            f"Saved start command. Now the entry file (just for reference / display). "
            f"Current: `{current}`\n\nSend a filename or `skip`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return CFG_ENTRY_FILE

    async def cfg_entry_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        name = ctx.user_data["cfg_project"]
        text = update.message.text.strip()
        if text.lower() != "skip":
            db.update_project(name, entry_file=text)
        await update.message.reply_text(
            f"✅ Configured *{name}*. Use `/run {name}` to start.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    async def cfg_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Cancelled.")
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
        ok = await runner.stop(name)
        await update.message.reply_text("Stopped." if ok else "Not running.")

    @auth
    async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: `/restart <name>`", parse_mode=ParseMode.MARKDOWN)
            return
        name = ctx.args[0]
        proj = db.get_project(name)
        if not proj or not proj.get("start_command"):
            await update.message.reply_text("Not configured.")
            return
        ok, msg = await runner.restart(name, proj["start_command"], proj["path"])
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

    config_conv = ConversationHandler(
        entry_points=[CommandHandler("config", cmd_config)],
        states={
            CFG_START_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_start_cmd)],
            CFG_ENTRY_FILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_entry_file)],
        },
        fallbacks=[CommandHandler("cancel", cfg_cancel)],
        conversation_timeout=300,
    )

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
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
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_error_handler(on_error)

    async def post_init(application):
        await application.bot.set_my_commands([
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
            BotCommand("remove", "Remove project"),
            BotCommand("help", "Show help"),
        ])

    app.post_init = post_init
    return app