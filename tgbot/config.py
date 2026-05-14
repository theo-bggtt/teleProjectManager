"""Configuration loader."""
import tomllib
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class TradingConfig:
    enabled: bool = False
    helius_api_key: str = ""
    alchemy_api_key: str = ""
    mc_poll_interval: int = 30
    evm_chains: list[str] = field(default_factory=lambda: ["eth", "base", "bsc"])


@dataclass
class Config:
    bot_token: str
    allowed_user_ids: set[int]
    data_dir: Path
    shell_timeout: int
    default_log_lines: int
    health_mounts: list[str] = field(default_factory=lambda: ["/"])
    trading: TradingConfig | None = None

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        trading_raw = raw.get("trading")
        trading_cfg = None
        if trading_raw is not None:
            trading_cfg = TradingConfig(
                enabled=bool(trading_raw.get("enabled", False)),
                helius_api_key=str(trading_raw.get("helius_api_key", "")),
                alchemy_api_key=str(trading_raw.get("alchemy_api_key", "")),
                mc_poll_interval=int(trading_raw.get("mc_poll_interval", 30)),
                evm_chains=list(trading_raw.get("evm_chains", ["eth", "base", "bsc"])),
            )
        return cls(
            bot_token=raw["bot_token"],
            allowed_user_ids=set(raw["allowed_user_ids"]),
            data_dir=Path(raw.get("data_dir", "./data")).expanduser().resolve(),
            shell_timeout=int(raw.get("shell_timeout", 30)),
            default_log_lines=int(raw.get("default_log_lines", 50)),
            health_mounts=list(raw.get("health_mounts", ["/"])),
            trading=trading_cfg,
        )
