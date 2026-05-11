# Plan — Module "Trading" pour teleProjectManager

## Context

Le bot Telegram actuel gère des "projets" (folders + start commands + tmux runner). On ajoute un second domaine indépendant : surveillance d'activité on-chain.

Objectifs utilisateur :
- Suivre l'activité de wallets (les siens et ceux d'autres traders) sur Solana + EVM (Ethereum, Base, BSC).
- Recevoir des push Telegram lors de : swaps DEX, transferts in/out, nouveaux tokens créés/mintés par ces wallets.
- Définir des alertes "X token atteint Y marketcap" (one-shot par défaut, persistant via flag).
- Consulter holdings + P&L à la demande.
- UX cohérente avec l'existant : menu inline `/start` + commandes courtes.

Choix techniques actés pendant le brainstorming :
- **APIs** : Helius (Solana), Alchemy (EVM), Dexscreener (prix/MC sans clé).
- **Transport temps réel** : WebSockets **sortants** (Helius Enhanced WS + Alchemy `alchemy_pendingTransactions` / `alchemy_minedTransactions`). Aucun port à exposer.
- **Polling MC** : périodique (Dexscreener), config `mc_poll_interval`.
- **Persistance** : SQLite séparée (`trading.db`) pour ne pas mélanger les domaines.

## Architecture

Nouveau package isolé `tgbot/trading/` avec une seule frontière d'intégration vers `bot.py` (un `register_trading(app, cfg)` + une entrée de menu).

```
tgbot/
├── bot.py                      # +entrée menu "📈 Trading", +appel register_trading()
├── config.py                   # +section [trading] dans Config
└── trading/
    ├── __init__.py             # expose register_trading(app, cfg)
    ├── db.py                   # SQLite store: wallets, alerts, seen_tx
    ├── prices.py               # Dexscreener client + cache TTL
    ├── solana.py               # Helius WSS (subscribe transactions) + REST (holdings, signatures)
    ├── evm.py                  # Alchemy WSS (subscribe address activity) + REST (balances, tx history)
    ├── monitor.py              # Orchestrateur asyncio: connexions WSS, polling MC, dispatch events
    ├── formatters.py           # Formatage messages Telegram (trade, MC alert, holdings)
    └── handlers.py             # Handlers commandes + callbacks inline
```

### Couches et responsabilités

- **`db.py`** — store SQLite (`data/trading.db`). Tables :
  - `wallets(address TEXT, chain TEXT, label TEXT, added_at TIMESTAMP, PRIMARY KEY(address, chain))`
  - `alerts(id INTEGER PK, token_address TEXT, chain TEXT, mc_target REAL, direction TEXT CHECK(direction IN ('above','below')), persistent INTEGER DEFAULT 0, cooldown_min INTEGER DEFAULT 60, last_triggered_at TIMESTAMP, label TEXT, created_at TIMESTAMP)`
  - `seen_tx(chain TEXT, sig_or_hash TEXT, seen_at TIMESTAMP, PRIMARY KEY(chain, sig_or_hash))` — déduplication aux reconnexions WSS.
  - Réutilise le pattern de `tgbot/db.py:19` (`DB` class + `@contextmanager _conn`).

- **`prices.py`** — wrapper async pour `https://api.dexscreener.com/latest/dex/tokens/{addr}`. Cache TTL en mémoire (~10s) pour éviter de re-frapper l'API quand plusieurs alertes ciblent le même token. Renvoie `{price_usd, mc_usd, symbol, name, pair_url}`.

- **`solana.py`** — connexion WSS à `wss://atlas-mainnet.helius-rpc.com/?api-key=...` avec `transactionSubscribe` filtrée sur les `accountInclude` (= wallets watchés Solana). Reçoit les transactions parsées (type swap / transfer / mint), normalise en `WalletEvent` (dataclass commune). REST helpers : `getAssetsByOwner` (holdings), `getSignaturesForAddress` (backfill au boot).

- **`evm.py`** — WSS à `wss://{chain}-mainnet.g.alchemy.com/v2/{key}` avec `alchemy_minedTransactions` filtré par `addresses`. Décodage minimal : in/out transfers, ERC20 swaps (détecter signature Uniswap V2/V3 + Universal Router). Holdings via `alchemy_getTokenBalances` + `getAssetTransfers`.

