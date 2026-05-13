# Root Shell Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ajouter un mode "shell" administrateur où chaque message Telegram est exécuté comme commande shell dans un cwd navigable, démarrant à la racine du bot, jusqu'à fermeture explicite ou 10 min d'inactivité.

**Architecture:** Un nouveau module `tgbot/shell_mode.py` contient les helpers purs (résolution de `cd`, troncature, strip ANSI) et un store de sessions en mémoire. `tgbot/bot.py` est étendu avec un bouton dans le menu admin, deux branches de callback dans `on_callback`, un `MessageHandler` en `group=-1` pour intercepter les messages en mode shell, et un job de cleanup périodique. Aucune persistance DB.

**Tech Stack:** Python 3.12+, python-telegram-bot v21, pytest, asyncio (existant). Réutilise `tgbot/shell.py::ShellRunner` pour l'exécution des commandes.

**Référence spec :** `docs/superpowers/specs/2026-05-14-root-shell-mode-design.md`

---

## File Structure

| Action | Fichier | Responsabilité |
| ------ | ------- | -------------- |
| ➕ | `tgbot/shell_mode.py` | Module shell mode : `ShellSession`, `ShellSessionStore`, `resolve_cd`, `strip_ansi`, `truncate_output`, constante `DEFAULT_TIMEOUT_SECONDS` |
| ➕ | `tests/test_shell_mode.py` | Tests unitaires des helpers et du store |
| ✏️ | `tgbot/bot.py` | Bouton admin, 3 branches handlers, job cleanup, wiring |

---

## Task 1 : Helpers de formatage (`strip_ansi`, `truncate_output`)

**Files:**
- Create: `tgbot/shell_mode.py`
- Test: `tests/test_shell_mode.py`

- [ ] **Step 1.1 : Écrire les tests des helpers de formatage**

Créer `tests/test_shell_mode.py` :

```python
"""Tests for the root shell mode helpers and session store."""

import time

import pytest

from tgbot.shell_mode import strip_ansi, truncate_output


def test_strip_ansi_color_codes():
    assert strip_ansi("\x1b[31merror\x1b[0m") == "error"


def test_strip_ansi_cursor_movement():
    assert strip_ansi("hello\x1b[2K\x1b[1Aworld") == "helloworld"


def test_strip_ansi_no_ansi_unchanged():
    assert strip_ansi("plain text") == "plain text"


def test_strip_ansi_empty_string():
    assert strip_ansi("") == ""


def test_truncate_output_under_limit():
    assert truncate_output("hello", limit=100) == "hello"


def test_truncate_output_at_limit():
    text = "a" * 100
    assert truncate_output(text, limit=100) == text


def test_truncate_output_over_limit():
    text = "a" * 200
    result = truncate_output(text, limit=100)
    assert result.startswith("a" * 100)
    assert result.endswith("… (tronqué)")
    assert len(result) == 100 + len("\n… (tronqué)")
```

- [ ] **Step 1.2 : Lancer les tests pour vérifier qu'ils échouent**

```bash
pytest tests/test_shell_mode.py -v
```

Attendu : `ModuleNotFoundError: No module named 'tgbot.shell_mode'`.

- [ ] **Step 1.3 : Créer `tgbot/shell_mode.py` avec les helpers minimaux**

```python
"""Root shell mode: in-memory shell sessions with navigable cwd.

This module exposes the data structures and pure helpers used by the
admin-gated "shell mode" wired up in tgbot.bot. The mode lets an admin
execute shell commands by sending plain text messages once they have
entered the mode from the admin menu.
"""

from __future__ import annotations

import re

DEFAULT_TIMEOUT_SECONDS = 600  # 10 minutes
DEFAULT_OUTPUT_LIMIT = 3500    # leaves room for header and HTML wrapping

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Strip ANSI CSI escape sequences (colors, cursor movement, etc.)."""
    return _ANSI_RE.sub("", text)


def truncate_output(text: str, limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    """Truncate `text` to `limit` characters and append a marker if cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… (tronqué)"
```

- [ ] **Step 1.4 : Vérifier que les tests passent**

```bash
pytest tests/test_shell_mode.py -v
```

Attendu : 7 tests `PASSED`.

- [ ] **Step 1.5 : Commit**

```bash
git add tgbot/shell_mode.py tests/test_shell_mode.py
git commit -m "feat(shell-mode): add ANSI strip and output truncation helpers"
```

