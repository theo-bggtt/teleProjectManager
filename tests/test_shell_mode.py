"""Tests for the root shell mode helpers and session store."""

import os

from tgbot.shell_mode import (
    DEFAULT_TIMEOUT_SECONDS,
    ShellSession,
    ShellSessionStore,
    resolve_cd,
    strip_ansi,
    truncate_output,
)


def test_strip_ansi_color_codes():
    assert strip_ansi("\x1b[31merror\x1b[0m") == "error"


def test_strip_ansi_cursor_movement():
    assert strip_ansi("hello\x1b[2K\x1b[1Aworld") == "helloworld"


def test_strip_ansi_no_ansi_unchanged():
    assert strip_ansi("plain text") == "plain text"


def test_strip_ansi_empty_string():
    assert strip_ansi("") == ""


def test_truncate_output_under_limit():
    assert truncate_output("hello", limit=100) == "hello"


def test_truncate_output_at_limit():
    text = "a" * 100
    assert truncate_output(text, limit=100) == text


def test_truncate_output_over_limit():
    text = "a" * 200
    result = truncate_output(text, limit=100)
    assert result.startswith("a" * 100)
    assert result.endswith("… (tronqué)")
    assert len(result) == 100 + len("\n… (tronqué)")


def test_store_start_creates_session():
    store = ShellSessionStore()
    s = store.start(user_id=42, chat_id=100, message_id=5, cwd="/tmp")
    assert s.user_id == 42
    assert s.chat_id == 100
    assert s.message_id == 5
    assert s.cwd == "/tmp"
    assert s.last_activity > 0


def test_store_get_returns_session():
    store = ShellSessionStore()
    store.start(42, 100, 5, "/tmp")
    s = store.get(42)
    assert s is not None
    assert s.user_id == 42


def test_store_get_returns_none_when_missing():
    store = ShellSessionStore()
    assert store.get(99) is None


def test_store_end_removes_session():
    store = ShellSessionStore()
    store.start(42, 100, 5, "/tmp")
    removed = store.end(42)
    assert removed is not None
    assert removed.user_id == 42
    assert store.get(42) is None


def test_store_end_returns_none_when_missing():
    store = ShellSessionStore()
    assert store.end(99) is None


def test_store_start_replaces_existing_session():
    store = ShellSessionStore()
    store.start(42, 100, 5, "/tmp")
    store.start(42, 100, 7, "/other")
    s = store.get(42)
    assert s.message_id == 7
    assert s.cwd == "/other"


def test_store_touch_updates_last_activity(monkeypatch):
    store = ShellSessionStore()
    times = iter([1000.0, 1050.0])
    monkeypatch.setattr("tgbot.shell_mode.time.monotonic", lambda: next(times))
    store.start(42, 100, 5, "/tmp")
    store.touch(42)
    assert store.get(42).last_activity == 1050.0


def test_store_touch_missing_session_noop():
    store = ShellSessionStore()
    store.touch(99)  # must not raise


def test_store_expired_returns_only_old_sessions(monkeypatch):
    store = ShellSessionStore()
    times = iter([100.0, 200.0])
    monkeypatch.setattr("tgbot.shell_mode.time.monotonic", lambda: next(times))
    store.start(1, 10, 1, "/a")  # last_activity=100
    store.start(2, 20, 2, "/b")  # last_activity=200
    expired = store.expired(now=750.0, ttl=DEFAULT_TIMEOUT_SECONDS)
    # 750 - 100 = 650 > 600 → expired
    # 750 - 200 = 550 < 600 → still active
    assert [s.user_id for s in expired] == [1]


def test_store_set_message_id_updates_panel():
    store = ShellSessionStore()
    store.start(42, 100, 5, "/tmp")
    store.set_message_id(42, 99)
    assert store.get(42).message_id == 99


def test_store_set_cwd_updates_directory():
    store = ShellSessionStore()
    store.start(42, 100, 5, "/tmp")
    store.set_cwd(42, "/var/log")
    assert store.get(42).cwd == "/var/log"


def test_resolve_cd_absolute_valid(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    result = resolve_cd(str(tmp_path), str(sub), bot_root=str(tmp_path))
    assert result == str(sub)


def test_resolve_cd_absolute_normalizes_trailing_separator(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    messy = str(sub).rstrip(os.sep) + os.sep  # trailing separator
    result = resolve_cd(str(tmp_path), messy, bot_root=str(tmp_path))
    assert result == os.path.normpath(str(sub))


def test_resolve_cd_relative_valid(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    result = resolve_cd(str(tmp_path), "sub", bot_root=str(tmp_path))
    assert result == os.path.normpath(str(sub))


def test_resolve_cd_dotdot(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    result = resolve_cd(str(sub), "..", bot_root=str(tmp_path))
    assert result == os.path.normpath(str(tmp_path))


def test_resolve_cd_invalid_path_returns_none(tmp_path):
    result = resolve_cd(str(tmp_path), "does-not-exist", bot_root=str(tmp_path))
    assert result is None


def test_resolve_cd_no_arg_returns_bot_root(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    result = resolve_cd(str(sub), None, bot_root=str(tmp_path))
    assert result == str(tmp_path)


def test_resolve_cd_empty_arg_returns_bot_root(tmp_path):
    result = resolve_cd(str(tmp_path), "", bot_root=str(tmp_path))
    assert result == str(tmp_path)


def test_resolve_cd_bot_root_missing_returns_none(tmp_path):
    missing = str(tmp_path / "nope")
    result = resolve_cd(str(tmp_path), None, bot_root=missing)
    assert result is None


def test_resolve_cd_file_not_dir_returns_none(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("hi")
    result = resolve_cd(str(tmp_path), "file.txt", bot_root=str(tmp_path))
    assert result is None


def test_resolve_cd_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows fallback
    result = resolve_cd("/somewhere", "~", bot_root=str(tmp_path))
    assert result == os.path.normpath(str(tmp_path))


def test_resolve_cd_expands_env_var(tmp_path, monkeypatch):
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.setenv("MY_DIR", str(sub))
    result = resolve_cd(str(tmp_path), "$MY_DIR", bot_root=str(tmp_path))
    assert result == os.path.normpath(str(sub))
