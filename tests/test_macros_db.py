"""Tests for tgbot.macros."""
import sqlite3
import time

import pytest

from tgbot.macros import MacrosDB, Macro, is_valid_name, is_valid_command


# ─── validators ──────────────────────────────────────────────────────────

def test_is_valid_name_ok():
    assert is_valid_name("deploy")
    assert is_valid_name("backup-bot")
    assert is_valid_name("a")
    assert is_valid_name("a" * 32)
    assert is_valid_name("123")
    assert is_valid_name("free-h")


def test_is_valid_name_rejects():
    assert not is_valid_name("")
    assert not is_valid_name("DEPLOY")
    assert not is_valid_name("-foo")
    assert not is_valid_name("a" * 33)
    assert not is_valid_name("with space")
    assert not is_valid_name("avec_underscore")
    assert not is_valid_name("dot.name")


def test_is_valid_command_ok():
    assert is_valid_command("ls")
    assert is_valid_command("git pull && systemctl restart bot")
    assert is_valid_command("a" * 4000)


def test_is_valid_command_rejects():
    assert not is_valid_command("")
    assert not is_valid_command("a" * 4001)


# ─── init ────────────────────────────────────────────────────────────────

def test_init_creates_table(tmp_db_path):
    MacrosDB(tmp_db_path)
    c = sqlite3.connect(tmp_db_path)
    names = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    c.close()
    assert "shell_macros" in names


# ─── add / get ───────────────────────────────────────────────────────────

def test_add_returns_id(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    mid = m.add(name="deploy", command="git pull", cwd=None)
    assert mid is not None
    assert mid > 0


def test_add_duplicate_name_returns_none(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    assert m.add(name="x", command="a", cwd=None) is not None
    assert m.add(name="x", command="b", cwd=None) is None


def test_get_by_id(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    mid = m.add(name="x", command="ls", cwd="/tmp")
    macro = m.get(mid)
    assert macro is not None
    assert macro.id == mid
    assert macro.name == "x"
    assert macro.command == "ls"
    assert macro.cwd == "/tmp"
    assert macro.last_run_at is None
    assert macro.created_at  # timestamp set


def test_get_missing_returns_none(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    assert m.get(999) is None


def test_get_by_name(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    m.add(name="foo", command="ls", cwd=None)
    assert m.get_by_name("foo") is not None
    assert m.get_by_name("foo").name == "foo"
    assert m.get_by_name("nope") is None


# ─── list / touch ────────────────────────────────────────────────────────

def test_list_empty(tmp_db_path):
    assert MacrosDB(tmp_db_path).list() == []


def test_list_orders_recent_first(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    a = m.add(name="a", command="x", cwd=None)
    b = m.add(name="b", command="x", cwd=None)
    c = m.add(name="c", command="x", cwd=None)
    time.sleep(0.01)  # ensure touch timestamp > created_at
    m.touch(b)
    ids = [x.id for x in m.list()]
    assert ids[0] == b  # touched -> first
    # remaining sorted by created_at DESC: c then a
    assert ids[1] == c
    assert ids[2] == a


def test_touch_unknown_is_noop(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    m.touch(999)  # must not raise
    assert m.list() == []


def test_touch_updates_last_run_at(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    mid = m.add(name="x", command="y", cwd=None)
    assert m.get(mid).last_run_at is None
    m.touch(mid)
    assert m.get(mid).last_run_at is not None


# ─── update ──────────────────────────────────────────────────────────────

def test_update_single_field(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    mid = m.add(name="x", command="a", cwd="/tmp")
    assert m.update(mid, command="b") is True
    macro = m.get(mid)
    assert macro.command == "b"
    assert macro.cwd == "/tmp"  # untouched
    assert macro.name == "x"  # untouched


def test_update_cwd_to_null_explicitly(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    mid = m.add(name="x", command="a", cwd="/tmp")
    assert m.update(mid, cwd=None) is True
    assert m.get(mid).cwd is None


def test_update_no_fields_returns_false(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    mid = m.add(name="x", command="a", cwd=None)
    assert m.update(mid) is False


def test_update_unknown_id_returns_false(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    assert m.update(999, command="x") is False


def test_update_name_collision(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    mid = m.add(name="x", command="a", cwd=None)
    m.add(name="y", command="b", cwd=None)
    assert m.update(mid, name="y") is False
    assert m.get(mid).name == "x"  # unchanged


# ─── delete ──────────────────────────────────────────────────────────────

def test_delete_returns_true_then_false(tmp_db_path):
    m = MacrosDB(tmp_db_path)
    mid = m.add(name="x", command="y", cwd=None)
    assert m.delete(mid) is True
    assert m.delete(mid) is False
    assert m.get(mid) is None


# ─── coexistence with other tables ───────────────────────────────────────

def test_coexists_with_other_tables(tmp_db_path):
    """MacrosDB must use IF NOT EXISTS so it can share projects.db."""
    c = sqlite3.connect(tmp_db_path)
    c.execute("CREATE TABLE projects (name TEXT PRIMARY KEY)")
    c.commit()
    c.close()
    # Should not raise
    m = MacrosDB(tmp_db_path)
    mid = m.add(name="x", command="y", cwd=None)
    assert mid is not None
