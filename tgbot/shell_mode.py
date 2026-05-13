"""Root shell mode: in-memory shell sessions with navigable cwd.

This module exposes the data structures and pure helpers used by the
admin-gated "shell mode" wired up in tgbot.bot. The mode lets an admin
execute shell commands by sending plain text messages once they have
entered the mode from the admin menu.
"""

import re

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
