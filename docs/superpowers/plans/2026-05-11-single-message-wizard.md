# Single-Message Wizard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** Refondre les 3 flux `ConversationHandler` (`add_conv`, `action_add_conv`, `config_conv`) en un wizard à message unique — le bot édite un seul message qui évolue d'étape en étape, supprime chaque réponse texte de l'utilisateur dès traitement, et termine en redevenant le menu principal.

**Architecture :** Deux helpers closures (`_wizard_step`, `_wizard_finish`) dans `build_app()` qui pilotent un unique message Telegram via `edit_message_text`. Chaque handler de réponse appelle `update.message.delete()` en première ligne. Un callback `wiz:cancel` et un fallback générique de "wizard escape" gèrent l'annulation et la navigation menu-pendant-wizard.

**Tech Stack :** Python 3.14, `python-telegram-bot` (PTB), pas de framework de tests automatisés — vérifications via `python -c "from tgbot import bot"` (syntax + import) + smoke tests manuels Telegram.

**Spec :** `docs/superpowers/specs/2026-05-11-single-message-wizard-design.md`

**Fichier(s) modifié(s) :** `tgbot/bot.py` uniquement.

---

## Note préliminaire : ordre des états action

Le spec fait passer le wizard d'action de l'ordre actuel (`NAME → COMMAND → CWD → MODE → CONFIRM`) à un nouvel ordre (`NAME → MODE → CONFIRM → COMMAND → CWD`). Cela améliore l'UX (boutons d'abord, saisie texte après) et reflète l'intention du user validée pendant le brainstorming. Le plan applique ce nouvel ordre.

## Note préliminaire : /config sans arg

Aujourd'hui `/config` sans argument refuse avec un message d'erreur. Le spec demande que `/config` seul affiche une liste de projets cliquables. Cela introduit un nouvel état `CFG_SELECT` (intercale avant `CFG_START_CMD`). `/config <name>` continue de fonctionner et saute directement à `CFG_START_CMD`.

---

## Task 1 : Helpers `_wizard_step` et `_wizard_finish`

**Files :**
- Modify : `tgbot/bot.py` (insérer les deux helpers juste après `_send_main_menu`, autour de la ligne 362)

- [ ] **Step 1 : Localiser le point d'insertion**

Run :
```bash
grep -n "async def _send_main_menu" tgbot/bot.py
```
Expected : une ligne `356:    async def _send_main_menu(update: Update) -> None:` (à ±5 lignes près).

- [ ] **Step 2 : Insérer les helpers**

Insère le bloc suivant **juste après** la fin du corps de `_send_main_menu` (après la ligne `reply_markup=_main_menu_markup(trading_enabled),` qui ferme `_send_main_menu`, donc après le `)` qui suit) et **avant** `async def _execute_action`. Indentation : 4 espaces (closures de `build_app`).

```python
    def _wizard_markup(extra_rows: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
        """Main-menu buttons + optional extra rows + Cancel row."""
        base = _main_menu_markup(trading_enabled).inline_keyboard
        rows: list[list[InlineKeyboardButton]] = [list(r) for r in base]
        if extra_rows:
            rows.extend(extra_rows)
        rows.append([InlineKeyboardButton("❌ Annuler", callback_data="wiz:cancel")])
        return InlineKeyboardMarkup(rows)

    async def _wizard_step(
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        text: str,
        extra_rows: list[list[InlineKeyboardButton]] | None = None,
    ) -> None:
        """Edit (or create) the single wizard message with the current question."""
        markup = _wizard_markup(extra_rows)
        msg_id = ctx.user_data.get("wizard_msg_id")
        chat_id = ctx.user_data.get("wizard_chat_id")
        # Prefer using the callback_query message if present (button-triggered entry)
        if msg_id is None and update.callback_query is not None:
            msg_id = update.callback_query.message.message_id
            chat_id = update.callback_query.message.chat_id
            ctx.user_data["wizard_msg_id"] = msg_id
            ctx.user_data["wizard_chat_id"] = chat_id
        if msg_id is not None and chat_id is not None:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup,
                )
                return
            except Exception:
                # Stale message, deleted, or too old — fall through to send a new one
                ctx.user_data.pop("wizard_msg_id", None)
                ctx.user_data.pop("wizard_chat_id", None)
        sent = await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
        )
        ctx.user_data["wizard_msg_id"] = sent.message_id
        ctx.user_data["wizard_chat_id"] = sent.chat_id

    async def _wizard_finish(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Replace the wizard message with the clean main menu. Clean user_data."""
        msg_id = ctx.user_data.get("wizard_msg_id")
        chat_id = ctx.user_data.get("wizard_chat_id")
        # Clear all wizard / flow keys
        for k in (
            "wizard_msg_id", "wizard_chat_id",
            "add_name",
            "addact_name", "addact_command", "addact_cwd", "addact_mode",
            "cfg_project",
        ):
            ctx.user_data.pop(k, None)
        text = "*Menu principal*\nChoisis une action :"
        markup = _main_menu_markup(trading_enabled)
        if msg_id is not None and chat_id is not None:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup,
                )
                return
            except Exception:
                pass
        # Fallback: send a fresh menu message
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup,
        )
```

