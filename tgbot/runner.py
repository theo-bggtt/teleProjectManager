"""Manage long-running projects via tmux sessions.

Each project gets a session named `tgbot_<project>`. Output is piped to
data/logs/<project>.log so /logs can tail it. Sessions survive bot restarts —
the bot just attaches to whatever's already running.
"""
import asyncio
import shlex
from pathlib import Path


class TmuxRunner:
    SESSION_PREFIX = "tgbot_"

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _session(self, project: str) -> str:
        return f"{self.SESSION_PREFIX}{project}"

    def log_path(self, project: str) -> Path:
        return self.log_dir / f"{project}.log"

    async def _run(self, *args) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def is_running(self, project: str) -> bool:
        rc, _, _ = await self._run("tmux", "has-session", "-t", self._session(project))
        return rc == 0

    async def start(self, project: str, command: str, cwd: str,
                    env: dict | None = None) -> tuple[bool, str]:
        if await self.is_running(project):
            return False, "Already running"

        session = self._session(project)
        log_file = self.log_path(project)
        log_file.write_text("")  # truncate previous run's log

        rc, _, err = await self._run("tmux", "new-session", "-d", "-s", session, "-c", cwd)
        if rc != 0:
            return False, f"tmux new-session failed: {err.strip() or '(no stderr)'}"

        # Pipe pane output to log file
        pipe_cmd = f"cat >> {shlex.quote(str(log_file))}"
        await self._run("tmux", "pipe-pane", "-t", session, "-o", pipe_cmd)

        if env:
            for k, v in env.items():
                await self._run(
                    "tmux", "send-keys", "-t", session,
                    f"export {k}={shlex.quote(str(v))}", "Enter",
                )

        await self._run("tmux", "send-keys", "-t", session, command, "Enter")
        return True, f"Started session `{session}`"

    async def stop(self, project: str) -> bool:
        if not await self.is_running(project):
            return False
        await self._run("tmux", "kill-session", "-t", self._session(project))
        return True

    async def restart(self, project: str, command: str, cwd: str,
                      env: dict | None = None) -> tuple[bool, str]:
        await self.stop(project)
        await asyncio.sleep(0.3)
        return await self.start(project, command, cwd, env)

    async def get_logs(self, project: str, lines: int = 50) -> str:
        log_file = self.log_path(project)
        if not log_file.exists():
            return "(no log file yet — has the project been started?)"
        proc = await asyncio.create_subprocess_exec(
            "tail", "-n", str(lines), str(log_file),
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="replace")
        return text or "(log is empty)"