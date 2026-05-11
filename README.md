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
- **Actions** — save named one-click commands separate from projects (a Docker
  compose, a `git pull`, a Python script, a long-running watcher, …). Each
  action has a name, a command, an optional working directory, a mode
  (`oneshot` for fire-and-forget, `managed` for start/stop/logs like projects)
  and an optional `require_confirm` flag for destructive operations.
- **Locked to a whitelist** of Telegram user IDs.
- **Trading module (optional)** — watch on-chain wallets across Solana and EVM
  chains (Ethereum, Base, BSC), get push notifications on every trade /
  transfer / contract call, set marketcap alerts (one-shot or persistent),
  and pull a USD holdings snapshot on demand. Read-only — no keys, no signing.

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

### Actions

```
/addaction
  > name:  deploy_prod
  > cmd:   docker compose -f /srv/app/docker-compose.yml up -d
  > cwd:   -
  > mode:  ⚡ Oneshot
  > confirm? Oui
/actions                  # list
/runaction deploy_prod    # triggers the confirm prompt, then runs
/delaction deploy_prod    # delete
```

From `/start`, the inline menu now offers **🚀 Actions** next to **📂 Projets**.
Managed actions reuse the same tmux/Windows runner that drives projects, so they
survive bot restarts and expose `▶️ Start / ⏹ Stop / 🔄 Restart / 📄 Logs`
buttons. Oneshot actions return the captured stdout + exit code right in chat
(or as a `.txt` attachment when long).

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
├── projects.db        # SQLite — projects + actions
├── logs/              # tmux pipe-pane captures
│   ├── <project>.log
│   └── action_<name>.log   # for managed actions
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

## Trading module (optional)

The bot can also monitor on-chain wallet activity and marketcap alerts in
parallel with the project manager. It is fully optional and stays off until
you add a `[trading]` section to `config.toml`. With the section missing or
`enabled = false`, no trading code runs, no extra dependencies are touched,
and the bot is byte-identical to the non-trading flow.

### What it does

- **Watch wallets.** Push notifications when any tracked wallet sees activity
  on Solana (Helius Atlas WSS) or EVM chains (Alchemy WSS — Ethereum, Base,
  BSC). Native transfers are decoded; ERC20 swaps / contract calls surface as
  generic activity with an explorer link.
- **Marketcap alerts.** `/alert <token> <chain> <mc> [--above|--below]
  [--persistent]`. Polled every `mc_poll_interval` seconds via Dexscreener
  (no API key). One-shot alerts disarm on trigger; persistent ones stay
  armed with a per-alert cooldown.
- **Holdings snapshot.** `/holdings <wallet> <chain>` returns the top
  positions sorted by USD value (top 15 + aggregated tail + total).
- **Read-only.** Nothing in the trading module signs or sends a transaction.
  No private keys are stored or accepted.

### Setup

1. Sign up for free API keys (free tiers are sufficient for personal use):
   - Helius — https://www.helius.dev — Solana RPC + WSS.
   - Alchemy — https://www.alchemy.com — EVM RPC + WSS (Ethereum, Base, BSC).
   - Dexscreener — used for prices / marketcap and needs no API key.

2. Install the extra dependencies (only needed when the module is enabled):

   ```bash
   pip install -r requirements.txt
   ```

3. Add this section to your `config.toml`:

   ```toml
   [trading]
   enabled = true
   helius_api_key  = "..."
   alchemy_api_key = "..."
   mc_poll_interval = 30                 # seconds between MC alert polls
   evm_chains = ["eth", "base", "bsc"]   # any subset
   ```

4. Restart the bot. You'll see `Trading module registered (chains: sol + eth,
   base, bsc)` in the logs and a new 📈 **Trading** entry in the `/start` menu.

### Trading commands

| Command | Effect |
|---|---|
| `/watch <addr> <chain> [label]` | Track a wallet (chain ∈ `sol`/`eth`/`base`/`bsc`) |
| `/unwatch <addr> [chain]` | Stop tracking |
| `/wallets` | List watched wallets |
| `/alert <token> <chain> <mc> [--above\|--below] [--persistent] [label...]` | Create MC alert; `<mc>` accepts `k`/`m`/`b` suffixes (`1m`, `500k`) |
| `/alerts` | List alerts (armed/disarmed) |
| `/unalert <id>` | Delete an alert |
| `/holdings <wallet> <chain>` | Snapshot positions + USD total |

The inline 📈 Trading menu mirrors all of this with tap-to-delete and
tap-to-holdings shortcuts.

### Security notes

- All trading commands inherit the same `allowed_user_ids` whitelist as the
  rest of the bot. Addresses are syntactically validated before storage
  (Solana base58 vs EVM hex).
- API keys live in `config.toml` (gitignored by default). Trading state lives
  in `data/trading.db`, separate from the project DB.
- WebSockets are **outbound only**. No ports need to be exposed.
- On reconnect after a network drop, the `seen_tx` table prevents duplicate
  notifications for transactions observed during both sessions.

## Extending

- Want to edit files inline? Add a `/cat` command that sends file contents as
  text (with a length guard) and a follow-up flow that accepts a reply.
- Want per-project env vars? The `env_vars` column already exists in the DB
  as JSON — wire a `/env` subcommand into the conversation handler.
- Want notifications on crash? Have `runner.start` spawn a watcher that
  checks `is_running` periodically and pings you on transition to stopped.
- Want richer trading parsing? `tgbot/trading/solana.py` and `evm.py`
  normalize transactions into a generic `WalletEvent`. Detailed swap
  decoding (Uniswap V2/V3 / Universal Router selectors, ERC20 Transfer log
  parsing on EVM; pre/post token balance diffing on Solana) plugs into the
  `_normalize` methods.