# Menu Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exposer 6 fonctionnalités (Config, Fichiers, Shell, Add Wallet, Add Alert, Holdings) dans le menu inline du bot Telegram, en réutilisant l'infrastructure single-message wizard existante.

**Architecture:** Boutons ajoutés dans les sous-menus existants (`_project_actions_markup`, `_home_markup`, `_wallets_markup`, `_alerts_markup`). Trois nouveaux `ConversationHandler` (shell, add wallet, add alert) montés AVANT le `CallbackQueryHandler(on_callback)` global. Deux renderers (files browser, holdings picker) ajoutés au routeur `on_callback` et au routeur trading. Un nouvel entry-point ajouté au `config_conv` existant.

**Tech Stack:** Python 3, python-telegram-bot v20, infra wizard existante (`_wizard_step`, `_wizard_finish`).

**Pas de TDD :** la suite de tests Telegram n'existe pas dans ce repo (décision validée pendant le brainstorming). Chaque tâche est validée par un smoke test manuel décrit en fin de tâche.

**Spec source :** `docs/superpowers/specs/2026-05-11-menu-features-design.md`.

---

## Task 1: Étendre `_project_actions_markup` avec Config/Fichiers/Shell

**Files:**
- Modify: `tgbot/bot.py:137` (`_project_actions_markup`)

- [ ] **Step 1: Remplacer le markup du menu projet**

Remplacer le corps de la fonction `_project_actions_markup` (autour de `tgbot/bot.py:137`) par :

```python
def _project_actions_markup(name: str, running: bool) -> InlineKeyboardMarkup:
    run_btn = (
        InlineKeyboardButton("⏹ Stop", callback_data=f"act:stop:{name}")
        if running
        else InlineKeyboardButton("▶️ Run", callback_data=f"act:run:{name}")
    )
    return InlineKeyboardMarkup([
        [run_btn, InlineKeyboardButton("🔄 Restart", callback_data=f"act:restart:{name}")],
        [
            InlineKeyboardButton("📄 Logs", callback_data=f"act:logs:{name}"),
            InlineKeyboardButton("ℹ️ Status", callback_data=f"act:status:{name}"),
        ],
        [
            InlineKeyboardButton("⚙️ Config", callback_data=f"proj:cfg:{name}"),
            InlineKeyboardButton("📁 Fichiers", callback_data=f"proj:files:{name}"),
        ],
        [
            InlineKeyboardButton("💻 Shell", callback_data=f"proj:shell:{name}"),
            InlineKeyboardButton("⬅️ Retour", callback_data="menu:projects"),
        ],
    ])
```

- [ ] **Step 2: Smoke test manuel**

Lancer le bot. `/start` → 📂 Projets → click sur un projet → vérifier que les 4 lignes apparaissent. Les 3 nouveaux boutons ne font rien encore (callback non géré → no-op).

- [ ] **Step 3: Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(bot): add Config/Fichiers/Shell buttons to project actions menu"
```

---

## Task 2: Étendre les markups Trading avec Holdings + boutons d'ajout

**Files:**
- Modify: `tgbot/trading/handlers.py:325` (`_home_markup`)
- Modify: `tgbot/trading/handlers.py:333` (`_wallets_markup`)
- Modify: `tgbot/trading/handlers.py:350` (`_alerts_markup`)

- [ ] **Step 1: Ajouter le bouton Holdings dans `_home_markup`**

Remplacer le corps de `_home_markup` par :

```python
def _home_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👛 Wallets surveillés", callback_data="trd:wallets")],
        [InlineKeyboardButton("🔔 Alertes MC", callback_data="trd:alerts")],
        [InlineKeyboardButton("💰 Holdings", callback_data="trd:hold")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")],
    ])
```

- [ ] **Step 2: Ajouter le bouton "Ajouter wallet" en tête de `_wallets_markup`**

Remplacer la première ligne de la fonction (la création de `rows`) par :

```python
def _wallets_markup(wallets: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Ajouter wallet", callback_data="trd:wadd")],
    ]
    for w in wallets:
        tag = f" — {w['label']}" if w["label"] else ""
        short = f"{w['address'][:4]}…{w['address'][-4:]}"
        rows.append([
            InlineKeyboardButton(
                f"{w['chain'].upper()} {short}{tag}",
                callback_data=f"trd:whold:{w['chain']}:{w['address']}",
            ),
            InlineKeyboardButton(
                "🗑", callback_data=f"trd:wdel:{w['chain']}:{w['address']}"
            ),
        ])
    rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="trd:home")])
    return InlineKeyboardMarkup(rows)
