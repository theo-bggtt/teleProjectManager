# Design — Mode Shell racine

Date : 2026-05-14
Branche cible : nouvelle branche `feat/shell-mode` (depuis `main`)

## Contexte

Le bot expose déjà un `/shell <projet> <commande>` ainsi qu'un bouton "💻 Shell" sur la carte de chaque projet, qui exécutent une commande dans le `cwd` du projet via `tgbot/shell.py::ShellRunner`. Ce qui manque : un shell **hors projet**, dans le dossier du bot, pour préparer du code (`git clone`, `npm install`, etc.) avant de l'importer comme projet.

## Objectif

Depuis le menu Admin, entrer dans un mode où chaque message texte est exécuté comme commande shell dans un répertoire courant navigable, jusqu'à fermeture explicite (bouton) ou 10 minutes d'inactivité.

## Non-objectifs

- Pas de persistance DB : la session est uniquement en mémoire et perdue au restart du bot (cohabite proprement avec l'auto-update existant).
- Pas de détection automatique de nouveaux projets après `git clone` — l'import reste manuel via le wizard "Ajouter projet" existant.
- Pas de PTY ni de commandes interactives (`nano`, `top`, `vim`, etc.) — elles bloqueront et seront tuées par le timeout natif du `ShellRunner` (30 s par défaut).
- Pas de variables d'environnement persistantes : chaque commande est un nouveau subprocess, donc `export FOO=bar` n'est pas vu par la commande suivante. Le seul état persistant est le `cwd`.
- Pas de support multi-utilisateur simultané : il y a une seule session active par `user_id`. Rentrer dans le mode shell alors qu'une session existe déjà la ré-utilise (cwd conservé).

## Décisions

| Sujet              | Choix                                                    |
| ------------------ | -------------------------------------------------------- |
| Working dir initial | Racine du bot (`teleProjectManager/`)                   |
| Navigation         | `cd <path>` intercepté côté bot, met à jour `session.cwd` |
| Mode d'interaction | Hybride : bouton pour entrer, tous messages texte = commandes, bouton "Quitter" ou timeout pour sortir |
| Timeout            | 10 minutes d'inactivité                                  |
| Persistance        | In-memory uniquement, perdue au restart                  |
| Placement bouton   | Menu admin uniquement                                    |
| Affichage          | Single-message (édition continue du même message)        |
| Auth               | Hérité de `restricted(cfg.allowed_user_ids)`             |

## UX

1. Le menu admin gagne un bouton **`💻 Shell`** (callback `admin:shell:enter`).
2. Clic → le message admin est édité en panel shell :
   ```
   🟢 SHELL ACTIF
   📁 /home/pi/teleProjectManager

   _Envoie une commande…_
   ```
   avec un seul bouton `[❌ Quitter shell]` (callback `shell:exit`).
3. À chaque message texte que l'utilisateur envoie pendant la session :
   - Le panel affiche brièvement `⏳ <commande>` (état "exécution en cours").
   - La commande est exécutée via `ShellRunner` dans `session.cwd`.
   - Le panel est ré-édité avec : header (cwd à jour) + bloc de sortie + `[❌ Quitter shell]`.
4. `cd <path>` est intercepté côté bot avant de toucher au subprocess :
   - Résolution : `os.path.normpath(os.path.join(session.cwd, path))` (ou `path` si absolu).
   - Validation : `os.path.isdir(resolved)`. Si non : panel affiche `cd: <path>: dossier introuvable`, `cwd` inchangé.
   - Si OK : `session.cwd = resolved`, panel re-rendu sans bloc de sortie (juste le header mis à jour).
   - Cas particulier `cd` seul (sans arg) : retour à la racine du bot (équivalent à `cd <bot_root>`).
5. Sortie de session :
   - Clic sur **`[❌ Quitter shell]`** → panel devient :
     ```
     🔴 Shell fermé.
     [↩️ Retour menu admin]
     ```
     (le bouton renvoie sur le menu admin classique via callback `menu:admin`).
   - 10 min sans message → idem mais texte `🔴 Shell fermé (inactivité).`

## Architecture

### Nouveau module : `tgbot/shell_mode.py`

```python
"""Root shell mode: in-memory shell sessions with navigable cwd."""

import os
import time
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 600  # 10 min


@dataclass
class ShellSession:
    user_id: int
    chat_id: int
    message_id: int       # le panel à éditer
    cwd: str
    last_activity: float  # time.monotonic()


class ShellSessionStore:
    """In-memory map user_id -> ShellSession.

    Thread-safety: PTB runs handlers serially per update by default, so a
    plain dict is sufficient. If concurrent_updates is ever enabled, wrap
    with an asyncio.Lock.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, ShellSession] = {}

    def get(self, user_id: int) -> ShellSession | None:
        return self._sessions.get(user_id)

    def start(self, user_id: int, chat_id: int, message_id: int, cwd: str) -> ShellSession:
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

    def expired(self, now: float, ttl: float = DEFAULT_TIMEOUT_SECONDS) -> list[ShellSession]:
        return [s for s in self._sessions.values() if (now - s.last_activity) > ttl]


def resolve_cd(current_cwd: str, arg: str | None, bot_root: str) -> str | None:
    """Resolve a `cd` target. Returns the new cwd, or None if invalid.

    `cd` with no arg returns `bot_root`.
    """
    if not arg:
        return bot_root if os.path.isdir(bot_root) else None
    target = arg if os.path.isabs(arg) else os.path.normpath(os.path.join(current_cwd, arg))
    return target if os.path.isdir(target) else None


# Stripping ANSI escape codes — safety net even if PYTHONUNBUFFERED=1
import re
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def truncate_output(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… (tronqué)"
```

### Intégration dans `bot.py`

**Imports & setup** (haut de `register_handlers` ou équivalent) :
```python
from .shell_mode import ShellSessionStore, resolve_cd, strip_ansi, truncate_output

shell_sessions = ShellSessionStore()
BOT_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))  # teleProjectManager/
```

**Bouton dans `_admin_menu_markup`** (vers ligne 139) :
ajouter une nouvelle ligne avec `InlineKeyboardButton("💻 Shell", callback_data="admin:shell:enter")`, idéalement avant les actions destructives (restart/update).

**3 nouveaux handlers** :
- `CallbackQueryHandler(on_shell_enter, pattern=r"^admin:shell:enter$")`
- `CallbackQueryHandler(on_shell_exit, pattern=r"^shell:exit$")`
- `MessageHandler(filters.TEXT & ~filters.COMMAND, on_shell_message)` enregistré en `group=-1` (priorité haute, avant les wizards).

**Comportement du `MessageHandler` shell** :
```python
async def on_shell_message(update, context):
    user_id = update.effective_user.id
    session = shell_sessions.get(user_id)
    if session is None:
        return  # laisse passer aux handlers de group=0 (wizards, etc.)

    # Important : si l'utilisateur est dans un wizard ET en mode shell, le mode
    # shell gagne. C'est OK car start() est appelé uniquement depuis le menu
    # admin, qui implique que l'utilisateur n'est pas mid-wizard.

    text = update.message.text
    shell_sessions.touch(user_id)

    # ... handle `cd` or run via ShellRunner, then edit panel
    raise ApplicationHandlerStop  # empêche la propagation au group 0
```

L'utilisation de `ApplicationHandlerStop` est **obligatoire** quand la session existe, pour empêcher le message d'être aussi traité par les wizards. Sans ça, un wizard actif pourrait consommer la commande shell.

**Tâche périodique** :
```python
async def _check_expired_shell_sessions(context):
    now = time.monotonic()
    for session in shell_sessions.expired(now):
        try:
            await context.bot.edit_message_text(
                chat_id=session.chat_id,
                message_id=session.message_id,
                text="🔴 Shell fermé (inactivité).",
                reply_markup=_back_to_admin_markup(),
            )
        except Exception:
            pass  # message éditable ou non, on cleanup quand même
        shell_sessions.end(session.user_id)

application.job_queue.run_repeating(_check_expired_shell_sessions, interval=60, first=60)
```

### Sécurité

- Tous les nouveaux handlers (callbacks et `MessageHandler`) sont enveloppés du décorateur `restricted(cfg.allowed_user_ids)` déjà utilisé dans le projet. Un utilisateur non autorisé ne peut donc pas créer de session ni envoyer de commande.
- La session est indexée par `user_id` — impossible pour un user A de toucher la session de B.
- Une seule session par user à la fois ; `start()` ré-utilise/écrase l'entrée existante.

### Affichage

- Le panel utilise `parse_mode=ParseMode.HTML`.
- La sortie est encapsulée dans `<pre>...</pre>` après HTML-escape pour préserver indentation et caractères spéciaux.
- ANSI escape codes strippés via `strip_ansi`.
- Sortie tronquée à 3500 caractères via `truncate_output`, suffixe `… (tronqué)`.
- Si l'édition du message échoue (ex : message trop vieux, > 48 h chez Telegram) : on envoie un **nouveau** message et on met à jour `session.message_id` pour les éditions suivantes.

## Tests

Fichier : `tests/test_shell_mode.py`. Toute la logique testée est synchrone et indépendante de Telegram (cohérent avec les autres tests du projet, qui mockent PTB).

- **`test_shell_session_store_lifecycle`** : `start`, `get`, `end`, `get` retourne `None` après `end`.
- **`test_shell_session_store_touch_updates_activity`** : `touch` met à jour `last_activity` (mock `time.monotonic`).
- **`test_shell_session_store_expired`** : sessions avec `last_activity` plus ancien que `ttl` sont retournées par `expired`.
- **`test_resolve_cd_absolute_valid`** : chemin absolu existant → retourné.
- **`test_resolve_cd_relative_valid`** : chemin relatif joint au cwd courant.
- **`test_resolve_cd_invalid`** : dossier inexistant → `None`.
- **`test_resolve_cd_no_arg_returns_bot_root`** : `cd` sans argument → bot root.
- **`test_resolve_cd_dotdot`** : `cd ..` remonte d'un niveau.
- **`test_truncate_output_under_limit`** : sortie courte inchangée.
- **`test_truncate_output_over_limit`** : sortie tronquée + suffixe.
- **`test_strip_ansi`** : codes ANSI courants retirés (couleur, déplacement curseur).

## Fichiers modifiés

| Action | Fichier                                                            | Pourquoi                                              |
| ------ | ------------------------------------------------------------------ | ----------------------------------------------------- |
| ➕     | `tgbot/shell_mode.py`                                              | Nouveau module (session store, cd resolver, helpers) |
| ✏️     | `tgbot/bot.py`                                                     | Bouton admin, 3 handlers, job de cleanup             |
| ➕     | `tests/test_shell_mode.py`                                         | Tests unitaires                                       |
| ✏️     | `docs/superpowers/specs/2026-05-14-root-shell-mode-design.md`     | Ce document                                           |

## Risque connu : `MessageHandler` prioritaire

Le `MessageHandler` shell tourne en `group=-1`, donc **avant** les wizards et autres handlers. Deux pièges :

1. Il doit retourner **sans consommer** l'update si la session n'existe pas (sinon il casserait les wizards et autres saisies).
2. Quand la session existe et qu'il traite le message, il doit lever `ApplicationHandlerStop` pour empêcher la propagation au `group=0`, sinon un wizard latent traiterait aussi la commande.

À tester explicitement : (a) wizard "Ajouter projet" non perturbé quand aucune session shell n'est ouverte ; (b) commande shell non traitée par un wizard si une session existe. Comme ces interactions impliquent PTB, ils peuvent être laissés en test manuel après l'implémentation plutôt qu'automatisés.
