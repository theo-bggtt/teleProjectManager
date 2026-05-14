# Shell Macros Implementation Plan

**Goal :** CRUD complet de macros shell nommées via le menu admin, exécution en un clic via le `ShellRunner` existant.

**Architecture :** Nouveau module `tgbot/macros.py` (CRUD SQLite + validators purs). Nouvelle table `shell_macros` dans `projects.db`, initialisée idempotamment. `tgbot/bot.py` reçoit un bouton admin, une cascade de callbacks `macro:*` / `admin:macros:list`, et trois `ConversationHandler` (add, edit single-field, delete confirmation).

**Tech Stack :** Python 3.12+, sqlite3 stdlib, python-telegram-bot v21, pytest.

**Spec :** `docs/superpowers/specs/2026-05-14-shell-macros-design.md`

---

## File Structure

| Action | Fichier                                  | Responsabilité                              |
| ------ | ---------------------------------------- | ------------------------------------------- |
| ➕     | `tgbot/macros.py`                        | `Macro`, `MacrosDB`, validators (`is_valid_name`, `is_valid_command`) |
| ➕     | `tests/test_macros_db.py`                | Tests unitaires CRUD + validators           |
| ✏️     | `tgbot/bot.py`                           | Bouton, callbacks, wizards                  |

---

## Task 1 : Validators purs

- [ ] **Step 1.1 : Tests d'abord**
  ```python
  from tgbot.macros import is_valid_name, is_valid_command

  def test_is_valid_name_ok():
      assert is_valid_name("deploy")
      assert is_valid_name("backup-bot")
      assert is_valid_name("a")

  def test_is_valid_name_rejects():
      assert not is_valid_name("")
      assert not is_valid_name("DEPLOY")
      assert not is_valid_name("-foo")
      assert not is_valid_name("a" * 33)
      assert not is_valid_name("with space")

  def test_is_valid_command_ok():
      assert is_valid_command("ls")
      assert is_valid_command("a" * 4000)

  def test_is_valid_command_rejects():
      assert not is_valid_command("")
      assert not is_valid_command("a" * 4001)
  ```

- [ ] **Step 1.2 : Implémentation**
  - Regex `^[a-z0-9][a-z0-9-]{0,31}$` pour `is_valid_name`.
  - `is_valid_command` : `0 < len(cmd) <= 4000`.

---

## Task 2 : MacrosDB — init, add, get

- [ ] **Step 2.1 : Tests**
  ```python
  import sqlite3
  from tgbot.macros import MacrosDB, Macro

  def test_init_creates_table(tmp_db_path):
      MacrosDB(tmp_db_path)
      c = sqlite3.connect(tmp_db_path)
      names = {r[0] for r in c.execute(
          "SELECT name FROM sqlite_master WHERE type='table'"
      )}
      assert "shell_macros" in names

  def test_add_returns_id(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      mid = m.add(name="deploy", command="git pull", cwd=None)
      assert mid is not None and mid > 0

  def test_add_duplicate_returns_none(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      assert m.add(name="x", command="a", cwd=None) is not None
      assert m.add(name="x", command="b", cwd=None) is None

  def test_get_by_id(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      mid = m.add(name="x", command="ls", cwd="/tmp")
      macro = m.get(mid)
      assert macro is not None
      assert macro.name == "x"
      assert macro.command == "ls"
      assert macro.cwd == "/tmp"
      assert macro.last_run_at is None

  def test_get_missing(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      assert m.get(999) is None

  def test_get_by_name(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      m.add(name="foo", command="ls", cwd=None)
      assert m.get_by_name("foo") is not None
      assert m.get_by_name("nope") is None
  ```

- [ ] **Step 2.2 : Implémentation**
  - Schema, `_conn` contextmanager, `__init__` qui exécute SCHEMA, `add` qui catch `IntegrityError`, `get`, `get_by_name`.

---

## Task 3 : MacrosDB — list, touch, delete