```

- [ ] **Step 3: Ajouter le bouton "Créer alerte" en tête de `_alerts_markup`**

Remplacer la première ligne par :

```python
def _alerts_markup(alerts: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Créer alerte", callback_data="trd:aadd")],
    ]
    for a in alerts:
        arrow = "↑" if a["direction"] == "above" else "↓"
        state = "🟢" if a["armed"] else "⚪"
        rows.append([
            InlineKeyboardButton(
                f"{state} #{a['id']} {a['chain'].upper()} {arrow}${_fmt_mc(a['mc_target'])}",
                callback_data=f"trd:anoop:{a['id']}",
            ),
            InlineKeyboardButton("🗑", callback_data=f"trd:adel:{a['id']}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="trd:home")])
    return InlineKeyboardMarkup(rows)
```

⚠️ Les caractères flèche/icône (`↑`, `↓`, `🟢`, `⚪`, `…`) dans les bouts originaux peuvent être encodés bizarrement (voir mojibake `�%�` dans le fichier source). Garder les caractères exacts présents dans le fichier original — ne pas les changer.

- [ ] **Step 4: Smoke test manuel**

Lancer le bot (avec trading enabled). `/start` → 📈 Trading → vérifier les 4 boutons. Click 👛 Wallets surveillés → vérifier le bouton ➕ Ajouter wallet en tête. Idem pour 🔔 Alertes MC. Les nouveaux callbacks (`trd:hold`, `trd:wadd`, `trd:aadd`) ne font rien encore.

- [ ] **Step 5: Commit**

```bash
git add tgbot/trading/handlers.py
git commit -m "feat(trading): add Holdings menu button + Ajouter wallet/alerte shortcuts"
```

---

## Task 3: Brancher `⚙️ Config` sur le `config_conv` existant

**Files:**
- Modify: `tgbot/bot.py` (la fonction `setup_app()` qui définit `config_conv`, vers `bot.py:1210`)

- [ ] **Step 1: Créer le handler `proj_cfg_entry`**

Ajouter cette fonction dans la même portée que `cfg_select` (juste après `cfg_select`, vers `bot.py:967`) :

```python
async def proj_cfg_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        return ConversationHandler.END
    name = parts[2]
    proj = db.get_project(name)
    if not proj:
        await _wizard_step(update, ctx, f"⚠️ Pas de projet `{name}`.")
        return ConversationHandler.END
    ctx.user_data["cfg_project"] = name
    current = proj.get("start_command") or "(none)"
    await _wizard_step(
        update, ctx,
        f"🛠️ Configurer *{name}*\n\nCommande actuelle : `{current}`\n\n"
        f"💬 Envoie la commande de démarrage.",
    )
    return CFG_START_CMD
```

- [ ] **Step 2: Ajouter `proj_cfg_entry` comme entry_point de `config_conv`**

Dans `setup_app()` à `tgbot/bot.py:1210`, modifier la définition de `config_conv` :

```python
config_conv = ConversationHandler(
    entry_points=[
        CommandHandler("config", cmd_config),
        CallbackQueryHandler(proj_cfg_entry, pattern=r"^proj:cfg:"),
    ],
    states={
        CFG_SELECT: [
            CallbackQueryHandler(cfg_select, pattern=r"^cfgsel:"),
            CommandHandler("cancel", cfg_cancel),
        ],
        CFG_START_CMD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_start_cmd),
            CommandHandler("cancel", cfg_cancel),
        ],
        CFG_ENTRY_FILE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_entry_file),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cfg_cancel),
        CallbackQueryHandler(_wizard_escape, pattern=r"^wiz:cancel$"),
    ],
)
```

⚠️ Vérifier l'état exact actuel du bloc `config_conv` avant remplacement — copier les `states` et `fallbacks` tels qu'ils existent et ajouter UNIQUEMENT la nouvelle ligne dans `entry_points`. Si la structure exacte diffère, adapter minimalement.

- [ ] **Step 3: Smoke test manuel**

`/start` → 📂 Projets → click projet → ⚙️ Config → vérifier que le wizard demande "Envoie la commande de démarrage." Compléter le wizard, vérifier que la commande est bien enregistrée (relancer `⚙️ Config` et vérifier l'affichage "Commande actuelle").

- [ ] **Step 4: Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(bot): wire ⚙️ Config button to config_conv via callback entry-point"
```

---

## Task 4: Wizard `💻 Shell` (nouveau `ConversationHandler`)