- **`monitor.py`** — orchestrateur central, lancé via `app.post_init` :
  - Démarre tâches asyncio : `solana_ws_loop()`, `evm_ws_loops()` (une par chain), `mc_alert_loop()`.
  - Reconnect exponential backoff sur disconnect WSS.
  - Reçoit les events normalisés, enrichit (prix au moment du trade via prices.py), filtre via `seen_tx` (dédup), formate, envoie via `app.bot.send_message(chat_id, ...)`.
  - Pour MC alerts : poll toutes les `mc_poll_interval` sec, compare contre `mc_target` + direction, déclenche, met à jour `last_triggered_at`, désarme si non-persistent.
  - Une seule queue `chat_id` cible : on push à tous les `allowed_user_ids` (déjà dans `Config`).

- **`handlers.py`** — register tous les handlers Telegram du domaine trading, exporte un seul `register_trading(app, cfg, monitor)`. Tous les handlers décorés `@restricted` (réutilise `tgbot/auth.py`).

- **`formatters.py`** — fonctions pures qui prennent un event et renvoient `(text_markdown, optional_inline_kb)`. Évite que `monitor.py` connaisse le format Telegram.

### Flux events (exemple : swap Solana)

```
Helius WSS push → solana.py parse → WalletEvent(kind=swap, wallet, token, amount, ...)
  → monitor.dispatch():
       seen_tx insert (skip si dup)
       prices.get(token) → enrichit avec MC actuel
       formatters.swap_message(event, mc) → markdown
       app.bot.send_message(chat_id, text, parse_mode=MARKDOWN)
```

## Configuration

Étendre `tgbot/config.py:7` avec une section optionnelle :

```toml
[trading]
enabled = true
helius_api_key = "..."
alchemy_api_key = "..."
mc_poll_interval = 30          # secondes
evm_chains = ["eth", "base", "bsc"]   # sous-set à surveiller
```

Si `[trading].enabled = false` ou section absente → `register_trading()` est no-op. Le bot existant reste 100% fonctionnel sans clés trading. Pas de breaking change.

## UI Telegram

### Menu inline (extension de `_main_menu_markup` dans `tgbot/bot.py:95`)

```
[ 📂 Projets ]
[ 📈 Trading ]    ← nouveau
[ ❓ Aide   ]
```

Sous-menu Trading :
```
[ 👛 Wallets surveillés ]
[ 🔔 Alertes MC        ]
[ 💰 Holdings          ]
[ ⬅️ Retour            ]
```

Callback data namespacée : `trd:wallets`, `trd:alerts`, `trd:hold`, `trd:wadd`, `trd:wdel:<id>`, `trd:adel:<id>`. Suit le pattern existant (`menu:`, `proj:`, `act:`, `cfm:`) de `bot.py:204`.

### Commandes courtes (power-user)

| Commande | Effet |
|---|---|
| `/watch <addr> <chain> [label]` | Ajoute un wallet (chain ∈ sol/eth/base/bsc) |
| `/unwatch <addr>` | Retire un wallet |
| `/wallets` | Liste wallets surveillés |
| `/alert <token_addr> <chain> <mc> [--above|--below] [--persistent]` | Crée alerte MC |
| `/alerts` | Liste alertes (avec état armé/déclenché) |
| `/unalert <id>` | Supprime alerte |
| `/holdings <wallet_addr> <chain>` | Snapshot positions + valeur USD |

## Points d'intégration dans le code existant

- **`tgbot/__main__.py:27`** : `allowed_updates` reste inchangé (les WSS sortants n'utilisent pas d'updates Telegram).
- **`tgbot/bot.py:136`** (`build_app`) : après la création de `app`, appeler `register_trading(app, cfg)` si `cfg.trading and cfg.trading.enabled`.
- **`tgbot/bot.py:95`** (`_main_menu_markup`) : ajouter la ligne `📈 Trading` (conditionnellement si trading enabled).
- **`tgbot/bot.py:204`** (`on_callback`) : ajouter un branche `if ns == "trd"` qui délègue à `handlers.on_trading_callback`. Alternative plus propre : enregistrer un `CallbackQueryHandler` dédié avec un `pattern=r"^trd:"` dans `register_trading` — pas besoin de toucher `on_callback`. **On part sur cette alternative** (boundary plus propre).
- **`tgbot/bot.py:614`** (`set_my_commands`) : ajouter les commandes trading.
- **`tgbot/config.py:8`** (`@dataclass Config`) : ajouter `trading: TradingConfig | None`.

