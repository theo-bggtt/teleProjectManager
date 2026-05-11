# Design — Exposition complète des fonctionnalités via le menu inline

**Date** : 2026-05-11
**Branche cible** : `feat/trading-module`
**Auteur** : brainstorm session (Theo + Claude)

## Contexte

Le bot Telegram `teleProjectManager` expose actuellement la plupart de ses fonctionnalités via des commandes texte (`/config`, `/ls`, `/get`, `/shell`, `/watch`, `/alert`, `/holdings`). Le menu inline ne couvre que Projets, Actions, Trading (home avec Wallets/Alertes), et Aide.

Objectif : exposer dans le menu inline les 6 fonctionnalités encore "cachées" en commandes texte, en réutilisant l'infrastructure single-message wizard (`_wizard_step` / `_wizard_finish`) finalisée précédemment sur cette branche.

## Portée

Six fonctionnalités à ajouter au menu inline :

**Côté Trading** :
1. ➕ Ajouter wallet (équivalent `/watch`)
2. ➕ Créer alerte MC (équivalent `/alert`)
3. 💰 Holdings (équivalent `/holdings`)

**Côté Projet** :
4. ⚙️ Config (équivalent `/config <name>`)
5. 📁 Fichiers (équivalent `/ls` + `/get`, navigation sous-dossiers incluse)
6. 💻 Shell (équivalent `/shell`)

**Hors portée** : `/put` (upload), `/remove`, autres commandes existantes. Tests automatisés.

## Architecture et placement

### Menu projet (`_project_actions_markup`, `tgbot/bot.py:137`)

Devient 4 lignes :
```
[ ⏹/▶️ Stop/Run ] [ 🔄 Restart  ]
[ 📄 Logs       ] [ ℹ️ Status   ]
[ ⚙️ Config     ] [ 📁 Fichiers ]
[ 💻 Shell      ] [ ⬅️ Retour   ]
```

### Trading home (`_home_markup`, `tgbot/trading/handlers.py:325`)

Ajout d'un bouton Holdings :
```
[ 👛 Wallets surveillés ]
[ 🔔 Alertes MC         ]
[ 💰 Holdings           ]   ← nouveau
[ ⬅️ Retour             ]
```

### Liste wallets (`_wallets_markup`)

`➕ Ajouter wallet` en première ligne (suit le pattern `➕ Ajouter un projet` du menu projets).

### Liste alertes (`_alerts_markup`)

`➕ Créer alerte` en première ligne.

### Namespaces callback_data (nouveaux)

| Préfixe | Usage |
|---|---|
| `proj:cfg:<name>` | Lance config wizard sur ce projet |
| `proj:files:<name>` | Ouvre browser fichiers (root) |
| `proj:files:<name>:<encoded-path>` | Browser fichiers sur sous-dossier |
| `proj:fget:<name>:<encoded-path>` | Télécharge un fichier |
| `proj:shell:<name>` | Lance shell wizard |
| `trd:wadd` | Lance "ajouter wallet" wizard |
| `trd:aadd` | Lance "créer alerte" wizard |
| `trd:hold` | Ouvre picker holdings |
| `trd:hget:<chain>:<addr>` | Affiche holdings du wallet choisi |

L'encodage du chemin (`<encoded-path>`) utilise un slug court (base64 URL-safe sans padding) pour éviter les conflits avec `:` dans callback_data. Le mapping `slug → path` est stocké dans `ctx.chat_data["files_path_map"]` (purgé à chaque retour menu).

## Wizards

Tous utilisent `_wizard_step` / `_wizard_finish` et le fallback global `wiz:cancel`. Chacun est un `ConversationHandler` distinct ajouté à `app.add_handler()`.

### Wizard 1 — Ajouter wallet

**Entry** : `CallbackQueryHandler(pattern=r"^trd:wadd$")`

| État | Prompt | Input | Validation |
|---|---|---|---|
| `TRD_WADD_CHAIN` | "Choisis la chaîne du wallet :" | Boutons SOL / ETH / BASE / BSC | — |
| `TRD_WADD_ADDR` | "Envoie l'adresse du wallet `<CHAIN>` :" | Texte | `validate_address(addr, chain)` ; sinon reste sur cet état |
| `TRD_WADD_LABEL` | "Label optionnel (ou tape `skip`) :" | Texte ou `skip` | — |

**Finish** :
1. `norm = _normalize_address(addr, chain)`
2. `db.add_wallet(norm, chain, label)` ; si déjà présent → message "déjà surveillé"
3. `monitor.notify_wallets_changed(chain)`
4. `_wizard_finish` puis renvoi vers `_render_wallets` via `_render_wallets(query)`.

