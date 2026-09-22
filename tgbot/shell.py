"""One-shot shell command execution for /shell."""
import asyncio
import os


def non_interactive_env() -> dict[str, str]:
    """Environment for commands launched from Telegram, where nobody can type.

    Without this, git asks for a username/password on the bot's terminal
    (HTTPS remote with no stored credentials) and the command hangs until
    the timeout instead of failing with a readable error.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


class ShellRunner:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    async def run(self, command: str, cwd: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=non_interactive_env(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            return proc.returncode, out.decode(errors="replace")
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return -1, f"(killed after {self.timeout}s timeout)"
