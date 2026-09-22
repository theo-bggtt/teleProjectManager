"""Tests for the one-shot ShellRunner used by /shell, actions and shell mode."""
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tgbot.shell import ShellRunner


class _AuthRequiredHandler(BaseHTTPRequestHandler):
    """Answers every request with 401, like GitHub does for a private repo over HTTPS."""

    def do_GET(self):  # noqa: N802 — http.server API
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="GitHub"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def auth_required_url():
    server = HTTPServer(("127.0.0.1", 0), _AuthRequiredHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/owner/private.git"
    server.shutdown()
    server.server_close()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_git_auth_prompt_fails_fast_instead_of_hanging(
    auth_required_url, tmp_path, monkeypatch,
):
    # Look like a normal machine, not a CI box that already disables prompts.
    for var in ("GIT_TERMINAL_PROMPT", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(var, raising=False)
    runner = ShellRunner(timeout=10)
    rc, out = await runner.run(f"git ls-remote {auth_required_url}", str(tmp_path))
    assert rc != 0
    assert "timeout" not in out
    assert "terminal prompts disabled" in out

