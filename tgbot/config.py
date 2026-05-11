"""Configuration loader."""
import tomllib
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Config:
    bot_token: str
    allowed_user_ids: set[int]
    data_dir: Path
    shell_timeout: int
    default_log_lines: int

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        return cls(
            bot_token=raw["bot_token"],
            allowed_user_ids=set(raw["allowed_user_ids"]),
            data_dir=Path(raw.get("data_dir", "./data")).expanduser().resolve(),
            shell_timeout=int(raw.get("shell_timeout", 30)),
            default_log_lines=int(raw.get("default_log_lines", 50)),
        )