**Files:**
- Modify: `tgbot/bot.py` (ajout d'un état, handler, et registration)

- [ ] **Step 1: Ajouter la constante d'état**

Trouver le bloc de constantes d'état (où sont définis `CFG_SELECT`, `CFG_START_CMD`, `CFG_ENTRY_FILE`, etc., grep `^CFG_SELECT` ou `ConversationHandler.END` voisin). Ajouter à la suite :

```python
PROJ_SHELL_CMD = 700
```

(700 — choisir une valeur unique non utilisée ; vérifier les constantes existantes pour éviter collision.)

- [ ] **Step 2: Ajouter les handlers du wizard shell**

Dans la même portée que `cfg_start_cmd` (vers `bot.py:967`), ajouter :

```python
async def proj_shell_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        return ConversationHandler.END
    name = parts[2]
    proj = db.get_project(name)
    if not proj:
        await _wizard_step(update, ctx, f"⚠️ Pas de projet `{name}`.")
        return ConversationHandler.END
    ctx.user_data["shell_project"] = name
    await _wizard_step(
        update, ctx,
        f"💻 Shell pour *{name}*\n\nEnvoie la commande à exécuter :",
    )
    return PROJ_SHELL_CMD


async def proj_shell_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    name = ctx.user_data.get("shell_project")
    if not name:
        await _wizard_step(update, ctx, "⚠️ État perdu, recommence depuis le menu.")
        return ConversationHandler.END
    proj = db.get_project(name)
    if not proj:
        await _wizard_step(update, ctx, f"⚠️ Projet `{name}` disparu.")
        return ConversationHandler.END
    cmd = update.message.text.strip()
    await _wizard_step(update, ctx, f"⏳ `{cmd}` …")
    rc, out = await shell.run(cmd, proj["path"])
    await _send_text_or_file(
        update, out or "(no output)", f"{name}-shell.txt",
        header=f"exit {rc}",
    )
    await _wizard_finish(update, ctx)
    return ConversationHandler.END


async def proj_shell_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("shell_project", None)
    await _wizard_finish(update, ctx)
    return ConversationHandler.END
```

- [ ] **Step 3: Enregistrer le `ConversationHandler`**

Dans `setup_app()`, AVANT la ligne `app.add_handler(CallbackQueryHandler(on_callback))` (`tgbot/bot.py:1278`), ajouter :

```python
proj_shell_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(proj_shell_start, pattern=r"^proj:shell:")],
    states={
        PROJ_SHELL_CMD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, proj_shell_run),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", proj_shell_cancel),
        CallbackQueryHandler(_wizard_escape, pattern=r"^wiz:cancel$"),
    ],
)
app.add_handler(proj_shell_conv)
```

- [ ] **Step 4: Mettre à jour `_wizard_finish` pour purger `shell_project`**

Dans `_wizard_finish` (vers `bot.py:411`), ajouter `"shell_project"` à la tuple de clés purgées :

Remplacer le tuple `for k in (...)` par :
```python
for k in (
    "wizard_msg_id", "wizard_chat_id",
    "add_name",
    "addact_name", "addact_command", "addact_cwd", "addact_mode",
    "cfg_project",
    "shell_project",
):
    ctx.user_data.pop(k, None)
```

- [ ] **Step 5: Smoke test manuel**

`/start` → projet → 💻 Shell → tape `echo hello` → vérifier la réception du résultat avec `exit 0` et `hello`. Tester aussi l'annulation via `❌ Annuler` du wizard.

- [ ] **Step 6: Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(bot): add 💻 Shell wizard ConversationHandler"
```

---

## Task 5: Helpers slug + renderer files browser (root)

**Files:**
- Modify: `tgbot/bot.py` (ajout de helpers et renderer)

- [ ] **Step 1: Ajouter les helpers de slug**

Dans la même portée que les autres helpers privés (juste avant `on_callback`, vers `bot.py:455`), ajouter :

```python
import hashlib
import base64


def _files_slug(rel_path: str) -> str:
    """8-char base32 hash of a relative path, callback_data-safe."""
    if rel_path in ("", "."):
        return "_"
    digest = hashlib.sha1(rel_path.encode("utf-8")).digest()[:5]
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def _files_resolve(ctx: ContextTypes.DEFAULT_TYPE, slug: str) -> str | None:
    """Return the rel path mapped to slug, or None if expired/unknown."""
    if slug == "_":
        return "."
    mapping = ctx.chat_data.get("files_path_map") or {}
    return mapping.get(slug)


def _files_remember(ctx: ContextTypes.DEFAULT_TYPE, rel_path: str) -> str:
    """Compute slug for rel_path and store mapping in chat_data."""
    slug = _files_slug(rel_path)
    mapping = ctx.chat_data.setdefault("files_path_map", {})
    mapping[slug] = rel_path
    return slug
```

⚠️ Si `import hashlib` ou `import base64` sont déjà en tête de fichier, ne pas les redupliquer. Vérifier les imports existants.

- [ ] **Step 2: Ajouter le renderer files browser**

Juste après les helpers ci-dessus, ajouter :

```python
PAGE_SIZE = 10


async def _render_files(
    query, ctx: ContextTypes.DEFAULT_TYPE, name: str, rel: str,
) -> None:
    proj = db.get_project(name)
    if not proj:
        await query.answer(text=f"Projet {name} introuvable", show_alert=True)
        return
    try:
        entries = files_mgr.list_dir(proj["path"], rel)
    except (PathEscapeError, FileNotFoundError, NotADirectoryError) as e:
        await query.answer(text=str(e), show_alert=True)
        return

    # Sort: dirs first, then files, both alphabetical
    dirs, files = [], []
    for e in entries:
        # files_mgr.list_dir returns strings like "name/" for dirs (verify)
        if e.endswith("/"):
            dirs.append(e.rstrip("/"))
        else:
            files.append(e)
    dirs.sort()
    files.sort()
    items = [(d, True) for d in dirs] + [(f, False) for f in files]

    page_key = f"files_page:{name}:{_files_slug(rel)}"
    page = ctx.chat_data.get(page_key, 0)
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    ctx.chat_data[page_key] = page
    slice_ = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    for entry_name, is_dir in slice_:
        child_rel = f"{rel}/{entry_name}" if rel not in ("", ".") else entry_name
        child_slug = _files_remember(ctx, child_rel)
        if is_dir:
            rows.append([InlineKeyboardButton(
                f"📁 {entry_name}/",
                callback_data=f"proj:files:{name}:{child_slug}",
            )])
        else:
            rows.append([InlineKeyboardButton(
                f"📄 {entry_name}",
                callback_data=f"proj:fget:{name}:{child_slug}",
            )])

    cur_slug = _files_slug(rel)
    if total_pages > 1:
        rows.append([
            InlineKeyboardButton("◀️", callback_data=f"proj:fpg:{name}:{cur_slug}:prev"),
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="wiz:noop"),
            InlineKeyboardButton("▶️", callback_data=f"proj:fpg:{name}:{cur_slug}:next"),
        ])

    if rel not in ("", "."):
        parent = "/".join(rel.split("/")[:-1]) or "."
        parent_slug = _files_remember(ctx, parent)
        rows.append([InlineKeyboardButton(
            "⬆️ Parent", callback_data=f"proj:files:{name}:{parent_slug}",
        )])

    rows.append([InlineKeyboardButton("⬅️ Retour", callback_data=f"proj:{name}")])

    text = f"*📁 Fichiers — {name}*\nChemin : `{rel}`"
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
```

⚠️ Vérifier le format de retour de `files_mgr.list_dir`. Si les dossiers ne sont PAS suffixés `/`, adapter la détection. On peut aussi tester `os.path.isdir(os.path.join(proj["path"], rel, e))` à la place de la détection par suffixe — préférer cette approche si list_dir retourne des noms bruts.

- [ ] **Step 3: Smoke test manuel partiel**

Pas encore de routing — sera testé en Task 7.

- [ ] **Step 4: Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(bot): add slug helpers and files browser renderer"
```