### Wizard 2 — Créer alerte MC

**Entry** : `CallbackQueryHandler(pattern=r"^trd:aadd$")`

| État | Prompt | Input | Validation |
|---|---|---|---|
| `TRD_AADD_CHAIN` | "Chaîne du token :" | Boutons SOL/ETH/BASE/BSC | — |
| `TRD_AADD_TOKEN` | "Adresse du token :" | Texte | `validate_address` |
| `TRD_AADD_MC` | "Marketcap cible (ex `1m`, `500k`) :" | Texte | `_parse_mc` retourne > 0 |
| `TRD_AADD_DIR` | "Déclenchement ?" | Boutons `↑ Above` / `↓ Below` | — |
| `TRD_AADD_PERSIST` | "Type d'alerte ?" | Boutons `One-shot` / `Persistent` | — |
| `TRD_AADD_LABEL` | "Label (ou `skip`) :" | Texte | — |

**Finish** : `db.add_alert(token_address=norm, chain, mc_target, direction, persistent, label)` puis `_render_alerts`.

### Wizard 3 — Shell

**Entry** : `CallbackQueryHandler(pattern=r"^proj:shell:")`

| État | Prompt | Input |
|---|---|---|
| `PROJ_SHELL_CMD` | "Commande shell pour `<name>` :" | Texte libre |

**Finish** :
1. `rc, out = await shell.run(cmd, proj["path"])`
2. `_send_text_or_file(update, out or "(no output)", f"{name}-shell.txt", header=f"exit {rc}")`
3. `_wizard_finish` puis retour `_send_main_menu`.

Le `proj_name` est extrait du callback_data initial et stocké dans `ctx.user_data["shell_project"]`.

## Renderers

### Renderer 1 — Holdings picker

**Route `trd:hold`** :
- Lit `db.list_wallets()`.
- Si vide : message "Aucun wallet surveillé. Ajoute-en un d'abord." + bouton retour `trd:home`.
- Sinon : `InlineKeyboardMarkup` listant chaque wallet `[ <CHAIN> <addr-short> — <label> ]` avec callback `trd:hget:<chain>:<addr>` ; ligne finale `⬅️ Retour` vers `trd:home`.

**Route `trd:hget:<chain>:<addr>`** :
- `edit_message_text("⏳ Fetching holdings…")` puis :
  - `chain == "sol"` → `await fetch_solana_holdings(monitor.helius_api_key, addr)`
  - sinon → `await fetch_evm_holdings(...)` (mêmes args que `cmd_holdings`)
- Formate via les helpers de `formatters.py` existants (réutiliser ce que `cmd_holdings` utilise).
- `edit_message_text(formatted, parse_mode=MARKDOWN, reply_markup=⬅️ Retour trd:hold)`.

### Renderer 2 — Files browser (avec navigation sous-dossiers)

**Route `proj:files:<name>`** : démarre à la racine du projet.
**Route `proj:files:<name>:<slug>`** : navigation vers un sous-chemin (slug résolu via `chat_data["files_path_map"]`).

Affichage :
- `entries = files_mgr.list_dir(proj["path"], current_rel)` → tri dossiers d'abord.
- Construction du markup paginé (10 entrées/page, page stockée dans `chat_data[f"files_page:{name}"]`) :
  - Fichier : `[ 📄 nom.ext ]` → `proj:fget:<name>:<slug-fichier>`
  - Dossier : `[ 📁 sub/  ]` → `proj:files:<name>:<slug-sous-dossier>`
- Pied :
  - `[ ⬅️ ] [ p/total ] [ ➡️ ]` si pages > 1 (callback `proj:files:<name>:<slug>?page=...` ; on stocke page dans chat_data, callback reste simple)
  - `[ ⬆️ Parent ]` si pas à la racine → `proj:files:<name>:<slug-parent>`
  - `[ ⬅️ Retour ]` → `proj:` (menu du projet)

**Route `proj:fget:<name>:<slug>`** :
- Résout `slug → rel` via `chat_data["files_path_map"]`. Si absent → toast "fichier expiré, rouvre le browser".
- `target = files_mgr.get_file(proj["path"], rel)` → `await query.message.reply_document(open(target, "rb"), filename=target.name, caption=f"/put {name} {rel}")`.
- Le menu fichiers reste en place (pas de `edit_message_text`).

Erreurs (`PathEscapeError`, `FileNotFoundError`, `NotADirectoryError`, `IsADirectoryError`) → `query.answer(text=str(e), show_alert=True)`.

### Branchement Config

`proj:cfg:<name>` est ajouté comme **2ᵉ entry_point** de `config_conv` (à côté de `CommandHandler("config", cmd_config)`) :

