"""Tests for tgbot.scheduler.db.SchedulerDB."""
import json
import pytest

from tgbot.scheduler.db import SchedulerDB


def test_init_creates_tables(tmp_db_path):
    SchedulerDB(tmp_db_path)
    import sqlite3
    conn = sqlite3.connect(tmp_db_path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    conn.close()
    assert "scheduled_tasks" in names
    assert "bot_settings" in names


def test_notifications_default_enabled(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    assert sdb.get_notifications_enabled() is True


def test_set_notifications(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    sdb.set_notifications_enabled(False)
    assert sdb.get_notifications_enabled() is False
    sdb.set_notifications_enabled(True)
    assert sdb.get_notifications_enabled() is True


def test_add_list_get_task(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    task_id = sdb.add_task(
        name="Restart trading",
        task_type="project_op",
        target="trading-bot",
        operation="restart",
        trigger_kind="daily",
        trigger_spec={"hour": 4, "minute": 0},
    )
    assert task_id > 0
    tasks = sdb.list_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["name"] == "Restart trading"
    assert t["task_type"] == "project_op"
    assert t["operation"] == "restart"
    assert t["enabled"] == 1
    assert t["trigger_spec"] == {"hour": 4, "minute": 0}  # round-trip JSON


def test_toggle_enabled(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    task_id = sdb.add_task(
        name="x", task_type="action", target="a", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    sdb.set_enabled(task_id, False)
    assert sdb.get_task(task_id)["enabled"] == 0
    sdb.set_enabled(task_id, True)
    assert sdb.get_task(task_id)["enabled"] == 1


def test_update_last_run(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    task_id = sdb.add_task(
        name="x", task_type="action", target="a", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    sdb.set_last_run(task_id, "ok")
    t = sdb.get_task(task_id)
    assert t["last_status"] == "ok"
    assert t["last_run_at"] is not None


def test_delete_task(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    task_id = sdb.add_task(
        name="x", task_type="action", target="a", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    assert sdb.delete_task(task_id) is True
    assert sdb.get_task(task_id) is None
    assert sdb.delete_task(task_id) is False