---

## Task 2 : `ShellSession` et `ShellSessionStore`

**Files:**
- Modify: `tgbot/shell_mode.py`
- Modify: `tests/test_shell_mode.py`

- [ ] **Step 2.1 : Ajouter les tests du store**

Ajouter à la fin de `tests/test_shell_mode.py` :

```python
from tgbot.shell_mode import ShellSession, ShellSessionStore, DEFAULT_TIMEOUT_SECONDS


def test_store_start_creates_session():
    store = ShellSessionStore()
    s = store.start(user_id=42, chat_id=100, message_id=5, cwd="/tmp")
    assert s.user_id == 42
    assert s.chat_id == 100
    assert s.message_id == 5
    assert s.cwd == "/tmp"
    assert s.last_activity > 0


def test_store_get_returns_session():
    store = ShellSessionStore()
    store.start(42, 100, 5, "/tmp")
    s = store.get(42)
    assert s is not None
    assert s.user_id == 42


def test_store_get_returns_none_when_missing():
    store = ShellSessionStore()
    assert store.get(99) is None


def test_store_end_removes_session():
    store = ShellSessionStore()
    store.start(42, 100, 5, "/tmp")
    removed = store.end(42)
    assert removed is not None
    assert removed.user_id == 42
    assert store.get(42) is None


def test_store_end_returns_none_when_missing():
    store = ShellSessionStore()
    assert store.end(99) is None


def test_store_start_replaces_existing_session():
    store = ShellSessionStore()
    store.start(42, 100, 5, "/tmp")
    store.start(42, 100, 7, "/other")
    s = store.get(42)
    assert s.message_id == 7
    assert s.cwd == "/other"


def test_store_touch_updates_last_activity(monkeypatch):
    store = ShellSessionStore()
    times = iter([1000.0, 1050.0])
    monkeypatch.setattr("tgbot.shell_mode.time.monotonic", lambda: next(times))
    store.start(42, 100, 5, "/tmp")
    store.touch(42)
    assert store.get(42).last_activity == 1050.0


def test_store_touch_missing_session_noop():
    store = ShellSessionStore()
    store.touch(99)  # must not raise


def test_store_expired_returns_only_old_sessions(monkeypatch):
    store = ShellSessionStore()
    times = iter([100.0, 200.0])
    monkeypatch.setattr("tgbot.shell_mode.time.monotonic", lambda: next(times))
    store.start(1, 10, 1, "/a")  # last_activity=100
    store.start(2, 20, 2, "/b")  # last_activity=200
    expired = store.expired(now=750.0, ttl=DEFAULT_TIMEOUT_SECONDS)
    # 750 - 100 = 650 > 600 → expired
    # 750 - 200 = 550 < 600 → still active
    assert [s.user_id for s in expired] == [1]


def test_store_set_message_id_updates_panel():
    store = ShellSessionStore()
    store.start(42, 100, 5, "/tmp")
    store.set_message_id(42, 99)
    assert store.get(42).message_id == 99


def test_store_set_cwd_updates_directory():
    store = ShellSessionStore()
    store.start(42, 100, 5, "/tmp")
    store.set_cwd(42, "/var/log")
    assert store.get(42).cwd == "/var/log"
```

- [ ] **Step 2.2 : Lancer les tests, vérifier qu'ils échouent**

```bash
pytest tests/test_shell_mode.py -v
```

Attendu : 7 tests passent (helpers), 10 tests `FAILED` avec `ImportError: cannot import name 'ShellSession'`.

- [ ] **Step 2.3 : Implémenter `ShellSession` et `ShellSessionStore`**

Modifier `tgbot/shell_mode.py`. Au début, ajouter les imports manquants :

```python
import time
from dataclasses import dataclass
```

Puis, à la fin du fichier, ajouter :

