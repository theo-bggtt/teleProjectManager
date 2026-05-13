"""Tests for tgbot.scheduler.executor.Executor."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from tgbot.scheduler.db import SchedulerDB
from tgbot.scheduler.executor import Executor


@pytest.fixture
def sdb(tmp_db_path):
    return SchedulerDB(tmp_db_path)


def make_executor(sdb, *, action_rc=0, action_out="ok output",
                  proj_op_success=True, notifications=True):
    """Build an Executor with mocked deps."""
    run_action = AsyncMock(return_value=(action_rc, action_out))

    project_ops = MagicMock()
    project_ops.start = AsyncMock(return_value=(proj_op_success, "started"))
    project_ops.stop = AsyncMock(return_value=proj_op_success)
    project_ops.restart = AsyncMock(return_value=(proj_op_success, "restarted"))

    bot = MagicMock()
    bot.send_message = AsyncMock()
    sdb.set_notifications_enabled(notifications)
    return Executor(
        scheduler_db=sdb,
        run_action=run_action,
        project_ops=project_ops,
        bot=bot,
        allowed_user_ids={42, 43},
    ), bot, run_action, project_ops


async def test_run_action_task_success(sdb):
    task_id = sdb.add_task(
        name="t", task_type="action", target="myact", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    ex, bot, run_action, _ = make_executor(sdb)
    await ex.run_task(task_id)
    run_action.assert_awaited_once_with("myact")
    t = sdb.get_task(task_id)
    assert t["last_status"] == "ok"
    assert t["last_run_at"] is not None
    # 2 allowed users → 2 notifications
    assert bot.send_message.await_count == 2


async def test_run_action_task_failure(sdb):
    task_id = sdb.add_task(
        name="t", task_type="action", target="myact", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    ex, _, _, _ = make_executor(sdb, action_rc=1)
    await ex.run_task(task_id)
    assert sdb.get_task(task_id)["last_status"] == "error"


async def test_run_project_op_restart(sdb):
    task_id = sdb.add_task(
        name="t", task_type="project_op", target="my-proj", operation="restart",
        trigger_kind="daily", trigger_spec={"hour": 4, "minute": 0},
    )
    ex, _, _, project_ops = make_executor(sdb)
    await ex.run_task(task_id)
    project_ops.restart.assert_awaited_once_with("my-proj")
    assert sdb.get_task(task_id)["last_status"] == "ok"


async def test_run_project_op_stop(sdb):
    task_id = sdb.add_task(
        name="t", task_type="project_op", target="my-proj", operation="stop",
        trigger_kind="daily", trigger_spec={"hour": 4, "minute": 0},
    )
    ex, _, _, project_ops = make_executor(sdb)
    await ex.run_task(task_id)
    project_ops.stop.assert_awaited_once_with("my-proj")


async def test_disabled_task_is_noop(sdb):
    task_id = sdb.add_task(
        name="t", task_type="action", target="myact", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    sdb.set_enabled(task_id, False)
    ex, _, run_action, _ = make_executor(sdb)
    await ex.run_task(task_id)
    run_action.assert_not_awaited()


async def test_missing_task_is_noop(sdb):
    ex, _, run_action, _ = make_executor(sdb)
    await ex.run_task(999)
    run_action.assert_not_awaited()


async def test_notifications_off_no_send(sdb):
    task_id = sdb.add_task(
        name="t", task_type="action", target="myact", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    ex, bot, _, _ = make_executor(sdb, notifications=False)
    await ex.run_task(task_id)
    bot.send_message.assert_not_awaited()


async def test_executor_swallows_internal_exceptions(sdb):
    """If the underlying action raises, run_task must still mark last_status='error'."""
    task_id = sdb.add_task(
        name="t", task_type="action", target="myact", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    run_action = AsyncMock(side_effect=RuntimeError("boom"))
    bot = MagicMock(); bot.send_message = AsyncMock()
    ex = Executor(
        scheduler_db=sdb, run_action=run_action,
        project_ops=MagicMock(),
        bot=bot, allowed_user_ids={1},
    )
    await ex.run_task(task_id)  # must NOT raise
    assert sdb.get_task(task_id)["last_status"] == "error"