- [ ] **Step 3 : Vérifier le syntax + import**

Run :
```bash
python -c "from tgbot import bot; print('OK')"
```
Expected : `OK` sur stdout, exit code 0.

- [ ] **Step 4 : Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(bot): add wizard helpers (_wizard_step, _wizard_finish)"
```

---

## Task 2 : Route `wiz:cancel` et fallback "wizard escape"

**Files :**
- Modify : `tgbot/bot.py` :
  - `on_callback` (autour ligne 401) — ajouter route `wiz:cancel`
  - Définition du `wizard_escape` (à insérer juste avant les 3 ConversationHandlers, autour ligne 1120)
  - Fallbacks des 3 ConversationHandlers (autour lignes 1128, 1138, 1154)

- [ ] **Step 1 : Repérer `on_callback`**

Run :
```bash
grep -n "async def on_callback" tgbot/bot.py
grep -n "data.split" tgbot/bot.py | head -3
```
Expected : 1 ligne `on_callback`, et `data.split(":", ...)` pour le routage par namespace.

- [ ] **Step 2 : Ajouter la route `wiz:cancel` dans `on_callback`**

Dans `on_callback`, en début de fonction (avant la première branche `if/elif` qui dispatche par namespace), ajouter :

```python
        if data == "wiz:cancel":
            await update.callback_query.answer()
            await _wizard_finish(update, ctx)
            return