```python
@dataclass
class ShellSession:
    """A live root-shell session for a single Telegram user.

    The `message_id` points at the panel message that is edited in place
    after each command; `cwd` tracks the navigable working directory and
    `last_activity` (monotonic seconds) drives the idle timeout.
    """

    user_id: int
    chat_id: int
    message_id: int
    cwd: str
    last_activity: float


class ShellSessionStore:
    """In-memory map of `user_id` -> `ShellSession`.

    PTB runs handlers serially per update by default, so a plain dict is
    safe. If `concurrent_updates` is ever enabled in the application, wrap
    mutations with an `asyncio.Lock`.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, ShellSession] = {}

    def get(self, user_id: int) -> ShellSession | None:
        return self._sessions.get(user_id)

    def start(
        self, user_id: int, chat_id: int, message_id: int, cwd: str
    ) -> ShellSession:
        session = ShellSession(
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            cwd=cwd,
            last_activity=time.monotonic(),
        )
        self._sessions[user_id] = session
        return session

    def end(self, user_id: int) -> ShellSession | None:
        return self._sessions.pop(user_id, None)

    def touch(self, user_id: int) -> None:
        s = self._sessions.get(user_id)
        if s is not None:
            s.last_activity = time.monotonic()

    def set_message_id(self, user_id: int, message_id: int) -> None:
        s = self._sessions.get(user_id)
        if s is not None:
            s.message_id = message_id

    def set_cwd(self, user_id: int, cwd: str) -> None:
        s = self._sessions.get(user_id)
        if s is not None:
            s.cwd = cwd

    def expired(
        self, now: float, ttl: float = DEFAULT_TIMEOUT_SECONDS
    ) -> list[ShellSession]:
        return [s for s in self._sessions.values() if (now - s.last_activity) > ttl]
```

- [ ] **Step 2.4 : Vérifier les tests**

```bash
pytest tests/test_shell_mode.py -v
```

Attendu : 17 tests `PASSED`.

- [ ] **Step 2.5 : Commit**

```bash
git add tgbot/shell_mode.py tests/test_shell_mode.py
git commit -m "feat(shell-mode): add ShellSession dataclass and in-memory store"
```

---

## Task 3 : Résolution de `cd` (`resolve_cd`)

**Files:**
- Modify: `tgbot/shell_mode.py`
- Modify: `tests/test_shell_mode.py`

- [ ] **Step 3.1 : Ajouter les tests de `resolve_cd`**

Ajouter à la fin de `tests/test_shell_mode.py` :

```python
import os

from tgbot.shell_mode import resolve_cd


def test_resolve_cd_absolute_valid(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    result = resolve_cd(str(tmp_path), str(sub), bot_root=str(tmp_path))
    assert result == str(sub)


def test_resolve_cd_relative_valid(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    result = resolve_cd(str(tmp_path), "sub", bot_root=str(tmp_path))
    assert result == os.path.normpath(str(sub))


def test_resolve_cd_dotdot(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    result = resolve_cd(str(sub), "..", bot_root=str(tmp_path))
    assert result == os.path.normpath(str(tmp_path))


def test_resolve_cd_invalid_path_returns_none(tmp_path):
    result = resolve_cd(str(tmp_path), "does-not-exist", bot_root=str(tmp_path))
    assert result is None


def test_resolve_cd_no_arg_returns_bot_root(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    result = resolve_cd(str(sub), None, bot_root=str(tmp_path))
    assert result == str(tmp_path)


def test_resolve_cd_empty_arg_returns_bot_root(tmp_path):
    result = resolve_cd(str(tmp_path), "", bot_root=str(tmp_path))
    assert result == str(tmp_path)


def test_resolve_cd_bot_root_missing_returns_none(tmp_path):
    # cd with no arg when bot_root doesn't exist
    missing = str(tmp_path / "nope")
    result = resolve_cd(str(tmp_path), None, bot_root=missing)
    assert result is None


def test_resolve_cd_file_not_dir_returns_none(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("hi")
    result = resolve_cd(str(tmp_path), "file.txt", bot_root=str(tmp_path))
    assert result is None
```

- [ ] **Step 3.2 : Lancer les tests, vérifier qu'ils échouent**

```bash
pytest tests/test_shell_mode.py -v
```

Attendu : 17 OK, 8 `FAILED` avec `ImportError: cannot import name 'resolve_cd'`.

- [ ] **Step 3.3 : Implémenter `resolve_cd`**

Ajouter à la fin de `tgbot/shell_mode.py`. D'abord ajouter `import os` en haut (à côté de `import time`, `import re`), puis :

```python
def resolve_cd(current_cwd: str, arg: str | None, *, bot_root: str) -> str | None:
    """Resolve a `cd` target relative to `current_cwd`.

    Returns the absolute, normalized path on success, or `None` if the
    target does not exist or is not a directory. `arg` is treated as
    absolute when it begins with a path separator; otherwise joined to
    `current_cwd`. An empty or missing `arg` returns `bot_root`.
    """
    if not arg:
        return bot_root if os.path.isdir(bot_root) else None
    target = arg if os.path.isabs(arg) else os.path.normpath(
        os.path.join(current_cwd, arg)
    )
    return target if os.path.isdir(target) else None
```

