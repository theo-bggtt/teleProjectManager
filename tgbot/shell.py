"""One-shot shell command execution for /shell."""
import asyncio


class ShellRunner:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    async def run(self, command: str, cwd: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
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
