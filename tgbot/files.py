"""File listing, reading, and writing with automatic backups.

Every write operation first copies the current file (if any) to
data/backups/<project>/<timestamp>_<safe_name>, keeping the latest 10.

All paths are resolved and verified to stay within the project root, so a
user can't write to /etc/passwd via a relative path with '../' segments.
"""
import shutil
import time
from pathlib import Path


class PathEscapeError(ValueError):
    """Raised when a relative path resolves outside its project root."""


class FileManager:
    BACKUPS_PER_FILE = 10

    def __init__(self, backup_dir: Path):
        self.backup_dir = backup_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_safe(self, root_str: str, rel: str) -> Path:
        root = Path(root_str).resolve()
        target = (root / rel).resolve()
        # Path.is_relative_to is 3.9+; we're on 3.11+
        if not target.is_relative_to(root):
            raise PathEscapeError(f"Path '{rel}' escapes project root")
        return target

    def list_dir(self, project_path: str, rel: str = ".") -> list[str]:
        target = self._resolve_safe(project_path, rel)
        if not target.exists():
            raise FileNotFoundError(rel)
        if not target.is_dir():
            raise NotADirectoryError(rel)
        entries = []
        for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            suffix = "/" if p.is_dir() else ""
            entries.append(f"{p.name}{suffix}")
        return entries

    def get_file(self, project_path: str, rel: str) -> Path:
        target = self._resolve_safe(project_path, rel)
        if not target.exists():
            raise FileNotFoundError(rel)
        if not target.is_file():
            raise IsADirectoryError(rel)
        return target

    def put_file(self, project: str, project_path: str, rel: str, content: bytes) -> Path:
        target = self._resolve_safe(project_path, rel)

        if target.exists():
            safe_name = rel.replace("/", "_").replace("\\", "_")
            backup_dir = self.backup_dir / project
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{int(time.time())}_{safe_name}"
            shutil.copy2(target, backup_path)
            self._prune_backups(backup_dir, safe_name)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def _prune_backups(self, backup_dir: Path, safe_name: str) -> None:
        matches = sorted(
            backup_dir.glob(f"*_{safe_name}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in matches[self.BACKUPS_PER_FILE:]:
            old.unlink(missing_ok=True)