- [ ] **Step 3.4 : Vérifier les tests**

```bash
pytest tests/test_shell_mode.py -v
```

Attendu : 25 tests `PASSED`.

- [ ] **Step 3.5 : Commit**

```bash
git add tgbot/shell_mode.py tests/test_shell_mode.py
git commit -m "feat(shell-mode): add cd resolver with absolute and relative paths"
```

---

## Task 4 : Bouton "💻 Shell" dans le menu admin

**Files:**
- Modify: `tgbot/bot.py` (fonction `_admin_menu_markup` autour de la ligne 139)

- [ ] **Step 4.1 : Repérer la structure actuelle du menu admin**

```bash
grep -n "_admin_menu_markup\|admin:" tgbot/bot.py | head -30
```

Lire les lignes 139–175 (`_admin_menu_markup`) pour comprendre le layout. Le menu retourne un `InlineKeyboardMarkup` avec des `InlineKeyboardButton` regroupés par lignes.

- [ ] **Step 4.2 : Ajouter le bouton "💻 Shell" en haut du menu admin**

Dans `_admin_menu_markup`, ajouter une nouvelle ligne **avant** les actions destructives (restart, update). La nouvelle entrée doit ressembler à :

```python
[InlineKeyboardButton("💻 Shell", callback_data="admin:shell:enter")],
```

Placer cette ligne juste après la première ligne existante du clavier (typiquement après la ligne du toggle notifications). Conserver `❌ Annuler` en dernier.

- [ ] **Step 4.3 : Smoke test rapide**

Lancer le bot localement avec `python -m tgbot`, ouvrir le menu admin, vérifier que le bouton "💻 Shell" s'affiche. Cliquer dessus : il ne se passe rien (handler pas encore branché) — c'est attendu. Arrêter le bot avec `Ctrl+C`.

- [ ] **Step 4.4 : Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(shell-mode): add '💻 Shell' button to admin menu"
```

---

## Task 5 : Handlers `admin:shell:enter` et `shell:exit`

**Files:**
- Modify: `tgbot/bot.py` — ajout d'un import + d'un store global + deux nouvelles branches dans `on_callback`

- [ ] **Step 5.1 : Importer le module et créer le store global**

En haut de `tgbot/bot.py`, à côté de `from .shell import ShellRunner` (ligne 36), ajouter :

```python
from html import escape as html_escape

from .shell_mode import (
    ShellSession,
    ShellSessionStore,
    resolve_cd,
    strip_ansi,
    truncate_output,
)
```

Toujours en haut du module, après les imports, ajouter :

```python
BOT_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
shell_sessions = ShellSessionStore()
```

(Si `os` n'est pas déjà importé en haut du fichier, l'ajouter.)

- [ ] **Step 5.2 : Ajouter les helpers de rendu du panel**

Juste avant `def on_callback(...)` (ligne 650), ajouter :

```python
def _shell_panel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Quitter shell", callback_data="shell:exit")]]
    )


def _shell_closed_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Retour menu admin", callback_data="menu:admin")]]
    )


def _render_shell_panel(cwd: str, command: str | None, output: str | None) -> str:
    """Render the live shell-mode panel as HTML."""
    parts = [
        "🟢 <b>SHELL ACTIF</b>",
        f"📁 <code>{html_escape(cwd)}</code>",
        "",
    ]
    if command is None and output is None:
        parts.append("<i>Envoie une commande…</i>")
    else:
        if command is not None:
            parts.append(f"<b>$</b> <code>{html_escape(command)}</code>")
        if output is not None and output != "":
            parts.append(f"<pre>{html_escape(output)}</pre>")
        elif command is not None:
            parts.append("<i>(pas de sortie)</i>")
    return "\n".join(parts)