- [ ] **Step 3.1 : Tests**
  ```python
  def test_list_empty(tmp_db_path):
      assert MacrosDB(tmp_db_path).list() == []

  def test_list_orders_recent_first(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      a = m.add(name="a", command="x", cwd=None)
      b = m.add(name="b", command="x", cwd=None)
      c = m.add(name="c", command="x", cwd=None)
      m.touch(b)  # b devient le plus récemment exécuté
      ids = [x.id for x in m.list()]
      assert ids[0] == b
      # Les non-touchés tombent ensuite, triés par created_at DESC
      assert set(ids[1:]) == {a, c}

  def test_touch_unknown_is_noop(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      m.touch(999)  # doit pas raise

  def test_delete(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      mid = m.add(name="x", command="y", cwd=None)
      assert m.delete(mid) is True
      assert m.delete(mid) is False
      assert m.get(mid) is None
  ```

- [ ] **Step 3.2 : Implémentation**
  - `list()` : `ORDER BY last_run_at DESC NULLS LAST, created_at DESC`.
  - `touch(id)` : UPDATE last_run_at, ignore rowcount.
  - `delete(id)` : retourne `cur.rowcount > 0`.

---

## Task 4 : MacrosDB — update avec sentinel

- [ ] **Step 4.1 : Tests**
  ```python
  def test_update_single_field(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      mid = m.add(name="x", command="a", cwd="/tmp")
      assert m.update(mid, command="b") is True
      macro = m.get(mid)
      assert macro.command == "b"
      assert macro.cwd == "/tmp"  # inchangé

  def test_update_cwd_to_null(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      mid = m.add(name="x", command="a", cwd="/tmp")
      assert m.update(mid, cwd=None) is True
      assert m.get(mid).cwd is None

  def test_update_unknown_id(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      assert m.update(999, command="x") is False

  def test_update_name_collision(tmp_db_path):
      m = MacrosDB(tmp_db_path)
      mid = m.add(name="x", command="a", cwd=None)
      m.add(name="y", command="b", cwd=None)
      assert m.update(mid, name="y") is False  # déjà pris
      assert m.get(mid).name == "x"  # inchangé
  ```

- [ ] **Step 4.2 : Implémentation**
  - Sentinel `_UNSET = object()`. Signature `update(macro_id, *, name=_UNSET, command=_UNSET, cwd=_UNSET)`.
  - Construire dynamiquement la liste de colonnes à modifier.
  - Au moins un champ doit être fourni — sinon return False (ou raise ValueError, à voir).
  - Catch `IntegrityError` sur collision de nom → return False.

---

## Task 5 : Wire dans bot.py — init + bouton + liste

- [ ] **Step 5.1 : Import et instanciation**
  ```python
  from .macros import MacrosDB, Macro, is_valid_name, is_valid_command

  # dans build_app :
  macros_db = MacrosDB(cfg.data_dir / "projects.db")
  ```

- [ ] **Step 5.2 : Bouton dans `_admin_menu_markup`**
  - Ajouter une ligne `[InlineKeyboardButton("🧷 Macros", callback_data="admin:macros:list")]`
    après Shell, avant Restart.

- [ ] **Step 5.3 : Helper render liste**
  ```python
  def _macros_list_markup(macros: list[Macro]) -> InlineKeyboardMarkup:
      rows = [[InlineKeyboardButton(m.name, callback_data=f"macro:show:{m.id}")]
              for m in macros]
      rows.append([InlineKeyboardButton("➕ Ajouter", callback_data="macro:add")])
      rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="menu:admin")])
      return InlineKeyboardMarkup(rows)

  def _macros_list_text(macros: list[Macro]) -> str:
      if not macros:
          return "🧷 <b>Macros</b>\n\nAucune macro enregistrée."
      return f"🧷 <b>Macros</b> ({len(macros)})\n\nRécentes en premier. Clique pour lancer."
  ```

- [ ] **Step 5.4 : Branche callback `admin:macros:list`**
  - Exact-match dans `on_callback`, charge `macros_db.list()`, edit le message.

- [ ] **Step 5.5 : Vérification manuelle**
  - Lancer le bot, ouvrir 🧷 Macros → liste vide affichée correctement, bouton Ajouter visible.

---

## Task 6 : Show panel + delete

- [ ] **Step 6.1 : Helper rendering panel détail**
  - Texte : nom, cwd résolu, commande en `<pre>`, dernier run formaté.
  - Boutons : Run / Edit / Delete / Retour liste.

- [ ] **Step 6.2 : Callbacks `macro:show:<id>`, `macro:delask:<id>`, `macro:del:<id>`**
  - Pattern `parts = data.split(":", 3)` pour récupérer l'id.
  - Delete : 2-clics. `macro:delask:<id>` → confirmation ; `macro:del:<id>` → DB delete + retour liste.

