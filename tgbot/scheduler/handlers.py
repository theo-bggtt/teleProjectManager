"""Telegram handlers for the scheduler module."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
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
from .db import SchedulerDB
from .executor import Executor
from .triggers import build_trigger, describe_trigger

if TYPE_CHECKING:
    from telegram.ext import Application

logger = logging.getLogger(__name__)

# Conversation states (offset 800 to avoid clashing with bot.py ranges).
SCHED_TYPE, SCHED_TARGET, SCHED_OP, SCHED_TRIGGER, \
    SCHED_INTERVAL_VALUE, SCHED_DAILY_TIME, SCHED_WEEKLY_DAY, \
    SCHED_WEEKLY_TIME, SCHED_CRON_EXPR, SCHED_NAME = range(800, 810)


_WEEKDAYS = [
    ("mon", "Lundi"), ("tue", "Mardi"), ("wed", "Mercredi"),
    ("thu", "Jeudi"), ("fri", "Vendredi"), ("sat", "Samedi"),
    ("sun", "Dimanche"),
]


def register_handlers(
    app: "Application",
    cfg,
    main_db,
    scheduler_db: SchedulerDB,
    executor: Executor,
    *,
    wizard_step: Callable,
    wizard_finish: Callable,
    wizard_escape: Callable,
) -> None:
    """Register all scheduler-related Telegram handlers."""
    auth = restricted(cfg.allowed_user_ids)
    scheduler = app.job_queue.scheduler

    # ─── helpers ────────────────────────────────────────────────────────
    def _job_id(task_id: int) -> str:
        return f"sched:{task_id}"

    def _schedule(task: dict) -> None:
        """(Re)register a task as an APScheduler job. Idempotent."""
        try:
            scheduler.remove_job(_job_id(task["id"]))
        except Exception:  # JobLookupError or similar
            pass
        trigger = build_trigger(task["trigger_kind"], task["trigger_spec"])
        scheduler.add_job(
            executor.run_task,
            trigger=trigger,
            args=[task["id"]],
            id=_job_id(task["id"]),
            misfire_grace_time=None,
            max_instances=1,
            replace_existing=True,
        )

    def _unschedule(task_id: int) -> None:
        try:
            scheduler.remove_job(_job_id(task_id))
        except Exception:
            pass

    # ─── markup builders ────────────────────────────────────────────────
    def _list_markup(tasks: list[dict]) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for t in tasks:
            check = "✓" if t["enabled"] else "✗"
            label = f"{check} {t['name']} — {describe_trigger(t['trigger_kind'], t['trigger_spec'])}"
            rows.append([InlineKeyboardButton(label, callback_data=f"sched:card:{t['id']}")])
        rows.append([InlineKeyboardButton("➕ Nouvelle", callback_data="sched:new")])
        rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")])
        return InlineKeyboardMarkup(rows)

    def _card_markup(task_id: int, enabled: bool) -> InlineKeyboardMarkup:
        toggle = "⏸ Désactiver" if enabled else "▶️ Activer"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle, callback_data=f"sched:toggle:{task_id}")],
            [InlineKeyboardButton("▶️ Exécuter maintenant", callback_data=f"sched:run:{task_id}")],
            [InlineKeyboardButton("🗑 Supprimer", callback_data=f"sched:del:{task_id}")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="sched:list")],
        ])

    def _confirm_delete_markup(task_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Oui, supprimer", callback_data=f"sched:dconfirm:{task_id}"),
                InlineKeyboardButton("❌ Annuler", callback_data=f"sched:card:{task_id}"),
            ],
        ])

    # ─── renderers ──────────────────────────────────────────────────────
    async def _render_list(query) -> None:
        tasks = scheduler_db.list_tasks()
        text = (
            f"*Tâches planifiées ({len(tasks)})*"
            if tasks
            else "*Tâches planifiées*\nAucune tâche."
        )
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=_list_markup(tasks),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def _format_card(t: dict) -> str:
        if t["task_type"] == "project_op":
            target_line = f"Cible       : `{t['target']}` · `{t['operation']}`"
            type_line = "Type        : opération projet"
        else:
            target_line = f"Cible       : action `{t['target']}`"
            type_line = "Type        : action enregistrée"
        recurrence = describe_trigger(t["trigger_kind"], t["trigger_spec"])
        status = "✓ activée" if t["enabled"] else "✗ désactivée"
        if t["last_run_at"]:
            icon = "✅" if t["last_status"] == "ok" else "❌"
            last = f"Dernière    : {t['last_run_at']} · {icon} {t['last_status']}"
        else:
            last = "Dernière    : jamais exécutée"
        return (
            f"*⏰ {t['name']}*\n"
            f"{type_line}\n"
            f"{target_line}\n"
            f"Récurrence  : {recurrence}\n"
            f"Statut      : {status}\n"
            f"{last}"
        )

    async def _render_card(query, task_id: int) -> None:
        t = scheduler_db.get_task(task_id)
        if t is None:
            await query.edit_message_text(
                f"Tâche `{task_id}` introuvable.", parse_mode=ParseMode.MARKDOWN,
            )
            return
        try:
            await query.edit_message_text(
                await _format_card(t),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_card_markup(task_id, bool(t["enabled"])),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    # ─── /scheduled command + non-wizard callbacks (sched:list/card/toggle/run/del) ──
    @auth
    async def cmd_scheduled(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        tasks = scheduler_db.list_tasks()
        text = (
            f"*Tâches planifiées ({len(tasks)})*"
            if tasks
            else "*Tâches planifiées*\nAucune tâche."
        )
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=_list_markup(tasks),
        )

    @auth
    async def on_sched_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        parts = data.split(":", 2)
        if len(parts) < 2:
            return
        sub = parts[1]

        if sub == "list":
            await _render_list(query)
            return

        if sub == "card" and len(parts) == 3:
            try:
                task_id = int(parts[2])
            except ValueError:
                return
            await _render_card(query, task_id)
            return

        if sub == "toggle" and len(parts) == 3:
            try:
                task_id = int(parts[2])
            except ValueError:
                return
            t = scheduler_db.get_task(task_id)
            if t is None:
                return
            new_enabled = not bool(t["enabled"])
            scheduler_db.set_enabled(task_id, new_enabled)
            if new_enabled:
                _schedule(scheduler_db.get_task(task_id))
            else:
                _unschedule(task_id)
            await _render_card(query, task_id)
            return

        if sub == "run" and len(parts) == 3:
            try:
                task_id = int(parts[2])
            except ValueError:
                return
            await query.edit_message_text("⏳ Exécution…", parse_mode=ParseMode.MARKDOWN)
            await executor.run_task(task_id)
            await _render_card(query, task_id)
            return

        if sub == "del" and len(parts) == 3:
            try:
                task_id = int(parts[2])
            except ValueError:
                return
            await query.edit_message_text(
                "🗑 Supprimer cette tâche planifiée ?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_confirm_delete_markup(task_id),
            )
            return

        if sub == "dconfirm" and len(parts) == 3:
            try:
                task_id = int(parts[2])
            except ValueError:
                return
            _unschedule(task_id)
            scheduler_db.delete_task(task_id)
            await _render_list(query)
            return

    # ─── wizard (create) ────────────────────────────────────────────────
    @auth
    async def wizard_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.callback_query is not None:
            await update.callback_query.answer()
        ctx.user_data["sched"] = {}
        await wizard_step(
            update, ctx,
            "*➕ Nouvelle tâche planifiée*\n\nQuel type ?",
            extra_rows=[[
                InlineKeyboardButton("🚀 Action enregistrée", callback_data="sched:wt:action"),
                InlineKeyboardButton("📂 Opération projet", callback_data="sched:wt:project_op"),
            ]],
        )
        return SCHED_TYPE

    async def wizard_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[2] not in ("action", "project_op"):
            return SCHED_TYPE
        ctx.user_data["sched"]["task_type"] = parts[2]
        if parts[2] == "action":
            actions = main_db.list_actions()
            if not actions:
                await wizard_step(update, ctx, "⚠️ Aucune action enregistrée. Crée d'abord une Action.")
                ctx.user_data.pop("sched", None)
                return ConversationHandler.END
            rows = [
                [InlineKeyboardButton(a["name"], callback_data=f"sched:wtg:{a['name']}")]
                for a in actions
            ]
            await wizard_step(update, ctx, "🚀 *Quelle action ?*", extra_rows=rows)
        else:
            projs = main_db.list_projects()
            if not projs:
                await wizard_step(update, ctx, "⚠️ Aucun projet enregistré. Crée d'abord un Projet.")
                ctx.user_data.pop("sched", None)
                return ConversationHandler.END
            rows = [
                [InlineKeyboardButton(p["name"], callback_data=f"sched:wtg:{p['name']}")]
                for p in projs
            ]
            await wizard_step(update, ctx, "📂 *Quel projet ?*", extra_rows=rows)
        return SCHED_TARGET

    async def wizard_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":", 2)
        if len(parts) != 3:
            return SCHED_TARGET
        ctx.user_data["sched"]["target"] = parts[2]
        if ctx.user_data["sched"]["task_type"] == "project_op":
            await wizard_step(
                update, ctx, "📂 *Quelle opération ?*",
                extra_rows=[[
                    InlineKeyboardButton("▶️ start", callback_data="sched:wop:start"),
                    InlineKeyboardButton("⏹ stop", callback_data="sched:wop:stop"),
                    InlineKeyboardButton("🔄 restart", callback_data="sched:wop:restart"),
                ]],
            )
            return SCHED_OP
        ctx.user_data["sched"]["operation"] = None
        return await _ask_trigger(update, ctx)

    async def wizard_op(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[2] not in ("start", "stop", "restart"):
            return SCHED_OP
        ctx.user_data["sched"]["operation"] = parts[2]
        return await _ask_trigger(update, ctx)

    async def _ask_trigger(update, ctx):
        await wizard_step(
            update, ctx, "⏱ *Récurrence ?*",
            extra_rows=[
                [InlineKeyboardButton("Toutes les X min", callback_data="sched:tr:interval")],
                [InlineKeyboardButton("Quotidien à HH:MM", callback_data="sched:tr:daily")],
                [InlineKeyboardButton("Hebdo : <jour> à HH:MM", callback_data="sched:tr:weekly")],
                [InlineKeyboardButton("Expression cron…", callback_data="sched:tr:cron")],
            ],
        )
        return SCHED_TRIGGER

    async def wizard_trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3:
            return SCHED_TRIGGER
        kind = parts[2]
        ctx.user_data["sched"]["trigger_kind"] = kind
        if kind == "interval":
            await wizard_step(update, ctx, "⏱ Toutes les combien de *minutes* ? (1–1440)")
            return SCHED_INTERVAL_VALUE
        if kind == "daily":
            await wizard_step(update, ctx, "🕓 *Heure quotidienne* au format `HH:MM` (ex: `04:00`)")
            return SCHED_DAILY_TIME
        if kind == "weekly":
            rows = [[InlineKeyboardButton(label, callback_data=f"sched:wd:{code}")]
                    for code, label in _WEEKDAYS]
            await wizard_step(update, ctx, "📅 *Quel jour ?*", extra_rows=rows)
            return SCHED_WEEKLY_DAY
        if kind == "cron":
            await wizard_step(
                update, ctx,
                "🧙 *Expression cron* (format `m h dom mon dow`)\n"
                "Exemple : `0 4 * * 1` = tous les lundis à 04h00.",
            )
            return SCHED_CRON_EXPR
        return SCHED_TRIGGER

    async def wizard_interval_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        text = update.message.text.strip()
        try:
            mins = int(text)
            if not (1 <= mins <= 1440):
                raise ValueError
        except ValueError:
            await wizard_step(update, ctx, "⚠️ Entier entre 1 et 1440 attendu. Réessaie.")
            return SCHED_INTERVAL_VALUE
        ctx.user_data["sched"]["trigger_spec"] = {"minutes": mins}
        return await _ask_name(update, ctx)

    async def wizard_daily_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        spec = _parse_hhmm(update.message.text.strip())
        if spec is None:
            await wizard_step(update, ctx, "⚠️ Format invalide. Envoie `HH:MM` (ex: `04:00`).")
            return SCHED_DAILY_TIME
        ctx.user_data["sched"]["trigger_spec"] = spec
        return await _ask_name(update, ctx)

    async def wizard_weekly_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[2] not in {c for c, _ in _WEEKDAYS}:
            return SCHED_WEEKLY_DAY
        ctx.user_data["sched"]["weekly_day"] = parts[2]
        await wizard_step(update, ctx, "🕓 *Heure* au format `HH:MM` (ex: `03:00`)")
        return SCHED_WEEKLY_TIME

    async def wizard_weekly_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        spec = _parse_hhmm(update.message.text.strip())
        if spec is None:
            await wizard_step(update, ctx, "⚠️ Format invalide. Envoie `HH:MM`.")
            return SCHED_WEEKLY_TIME
        spec["day_of_week"] = ctx.user_data["sched"].pop("weekly_day")
        ctx.user_data["sched"]["trigger_spec"] = spec
        return await _ask_name(update, ctx)

    async def wizard_cron_expr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        expr = update.message.text.strip()
        try:
            build_trigger("cron", {"expr": expr})
        except ValueError as e:
            await wizard_step(update, ctx, f"⚠️ Cron invalide : `{e}`\nRéessaie.")
            return SCHED_CRON_EXPR
        ctx.user_data["sched"]["trigger_spec"] = {"expr": expr}
        return await _ask_name(update, ctx)

    async def _ask_name(update, ctx):
        await wizard_step(update, ctx, "🏷 *Nom de la tâche ?* (libellé affiché dans la liste)")
        return SCHED_NAME

    async def wizard_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = update.message.text.strip()
        if not name:
            await wizard_step(update, ctx, "⚠️ Nom vide. Réessaie.")
            return SCHED_NAME
        s = ctx.user_data.get("sched", {})
        required_keys = ("task_type", "target", "trigger_kind", "trigger_spec")
        if not all(k in s for k in required_keys):
            await wizard_step(update, ctx, "⚠️ État du wizard perdu. Recommence depuis le menu.")
            ctx.user_data.pop("sched", None)
            return ConversationHandler.END
        task_id = scheduler_db.add_task(
            name=name,
            task_type=s["task_type"],
            target=s["target"],
            operation=s.get("operation"),
            trigger_kind=s["trigger_kind"],
            trigger_spec=s["trigger_spec"],
        )
        _schedule(scheduler_db.get_task(task_id))
        ctx.user_data.pop("sched", None)
        await wizard_finish(update, ctx)
        return ConversationHandler.END

    def _parse_hhmm(text: str) -> dict | None:
        try:
            hh, mm = text.split(":")
            h, m = int(hh), int(mm)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                return None
            return {"hour": h, "minute": m}
        except (ValueError, AttributeError):
            return None

    # ─── ConversationHandler ────────────────────────────────────────────
    async def sched_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await wizard_finish(update, ctx)
        return ConversationHandler.END

    sched_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(wizard_start, pattern=r"^sched:new$"),
        ],
        states={
            SCHED_TYPE: [CallbackQueryHandler(wizard_type, pattern=r"^sched:wt:")],
            SCHED_TARGET: [CallbackQueryHandler(wizard_target, pattern=r"^sched:wtg:")],
            SCHED_OP: [CallbackQueryHandler(wizard_op, pattern=r"^sched:wop:")],
            SCHED_TRIGGER: [CallbackQueryHandler(wizard_trigger, pattern=r"^sched:tr:")],
            SCHED_INTERVAL_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_interval_value),
            ],
            SCHED_DAILY_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_daily_time),
            ],
            SCHED_WEEKLY_DAY: [CallbackQueryHandler(wizard_weekly_day, pattern=r"^sched:wd:")],
            SCHED_WEEKLY_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_weekly_time),
            ],
            SCHED_CRON_EXPR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_cron_expr),
            ],
            SCHED_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_name),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", sched_cancel),
            CallbackQueryHandler(wizard_escape),
        ],
        conversation_timeout=300,
    )

    # Conversation handler must be added BEFORE the global sched:* callback
    # so wizard entry/escape patterns match first.
    app.add_handler(sched_conv)
    app.add_handler(CommandHandler("scheduled", cmd_scheduled))
    app.add_handler(CallbackQueryHandler(on_sched_callback, pattern=r"^sched:"))