```

- [ ] **Step 5.3 : Brancher `admin:shell:enter` et `shell:exit` dans `on_callback`**

Localiser dans `on_callback` (commence ligne 650) l'endroit où les `data == "menu:admin"` ou `target == "admin"` sont gérés. Ajouter des branches supplémentaires pour les deux nouveaux callbacks.

Approche : juste avant le `if data == "menu:..."` global, ajouter le bloc suivant. Adapter au pattern de dispatch existant (le code actuel utilise `target` extrait de `data.split(":")` — répliquer cette convention) :

```python
if data == "admin:shell:enter":
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    msg = update.callback_query.message
    text = _render_shell_panel(BOT_ROOT, command=None, output=None)
    await msg.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_shell_panel_markup(),
    )
    shell_sessions.start(
        user_id=user_id,
        chat_id=chat_id,
        message_id=msg.message_id,
        cwd=BOT_ROOT,
    )
    await update.callback_query.answer()
    return

if data == "shell:exit":
    user_id = update.effective_user.id
    shell_sessions.end(user_id)
    await update.callback_query.message.edit_text(
        "🔴 Shell fermé.",
        reply_markup=_shell_closed_markup(),
    )
    await update.callback_query.answer()
    return
```

(Vérifier que `ParseMode` est déjà importé en haut de `bot.py`. Si non, ajouter `from telegram.constants import ParseMode`.)

- [ ] **Step 5.4 : Smoke test entrée/sortie**

Relancer le bot, ouvrir le menu admin, cliquer sur "💻 Shell". Le panel doit s'afficher avec le bon `cwd` (`/.../teleProjectManager`) et le bouton "❌ Quitter shell". Cliquer dessus, vérifier que le panel devient "🔴 Shell fermé." avec le bouton retour. Cliquer "↩️ Retour menu admin", confirmer que le menu admin classique réapparaît.

- [ ] **Step 5.5 : Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(shell-mode): wire enter/exit callbacks for shell panel"
```

---

## Task 6 : `MessageHandler` shell — exécution et `cd`

**Files:**
- Modify: `tgbot/bot.py`

- [ ] **Step 6.1 : Définir `on_shell_message`**

Juste après les helpers de rendu (Step 5.2), ajouter la fonction handler. Elle est wrappée par `restricted(...)` au moment du enregistrement (Step 6.3), donc ici elle reçoit déjà un user autorisé.

```python
async def on_shell_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Execute incoming text as a shell command when the user is in shell mode.

    Registered in group=-1. If the user has no active shell session, returns
    immediately so the message propagates to lower-priority handlers
    (wizards, etc.). Otherwise raises ApplicationHandlerStop to prevent
    propagation.
    """
    user_id = update.effective_user.id
    session = shell_sessions.get(user_id)
    if session is None:
        return  # not in shell mode → let other handlers run

    command = (update.message.text or "").strip()
    if not command:
        raise ApplicationHandlerStop

    shell_sessions.touch(user_id)

    # Intercept `cd` so cwd persists between commands.
    if command == "cd" or command.startswith("cd "):
        arg = command[3:].strip() if command.startswith("cd ") else None
        new_cwd = resolve_cd(session.cwd, arg, bot_root=BOT_ROOT)
        if new_cwd is None:
            display = arg if arg else ""
            await _edit_shell_panel(
                ctx,
                session,
                command=command,
                output=f"cd: {display}: dossier introuvable",
            )
        else:
            shell_sessions.set_cwd(user_id, new_cwd)
            await _edit_shell_panel(
                ctx,
                shell_sessions.get(user_id),
                command=command,
                output=None,
            )
        raise ApplicationHandlerStop

    # Real command: run through the existing ShellRunner.
    rc, out = await _shell_runner.run(command, cwd=session.cwd)
    cleaned = strip_ansi(out)
    truncated = truncate_output(cleaned)
    suffix = "" if rc == 0 else f"\n[exit {rc}]"
    await _edit_shell_panel(
        ctx,
        session,
        command=command,
        output=(truncated + suffix) if truncated else suffix.lstrip(),
    )
    raise ApplicationHandlerStop


async def _edit_shell_panel(
    ctx: ContextTypes.DEFAULT_TYPE,
    session: ShellSession,
    *,
    command: str | None,
    output: str | None,
):
    """Edit the shell panel message in place. Falls back to a new message
    if the original cannot be edited (e.g. >48h old)."""
    text = _render_shell_panel(session.cwd, command=command, output=output)
    try:
        await ctx.bot.edit_message_text(
            chat_id=session.chat_id,
            message_id=session.message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=_shell_panel_markup(),
        )
    except Exception:
        sent = await ctx.bot.send_message(
            chat_id=session.chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=_shell_panel_markup(),
        )
        shell_sessions.set_message_id(session.user_id, sent.message_id)
```

