"""Manage long-running projects.

Two backends with the same interface:
  * TmuxRunner  — Linux/macOS, sessions survive bot restarts via tmux.
  * WindowsRunner — Windows, detached subprocess + .pid file for cross-restart tracking.

Use ``make_runner(log_dir)`` to pick the right one for the current platform.
"""
import asyncio
import os
import shlex
import subprocess
import sys
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
        try:
            rc, _, _ = await self._run("tmux", "has-session", "-t", self._session(project))
        except FileNotFoundError:
            return False
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


class WindowsRunner:
    """Windows backend: detached child process per project, PID tracked on disk.

    Processes survive the bot's lifetime (DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP)
    and are re-discovered after a bot restart via the .pid file next to the log.
    """

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def log_path(self, project: str) -> Path:
        return self.log_dir / f"{project}.log"

    def _pid_path(self, project: str) -> Path:
        return self.log_dir / f"{project}.pid"

    def _read_pid(self, project: str) -> int | None:
        p = self._pid_path(project)
        if not p.exists():
            return None
        try:
            return int(p.read_text().strip())
        except (ValueError, OSError):
            return None

    async def is_running(self, project: str) -> bool:
        pid = self._read_pid(project)
        if pid is None:
            return False
        if _pid_alive_win(pid):
            return True
        # Stale pid file — clean up.
        try:
            self._pid_path(project).unlink()
        except OSError:
            pass
        return False

    async def start(self, project: str, command: str, cwd: str,
                    env: dict | None = None) -> tuple[bool, str]:
        if await self.is_running(project):
            return False, "Already running"

        log_file = self.log_path(project)
        log_file.write_text("")  # truncate previous run

        merged_env = os.environ.copy()
        if env:
            merged_env.update({k: str(v) for k, v in env.items()})

        # DETACHED_PROCESS (0x00000008) + CREATE_NEW_PROCESS_GROUP (0x00000200)
        # so the child outlives the bot and isn't killed by Ctrl-C on the bot.
        creationflags = 0x00000008 | 0x00000200
        log_fp = open(log_file, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=merged_env,
                creationflags=creationflags,
                close_fds=True,
            )
        except OSError as e:
            log_fp.close()
            return False, f"spawn failed: {e}"
        finally:
            # Parent's copy of the fd; child keeps its own.
            log_fp.close()

        self._pid_path(project).write_text(str(proc.pid))
        return True, f"Started `{project}` (pid {proc.pid})"

    async def stop(self, project: str) -> bool:
        pid = self._read_pid(project)
        if pid is None or not _pid_alive_win(pid):
            try:
                self._pid_path(project).unlink()
            except OSError:
                pass
            return False
        # taskkill /T kills the whole tree (shell + spawned children).
        proc = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        try:
            self._pid_path(project).unlink()
        except OSError:
            pass
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
        # Pure-Python tail: read whole file then slice. Logs are small in practice.
        try:
            content = log_file.read_text(errors="replace")
        except OSError as e:
            return f"(error reading log: {e})"
        if not content:
            return "(log is empty)"
        tail = content.splitlines()[-lines:]
        return "\n".join(tail)


def _pid_alive_win(pid: int) -> bool:
    """Return True if a PID corresponds to a live process on Windows."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack rights to signal it.
        return True
    except OSError:
        return False
    return True


def make_runner(log_dir: Path):
    """Pick the right runner for the current platform."""
    if sys.platform == "win32":
        return WindowsRunner(log_dir)
    return TmuxRunner(log_dir)