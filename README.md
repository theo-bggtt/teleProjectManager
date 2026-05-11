# Telegram Project Manager

A Telegram bot that lets you manage long-running projects (web servers, scripts, bots,
anything that has a "start command") on your Raspberry Pi or WSL dev machine.

## What it does

- **Add folders as projects.** Each project gets a name, path, and a start command.
- **Run / stop / restart** projects from Telegram. They run inside detached `tmux`
  sessions, so they survive the bot itself restarting — and you can SSH in and
  `tmux attach -t tgbot_<name>` to see live output.
- **Tail logs** with `/logs`.
- **Browse and edit files**: `/ls`, `/get` to download, reply with a document
  (caption `/put name path`) to upload. Previous versions are automatically backed up.
- **Shell access**: `/shell <project> <command>` runs anything in the project's
  directory. Output (or output-as-document, if large) comes back.
- **Locked to a whitelist** of Telegram user IDs.

## Setup

### 1. Get a bot token

Message [@BotFather](https://t.me/BotFather), `/newbot`, follow the prompts.
Save the token.

### 2. Get your user ID

Message [@userinfobot](https://t.me/userinfobot). It replies with your numeric ID.

### 3. Install on your machine

You need:
- Python 3.11+
- `tmux` (Linux/WSL only — see Windows note below)

#### Ubuntu / Debian / Raspberry Pi OS / WSL

```bash
sudo apt update && sudo apt install -y python3 python3-venv tmux

# From the repo root:
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.toml config.toml
# Edit config.toml: paste your bot_token and allowed_user_ids
```

#### Windows (PowerShell)

`tmux` does not run natively on Windows. For development you can either:
- Run the bot inside **WSL** (recommended — follow the Ubuntu steps above), or
- Run on native Windows for testing, knowing that the tmux-based features
  (`/run`, `/stop`, `/logs`, persistent sessions) require WSL or Linux.

```powershell
# Install Python 3.11+ from python.org first, then from the repo root:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item config.example.toml config.toml
# Edit config.toml: paste your bot_token and allowed_user_ids
```

### 4. Run it

Ubuntu / WSL:
```bash
python -m tgbot
# or with explicit config path:
python -m tgbot /path/to/config.toml
```

Windows (PowerShell):
```powershell
python -m tgbot
# or with explicit config path:
python -m tgbot C:\path\to\config.toml
```

Message your bot `/help` on Telegram.

## Production on Raspberry Pi (systemd)

```bash
# After installing as above at /home/pi/tgpm:
sudo cp deploy/tgbot.service /etc/systemd/system/tgbot.service
# Edit the User and paths in the unit file if different from defaults.
sudo systemctl daemon-reload
sudo systemctl enable --now tgbot.service

# Check it:
systemctl status tgbot
journalctl -u tgbot -f
```

## Typical usage

```
/add myapi /home/pi/projects/myapi
/config myapi
  > python main.py
  > main.py
/run myapi
/status myapi
/logs myapi
/ls myapi
/get myapi main.py
   (edit locally, then reply with the file — caption is pre-filled)
/restart myapi
/shell myapi git pull
```

## Layout

```
tgpm/
├── tgbot/
│   ├── __main__.py    # `python -m tgbot` entry point
│   ├── bot.py         # All command handlers + Application wiring
│   ├── config.py      # TOML config loader
│   ├── db.py          # SQLite project store
│   ├── runner.py      # tmux session management
│   ├── files.py       # File ops with safe-path resolution + backups
│   ├── shell.py       # /shell executor with timeout
│   └── auth.py        # User ID whitelist decorator
├── deploy/
│   └── tgbot.service  # systemd unit
├── config.example.toml
└── requirements.txt
```

After running you'll also see:
```
data/
├── projects.db        # SQLite — project list + config
├── logs/              # tmux pipe-pane captures
│   └── <project>.log
└── backups/           # File backups before /put overwrites
    └── <project>/
        └── <ts>_<safe_name>
```

## Notes and caveats

- **`/shell` runs unrestricted commands as the bot's Unix user.** That's why the
  whitelist exists. Don't add anyone you wouldn't give SSH to.
- **`shell=True` is intentional** — pipes, redirects, and globs all work.
- **Path safety on `/ls /get /put`**: relative paths are resolved against the
  project root and rejected if they escape it. (`/shell` doesn't enforce this —
  if you want it to, that's a config flag away.)
- **WSL dev specifics**: run inside WSL, not Windows. `tmux` lives there.
  Paths like `/mnt/c/Users/you/code/proj` work fine.
- **State recovery**: if the bot crashes or restarts, running projects keep
  going inside tmux. The next `/status` will correctly show them as running.
- **The Telegram message limit (~4096 chars)** — long logs or shell output is
  auto-shipped as a `.txt` document instead.
- **Markdown escaping is light** — if project names or paths contain `_`, `*`,
  or backticks, formatting might look odd but nothing breaks. Hardening pass
  available if you want one.

## Extending

- Want to edit files inline? Add a `/cat` command that sends file contents as
  text (with a length guard) and a follow-up flow that accepts a reply.
- Want per-project env vars? The `env_vars` column already exists in the DB
  as JSON — wire a `/env` subcommand into the conversation handler.
- Want notifications on crash? Have `runner.start` spawn a watcher that
  checks `is_running` periodically and pings you on transition to stopped.