---

## Task 6: Routes `proj:files:`, `proj:fget:`, `proj:fpg:` dans `on_callback`

**Files:**
- Modify: `tgbot/bot.py:455` (`on_callback`)

- [ ] **Step 1: Ajouter le routage avant le handler générique `proj:<name>`**

Dans `on_callback`, JUSTE AVANT le bloc `if ns == "proj" and len(parts) >= 2:` (vers `bot.py:493`), insérer :

```python
if ns == "proj" and len(parts) >= 2 and parts[1] in ("files", "fget", "fpg"):
    # parts == ["proj", "files"|"fget"|"fpg", "<name>:<rest>"]
    sub = parts[1]
    rest = parts[2] if len(parts) > 2 else ""
    rest_parts = rest.split(":")
    name = rest_parts[0]
    if not db.get_project(name):
        await query.answer(text=f"Projet {name} introuvable", show_alert=True)
        return

    if sub == "files":
        slug = rest_parts[1] if len(rest_parts) > 1 else "_"
        rel = _files_resolve(ctx, slug)
        if rel is None:
            await query.answer(text="Chemin expiré, réouvre Fichiers", show_alert=True)
            return
        # Reset page when opening a fresh dir via slug click (preserve when paginating)
        await _render_files(query, ctx, name, rel)
        return

    if sub == "fget":
        slug = rest_parts[1] if len(rest_parts) > 1 else None
        rel = _files_resolve(ctx, slug) if slug else None
        if rel is None:
            await query.answer(text="Fichier expiré, réouvre Fichiers", show_alert=True)
            return
        proj = db.get_project(name)
        try:
            target = files_mgr.get_file(proj["path"], rel)
        except (PathEscapeError, FileNotFoundError, IsADirectoryError) as e:
            await query.answer(text=str(e), show_alert=True)
            return
        with target.open("rb") as f:
            await query.message.reply_document(
                document=f, filename=target.name,
                caption=f"/put {name} {rel}",
            )
        return

    if sub == "fpg":
        # rest_parts = [name, slug, "prev"|"next"]
        if len(rest_parts) < 3:
            return
        slug, direction = rest_parts[1], rest_parts[2]
        rel = _files_resolve(ctx, slug)
        if rel is None:
            await query.answer(text="Page expirée", show_alert=True)
            return
        page_key = f"files_page:{name}:{slug}"
        cur = ctx.chat_data.get(page_key, 0)
        ctx.chat_data[page_key] = cur + (1 if direction == "next" else -1)
        await _render_files(query, ctx, name, rel)
        return
```

⚠️ Important : `parts = data.split(":", 2)` au début de `on_callback` ne crée que 3 éléments max. Donc pour `proj:files:myproj:abc123`, `parts = ["proj", "files", "myproj:abc123"]`. Le split sur `rest` ci-dessus est nécessaire.