## Dépendances à ajouter dans `requirements.txt`

```
aiohttp>=3.9         # client HTTP async (Dexscreener + REST Helius/Alchemy)
websockets>=12.0     # WSS sortants
```

`python-telegram-bot` embarque déjà `httpx` mais on garde `aiohttp` pour ne pas conflicter avec sa configuration interne.

## Sécurité

- **Aucune signature de transaction nulle part.** Read-only strict. Pas de clé privée stockée. Pas de RPC `sendTransaction`.
- Les clés API Helius/Alchemy vivent dans `config.toml` (déjà gitignored via `.gitignore` créé récemment).
- Adresses validées avant insert : regex `^[1-9A-HJ-NP-Za-km-z]{32,44}$` pour Solana, `^0x[a-fA-F0-9]{40}$` pour EVM.
- Le whitelist `allowed_user_ids` continue de protéger toutes les commandes.

## Plan d'exécution (ordre d'implémentation)

1. **Squelette + config** : créer `tgbot/trading/` vide, étendre `Config` (rétrocompat), no-op `register_trading()` si désactivé. Vérifier que le bot démarre toujours.
2. **`db.py`** : schéma + tests manuels (insert/list/delete wallets + alerts).
3. **`prices.py`** : Dexscreener client + cache, test sur quelques tokens.
4. **`handlers.py` (commandes simples)** : `/watch /unwatch /wallets /alert /alerts /unalert` en commandes texte d'abord, sans monitor. Test : commandes répondent, DB persiste.
5. **`monitor.py` + `solana.py`** : boucle WSS Helius, dispatch vers `chat_id`. Test : ajouter wallet test, observer messages sur trade réel.
6. **`evm.py`** : même chose pour Alchemy (chain par chain).
7. **`mc_alert_loop`** : polling Dexscreener, déclenche alertes, gère persistent/cooldown.
8. **`holdings`** : commande `/holdings` (snapshot REST).
9. **UI inline** : sous-menu Trading + callbacks, en plus des commandes texte.
10. **README** : section "Trading module" + setup des clés API.

Chaque étape est un commit. Les étapes 1-4 sont indépendantes de toute API externe et peuvent être validées hors-ligne.

## Fichiers critiques modifiés

- `tgbot/config.py` (étendu)
- `tgbot/bot.py` (3 hooks : menu, register_trading, set_my_commands)
- `requirements.txt` (+aiohttp, +websockets)
- `config.example.toml` (section `[trading]` documentée)
- `README.md` (section trading)

## Fichiers critiques créés

- `tgbot/trading/__init__.py`
- `tgbot/trading/db.py`
- `tgbot/trading/prices.py`
- `tgbot/trading/solana.py`
- `tgbot/trading/evm.py`
- `tgbot/trading/monitor.py`
- `tgbot/trading/formatters.py`
- `tgbot/trading/handlers.py`

## Vérification end-to-end

1. **Bot démarre avec section `[trading]` absente** → comportement identique à aujourd'hui, aucun handler trading enregistré.
2. **Bot démarre avec `enabled = true`** → log `Trading monitor started (sol + eth + base + bsc)`, WSS connectés.
3. `/watch <my_solana_wallet> sol perso` → confirmé en DB, listé via `/wallets`.
4. Effectuer un petit swap sur Jupiter avec ce wallet → message Telegram reçu dans les ~5s, contenant token in/out, amount, MC.
5. `/alert <token_solana> sol 1000000 --above` → alerte créée.
6. Quand le MC du token traverse 1M USD → notification reçue, alerte marquée `triggered` (ou re-armée si `--persistent`).
7. `/holdings <wallet> sol` → liste des tokens + valeur USD + total.
8. Couper la connexion réseau du bot 30s puis la rétablir → reconnect WSS automatique visible dans les logs, pas de doublons sur les transactions reçues pendant la coupure (grâce à `seen_tx`).
9. **Régression projets** : `/projects`, `/run`, `/logs` continuent de fonctionner exactement comme avant.

## Hors-scope explicite (YAGNI)

- Pas d'exécution de trades (pas de signing, pas de wallets actifs).
- Pas de P&L historique multi-période (juste snapshot actuel).
- Pas de copy-trading automatique.
- Pas de support multi-utilisateur séparé : les alertes sont globales aux `allowed_user_ids`.
- Pas de dashboard web. Tout via Telegram.
- Pas de backtesting.
