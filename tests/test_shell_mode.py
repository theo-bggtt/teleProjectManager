"""Tests for the root shell mode helpers and session store."""

from tgbot.shell_mode import strip_ansi, truncate_output


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