⚠️ Le `wiz:noop` (callback du compteur de page) doit être no-op — il l'est déjà car aucun handler ne le matche, donc `query.answer()` au début de `on_callback` consume bien le tap sans rien faire.

- [ ] **Step 2: Smoke test manuel**

`/start` → projet → 📁 Fichiers → vérifier la liste avec dossiers/fichiers. Click un fichier → reçoit le document. Click un dossier → navigation. ⬆️ Parent → remonte. Pagination ▶️/◀️ si > 10 entrées. ⬅️ Retour → menu projet.

- [ ] **Step 3: Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(bot): wire files browser callbacks (files/fget/fpg)"
```

---

## Task 7: Renderer Holdings picker

**Files:**
- Modify: `tgbot/trading/handlers.py` (ajout d'un renderer + routage)

- [ ] **Step 1: Ajouter le renderer du picker holdings**

Dans `register_handlers`, à proximité des autres `_render_*` (vers `handlers.py:380`), ajouter :

```python
async def _render_holdings_picker(query) -> None:
    wallets = db.list_wallets()
    if not wallets:
        rows = [[
            InlineKeyboardButton("👛 Aller aux Wallets", callback_data="trd:wallets"),
        ], [
            InlineKeyboardButton("⬅️ Retour", callback_data="trd:home"),
        ]]
        await query.edit_message_text(
            "*💰 Holdings*\nAucun wallet surveillé. Ajoute-en un d'abord.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    rows: list[list[InlineKeyboardButton]] = []
    for w in wallets:
        tag = f" — {w['label']}" if w["label"] else ""
        short = f"{w['address'][:4]}…{w['address'][-4:]}"
        rows.append([InlineKeyboardButton(
            f"{w['chain'].upper()} {short}{tag}",
            callback_data=f"trd:hget:{w['chain']}:{w['address']}",
        )])
    rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="trd:home")])
    await query.edit_message_text(
        "*💰 Holdings*\nChoisis un wallet :",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )
```

- [ ] **Step 2: Ajouter le renderer holdings pour un wallet précis**

Toujours dans `register_handlers`, ajouter (réutiliser la logique de `cmd_holdings` à `handlers.py` autour de la ligne du commentaire `/holdings`) :

```python
async def _render_holdings_for(query, chain: str, addr: str) -> None:
    norm = _normalize_address(addr, chain)
    await query.edit_message_text(
        f"⏳ Fetching holdings pour `{norm}`…",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        if chain == "sol":
            from .solana import fetch_solana_holdings
            holdings, _ = await fetch_solana_holdings(monitor.helius_api_key, norm)
            text = _format_solana_holdings(holdings)  # ⚠️ voir step 3
        else:
            from .evm import fetch_evm_holdings
            holdings, native_value = await fetch_evm_holdings(
                monitor.alchemy_api_key, norm, chain,  # ⚠️ adapter aux args réels
            )
            text = _format_evm_holdings(holdings, native_value, chain)
    except Exception as e:
        text = f"*Erreur*\n`{e}`"
    rows = [[InlineKeyboardButton("⬅️ Retour", callback_data="trd:hold")]]
    await query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(rows),
    )
```

- [ ] **Step 3: Aligner sur la logique exacte de `cmd_holdings`**

Lire `cmd_holdings` dans `handlers.py` (autour de la ligne du commentaire `# /holdings`) et reprendre EXACTEMENT :
- Les args passés à `fetch_solana_holdings` et `fetch_evm_holdings`.
- Les helpers de formatting utilisés (probablement dans `tgbot/trading/formatters.py`).

Remplacer les appels `_format_solana_holdings(...)` / `_format_evm_holdings(...)` ci-dessus par les vrais helpers de `formatters.py`. Importer en haut du module si besoin.

- [ ] **Step 4: Brancher le routage**

Dans le dispatcher de callbacks trading (chercher la fonction qui matche `trd:*`, probablement nommée `on_trd_callback` ou similaire — grep `trd:wallets` ou `trd:alerts` dans `handlers.py`). Avant le retour final, ajouter :

```python
if data == "trd:hold":
    await _render_holdings_picker(query)
    return
if data.startswith("trd:hget:"):
    rest = data[len("trd:hget:"):]
    chain, _, addr = rest.partition(":")
    if not chain or not addr:
        return
    await _render_holdings_for(query, chain, addr)
    return
```

⚠️ Si le dispatcher trading utilise des patterns différents (par exemple split puis switch), adapter au pattern existant.

- [ ] **Step 5: Smoke test manuel**

`/start` → 📈 Trading → 💰 Holdings → si vides → vérifier le message. Sinon → click un wallet → "Fetching…" → résultat formaté. ⬅️ Retour fonctionne.

- [ ] **Step 6: Commit**

```bash
git add tgbot/trading/handlers.py
git commit -m "feat(trading): add Holdings picker + per-wallet renderer"
```

---

## Task 8: Wizard `➕ Ajouter wallet`