```python
config_conv = ConversationHandler(
    entry_points=[
        CommandHandler("config", cmd_config),
        CallbackQueryHandler(proj_cfg_entry, pattern=r"^proj:cfg:"),
    ],
    states={...inchangé...},
    fallbacks=[...inchangé...],
)
```

`proj_cfg_entry(update, ctx)` :
1. Lit `name = update.callback_query.data.split(":", 2)[2]`.
2. `await update.callback_query.answer()`.
3. Set `ctx.user_data["cfg_project"] = name`.
4. `current = proj.get("start_command") or "(none)"`.
5. `await _wizard_step(update, ctx, f"🛠️ Configurer *{name}*\n\nCommande actuelle : `{current}`\n\n💬 Envoie la commande de démarrage.")`.
6. `return CFG_START_CMD`.

Le reste du flow (`cfg_start_cmd`, `cfg_entry_file`) est inchangé. À la sortie, `_wizard_finish` puis menu principal.

## Routing dans `on_callback`

Le routeur `on_callback` (qui dispatche `menu:*`, `proj:*`, `act:*`) doit gérer les nouveaux patterns NON consommés par les ConversationHandlers :
- `proj:files:*` et `proj:fget:*` → renderers (pas de wizard).
- `trd:hold` et `trd:hget:*` → renderers.

Les wizards (`trd:wadd`, `trd:aadd`, `proj:shell:*`, `proj:cfg:*`) sont interceptés en amont par leur `ConversationHandler.entry_points`.

⚠️ Important : l'ordre d'enregistrement matter. Les `ConversationHandler` doivent être `add_handler`ed AVANT le `CallbackQueryHandler(on_callback)` global, sinon `on_callback` mange l'entry point.

## Fichiers touchés

- `tgbot/bot.py` :
  - `_project_actions_markup` : 3 nouveaux boutons.
  - Nouveau routeur dans `on_callback` pour `proj:files:`, `proj:fget:`.
  - Nouveau `proj_cfg_entry` ajouté comme entry à `config_conv`.
  - Nouveau `proj_files_render` + `proj_fget` (helpers renderer).
  - Nouveau `proj_shell_conv` ConversationHandler.
  - Maj `setup_app()` pour wire le tout.
- `tgbot/trading/handlers.py` :
  - `_home_markup` : bouton Holdings.
  - `_wallets_markup` : `➕ Ajouter wallet` en tête.
  - `_alerts_markup` : `➕ Créer alerte` en tête.
  - Nouveaux `_render_holdings_picker`, `_render_holdings_for(chain, addr)`.
  - Routage `trd:hold` et `trd:hget:*` dans le router callback de trading.
  - Nouveaux `trd_wadd_conv` et `trd_aadd_conv` ConversationHandlers, enregistrés AVANT le router callback global.

## Erreurs et états

- **Wizard interrompu** (`wiz:cancel`) : `_wizard_finish` purge l'état et renvoie au menu d'origine.
- **Adresse invalide** : message d'erreur, reste sur l'état courant.
- **Doublon wallet** : message "déjà surveillé", finish.
- **`files_path_map` expiré** (changement de session) : toast d'erreur via `query.answer(show_alert=True)`.
- **Output shell vide** : remplacé par `(no output)`.
- **Output trop long** : `_send_text_or_file` gère déjà l'envoi en fichier `.txt`.

## Décisions YAGNI

- Pas de `/put` (upload) inline — usage minoritaire, `/put` en caption suffit.
- Pas de suppression de wallet/alerte depuis le picker holdings — déjà disponible dans les listes Wallets/Alertes.
- Pas de tests unitaires — le code Telegram bot n'a pas de suite de tests à étendre, validation manuelle.
- Le bouton `📁 Fichiers` est désactivé visuellement si `proj.path` n'existe pas (toast erreur au click).

## Validation manuelle (après build)

Scénarios à exécuter une fois implémenté :

1. Menu projet → ⚙️ Config → wizard se lance avec le bon projet préchargé → finish → retour menu.
2. Menu projet → 📁 Fichiers → liste root → click sous-dossier → liste sous-dossier → ⬆️ Parent → root → click fichier → reçoit document.
3. Menu projet → 💻 Shell → tape `ls` → reçoit output.
4. Trading → 💰 Holdings → liste wallets → click un wallet → reçoit holdings.
5. Trading → 👛 Wallets surveillés → ➕ Ajouter wallet → wizard complet → wallet apparaît.
6. Trading → 🔔 Alertes MC → ➕ Créer alerte → wizard complet → alerte apparaît.
7. `wiz:cancel` annule chacun des 3 nouveaux wizards proprement.