- [ ] **Step 6.3 : Vérification manuelle**
  - Ajouter manuellement une macro en SQL (`sqlite3 data/projects.db`), tester show et delete.

---

## Task 7 : Run macro

- [ ] **Step 7.1 : Callback `macro:exec:<id>`**
  - Charger macro, résoudre cwd (`macro.cwd or BOT_ROOT`).
  - Édite message en "⏳ <name> en cours…".
  - `rc, out = await _shell_runner.run(macro.command, cwd=resolved_cwd)`.
  - `cleaned = strip_ansi(out); truncated = truncate_output(cleaned)`.
  - `macros_db.touch(macro.id)`.
  - Édite message avec header `✓` ou `✗`, durée, output en `<pre>`.
  - Boutons : Re-run / Liste / Retour.

- [ ] **Step 7.2 : Vérification manuelle**
  - Run une macro simple (`ls`) → output affiché, rc=0.
  - Run une macro qui échoue (`false` ou commande inexistante) → rc visible, pas de crash.
  - Re-run → nouveau timestamp.

---

## Task 8 : Add wizard

- [ ] **Step 8.1 : Déclarer les états**
  - Trouver le `range(...)` le plus haut dans `bot.py`, continuer la numérotation.
  - `MACRO_NAME, MACRO_CMD, MACRO_CWD, MACRO_CONFIRM = range(N, N+4)`

- [ ] **Step 8.2 : Handlers d'état**
  - `cmd_macro_add_entry` (callback `macro:add`) → demande le nom, retourne `MACRO_NAME`.
  - `cmd_macro_add_name` (MessageHandler dans state MACRO_NAME) → valide via `is_valid_name`, vérifie unicité via `macros_db.get_by_name`, stocke dans `ctx.chat_data`, demande commande, retourne MACRO_CMD.
  - Idem pour cmd, cwd.
  - Confirm state : preview + boutons Créer/Annuler. Sur Créer : `macros_db.add(...)` + render liste.

- [ ] **Step 8.3 : ConversationHandler enregistré**
  - Entry point : `CallbackQueryHandler(cmd_macro_add_entry, pattern=r"^macro:add$")`.
  - States : map state → MessageHandler / CallbackQueryHandler.
  - Fallbacks : `wiz:cancel` global.

- [ ] **Step 8.4 : Vérification manuelle**
  - Add macro via wizard : nom invalide refusé + reste dans l'état ; nom dupliqué refusé ; cmd vide refusé ; cwd inexistant refusé ; confirm → macro en tête de liste.

---

## Task 9 : Edit wizard (single-field)

- [ ] **Step 9.1 : Sélecteur de champ**
  - Callback `macro:edit:<id>` → édite le message avec boutons Nom / Commande / Cwd / Retour.

- [ ] **Step 9.2 : 3 mini-wizards (un état chacun)**
  - `macro:edit:<id>:name` → entre `MACRO_EDIT_NAME`, attend un message texte, valide, update, retour show panel.
  - Idem pour cmd et cwd.

- [ ] **Step 9.3 : Vérification manuelle**
  - Éditer le nom : collision détectée. Éditer la commande : sauvegarde OK. Éditer cwd → `.` remet à NULL.

---

## Task 10 : Vérification finale

- [ ] **Step 10.1 : Tests**
  ```
  pytest -q
  ```
  Tous les tests verts (anciens + macros).

- [ ] **Step 10.2 : Test E2E manuel**
  - Add `free` = `df -h && free -h` (cwd `.`). Run → output visible. Re-run depuis l'écran résultat. Edit la commande. Delete avec confirmation.

- [ ] **Step 10.3 : Commit**
  ```
  feat(macros): named shell macros with CRUD wizards from admin menu

  Adds tgbot/macros.py (MacrosDB + Macro + validators), table shell_macros
  in projects.db, and a "🧷 Macros" entry in the admin menu. Each macro is
  add/edit/delete via Telegram wizards and runs through the existing
  ShellRunner with cwd resolution to BOT_ROOT when unset.
  ```

- [ ] **Step 10.4 : Push + PR `feat/shell-macros` → `main`** (sur demande utilisateur).