**Files:**
- Modify: `tgbot/trading/handlers.py`

- [ ] **Step 1: Importer les helpers wizard depuis bot.py**

Au sommet de `handlers.py`, vérifier l'import existant des helpers wizard (`_wizard_step`, `_wizard_finish`, `_wizard_escape`). S'ils ne sont pas exposés, deux options :
- **Option A (préférée)** : exposer ces helpers via `tgbot/bot.py` (les déplacer hors du closure `setup_app`) — risqué car ils dépendent de `trading_enabled`.
- **Option B (pragmatique)** : passer les helpers en paramètres à `register_trading()` (`tgbot/trading/__init__.py`) et les transmettre à `register_handlers()`.

Implémenter **Option B** :

Dans `tgbot/trading/__init__.py`, ajouter `wizard_step`, `wizard_finish`, `wizard_escape` comme paramètres optionnels de `register_trading()` :

```python
def register_trading(
    app, cfg, *, wizard_step=None, wizard_finish=None, wizard_escape=None,
):
    ...
    register_handlers(
        app, cfg, db, monitor,
        wizard_step=wizard_step,
        wizard_finish=wizard_finish,
        wizard_escape=wizard_escape,
    )
```

Et dans `register_handlers` (`handlers.py:76`), ajouter ces paramètres au sommet :

```python
def register_handlers(
    app, cfg, db, monitor,
    *, wizard_step=None, wizard_finish=None, wizard_escape=None,
):
    ...
```

Dans `bot.py:1317` (`register_trading(app, cfg)`), passer les helpers :

```python
register_trading(
    app, cfg,
    wizard_step=_wizard_step,
    wizard_finish=_wizard_finish,
    wizard_escape=_wizard_escape,
)
```

- [ ] **Step 2: Ajouter les constantes d'état**

Dans `handlers.py`, après `SUPPORTED_CHAINS`, ajouter :

```python
TRD_WADD_CHAIN = 800
TRD_WADD_ADDR = 801
TRD_WADD_LABEL = 802
```

- [ ] **Step 3: Implémenter les handlers du wizard**

Dans `register_handlers`, à côté des autres `cmd_*`, ajouter (en fermant sur `wizard_step`, `wizard_finish`, `db`, `monitor`) :

```python
async def wadd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rows = [[
        InlineKeyboardButton(c.upper(), callback_data=f"trd:wadd:chain:{c}")
        for c in SUPPORTED_CHAINS
    ]]
    await wizard_step(update, ctx, "➕ Ajouter wallet\n\nChoisis la chaîne :", extra_rows=rows)
    return TRD_WADD_CHAIN


async def wadd_chain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        return TRD_WADD_CHAIN
    chain = parts[3]
    if chain not in SUPPORTED_CHAINS:
        return TRD_WADD_CHAIN
    ctx.user_data["wadd_chain"] = chain
    await wizard_step(update, ctx, f"Chaîne : *{chain.upper()}*\n\nEnvoie l'adresse du wallet :")
    return TRD_WADD_ADDR


async def wadd_addr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    chain = ctx.user_data.get("wadd_chain")
    if not chain:
        await wizard_step(update, ctx, "⚠️ État perdu.")
        return ConversationHandler.END
    addr = update.message.text.strip()
    if not validate_address(addr, chain):
        await wizard_step(
            update, ctx,
            f"❌ Adresse invalide pour *{chain.upper()}*. Réessaie :",
        )
        return TRD_WADD_ADDR
    ctx.user_data["wadd_addr"] = _normalize_address(addr, chain)
    await wizard_step(update, ctx, "Label optionnel (ou tape `skip`) :")
    return TRD_WADD_LABEL


async def wadd_label(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    text = update.message.text.strip()
    label = None if text.lower() == "skip" else text
    chain = ctx.user_data.get("wadd_chain")
    addr = ctx.user_data.get("wadd_addr")
    if not chain or not addr:
        await wizard_step(update, ctx, "⚠️ État perdu.")
        return ConversationHandler.END
    if db.add_wallet(addr, chain, label):
        monitor.notify_wallets_changed(chain)
        msg = f"✅ Watching `{addr}` sur *{chain.upper()}*"
    else:
        msg = f"ℹ️ Déjà surveillé : `{addr}` sur *{chain.upper()}*"
    await wizard_step(update, ctx, msg)
    # Cleanup
    for k in ("wadd_chain", "wadd_addr"):
        ctx.user_data.pop(k, None)
    await wizard_finish(update, ctx)
    return ConversationHandler.END


async def wadd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    for k in ("wadd_chain", "wadd_addr"):
        ctx.user_data.pop(k, None)
    await wizard_finish(update, ctx)
    return ConversationHandler.END
```

- [ ] **Step 4: Enregistrer le ConversationHandler**

À la fin de `register_handlers`, AVANT le retour ou les `app.add_handler(CommandHandler(...))` finaux, ajouter :

