"""Tests for the root shell mode helpers and session store."""

from tgbot.shell_mode import (
    DEFAULT_TIMEOUT_SECONDS,
    ShellSession,
    ShellSessionStore,
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
