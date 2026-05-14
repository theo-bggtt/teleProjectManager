"""Root shell mode: in-memory shell sessions with navigable cwd.

This module exposes the data structures and pure helpers used by the
admin-gated "shell mode" wired up in tgbot.bot. The mode lets an admin
execute shell commands by sending plain text messages once they have
entered the mode from the admin menu.
"""

import os
import re
import time  # imported as module so tests can monkeypatch time.monotonic
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 600  # 10 minutes
DEFAULT_OUTPUT_LIMIT = 3500    # leaves room for header and HTML wrapping

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Strip ANSI CSI escape sequences (colors, cursor movement, etc.)."""
    return _ANSI_RE.sub("", text)


def truncate_output(text: str, limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    """Truncate `text` to `limit` characters and append a marker if cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… (tronqué)"


@dataclass
class ShellSession:
    """A live root-shell session for a single Telegram user.

    The `message_id` points at the panel message that is edited in place
    after each command; `cwd` tracks the navigable working directory and
    `last_activity` (monotonic seconds) drives the idle timeout.
    """

    user_id: int
    chat_id: int
    message_id: int
    cwd: str
    last_activity: float


class ShellSessionStore:
    """In-memory map of `user_id` -> `ShellSession`.

    PTB runs handlers serially per update by default, so a plain dict is
    safe. If `concurrent_updates` is ever enabled in the application, wrap
    mutations with an `asyncio.Lock`.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, ShellSession] = {}

    def get(self, user_id: int) -> ShellSession | None:
        return self._sessions.get(user_id)

    def start(
        self, user_id: int, chat_id: int, message_id: int, cwd: str
    ) -> ShellSession:
        """Create or replace the session for `user_id` and return it.

        Any existing session for the same user is silently discarded,
        including its `chat_id`, `message_id`, and `cwd`.
        """
        session = ShellSession(
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            cwd=cwd,
            last_activity=time.monotonic(),
        )
        self._sessions[user_id] = session
        return session

    def end(self, user_id: int) -> ShellSession | None:
        return self._sessions.pop(user_id, None)

    def touch(self, user_id: int) -> None:
        """Refresh the last-activity timestamp. No-op if no session exists."""
        s = self._sessions.get(user_id)
        if s is not None:
            s.last_activity = time.monotonic()

    def set_message_id(self, user_id: int, message_id: int) -> None:
        """Update the panel `message_id`. No-op if no session exists."""
        s = self._sessions.get(user_id)
        if s is not None:
            s.message_id = message_id

    def set_cwd(self, user_id: int, cwd: str) -> None:
        """Update the working directory. No-op if no session exists."""
        s = self._sessions.get(user_id)
        if s is not None:
            s.cwd = cwd

    def expired(
        self, now: float, ttl: float = DEFAULT_TIMEOUT_SECONDS
    ) -> list[ShellSession]:
        return [s for s in self._sessions.values() if (now - s.last_activity) > ttl]


def resolve_cd(current_cwd: str, arg: str | None, *, bot_root: str) -> str | None:
    """Resolve a `cd` target relative to `current_cwd`.

    Returns the absolute, normalized path on success, or `None` if the
    target does not exist or is not a directory. `arg` is treated as
    absolute when it begins with a path separator; otherwise joined to
    `current_cwd`. Tilde (`~`) and `$VAR` expansion are performed.
    An empty or missing `arg` returns `bot_root`.
    """
    if not arg:
        return os.path.normpath(bot_root) if os.path.isdir(bot_root) else None
    expanded = os.path.expandvars(os.path.expanduser(arg))
    target = os.path.normpath(
        expanded if os.path.isabs(expanded) else os.path.join(current_cwd, expanded)
    )
    return target if os.path.isdir(target) else None