```python
wadd_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(wadd_start, pattern=r"^trd:wadd$")],
    states={
        TRD_WADD_CHAIN: [CallbackQueryHandler(wadd_chain, pattern=r"^trd:wadd:chain:")],
        TRD_WADD_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, wadd_addr)],
        TRD_WADD_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, wadd_label)],
    },
    fallbacks=[
        CallbackQueryHandler(wadd_cancel, pattern=r"^wiz:cancel$"),
    ],
)
app.add_handler(wadd_conv)
```

⚠️ Imports nécessaires en haut de `handlers.py` si absents : `ConversationHandler`, `CallbackQueryHandler`, `MessageHandler`, `filters`.

- [ ] **Step 5: Smoke test manuel**

📈 Trading → 👛 Wallets surveillés → ➕ Ajouter wallet → choisir SOL → coller une adresse SOL valide → label `skip` → vérifier message ✅ et retour menu. Tester aussi adresse invalide (reste sur l'état). Tester ❌ Annuler.

- [ ] **Step 6: Commit**

```bash
git add tgbot/bot.py tgbot/trading/__init__.py tgbot/trading/handlers.py
git commit -m "feat(trading): add wizard for ➕ Ajouter wallet"
```

---

## Task 9: Wizard `➕ Créer alerte`

**Files:**
- Modify: `tgbot/trading/handlers.py`

- [ ] **Step 1: Ajouter les constantes d'état**

Dans `handlers.py`, après les constantes `TRD_WADD_*` :

```python
TRD_AADD_CHAIN = 810
TRD_AADD_TOKEN = 811
TRD_AADD_MC = 812
TRD_AADD_DIR = 813
TRD_AADD_PERSIST = 814
TRD_AADD_LABEL = 815
```

- [ ] **Step 2: Implémenter les handlers**

À côté des handlers `wadd_*`, ajouter :

```python
async def aadd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rows = [[
        InlineKeyboardButton(c.upper(), callback_data=f"trd:aadd:chain:{c}")
        for c in SUPPORTED_CHAINS
    ]]
    await wizard_step(update, ctx, "➕ Créer alerte MC\n\nChaîne du token :", extra_rows=rows)
    return TRD_AADD_CHAIN


async def aadd_chain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[3] not in SUPPORTED_CHAINS:
        return TRD_AADD_CHAIN
    ctx.user_data["aadd_chain"] = parts[3]
    await wizard_step(update, ctx, f"Chaîne : *{parts[3].upper()}*\n\nAdresse du token :")
    return TRD_AADD_TOKEN


async def aadd_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    chain = ctx.user_data.get("aadd_chain")
    if not chain:
        await wizard_step(update, ctx, "⚠️ État perdu.")
        return ConversationHandler.END
    token = update.message.text.strip()
    if not validate_address(token, chain):
        await wizard_step(update, ctx, f"❌ Adresse invalide pour *{chain.upper()}*. Réessaie :")
        return TRD_AADD_TOKEN
    ctx.user_data["aadd_token"] = _normalize_address(token, chain)
    await wizard_step(update, ctx, "Marketcap cible (ex `1m`, `500k`) :")
    return TRD_AADD_MC


async def aadd_mc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    mc = _parse_mc(update.message.text.strip())
    if mc is None or mc <= 0:
        await wizard_step(update, ctx, "❌ Marketcap invalide. Réessaie (ex `1m`, `500k`) :")
        return TRD_AADD_MC
    ctx.user_data["aadd_mc"] = mc
    rows = [[
        InlineKeyboardButton("↑ Above", callback_data="trd:aadd:dir:above"),
        InlineKeyboardButton("↓ Below", callback_data="trd:aadd:dir:below"),
    ]]
    await wizard_step(update, ctx, f"Marketcap : *${_fmt_mc(mc)}*\n\nDéclenchement ?", extra_rows=rows)
    return TRD_AADD_DIR


async def aadd_dir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[3] not in ("above", "below"):
        return TRD_AADD_DIR
    ctx.user_data["aadd_dir"] = parts[3]
    rows = [[
        InlineKeyboardButton("One-shot", callback_data="trd:aadd:persist:no"),
        InlineKeyboardButton("Persistent", callback_data="trd:aadd:persist:yes"),
    ]]
    await wizard_step(update, ctx, f"Direction : *{parts[3]}*\n\nType d'alerte ?", extra_rows=rows)
    return TRD_AADD_PERSIST


async def aadd_persist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[3] not in ("yes", "no"):
        return TRD_AADD_PERSIST
    ctx.user_data["aadd_persist"] = parts[3] == "yes"
    await wizard_step(update, ctx, "Label (ou tape `skip`) :")
    return TRD_AADD_LABEL


async def aadd_label(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    text = update.message.text.strip()
    label = None if text.lower() == "skip" else text
    chain = ctx.user_data.get("aadd_chain")
    token = ctx.user_data.get("aadd_token")
    mc = ctx.user_data.get("aadd_mc")
    direction = ctx.user_data.get("aadd_dir")
    persistent = ctx.user_data.get("aadd_persist", False)
    if not all([chain, token, mc, direction]):
        await wizard_step(update, ctx, "⚠️ État perdu.")
        return ConversationHandler.END
    aid = db.add_alert(
        token_address=token, chain=chain, mc_target=mc,
        direction=direction, persistent=persistent, label=label,
    )
    arrow = "↑" if direction == "above" else "↓"
    kind = "persistent" if persistent else "one-shot"
    await wizard_step(
        update, ctx,
        f"🔔 Alert *#{aid}* armed: `{token}` MC {arrow} *${_fmt_mc(mc)}* "
        f"({chain.upper()}, {kind})",
    )
    for k in ("aadd_chain", "aadd_token", "aadd_mc", "aadd_dir", "aadd_persist"):
        ctx.user_data.pop(k, None)
    await wizard_finish(update, ctx)
    return ConversationHandler.END


async def aadd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    for k in ("aadd_chain", "aadd_token", "aadd_mc", "aadd_dir", "aadd_persist"):
        ctx.user_data.pop(k, None)
    await wizard_finish(update, ctx)
    return ConversationHandler.END
```

- [ ] **Step 3: Enregistrer le ConversationHandler**

Après `wadd_conv` :

```python
aadd_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(aadd_start, pattern=r"^trd:aadd$")],
    states={
        TRD_AADD_CHAIN: [CallbackQueryHandler(aadd_chain, pattern=r"^trd:aadd:chain:")],
        TRD_AADD_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, aadd_token)],
        TRD_AADD_MC: [MessageHandler(filters.TEXT & ~filters.COMMAND, aadd_mc)],
        TRD_AADD_DIR: [CallbackQueryHandler(aadd_dir, pattern=r"^trd:aadd:dir:")],
        TRD_AADD_PERSIST: [CallbackQueryHandler(aadd_persist, pattern=r"^trd:aadd:persist:")],
        TRD_AADD_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, aadd_label)],
    },
    fallbacks=[CallbackQueryHandler(aadd_cancel, pattern=r"^wiz:cancel$")],
)
app.add_handler(aadd_conv)
```

- [ ] **Step 4: Smoke test manuel**

📈 Trading → 🔔 Alertes MC → ➕ Créer alerte → choisir SOL → coller une adresse token → `500k` → Above → One-shot → `skip` → vérifier le message d'arming, retour menu, et présence de l'alerte dans 🔔 Alertes MC.

- [ ] **Step 5: Commit**

```bash
git add tgbot/trading/handlers.py
git commit -m "feat(trading): add wizard for ➕ Créer alerte MC"
```

---

## Task 10: Validation manuelle finale + cleanup

**Files:** N/A (test only)

- [ ] **Step 1: Exécuter le scénario complet du spec**

Suivre les 7 scénarios listés en section "Validation manuelle" du spec :

1. Menu projet → ⚙️ Config → wizard pré-chargé → finish → retour menu.
2. Menu projet → 📁 Fichiers → root → sous-dossier → ⬆️ Parent → root → fichier → reçoit document.
3. Menu projet → 💻 Shell → `ls` → reçoit output.
4. Trading → 💰 Holdings → liste wallets → click wallet → reçoit holdings.
5. Trading → 👛 Wallets → ➕ Ajouter wallet → wizard complet → wallet apparaît.
6. Trading → 🔔 Alertes → ➕ Créer alerte → wizard complet → alerte apparaît.
7. `wiz:cancel` annule chacun des 3 nouveaux wizards proprement.

- [ ] **Step 2: Vérifier qu'aucun handler n'est dupliqué ou en conflit**

Lancer `python -c "from tgbot.bot import setup_app; ..."` ou démarrer le bot et confirmer qu'il démarre sans erreur. Vérifier les logs pour des warnings de pattern overlap.

- [ ] **Step 3: Commit récap (optionnel)**

Si des ajustements mineurs ont été nécessaires pendant la validation :

```bash
git add -A
git commit -m "fix(bot): smoke test adjustments for menu features"
```

---

## Self-Review Notes

- **Couverture spec :** les 6 fonctionnalités du spec sont chacune couvertes par au moins une tâche (Config = T3, Fichiers = T5+T6, Shell = T4, Holdings = T7, AddWallet = T8, AddAlert = T9). Les markups (placement) sont T1 + T2.
- **Pas de placeholders :** chaque step contient du code complet ou des commandes exactes.
- **Cohérence des noms :** états `TRD_WADD_*` (800-802), `TRD_AADD_*` (810-815), `PROJ_SHELL_CMD` (700). Helpers slug : `_files_slug`, `_files_resolve`, `_files_remember`. Constantes utilisées de manière cohérente entre tâches.
- **Warning à relire pendant exécution :** la structure exacte de `cmd_holdings` (T7 step 3) et le format de retour de `files_mgr.list_dir` (T5 step 2) sont à confirmer en lisant le code existant — j'ai signalé ces points avec ⚠️ dans les tâches.
