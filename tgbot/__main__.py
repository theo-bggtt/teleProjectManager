"""Entry point: `python -m tgbot [config-path]`."""
import logging
import sys
from pathlib import Path

from .bot import build_app
from .config import Config


def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    )
    # Quiet the noisy httpx polling logs from telegram-bot
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.toml")
    if not config_path.exists():
        sys.exit(
            f"Config file not found: {config_path}\n"
            f"Copy config.example.toml to {config_path} and fill in your details."
        )
    cfg = Config.load(config_path)
    app = build_app(cfg)
    logging.info("Bot starting (data_dir=%s)", cfg.data_dir)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