- [ ] **Step 6.2 : Exposer `ShellRunner` globalement et importer `ApplicationHandlerStop`**

`ShellRunner` est aujourd'hui instancié à l'intérieur de `main()` (ligne 315). Pour que `on_shell_message` y accède sans paramètre, le hisser au niveau module :

1. Importer `ApplicationHandlerStop` en haut de `bot.py` :
   ```python
   from telegram.ext import ApplicationHandlerStop
   ```
   (Ajouter à l'import groupé existant `from telegram.ext import ...`.)
2. Juste après la création de `shell_sessions` (Step 5.1), déclarer un placeholder :
   ```python
   _shell_runner: ShellRunner | None = None
   ```
3. Dans `main()` (autour de la ligne 315), remplacer :
   ```python
   shell = ShellRunner(timeout=cfg.shell_timeout)
   ```
   par :
   ```python
   global _shell_runner
   _shell_runner = ShellRunner(timeout=cfg.shell_timeout)
   shell = _shell_runner  # keep the existing per-project /shell working
   ```

Cela laisse le `/shell <name>` existant intact (il continue d'utiliser la variable locale `shell`) tout en exposant la même instance via `_shell_runner` au module.

- [ ] **Step 6.3 : Enregistrer le `MessageHandler` en `group=-1`**

Dans `main()`, après les autres `app.add_handler(...)` mais **avant** `app.add_handler(CallbackQueryHandler(on_callback))` (ligne 1754), ajouter :

```python
app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        auth(on_shell_message),
    ),
    group=-1,
)
```

`auth` est la closure `restricted(cfg.allowed_user_ids)` déjà créée plus haut dans `main()` (ligne 316).

- [ ] **Step 6.4 : Smoke test exécution**

1. Lancer le bot, entrer en mode shell via le menu admin.
2. Envoyer `pwd` — le panel doit afficher la racine du bot.
3. Envoyer `ls` — la sortie doit s'afficher dans un bloc `<pre>`.
4. Envoyer `cd tgbot` — le header doit afficher `📁 .../teleProjectManager/tgbot`, pas de bloc de sortie.
5. Envoyer `cd ..` — retour à la racine.
6. Envoyer `cd /nope/nada` — message `cd: /nope/nada: dossier introuvable`, cwd inchangé.
7. Envoyer une commande qui échoue (`ls /nope`) — la sortie de stderr doit être visible avec un suffixe `[exit <n>]`.
8. Cliquer "❌ Quitter shell" — panel fermé, envoyer un autre message texte : il **ne** doit **pas** être ré-interprété comme commande shell (les handlers lower-priority le verront).

- [ ] **Step 6.5 : Smoke test non-interférence**

1. Sortir du mode shell.
2. Démarrer un wizard "Ajouter projet" (`menu:add`) — vérifier que la saisie du nom marche normalement.
3. Annuler ce wizard.
4. Re-rentrer en mode shell, vérifier qu'on peut taper des commandes.

- [ ] **Step 6.6 : Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(shell-mode): execute messages as shell commands with cd interception"
```

---

## Task 7 : Cleanup périodique des sessions expirées

**Files:**
- Modify: `tgbot/bot.py`

- [ ] **Step 7.1 : Définir la coroutine de cleanup**

Juste après `_edit_shell_panel` (Step 6.1), ajouter :

```python
import time as _time  # local alias to avoid shadowing in callers


async def _check_expired_shell_sessions(ctx: ContextTypes.DEFAULT_TYPE):
    """Job: close sessions idle for more than DEFAULT_TIMEOUT_SECONDS."""
    from .shell_mode import DEFAULT_TIMEOUT_SECONDS

    now = _time.monotonic()
    for session in shell_sessions.expired(now, ttl=DEFAULT_TIMEOUT_SECONDS):
        try:
            await ctx.bot.edit_message_text(
                chat_id=session.chat_id,
                message_id=session.message_id,
                text="🔴 Shell fermé (inactivité).",
                reply_markup=_shell_closed_markup(),
            )
        except Exception:
            pass  # message un-editable: still clean up the session below
        shell_sessions.end(session.user_id)
```

Si `time` est déjà importé au top de `bot.py`, supprimer l'alias `_time` et utiliser `time.monotonic()` directement.

- [ ] **Step 7.2 : Enregistrer le job dans `main()`**

Dans `main()`, après l'enregistrement des handlers et avant `app.run_polling()`, ajouter :

```python
app.job_queue.run_repeating(
    _check_expired_shell_sessions,
    interval=60,
    first=60,
    name="shell_mode_cleanup",
)
```

Si le projet n'utilise pas encore `job_queue`, vérifier que `python-telegram-bot[job-queue]` est dans `requirements.txt`. Le scheduler récemment ajouté a déjà introduit cette dépendance (APScheduler est séparé, mais PTB job-queue est livré avec PTB lui-même via l'extra).

- [ ] **Step 7.3 : Smoke test timeout**

Pour tester rapidement sans attendre 10 minutes, abaisser temporairement `DEFAULT_TIMEOUT_SECONDS` à 30 dans `tgbot/shell_mode.py` et `interval=10` dans `main()`.

1. Lancer le bot, entrer en mode shell.
2. Ne rien envoyer pendant 40 s.
3. Vérifier que le panel devient "🔴 Shell fermé (inactivité)." avec le bouton retour.
4. Restaurer `DEFAULT_TIMEOUT_SECONDS = 600` et `interval=60`.

- [ ] **Step 7.4 : Commit**

```bash
git add tgbot/bot.py
git commit -m "feat(shell-mode): auto-close idle sessions after 10 minutes"
```

---

## Task 8 : Vérification finale et lancement complet de la suite

**Files:** (lecture seule)

- [ ] **Step 8.1 : Lancer toute la suite de tests**

```bash
pytest -v
```

Attendu : tous les tests passent, dont les 25 de `tests/test_shell_mode.py` ajoutés dans Tasks 1–3.

- [ ] **Step 8.2 : Vérifier qu'aucune régression visible n'affecte le scheduler ou les wizards**

Smoke test :
- Menu principal → "Planifié" → la liste s'affiche
- Menu principal → "Ajouter projet" → wizard fonctionne
- Menu admin → "💻 Shell" → entre en mode shell, sortie de commande affichée, `cd` ok, exit ok
- Menu admin → "💻 Shell" → ne rien envoyer 10 min (ou abaissé pour test) → timeout déclenche fermeture

- [ ] **Step 8.3 : Vérifier les imports en haut de `bot.py`**

S'assurer que les ajouts suivants sont bien présents et regroupés sans doublon :

```python
import os
import time
from html import escape as html_escape

from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop

from .shell_mode import (
    ShellSession,
    ShellSessionStore,
    resolve_cd,
    strip_ansi,
    truncate_output,
)
```

- [ ] **Step 8.4 : Pousser la branche**

```bash
git push -u origin feat/shell-mode
```

(À ne lancer qu'après accord explicite de l'utilisateur — le push est une action partagée.)

---

## Self-Review

**Spec coverage :**
- ✅ Bouton admin → Task 4
- ✅ Mode hybride (bouton entrée + auto-sortie) → Tasks 5, 6, 7
- ✅ cwd navigable + `cd` intercepté → Task 6
- ✅ Single-message UX (édition continue) → Task 6 (`_edit_shell_panel`)
- ✅ Fallback nouveau message si édition impossible → Task 6 (`except Exception` dans `_edit_shell_panel`)
- ✅ Truncation 3500 chars + suffixe → Task 1 (`truncate_output`)
- ✅ Strip ANSI → Task 1 (`strip_ansi`)
- ✅ Timeout 10 min → Task 7
- ✅ `ApplicationHandlerStop` quand session active → Task 6
- ✅ Auth héritée via `restricted(cfg.allowed_user_ids)` → Task 6.3
- ✅ Tests unitaires couvrant store, helpers, cd resolver → Tasks 1–3

**Placeholder scan :** aucun "TBD", "implement later", "similar to Task N". Chaque step contient le code complet à coller.

**Type consistency :** `ShellSession.user_id` (int), `chat_id` (int), `message_id` (int), `cwd` (str), `last_activity` (float) — utilisés cohéremment dans `start`, `_edit_shell_panel`, `_check_expired_shell_sessions`. `resolve_cd` retourne `str | None` partout.

**Risque connu identifié dans la spec :** Task 6.5 (smoke test non-interférence) vérifie explicitement que les wizards ne sont pas cassés par le `MessageHandler` en `group=-1`.
