"""Scheduler module — periodic execution of Actions and project operations.

Entry point: register_scheduler(app, cfg, db, *, wizard_step, wizard_finish,
wizard_escape, run_action, project_ops).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from .db import SchedulerDB
from .executor import Executor, ProjectOps
from .handlers import register_handlers

if TYPE_CHECKING:
    from telegram.ext import Application

    from ..config import Config
    from ..db import DB


logger = logging.getLogger(__name__)


def register_scheduler(
    app: "Application",
    cfg: "Config",
    db: "DB",
    *,
    wizard_step: Callable,
    wizard_finish: Callable,
    wizard_escape: Callable,
    run_action: Callable[[str], Awaitable[tuple[int, str]]],
    project_ops: ProjectOps,
) -> SchedulerDB:
    """Wire the scheduler module into the Telegram Application.

    Returns the `SchedulerDB` so callers (e.g. admin notifs toggle in bot.py)
    can read/write `bot_settings.notifications_enabled` directly.
    """
    scheduler_db = SchedulerDB(cfg.data_dir / "projects.db")
    executor = Executor(
        scheduler_db=scheduler_db,
        run_action=run_action,
        project_ops=project_ops,
        bot=app.bot,
        allowed_user_ids=cfg.allowed_user_ids,
    )

    register_handlers(
        app, cfg, db, scheduler_db, executor,
        wizard_step=wizard_step,
        wizard_finish=wizard_finish,
        wizard_escape=wizard_escape,
    )

    # Replay enabled tasks into APScheduler at startup.
    from .triggers import build_trigger
    scheduler = app.job_queue.scheduler
    for task in scheduler_db.list_tasks(only_enabled=True):
        try:
            trigger = build_trigger(task["trigger_kind"], task["trigger_spec"])
            scheduler.add_job(
                executor.run_task,
                trigger=trigger,
                args=[task["id"]],
                id=f"sched:{task['id']}",
                misfire_grace_time=None,
                max_instances=1,
                replace_existing=True,
            )
        except Exception:
            logger.exception("failed to replay scheduled task %s at boot", task["id"])

    return scheduler_db
