# Design — Macros shell nommées

Date : 2026-05-14
Branche cible : nouvelle branche `feat/shell-macros` (depuis `main`)

## Contexte

Le bot dispose d'un mode shell racine (`💻 Shell` dans le menu admin) qui permet d'exécuter des commandes shell ad-hoc. Mais certaines séquences reviennent souvent (déployer le bot, redémarrer un service, faire un backup, regarder l'état disque + RAM). Les retaper à chaque fois est pénible — et impossible sur mobile.

## Objectif

Ajouter un système de **macros shell nommées** : l'utilisateur enregistre une séquence shell sous un alias (ex: `deploy = git pull && systemctl restart bot`), puis la lance en un clic depuis le menu admin. CRUD complet (add / edit / delete) via wizards Telegram, stockage SQLite, exécution via le `ShellRunner` existant.

## Non-objectifs

- Pas de variables / templating dans les macros (pas de `{{branch}}` interpolé au runtime). Si besoin plus tard, ce sera une feature séparée.
- Pas de scheduling automatique (les macros sont déclenchées manuellement). Si l'utilisateur veut une macro planifiée, il pourra la lancer depuis le scheduler existant en référençant son nom — hors scope ici.
- Pas d'export/import des macros (pas de `macros.toml`). Le stockage est exclusivement dans `projects.db`.
- Pas de partage multi-utilisateurs : les macros sont communes à tous les `allowed_user_ids` (même pattern que projets et actions).
- Pas de PTY / commande interactive (`nano`, `top`) — limite héritée du `ShellRunner` (timeout natif).
- Pas de stream live de la sortie : le bot édite le message final avec la sortie complète (comme le shell mode existant).

## Décisions

| Sujet                | Choix                                                                     |
| -------------------- | ------------------------------------------------------------------------- |
| Placement bouton     | Entrée dédiée `🧷 Macros` dans le menu admin                              |
| CRUD                 | Wizards Telegram (ConversationHandler) pour add/edit ; bouton 2-clics delete |
| Stockage             | Nouvelle table `shell_macros` dans `projects.db`                          |
| Schéma               | id, name UNIQUE, command, cwd (nullable = BOT_ROOT), created_at, last_run_at |
| Exécution            | `_shell_runner.run(cmd, cwd=macro.cwd or BOT_ROOT)` (réutilise l'existant) |
| Affichage résultat   | Single-message édité, header `✓` ou `✗` + rc + durée, output en `<pre>`  |
| Validation nom       | Slug-like (`[a-z0-9-]+`), 1–32 chars, unicité enforced par DB             |
| Validation commande  | Non vide, ≤ 4000 caractères (limite Telegram message)                     |
| Validation cwd       | `None` ou chemin absolu qui existe au moment du add/edit                  |
| Tri liste            | `last_run_at DESC NULLS LAST, created_at DESC` (récent en haut)           |
| Auth                 | Hérité de `restricted(cfg.allowed_user_ids)` sur `on_callback`            |

## UX

### 1. Entrée dans la liste

`/menu` → ⚙️ Admin → 🧷 Macros (callback `admin:macros:list`).

```
🧷 Macros (3)

Récentes en premier. Clique pour lancer.
```

Boutons inline :
```
[ deploy            ]
[ backup-bot        ]
[ free              ]
[ ➕ Ajouter        ]
[ ⬅️ Retour         ]
```

Si liste vide :
```
🧷 Macros (0)

Aucune macro enregistrée.
```
Avec uniquement `[ ➕ Ajouter ]` et `[ ⬅️ Retour ]`.

### 2. Confirmation avant run

Clic sur `deploy` → callback `macro:show:<id>` :

```
🧷 deploy
📁 /home/pi/teleProjectManager
$ git pull
  systemctl restart bot

Dernier run : il y a 2h (rc=0)
```

Boutons : `[▶️ Run]` `[✏️ Edit]` `[🗑 Delete]` `[⬅️ Retour]`

Le double-affichage `cwd: …` + `$ <commande>` est rendu en `<pre>` ; les commandes multi-lignes sont affichées intactes.

### 3. Exécution

Clic sur `▶️ Run` → callback `macro:exec:<id>` :

a. Le message est édité en `⏳ deploy en cours…` avec un seul bouton `[⬅️ Retour]`.

b. `_shell_runner.run(macro.command, cwd=resolved_cwd, timeout=cfg.shell_timeout)`.

c. À la fin, le message est ré-édité :

```
✓ deploy (rc=0 — 1.4s)
📁 /home/pi/teleProjectManager

<pre>
Already up to date.
Stopping bot... done.
Starting bot... done.
</pre>
```

Boutons : `[▶️ Re-run]` `[🧷 Liste]` `[⬅️ Retour]`

En cas d'échec (rc ≠ 0) : header `✗ deploy (rc=1 — 0.3s)` (croix rouge), output identique. Pas d'alerte / pas de notification spéciale.

Truncation output : `strip_ansi` + `truncate_output` (helpers existants de `shell_mode.py`, limite 3500 chars).

### 4. Add wizard

Bouton `➕ Ajouter` → callback `macro:add` → entre un `ConversationHandler` à 3 états :

1. **MACRO_NAME** : "Nom de la macro ? (kebab-case, 1–32 caractères)"
   - Validation : regex `^[a-z0-9][a-z0-9-]{0,31}$`. Si invalide : message d'erreur + reste dans l'état.
   - Unicité : si nom déjà pris → message d'erreur explicite + reste dans l'état.

2. **MACRO_CMD** : "Commande shell ? (multi-lignes acceptées, ≤ 4000 chars)"
   - Validation : non vide. Si invalide : message + reste dans l'état.

3. **MACRO_CWD** : "Cwd ? Envoie `.` pour la racine du bot, ou un chemin absolu existant."
   - `.` ou vide → cwd = `None` (stocké null, résolu à BOT_ROOT au runtime).
   - Sinon : `os.path.isdir(path)` doit être vrai. Sinon : message d'erreur + reste dans l'état.

4. Confirmation : preview de la macro (même rendu que step 2 de l'UX), boutons `[✅ Créer]` `[❌ Annuler]`.

5. Sur création : retour à la liste avec la nouvelle macro en tête.

Tous les états supportent un bouton `[❌ Annuler]` global (callback `wiz:cancel`, hérité du pattern existant).

### 5. Edit wizard

Clic `✏️ Edit` → callback `macro:edit:<id>` :

Premier écran : sélecteur de champ.

```
✏️ Éditer "deploy"
Que modifier ?
```

Boutons : `[ Nom ]` `[ Commande ]` `[ Cwd ]` `[ ⬅️ Retour ]`

Chaque bouton entre un `ConversationHandler` à un seul état (même validation que add). Confirme → DB update → retour au show panel.

### 6. Delete (2 clics)

Clic `🗑 Delete` → callback `macro:delask:<id>` :

```
🗑 Supprimer "deploy" ?
```

Boutons : `[ ✅ Oui ]` (callback `macro:del:<id>`) `[ ❌ Non ]` (callback `macro:show:<id>`).

Confirme → DB delete → retour liste.

## Architecture

### Nouveau module : `tgbot/macros.py`

```python
"""SQLite store for named shell macros."""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS shell_macros (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    command      TEXT NOT NULL,
    cwd          TEXT,
    created_at   TEXT NOT NULL,
    last_run_at  TEXT
);
"""


@dataclass
class Macro:
    id: int
    name: str
    command: str
    cwd: Optional[str]
    created_at: str
    last_run_at: Optional[str]


class MacrosDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self): ...

    def add(self, *, name: str, command: str, cwd: Optional[str]) -> Optional[int]:
        """Returns new id, or None if name already taken (IntegrityError caught)."""

    def get(self, macro_id: int) -> Optional[Macro]: ...
    def get_by_name(self, name: str) -> Optional[Macro]: ...
    def list(self) -> list[Macro]:
        """Ordered by last_run_at DESC NULLS LAST, created_at DESC."""

    def update(self, macro_id: int, *,
               name: Optional[str] = None,
               command: Optional[str] = None,
               cwd: Optional[str] = None) -> bool:
        """At least one field must be provided. Returns False if no row updated
        (id not found) or IntegrityError on name collision."""

    def touch(self, macro_id: int) -> None:
        """Update last_run_at to now."""

    def delete(self, macro_id: int) -> bool: ...
```

**Notes** :
- Pattern miroir de `SchedulerDB` (même `_conn` contextmanager, même init).
- Cohabite avec les autres tables dans `projects.db` (idempotent `CREATE IF NOT EXISTS`).
- `update(cwd=None)` est ambigu : il faut un sentinel pour distinguer "ne pas toucher" de "remettre à None". Solution : utiliser un sentinel module-level `_UNSET = object()` et signatures `cwd: Optional[str] | object = _UNSET`. Pratique : on ne change `cwd` que si != `_UNSET`.

### Intégration dans `bot.py`

**Import & init** :
```python
from .macros import MacrosDB, Macro

# dans build_app(), après db = DB(...) :
macros_db = MacrosDB(cfg.data_dir / "projects.db")
```

**Bouton dans `_admin_menu_markup`** : nouvelle ligne après Shell, avant Restart (ou après Health Pi si la branche healthcheck est mergée).

**Validation helpers** (purs, dans `tgbot/macros.py`) :
```python
import re
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

def is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))

def is_valid_command(cmd: str) -> bool:
    return 0 < len(cmd) <= 4000
```

**Wizard states** :
```python
MACRO_NAME, MACRO_CMD, MACRO_CWD, MACRO_CONFIRM = range(N, N + 4)
MACRO_EDIT_NAME, MACRO_EDIT_CMD, MACRO_EDIT_CWD = range(N + 4, N + 7)
```
Où `N` continue la numérotation existante des autres états ConversationHandler.

**Routing dans `on_callback`** :

| `callback_data`             | Action                                  |
| --------------------------- | --------------------------------------- |
| `admin:macros:list`         | Render liste                            |
| `macro:show:<id>`           | Render panel détail                     |
| `macro:exec:<id>`           | Exécute + render résultat               |
| `macro:edit:<id>`           | Render sélecteur de champ à éditer      |
| `macro:edit:<id>:name`      | Entre wizard MACRO_EDIT_NAME            |
| `macro:edit:<id>:cmd`       | Entre wizard MACRO_EDIT_CMD             |
| `macro:edit:<id>:cwd`       | Entre wizard MACRO_EDIT_CWD             |
| `macro:delask:<id>`         | Confirmation supprimer                  |
| `macro:del:<id>`            | Supprime + retour liste                 |
| `macro:add`                 | Entre wizard MACRO_NAME                 |

### Sécurité

- Tous les handlers sont sous `restricted(cfg.allowed_user_ids)` via `on_callback`.
- Le contenu de la commande n'est pas filtré : un utilisateur autorisé peut faire ce qu'il veut (cohérent avec le shell mode existant).
- `cwd` doit être absolu et existant lors de l'add/edit (pas de vérification au runtime — si le dir est supprimé entre temps, `ShellRunner` retournera une erreur normale).
- `name` est slug-only donc pas d'injection dans le callback_data (qui a une limite de 64 bytes Telegram).

### Persistance & migration

- Pas de migration nécessaire : nouvelle table créée idempotamment au boot.
- Coexistence : `MacrosDB.__init__` est appelé après `DB(projects.db)` ; les `CREATE IF NOT EXISTS` sont sûrs.

## Tests

Fichier : `tests/test_macros_db.py`. Tests purs sur la DB, pas de Telegram mocké.

- **`test_init_creates_table`** : après instanciation, table `shell_macros` existe.
- **`test_add_returns_id`** : add → retourne un id > 0.
- **`test_add_duplicate_name_returns_none`** : 2e add avec même nom → `None`.
- **`test_get_by_id`** : add → get(id) retourne Macro avec bonnes valeurs.
- **`test_get_by_name`** : add → get_by_name → même résultat.
- **`test_get_missing_returns_none`** : get(999) → `None`.
- **`test_list_empty`** : DB neuve → list() == [].
- **`test_list_orders_by_recency`** : 3 add + 1 touch sur le 2e → list()[0].id == 2.
- **`test_update_single_field`** : update(id, command="...") → get reflète, autres champs intacts.
- **`test_update_cwd_to_null`** : update(id, cwd=None) → cwd devient NULL (test du sentinel `_UNSET` vs explicit `None`).
- **`test_update_name_to_existing_fails`** : update(id, name=other_name_taken) → False.
- **`test_update_unknown_id_returns_false`** : update(999, ...) → False.
- **`test_touch_updates_last_run`** : add → touch(id) → list()[0].last_run_at != None.
- **`test_delete_returns_true`** : add + delete → True ; re-delete → False.
- **`test_is_valid_name`** : `"deploy"` OK, `"DEPLOY"` KO, `"a"*33` KO, `"-foo"` KO, vide KO.
- **`test_is_valid_command`** : non vide / max len.

**Pas de test des handlers Telegram** (cohérent avec la convention du projet ; le wiring est validé manuellement).

## Fichiers modifiés

| Action | Fichier                                                              | Pourquoi                                   |
| ------ | -------------------------------------------------------------------- | ------------------------------------------ |
| ➕     | `tgbot/macros.py`                                                    | MacrosDB + Macro + validators              |
| ➕     | `tests/test_macros_db.py`                                            | Tests unitaires                            |
| ✏️     | `tgbot/bot.py`                                                       | Bouton, callbacks, wizards add/edit/delete |
| ➕     | `docs/superpowers/specs/2026-05-14-shell-macros-design.md`           | Ce document                                |
| ➕     | `docs/superpowers/plans/2026-05-14-shell-macros.md`                  | Plan d'exécution                           |
