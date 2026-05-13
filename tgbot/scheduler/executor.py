"""Run scheduled tasks and notify users."""
import logging
from typing import Awaitable, Callable, Iterable, Protocol

from .db import SchedulerDB

logger = logging.getLogger(__name__)


class ProjectOps(Protocol):
    async def start(self, name: str) -> tuple[bool, str]: ...
    async def stop(self, name: str) -> bool: ...
    async def restart(self, name: str) -> tuple[bool, str]: ...


RunActionFn = Callable[[str], Awaitable[tuple[int, str]]]


class Executor:
    """Bound to APScheduler jobs as `executor.run_task` (one instance per bot)."""

    def __init__(
        self,
        *,
        scheduler_db: SchedulerDB,
        run_action: RunActionFn,
        project_ops: ProjectOps,
        bot,
        allowed_user_ids: Iterable[int],
    ):
        self.sdb = scheduler_db
        self.run_action = run_action
        self.project_ops = project_ops
        self.bot = bot
        self.user_ids = list(allowed_user_ids)

    async def run_task(self, task_id: int) -> None:
        """APScheduler job entry point. Must never raise (would kill the job)."""
        task = self.sdb.get_task(task_id)
        if task is None:
            logger.warning("scheduled task %s vanished — skipping", task_id)
            return
        if not task["enabled"]:
            logger.debug("scheduled task %s disabled — skipping", task_id)
            return

        name = task["name"]
        status: str = "error"
        detail: str = ""
        try:
            if task["task_type"] == "action":
                rc, out = await self.run_action(task["target"])
                status = "ok" if rc == 0 else "error"
                lines = (out or "").strip().splitlines()
                detail = lines[-1] if lines else ""
            elif task["task_type"] == "project_op":
                op = task["operation"]
                target = task["target"]
                if op == "start":
                    ok, msg = await self.project_ops.start(target)
                    status = "ok" if ok else "error"
                    detail = msg
                elif op == "stop":
                    ok = await self.project_ops.stop(target)
                    status = "ok" if ok else "error"
                elif op == "restart":
                    ok, msg = await self.project_ops.restart(target)
                    status = "ok" if ok else "error"
                    detail = msg
                else:
                    logger.error("unknown project_op operation: %s", op)
            else:
                logger.error("unknown task_type: %s", task["task_type"])
        except Exception:  # noqa: BLE001 — must not crash the job loop
            logger.exception("scheduled task %s raised", task_id)
            status = "error"

        self.sdb.set_last_run(task_id, status)
        if self.sdb.get_notifications_enabled():
            await self._notify(name, status, detail)

    async def _notify(self, task_name: str, status: str, detail: str) -> None:
        icon = "✅" if status == "ok" else "❌"
        text = f"⏰ *{task_name}* · {icon}"
        if detail:
            # Telegram has a 4096-char limit; keep notifications short.
            text += f"\n`{detail[:200]}`"
        for uid in self.user_ids:
            try:
                await self.bot.send_message(
                    chat_id=uid, text=text, parse_mode="Markdown",
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to notify user %s", uid)