```

- [ ] **Step 3 : Ajouter le `_wizard_escape` handler**

Juste **avant** la définition de `config_conv` (donc avant la ligne contenant `config_conv = ConversationHandler(`, autour ligne 1120), insérer :

```python
    async def _wizard_escape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Fallback when user clicks a non-wizard button (e.g. main menu) during a wizard.
        Cleans state and lets on_callback handle the navigation by editing the same message."""
        # Don't intercept wiz:cancel — it has its own pattern handler routed through on_callback
        # and we want this fallback to be the catch-all for everything else.
        ctx.user_data.pop("wizard_msg_id", None)
        ctx.user_data.pop("wizard_chat_id", None)
        for k in ("add_name", "addact_name", "addact_command", "addact_cwd", "addact_mode", "cfg_project"):
            ctx.user_data.pop(k, None)
        await on_callback(update, ctx)
        return ConversationHandler.END
```

- [ ] **Step 4 : Ajouter `wiz:cancel` et le fallback générique aux 3 ConversationHandlers**

Pour **chacun** des 3 ConversationHandlers (`config_conv`, `add_conv`, `action_add_conv`), remplacer la ligne `fallbacks=[CommandHandler("cancel", <existing>)],` par :

```python
        fallbacks=[
            CommandHandler("cancel", <existing_cancel_handler>),
            CallbackQueryHandler(_wizard_escape),
        ],
```

Concrètement, pour les 3 :
- `config_conv` : `<existing_cancel_handler>` = `cfg_cancel`
- `add_conv` : `<existing_cancel_handler>` = `cfg_cancel` (réutilisé tel quel)
- `action_add_conv` : `<existing_cancel_handler>` = `action_add_cancel`

Note : le `CallbackQueryHandler(_wizard_escape)` n'a **pas** de pattern → il attrape tous les callbacks non gérés par les states. Le routage `wiz:cancel` côté `on_callback` (Step 2) sera donc atteint via `_wizard_escape → on_callback`.

- [ ] **Step 5 : Vérifier syntax + import**

Run :
```bash
python -c "from tgbot import bot; print('OK')"
```
Expected : `OK`.

- [ ] **Step 6 : Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(bot): wire wiz:cancel route and wizard escape fallback"
```

---

## Task 3 : Refondre `add_conv` (ajout de projet)

**Files :**
- Modify : `tgbot/bot.py` :
  - Trouver `async def add_project_start` (l'entry point du flux — repérer via grep)
  - Réécrire `add_name` (autour ligne 576)
  - Réécrire `add_path` (autour ligne 597)

- [ ] **Step 1 : Repérer l'entry-point du flux**

Run :
```bash
grep -n "ADD_NAME\|add_project_start\|addproj:" tgbot/bot.py
```
Expected : voir les retours `return ADD_NAME` et la fonction qui ouvre le flux (probablement appelée depuis un bouton ou la commande `/addpath`).

- [ ] **Step 2 : Réécrire l'entry-point pour utiliser le wizard**

L'entry-point actuel envoie (ou édite) un message demandant le nom. Le remplacer pour appeler `_wizard_step` :

```python
    async def add_project_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.callback_query is not None:
            await update.callback_query.answer()
        await _wizard_step(update, ctx, "📂 *Nouveau projet*\n\nEnvoie un nom court (pas d'espace ni de `:`).")
        return ADD_NAME
```

Garde la signature de la fonction existante et son décorateur `@auth`. Si le nom de la fonction existante diffère (ex : `cmd_addpath`, `add_start`), conserve ce nom.

- [ ] **Step 3 : Réécrire `add_name`**

Remplace le corps de `add_name` (ligne ~576) intégralement par :

```python
    async def add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = update.message.text.strip()
        if not name or ":" in name or " " in name:
            await _wizard_step(update, ctx, "⚠️ Nom invalide (pas d'espace ni de `:`).\n\n📂 Envoie un nom court.")
            return ADD_NAME
        if db.get_project(name):
            await _wizard_step(update, ctx, f"⚠️ Le projet `{name}` existe déjà.\n\n📂 Choisis un autre nom.")
            return ADD_NAME
        ctx.user_data["add_name"] = name
        await _wizard_step(update, ctx, f"📂 Projet : *{name}*\n\n📄 Envoie le chemin absolu du dossier.")
        return ADD_PATH
```

- [ ] **Step 4 : Réécrire `add_path`**

Remplace le corps de `add_path` (ligne ~597) intégralement par :

```python
    async def add_path(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        path_obj = Path(update.message.text.strip()).expanduser().resolve()
        if not path_obj.is_dir():
            await _wizard_step(update, ctx, f"⚠️ Pas un dossier : `{path_obj}`.\n\n📄 Envoie un chemin valide.")
            return ADD_PATH
        name = ctx.user_data.get("add_name")
        if not name or not db.add_project(name, str(path_obj)):
            await _wizard_step(update, ctx, f"⚠️ Échec : projet `{name}` déjà existant ou état perdu.")
            await _wizard_finish(update, ctx)
            return ConversationHandler.END
        await _wizard_finish(update, ctx)
        return ConversationHandler.END
```

- [ ] **Step 5 : Vérifier syntax + import**

Run :
```bash
python -c "from tgbot import bot; print('OK')"
```
Expected : `OK`.

- [ ] **Step 6 : Smoke test Telegram manuel**

Démarrer le bot (`python -m tgbot`), puis dans Telegram :
1. Cliquer "📂 Projets" → "+ Ajouter" (ou taper `/addpath`).
2. Vérifier que **le menu courant** est édité en wizard (boutons menu + question + Annuler).
3. Taper un nom valide → ton message disparaît, le wizard édite pour demander le chemin.
4. Taper un chemin valide → le message redevient le menu principal propre.
5. Recommencer, mais à l'étape "nom" cliquer "❌ Annuler" → retour menu propre.
6. Recommencer, mais à l'étape "chemin" cliquer "📂 Projets" → la liste des projets s'affiche dans le même message, sans wizard résiduel.

- [ ] **Step 7 : Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(bot): refactor add_conv into single-message wizard"
```

---

## Task 4 : Refondre `action_add_conv` (ajout d'action, nouvel ordre)

**Files :**
- Modify : `tgbot/bot.py` :
  - Constantes d'états ligne 39
  - `action_add_start` (ligne ~621)
  - `action_add_name` (ligne ~636)
  - `action_add_command` (ligne ~659)
  - `action_add_cwd` (ligne ~671)
  - `action_add_mode` (ligne ~691)
  - `action_add_confirm` (ligne ~705)
  - `action_add_cancel` (ligne ~736)
  - Registration `states={...}` de `action_add_conv` (ligne ~1147)
  - Markup helper `_action_yesno_markup` reste tel quel — vérifier qu'il existe (ligne ~223).

- [ ] **Step 1 : Étendre les constantes d'états**

Trouver la ligne 39 actuelle :
```python
ADD_A_NAME, ADD_A_COMMAND, ADD_A_CWD, ADD_A_MODE, ADD_A_CONFIRM = range(4, 9)
```

Garde-la **inchangée** (les constantes restent disponibles, on ré-utilise les mêmes 5). Seul l'ordre de transition entre elles change.

- [ ] **Step 2 : Réécrire `action_add_start`**

Remplace son corps par :

```python
    async def action_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.callback_query is not None:
            await update.callback_query.answer()
        await _wizard_step(update, ctx, "🚀 *Nouvelle action*\n\nEnvoie un nom court (pas d'espace ni de `:`).")
        return ADD_A_NAME
```

- [ ] **Step 3 : Réécrire `action_add_name` — sortie vers MODE**

```python
    async def action_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = update.message.text.strip()
        if not name or ":" in name or " " in name:
            await _wizard_step(update, ctx, "⚠️ Nom invalide (pas d'espace ni de `:`).\n\n🚀 Envoie un nom court.")
            return ADD_A_NAME
        if db.get_action(name):
            await _wizard_step(update, ctx, f"⚠️ L'action `{name}` existe déjà.\n\n🚀 Choisis un autre nom.")
            return ADD_A_NAME
        ctx.user_data["addact_name"] = name
        await _wizard_step(
            update, ctx,
            f"🚀 Action : *{name}*\n\nMode d'exécution ?",
            extra_rows=[[
                InlineKeyboardButton("⚡ Oneshot", callback_data="addact:mode:oneshot"),
                InlineKeyboardButton("🔁 Long-running", callback_data="addact:mode:managed"),
            ]],
        )
        return ADD_A_MODE
```

- [ ] **Step 4 : Réécrire `action_add_mode` — sortie vers CONFIRM**

```python
    async def action_add_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[2] not in ("oneshot", "managed"):
            return ADD_A_MODE  # invalid, redisplay implicitly via no-op
        ctx.user_data["addact_mode"] = parts[2]
        await _wizard_step(
            update, ctx,
            "Demander une confirmation avant chaque exécution ?",
            extra_rows=[[
                InlineKeyboardButton("✅ Oui", callback_data="addact:cfm:yes"),
                InlineKeyboardButton("❌ Non", callback_data="addact:cfm:no"),
            ]],
        )
        return ADD_A_CONFIRM
```

- [ ] **Step 5 : Réécrire `action_add_confirm` — sortie vers COMMAND**

Attention : cet handler change radicalement de rôle. Avant : c'était l'étape **finale**. Désormais : c'est une étape **intermédiaire** qui demande la commande shell ensuite.

```python
    async def action_add_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[2] not in ("yes", "no"):
            return ADD_A_CONFIRM
        ctx.user_data["addact_confirm"] = (parts[2] == "yes")
        await _wizard_step(update, ctx, "💻 Commande shell à exécuter ?")
        return ADD_A_COMMAND
```

- [ ] **Step 6 : Réécrire `action_add_command` — sortie vers CWD avec bouton Passer**

```python
    async def action_add_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        command = update.message.text.strip()
        if not command:
            await _wizard_step(update, ctx, "⚠️ Commande vide.\n\n💻 Envoie la commande shell à exécuter.")
            return ADD_A_COMMAND
        ctx.user_data["addact_command"] = command
        await _wizard_step(
            update, ctx,
            "📁 Répertoire de travail (optionnel) ?\n\nEnvoie un chemin absolu, ou clique *Passer*.",
            extra_rows=[[InlineKeyboardButton("⏭️ Passer", callback_data="addact:cwd:skip")]],
        )
        return ADD_A_CWD
```

- [ ] **Step 7 : Réécrire `action_add_cwd` — étape finale (gère texte ET bouton Passer)**

```python
    async def action_add_cwd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Two entry paths: callback (Passer button) or text message
        cwd = None
        if update.callback_query is not None:
            query = update.callback_query
            await query.answer()
            if query.data != "addact:cwd:skip":
                return ADD_A_CWD
            cwd = None
        else:
            try:
                await update.message.delete()
            except Exception:
                pass
            text = update.message.text.strip()
            if text == "" or text == "-":
                cwd = None
            else:
                cwd_path = Path(text).expanduser()
                if not cwd_path.is_dir():
                    await _wizard_step(
                        update, ctx,
                        f"⚠️ Pas un dossier : `{cwd_path}`.\n\n📁 Envoie un chemin valide, ou *Passer*.",
                        extra_rows=[[InlineKeyboardButton("⏭️ Passer", callback_data="addact:cwd:skip")]],
                    )
                    return ADD_A_CWD
                cwd = str(cwd_path.resolve())
        # Finalize: persist the action
        name = ctx.user_data.get("addact_name")
        command = ctx.user_data.get("addact_command")
        mode = ctx.user_data.get("addact_mode", "oneshot")
        require_confirm = ctx.user_data.get("addact_confirm", False)
        if not name or not command:
            await _wizard_finish(update, ctx)
            return ConversationHandler.END
        db.add_action(name, command, cwd, mode, require_confirm)
        await _wizard_finish(update, ctx)
        return ConversationHandler.END
```

- [ ] **Step 8 : Mettre à jour `states={}` de `action_add_conv`**

Trouver la ligne ~1147 contenant `action_add_conv = ConversationHandler(`, puis modifier le bloc `states={...}` ainsi :

```python
        states={
            ADD_A_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, action_add_name)],
            ADD_A_MODE: [CallbackQueryHandler(action_add_mode, pattern=r"^addact:mode:")],
            ADD_A_CONFIRM: [CallbackQueryHandler(action_add_confirm, pattern=r"^addact:cfm:")],
            ADD_A_COMMAND: [MessageHandler(filters.TEXT & ~filters.COMMAND, action_add_command)],
            ADD_A_CWD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, action_add_cwd),
                CallbackQueryHandler(action_add_cwd, pattern=r"^addact:cwd:skip$"),
            ],
        },
```

Le `pattern` sur ADD_A_CWD garantit que seul le bouton "Passer" route vers cet handler en callback ; les autres callbacks (menu) tombent dans le fallback `_wizard_escape`.

- [ ] **Step 9 : Simplifier `action_add_cancel`**

Remplace son corps par :

```python
    async def action_add_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await _wizard_finish(update, ctx)
        return ConversationHandler.END
```

- [ ] **Step 10 : Syntax + import check**

Run :
```bash
python -c "from tgbot import bot; print('OK')"
```
Expected : `OK`.

- [ ] **Step 11 : Smoke test Telegram manuel**

Dans Telegram :
1. Cliquer "🚀 Actions" → "+ Ajouter" (ou `/addaction`).
2. Suivre : Nom (texte) → Mode (boutons) → Confirmation (boutons) → Commande (texte) → Cwd (Passer ou texte).
3. À chaque étape : le menu reste visible en haut, la question change en bas, ton message texte disparaît.
4. Vérifier dans la liste `/actions` que l'action est bien créée avec les bons paramètres.
5. Tester le chemin "Passer" sur cwd, puis dans une autre tentative un chemin invalide → message d'erreur dans le même message.
6. Tester "❌ Annuler" à chaque étape.

- [ ] **Step 12 : Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(bot): refactor action_add_conv into single-message wizard"
```

---

## Task 5 : Refondre `config_conv` (avec nouvel état `CFG_SELECT`)

**Files :**
- Modify : `tgbot/bot.py` :
  - Constantes d'états ligne 35 — étendre
  - `cmd_config` (ligne ~878)
  - Nouvel handler `cfg_select` (callback pour sélection projet)
  - `cfg_start_cmd` (ligne ~900)
  - `cfg_entry_file` (ligne ~913)
  - `cfg_cancel` (ligne ~925)
  - `states={}` de `config_conv` (ligne ~1124)

- [ ] **Step 1 : Étendre les constantes d'états**

Ligne 35 actuelle :
```python
CFG_START_CMD, CFG_ENTRY_FILE = range(2)
```

La remplacer par :
```python
CFG_SELECT, CFG_START_CMD, CFG_ENTRY_FILE = range(3)
```

Attention : cela décale `range`. Mais les autres constantes (`ADD_NAME, ADD_PATH = range(2, 4)` et `ADD_A_NAME, ... = range(4, 9)`) sont basées sur `range(2, 4)` et `range(4, 9)` qui sont indépendantes. Vérifier qu'aucune n'utilise `range(2)` implicitement. Run :

```bash
grep -n "range(" tgbot/bot.py | head -10
```
Expected : seules les 3 lignes connues utilisent `range(...)` pour les constantes d'états. OK à étendre.

- [ ] **Step 2 : Réécrire `cmd_config`**

```python
    async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Two entry paths: /config <name> (args) → jump to CFG_START_CMD
        #                  /config (no args) → CFG_SELECT
        if ctx.args:
            name = ctx.args[0]
            proj = db.get_project(name)
            if not proj:
                await _wizard_step(update, ctx, f"⚠️ Pas de projet `{name}`.")
                await _wizard_finish(update, ctx)
                return ConversationHandler.END
            ctx.user_data["cfg_project"] = name
            current = proj.get("start_command") or "(none)"
            await _wizard_step(
                update, ctx,
                f"⚙️ Configurer *{name}*\n\nCommande actuelle : `{current}`\n\n💻 Envoie la commande de démarrage.",
            )
            return CFG_START_CMD
        # No args → list projects
        projects = db.list_projects()
        if not projects:
            await _wizard_step(update, ctx, "Aucun projet. Crée-en un d'abord via *📂 Projets*.")
            await _wizard_finish(update, ctx)
            return ConversationHandler.END
        rows = [[InlineKeyboardButton(p["name"], callback_data=f"cfgsel:{p['name']}")] for p in projects]
        await _wizard_step(update, ctx, "⚙️ Sélectionne un projet à configurer.", extra_rows=rows)
        return CFG_SELECT
```

- [ ] **Step 3 : Ajouter `cfg_select`**

Insérer juste après `cmd_config` :

```python
    async def cfg_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":", 1)
        if len(parts) != 2:
            return CFG_SELECT
        name = parts[1]
        proj = db.get_project(name)
        if not proj:
            await _wizard_step(update, ctx, f"⚠️ Pas de projet `{name}`.")
            await _wizard_finish(update, ctx)
            return ConversationHandler.END
        ctx.user_data["cfg_project"] = name
        current = proj.get("start_command") or "(none)"
        await _wizard_step(
            update, ctx,
            f"⚙️ Configurer *{name}*\n\nCommande actuelle : `{current}`\n\n💻 Envoie la commande de démarrage.",
        )
        return CFG_START_CMD
```

- [ ] **Step 4 : Réécrire `cfg_start_cmd`**

```python
    async def cfg_start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = ctx.user_data.get("cfg_project")
        if not name:
            await _wizard_finish(update, ctx)
            return ConversationHandler.END
        cmd = update.message.text.strip()
        db.update_project(name, start_command=cmd)
        proj = db.get_project(name)
        current = proj.get("entry_file") or "(none)"
        await _wizard_step(
            update, ctx,
            f"✅ Commande enregistrée pour *{name}*.\n\n"
            f"📄 Fichier de log d'entrée (actuel : `{current}`) ?\n"
            f"Envoie un nom de fichier ou `skip`.",
        )
        return CFG_ENTRY_FILE
```

- [ ] **Step 5 : Réécrire `cfg_entry_file`**

```python
    async def cfg_entry_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = ctx.user_data.get("cfg_project")
        if not name:
            await _wizard_finish(update, ctx)
            return ConversationHandler.END
        text = update.message.text.strip()
        if text.lower() != "skip":
            db.update_project(name, entry_file=text)
        await _wizard_finish(update, ctx)
        return ConversationHandler.END
```

- [ ] **Step 6 : Simplifier `cfg_cancel`**

```python
    async def cfg_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await _wizard_finish(update, ctx)
        return ConversationHandler.END
```

- [ ] **Step 7 : Mettre à jour `states={}` de `config_conv`**

Modifier le bloc :

```python
        states={
            CFG_SELECT: [CallbackQueryHandler(cfg_select, pattern=r"^cfgsel:")],
            CFG_START_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_start_cmd)],
            CFG_ENTRY_FILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_entry_file)],
        },
```

- [ ] **Step 8 : Syntax + import check**

Run :
```bash
python -c "from tgbot import bot; print('OK')"
```
Expected : `OK`.

- [ ] **Step 9 : Smoke test Telegram manuel**

Dans Telegram :
1. Taper `/config` (sans arg) → wizard avec liste de projets cliquables.
2. Cliquer un projet → demande la commande de démarrage.
3. Taper la commande → ton message disparaît, demande le fichier de log.
4. Taper `skip` → retour au menu principal propre.
5. Tester aussi `/config nomprojet` → saute directement à l'étape commande.
6. Tester "❌ Annuler" à chaque étape.

- [ ] **Step 10 : Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(bot): refactor config_conv into single-message wizard with project picker"
```

---

## Task 6 : Vérification finale + cleanup

**Files :** `tgbot/bot.py`

- [ ] **Step 1 : Vérifier qu'aucun appel résiduel à `_send_main_menu` ne reste dans les 3 flux refactorés**

Run :
```bash
grep -n "_send_main_menu" tgbot/bot.py
```

Expected : il ne doit rester que la **définition** (`async def _send_main_menu`) — aucun appel `await _send_main_menu(...)` dans `add_path`, `cfg_entry_file`, `cfg_cancel`, `action_add_cancel`. Si des appels subsistent, les supprimer.

- [ ] **Step 2 : Décider du sort de `_send_main_menu`**

Si `grep -n "_send_main_menu" tgbot/bot.py` ne montre **plus que la définition**, le helper n'est plus appelé nulle part. Le supprimer entièrement.

Si d'autres usages existent (autres flux hors scope), laisser tel quel.

- [ ] **Step 3 : Smoke test final intégré**

Démarrer le bot. Dans Telegram, exécuter dans l'ordre :
1. Cliquer "📂 Projets" → ajouter un projet test → vérifier menu propre.
2. Cliquer "🚀 Actions" → ajouter une action test → vérifier menu propre.
3. `/config <projet_test>` → configurer → vérifier menu propre.
4. Au milieu d'un wizard d'action, cliquer "📂 Projets" → la nav doit fonctionner (le wizard est implicitement annulé).
5. Vérifier dans `db` (via `/projects` et `/actions`) que toutes les entrées sont bien persistées.
6. Aucun message "Cancelled.", "✅ Ajouté", ou autre confirmation persistante ne reste dans le fil — uniquement le menu courant et le contexte de navigation.

- [ ] **Step 4 : Commit final (s'il y a des modifs cleanup)**

```bash
git add tgbot/bot.py
git commit -m "chore(bot): remove unused _send_main_menu helper post-wizard refactor"
```

(Si rien à committer après Step 3, sauter ce step.)

---

## Self-Review (effectué)

**Spec coverage :**
- `_wizard_step` / `_wizard_finish` → Task 1 ✓
- `wiz:cancel` route → Task 2 Step 2 ✓
- Suppression message user → présent dans tous les handlers texte (Tasks 3, 4, 5) ✓
- 3 flux refondus → Tasks 3, 4, 5 ✓
- Bouton "Passer" sur cwd → Task 4 Step 6 ✓
- Retour direct au menu (pas de confirmation persistante) → Task 4 Step 7, Task 3 Step 4 ✓
- Edge case message ancien/supprimé → fallback dans `_wizard_step` Task 1 ✓
- Navigation menu pendant wizard → `_wizard_escape` Task 2 ✓
- Suppression de `_send_main_menu` dans les flux → Tasks 3-5 (le helper n'est plus appelé) + Task 6 nettoyage ✓

**Placeholder scan :** Aucun TBD/TODO/"add appropriate handling". Tous les blocs de code sont concrets.

**Type consistency :** `_wizard_step(update, ctx, text, extra_rows=None)` signature stable, `_wizard_finish(update, ctx)` aussi. Constantes d'états cohérentes après extension `CFG_SELECT, CFG_START_CMD, CFG_ENTRY_FILE = range(3)`.
