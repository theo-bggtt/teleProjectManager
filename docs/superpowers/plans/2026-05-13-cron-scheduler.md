# Planificateur de tâches (cron) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ajouter un planificateur qui exécute automatiquement des Actions enregistrées ou des opérations Projet (start/stop/restart) à intervalles ou heures fixes, persisté en SQLite et survivant aux redémarrages du bot.

**Architecture:** Nouveau package `tgbot/scheduler/` (db, triggers, executor, handlers, façade `register_scheduler`). La persistance des jobs est gérée par nous via la table `scheduled_tasks` dans `projects.db` (source unique de vérité) ; au boot on rejoue la table dans le `JobQueue` APScheduler de python-telegram-bot. Le wizard de création réutilise l'infra single-message (`_wizard_step` / `_wizard_finish` / `_wizard_escape`) déjà exposée par `bot.py`.

**Tech Stack:** Python 3, python-telegram-bot v21 (`JobQueue` + APScheduler `AsyncIOScheduler` inclus), SQLite, pytest + pytest-asyncio (nouvelle dépendance dev — premier test suite du repo, demandé explicitement par la spec).

**Spec source:** `docs/superpowers/specs/2026-05-13-cron-scheduler-design.md`.

**Hypothèses :**
- Les notifications sont envoyées à **chaque user_id** dans `cfg.allowed_user_ids` (en chat privé Telegram, `chat_id == user_id`). Pas d'ajout de champ `admin_chat_id` à `Config`.
- L'exécution d'une Action réutilise la logique existante de `cmd_runaction` factorisée en `_run_action_by_name`.
- L'exécution d'une opération projet réutilise les helpers existants de `build_app` (`_stop_project`, `_restart_project`, `runner.start`).
- Les callbacks de wizard utilisent le namespace `sched:` pour ne pas collisionner avec `proj:`, `act:`, `addact:`, `trd:`.

---

## Task 1: Infrastructure pytest + premier test

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_scheduler_db.py`
- Modify: `requirements.txt` (ajout en bas)
- Create: `pytest.ini`

- [ ] **Step 1: Créer `pytest.ini` à la racine du repo**

```ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

- [ ] **Step 2: Ajouter pytest aux dépendances dev**

Ajouter ces lignes à la fin de `requirements.txt` :

```
# Dev (tests)
pytest>=8.0
pytest-asyncio>=0.23
```

- [ ] **Step 3: Créer `tests/__init__.py` (fichier vide)**

```python
```

- [ ] **Step 4: Créer `tests/conftest.py`**

```python
"""Shared pytest fixtures."""
from pathlib import Path
import pytest


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Fresh SQLite file path inside a per-test tmp dir."""
    return tmp_path / "projects.db"
```

- [ ] **Step 5: Écrire le premier test (échouera tant que `SchedulerDB` n'existe pas)**

`tests/test_scheduler_db.py` :

```python
"""Tests for tgbot.scheduler.db.SchedulerDB."""
import json
import pytest

from tgbot.scheduler.db import SchedulerDB


def test_init_creates_tables(tmp_db_path):
    SchedulerDB(tmp_db_path)
    import sqlite3
    conn = sqlite3.connect(tmp_db_path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    conn.close()
    assert "scheduled_tasks" in names
    assert "bot_settings" in names


def test_notifications_default_enabled(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    assert sdb.get_notifications_enabled() is True


def test_set_notifications(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    sdb.set_notifications_enabled(False)
    assert sdb.get_notifications_enabled() is False
    sdb.set_notifications_enabled(True)
    assert sdb.get_notifications_enabled() is True


def test_add_list_get_task(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    task_id = sdb.add_task(
        name="Restart trading",
        task_type="project_op",
        target="trading-bot",
        operation="restart",
        trigger_kind="daily",
        trigger_spec={"hour": 4, "minute": 0},
    )
    assert task_id > 0
    tasks = sdb.list_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["name"] == "Restart trading"
    assert t["task_type"] == "project_op"
    assert t["operation"] == "restart"
    assert t["enabled"] == 1
    assert t["trigger_spec"] == {"hour": 4, "minute": 0}  # round-trip JSON


def test_toggle_enabled(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    task_id = sdb.add_task(
        name="x", task_type="action", target="a", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    sdb.set_enabled(task_id, False)
    assert sdb.get_task(task_id)["enabled"] == 0
    sdb.set_enabled(task_id, True)
    assert sdb.get_task(task_id)["enabled"] == 1


def test_update_last_run(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    task_id = sdb.add_task(
        name="x", task_type="action", target="a", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    sdb.set_last_run(task_id, "ok")
    t = sdb.get_task(task_id)
    assert t["last_status"] == "ok"
    assert t["last_run_at"] is not None


def test_delete_task(tmp_db_path):
    sdb = SchedulerDB(tmp_db_path)
    task_id = sdb.add_task(
        name="x", task_type="action", target="a", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    assert sdb.delete_task(task_id) is True
    assert sdb.get_task(task_id) is None
    assert sdb.delete_task(task_id) is False
```

- [ ] **Step 6: Installer les dépendances dev + vérifier que les tests échouent**

```
pip install -r requirements.txt
pytest tests/test_scheduler_db.py -v
```

Expected : tous les tests échouent avec `ModuleNotFoundError: No module named 'tgbot.scheduler'`.

- [ ] **Step 7: Commit**

```
git add pytest.ini requirements.txt tests/
git commit -m "test(scheduler): pytest infra + failing tests for SchedulerDB"
```

---

## Task 2: Implémenter `SchedulerDB` (scheduled_tasks + bot_settings)

**Files:**
- Create: `tgbot/scheduler/__init__.py` (vide pour l'instant)
- Create: `tgbot/scheduler/db.py`
- Test: `tests/test_scheduler_db.py` (déjà écrit en Task 1)

- [ ] **Step 1: Créer `tgbot/scheduler/__init__.py` vide**

```python
```

- [ ] **Step 2: Implémenter `tgbot/scheduler/db.py`**

```python
"""SQLite store for scheduled tasks and bot settings."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    target       TEXT NOT NULL,
    operation    TEXT,
    trigger_kind TEXT NOT NULL,
    trigger_spec TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run_at  TEXT,
    last_status  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class SchedulerDB:
    """Thin wrapper over the shared `projects.db` for scheduler-specific tables.

    Uses idempotent CREATE TABLE IF NOT EXISTS so it can coexist with the
    main `DB` class without ordering constraints.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            row = c.execute(
                "SELECT value FROM bot_settings WHERE key = 'notifications_enabled'"
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO bot_settings (key, value) VALUES ('notifications_enabled', '1')"
                )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ─── bot_settings ────────────────────────────────────────────────────
    def get_notifications_enabled(self) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM bot_settings WHERE key = 'notifications_enabled'"
            ).fetchone()
            return row is not None and row["value"] == "1"

    def set_notifications_enabled(self, enabled: bool) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO bot_settings (key, value) VALUES ('notifications_enabled', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if enabled else "0",),
            )

    # ─── scheduled_tasks ─────────────────────────────────────────────────
    def add_task(self, *, name: str, task_type: str, target: str,
                 operation: Optional[str], trigger_kind: str,
                 trigger_spec: dict) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO scheduled_tasks "
                "(name, task_type, target, operation, trigger_kind, trigger_spec, "
                " enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (name, task_type, target, operation,
                 trigger_kind, json.dumps(trigger_spec), now),
            )
            return cur.lastrowid

    def get_task(self, task_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def list_tasks(self, *, only_enabled: bool = False) -> list[dict]:
        sql = "SELECT * FROM scheduled_tasks"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self._conn() as c:
            rows = c.execute(sql).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def set_enabled(self, task_id: int, enabled: bool) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE scheduled_tasks SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, task_id),
            )
            return cur.rowcount > 0

    def set_last_run(self, task_id: int, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                "UPDATE scheduled_tasks SET last_run_at = ?, last_status = ? WHERE id = ?",
                (now, status, task_id),
            )

    def update_trigger(self, task_id: int, trigger_kind: str, trigger_spec: dict) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE scheduled_tasks SET trigger_kind = ?, trigger_spec = ? WHERE id = ?",
                (trigger_kind, json.dumps(trigger_spec), task_id),
            )
            return cur.rowcount > 0

    def delete_task(self, task_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
            return cur.rowcount > 0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["trigger_spec"] = json.loads(d["trigger_spec"])
        return d
```

- [ ] **Step 3: Lancer les tests**

```
pytest tests/test_scheduler_db.py -v
```

Expected : 7 passed.

- [ ] **Step 4: Commit**

```
git add tgbot/scheduler/__init__.py tgbot/scheduler/db.py
git commit -m "feat(scheduler): SchedulerDB with scheduled_tasks + bot_settings"
```

---

## Task 3: Triggers parser (preset/cron → APScheduler)

**Files:**
- Create: `tgbot/scheduler/triggers.py`
- Create: `tests/test_scheduler_triggers.py`

- [ ] **Step 1: Écrire les tests d'abord**

`tests/test_scheduler_triggers.py` :

```python
"""Tests for tgbot.scheduler.triggers."""
import pytest

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from tgbot.scheduler.triggers import build_trigger, describe_trigger


def test_build_interval():
    t = build_trigger("interval", {"minutes": 10})
    assert isinstance(t, IntervalTrigger)
    # IntervalTrigger stores interval as datetime.timedelta
    assert t.interval.total_seconds() == 600


def test_build_daily():
    t = build_trigger("daily", {"hour": 4, "minute": 30})
    assert isinstance(t, CronTrigger)


def test_build_weekly():
    t = build_trigger("weekly", {"day_of_week": "mon", "hour": 3, "minute": 0})
    assert isinstance(t, CronTrigger)


def test_build_cron_valid():
    t = build_trigger("cron", {"expr": "0 4 * * 1"})
    assert isinstance(t, CronTrigger)


def test_build_cron_invalid_raises():
    with pytest.raises(ValueError):
        build_trigger("cron", {"expr": "not a cron expr"})


def test_build_unknown_kind_raises():
    with pytest.raises(ValueError):
        build_trigger("monthly", {})


def test_describe_interval_minutes():
    assert describe_trigger("interval", {"minutes": 10}) == "toutes les 10 min"


def test_describe_interval_hours():
    assert describe_trigger("interval", {"hours": 2}) == "toutes les 2 h"


def test_describe_daily():
    assert describe_trigger("daily", {"hour": 4, "minute": 0}) == "quotidien 04:00"


def test_describe_weekly():
    assert (
        describe_trigger("weekly", {"day_of_week": "mon", "hour": 3, "minute": 0})
        == "hebdo lundi 03:00"
    )


def test_describe_cron():
    assert describe_trigger("cron", {"expr": "0 4 * * 1"}) == "cron `0 4 * * 1`"
```

- [ ] **Step 2: Vérifier que les tests échouent**

```
pytest tests/test_scheduler_triggers.py -v
```

Expected : `ModuleNotFoundError: tgbot.scheduler.triggers`.

- [ ] **Step 3: Implémenter `tgbot/scheduler/triggers.py`**

```python
"""Convert (trigger_kind, trigger_spec) records to APScheduler triggers + labels."""
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


_WEEKDAYS_FR = {
    "mon": "lundi", "tue": "mardi", "wed": "mercredi", "thu": "jeudi",
    "fri": "vendredi", "sat": "samedi", "sun": "dimanche",
}


def build_trigger(kind: str, spec: dict):
    """Return an APScheduler trigger for the given (kind, spec).

    Raises ValueError on unknown kind or invalid cron expression.
    """
    if kind == "interval":
        # spec keys: any of seconds/minutes/hours/days (all int)
        return IntervalTrigger(**spec)
    if kind == "daily":
        return CronTrigger(hour=spec["hour"], minute=spec["minute"])
    if kind == "weekly":
        return CronTrigger(
            day_of_week=spec["day_of_week"],
            hour=spec["hour"],
            minute=spec["minute"],
        )
    if kind == "cron":
        try:
            return CronTrigger.from_crontab(spec["expr"])
        except (ValueError, KeyError) as e:
            raise ValueError(f"Invalid cron expression: {e}") from e
    raise ValueError(f"Unknown trigger kind: {kind}")


def describe_trigger(kind: str, spec: dict) -> str:
    """Human-readable French summary of a trigger, used in list/card UI."""
    if kind == "interval":
        if "minutes" in spec:
            return f"toutes les {spec['minutes']} min"
        if "hours" in spec:
            return f"toutes les {spec['hours']} h"
        if "seconds" in spec:
            return f"toutes les {spec['seconds']} s"
        if "days" in spec:
            return f"tous les {spec['days']} j"
        return "intervalle"
    if kind == "daily":
        return f"quotidien {spec['hour']:02d}:{spec['minute']:02d}"
    if kind == "weekly":
        day = _WEEKDAYS_FR.get(spec["day_of_week"], spec["day_of_week"])
        return f"hebdo {day} {spec['hour']:02d}:{spec['minute']:02d}"
    if kind == "cron":
        return f"cron `{spec['expr']}`"
    return kind
```

- [ ] **Step 4: Lancer les tests**

```
pytest tests/test_scheduler_triggers.py -v
```

Expected : 11 passed.

- [ ] **Step 5: Commit**

```
git add tgbot/scheduler/triggers.py tests/test_scheduler_triggers.py
git commit -m "feat(scheduler): trigger parser (interval/daily/weekly/cron) with descriptions"
```

---

## Task 4: Factoriser `_run_action_by_name` dans `bot.py`

Le code de `cmd_runaction` (`tgbot/bot.py:1130-1168`) sait exécuter une Action par son nom. Pour le réutiliser depuis le scheduler, il faut l'extraire en helper interne à `build_app` (il dépend de `db`, `shell`, `runner`, `_action_runner_key`).

**Files:**
- Modify: `tgbot/bot.py:1130-1168` (extraire un helper)

- [ ] **Step 1: Insérer le helper `_run_action_by_name` AVANT `cmd_runaction`**

Juste avant la ligne 1129 (`@auth\n    async def cmd_runaction(...)`), ajouter :

```python
    async def _run_action_by_name(name: str) -> tuple[int, str]:
        """Execute a saved Action by name. Returns (rc, output).

        - oneshot mode: runs via shell, returns (returncode, merged stdout/stderr).
        - managed mode: runs via runner.start; returns (0, msg) on success,
          (1, msg) on failure. There is no captured output for managed Actions.
        - Unknown action: returns (1, "action not found: <name>").

        Notes:
          * Does NOT honour require_confirm — confirmation is a UI concern; the
            scheduler always proceeds.
        """
        action = db.get_action(name)
        if not action:
            return 1, f"action not found: {name}"
        mode = action.get("mode", "oneshot")
        cwd = action.get("cwd") or None
        if mode == "managed":
            ok, msg = await runner.start(
                _action_runner_key(name), action["command"], cwd or str(Path.cwd()),
            )
            return (0 if ok else 1), msg
        rc, out = await shell.run(action["command"], cwd)
        return rc, out
```

- [ ] **Step 2: Remplacer le corps de `cmd_runaction` (lignes 1129-1168) pour réutiliser le helper**

Remplacer tout le bloc `cmd_runaction` par :

```python
    @auth
    async def cmd_runaction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text(
                "Usage: `/runaction <name>`", parse_mode=ParseMode.MARKDOWN,
            )
            return
        name = ctx.args[0]
        action = db.get_action(name)
        if not action:
            await update.message.reply_text(
                f"Action `{name}` introuvable.", parse_mode=ParseMode.MARKDOWN,
            )
            return
        if action.get("require_confirm"):
            await update.message.reply_text(
                f"Confirmer : exécuter `{name}` ?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_action_confirm_markup("run", name),
            )
            return
        mode = action.get("mode", "oneshot")
        if mode == "managed":
            rc, msg = await _run_action_by_name(name)
            prefix = "▶️" if rc == 0 else "⚠️"
            await update.message.reply_text(
                f"{prefix} `{name}` : {msg}", parse_mode=ParseMode.MARKDOWN,
            )
            return
        await update.message.reply_text(
            f"⏳ Exécution de `{name}`…", parse_mode=ParseMode.MARKDOWN,
        )
        rc, out = await _run_action_by_name(name)
        await _send_text_or_file(
            update, out or "(aucune sortie)", f"{name}-output.txt",
            header=f"{name} — exit {rc}",
        )
```

- [ ] **Step 3: Smoke test manuel — lancer le bot et vérifier qu'une Action fonctionne toujours**

```
python -m tgbot config.toml
```

Dans Telegram :
- `/runaction <une_action_oneshot_existante>` doit afficher la sortie comme avant.
- `/runaction <une_action_managed>` doit la lancer et afficher `▶️ ...`.

- [ ] **Step 4: Commit**

```
git add tgbot/bot.py
git commit -m "refactor(bot): extract _run_action_by_name helper for scheduler reuse"
```

---

## Task 5: Executor

L'executor reçoit toutes ses dépendances à la construction et expose une méthode `run_task(task_id)` que l'on bindera comme job APScheduler.

**Files:**
- Create: `tgbot/scheduler/executor.py`
- Create: `tests/test_scheduler_executor.py`

- [ ] **Step 1: Écrire les tests d'abord**

`tests/test_scheduler_executor.py` :

```python
"""Tests for tgbot.scheduler.executor.Executor."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from tgbot.scheduler.db import SchedulerDB
from tgbot.scheduler.executor import Executor


@pytest.fixture
def sdb(tmp_db_path):
    return SchedulerDB(tmp_db_path)


def make_executor(sdb, *, action_rc=0, action_out="ok output",
                  proj_op_success=True, notifications=True):
    """Build an Executor with mocked deps."""
    run_action = AsyncMock(return_value=(action_rc, action_out))

    project_ops = MagicMock()
    project_ops.start = AsyncMock(return_value=(proj_op_success, "started"))
    project_ops.stop = AsyncMock(return_value=proj_op_success)
    project_ops.restart = AsyncMock(return_value=(proj_op_success, "restarted"))

    bot = MagicMock()
    bot.send_message = AsyncMock()
    sdb.set_notifications_enabled(notifications)
    return Executor(
        scheduler_db=sdb,
        run_action=run_action,
        project_ops=project_ops,
        bot=bot,
        allowed_user_ids={42, 43},
    ), bot, run_action, project_ops


async def test_run_action_task_success(sdb):
    task_id = sdb.add_task(
        name="t", task_type="action", target="myact", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    ex, bot, run_action, _ = make_executor(sdb)
    await ex.run_task(task_id)
    run_action.assert_awaited_once_with("myact")
    t = sdb.get_task(task_id)
    assert t["last_status"] == "ok"
    assert t["last_run_at"] is not None
    # 2 allowed users → 2 notifications
    assert bot.send_message.await_count == 2


async def test_run_action_task_failure(sdb):
    task_id = sdb.add_task(
        name="t", task_type="action", target="myact", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    ex, _, _, _ = make_executor(sdb, action_rc=1)
    await ex.run_task(task_id)
    assert sdb.get_task(task_id)["last_status"] == "error"


async def test_run_project_op_restart(sdb):
    task_id = sdb.add_task(
        name="t", task_type="project_op", target="my-proj", operation="restart",
        trigger_kind="daily", trigger_spec={"hour": 4, "minute": 0},
    )
    ex, _, _, project_ops = make_executor(sdb)
    await ex.run_task(task_id)
    project_ops.restart.assert_awaited_once_with("my-proj")
    assert sdb.get_task(task_id)["last_status"] == "ok"


async def test_run_project_op_stop(sdb):
    task_id = sdb.add_task(
        name="t", task_type="project_op", target="my-proj", operation="stop",
        trigger_kind="daily", trigger_spec={"hour": 4, "minute": 0},
    )
    ex, _, _, project_ops = make_executor(sdb)
    await ex.run_task(task_id)
    project_ops.stop.assert_awaited_once_with("my-proj")


async def test_disabled_task_is_noop(sdb):
    task_id = sdb.add_task(
        name="t", task_type="action", target="myact", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    sdb.set_enabled(task_id, False)
    ex, _, run_action, _ = make_executor(sdb)
    await ex.run_task(task_id)
    run_action.assert_not_awaited()


async def test_missing_task_is_noop(sdb):
    ex, _, run_action, _ = make_executor(sdb)
    await ex.run_task(999)
    run_action.assert_not_awaited()


async def test_notifications_off_no_send(sdb):
    task_id = sdb.add_task(
        name="t", task_type="action", target="myact", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    ex, bot, _, _ = make_executor(sdb, notifications=False)
    await ex.run_task(task_id)
    bot.send_message.assert_not_awaited()


async def test_executor_swallows_internal_exceptions(sdb):
    """If the underlying action raises, run_task must still mark last_status='error'."""
    task_id = sdb.add_task(
        name="t", task_type="action", target="myact", operation=None,
        trigger_kind="interval", trigger_spec={"minutes": 5},
    )
    run_action = AsyncMock(side_effect=RuntimeError("boom"))
    bot = MagicMock(); bot.send_message = AsyncMock()
    ex = Executor(
        scheduler_db=sdb, run_action=run_action,
        project_ops=MagicMock(),
        bot=bot, allowed_user_ids={1},
    )
    await ex.run_task(task_id)  # must NOT raise
    assert sdb.get_task(task_id)["last_status"] == "error"
```

- [ ] **Step 2: Vérifier que les tests échouent**

```
pytest tests/test_scheduler_executor.py -v
```

Expected : `ModuleNotFoundError: tgbot.scheduler.executor`.

- [ ] **Step 3: Implémenter `tgbot/scheduler/executor.py`**

```python
"""Run scheduled tasks and notify users."""
import logging
from typing import Awaitable, Callable, Iterable, Protocol

from .db import SchedulerDB

logger = logging.getLogger(__name__)


class ProjectOps(Protocol):
    async def start(self, name: str) -> tuple[bool, str]: ...
    async def stop(self, name: str) -> bool: ...
    async def restart(self, name: str) -> tuple[bool, str]: ...


RunActionFn = Callable[[str], Awaitable[tuple[int, str]]]


class Executor:
    """Bound to APScheduler jobs as `executor.run_task` (one instance per bot)."""

    def __init__(
        self,
        *,
        scheduler_db: SchedulerDB,
        run_action: RunActionFn,
        project_ops: ProjectOps,
        bot,
        allowed_user_ids: Iterable[int],
    ):
        self.sdb = scheduler_db
        self.run_action = run_action
        self.project_ops = project_ops
        self.bot = bot
        self.user_ids = list(allowed_user_ids)

    async def run_task(self, task_id: int) -> None:
        """APScheduler job entry point. Must never raise (would kill the job)."""
        task = self.sdb.get_task(task_id)
        if task is None:
            logger.warning("scheduled task %s vanished — skipping", task_id)
            return
        if not task["enabled"]:
            logger.debug("scheduled task %s disabled — skipping", task_id)
            return

        name = task["name"]
        status: str = "error"
        detail: str = ""
        try:
            if task["task_type"] == "action":
                rc, out = await self.run_action(task["target"])
                status = "ok" if rc == 0 else "error"
                detail = (out or "").strip().splitlines()[-1] if out else ""
            elif task["task_type"] == "project_op":
                op = task["operation"]
                target = task["target"]
                if op == "start":
                    ok, msg = await self.project_ops.start(target)
                    status = "ok" if ok else "error"
                    detail = msg
                elif op == "stop":
                    ok = await self.project_ops.stop(target)
                    status = "ok" if ok else "error"
                elif op == "restart":
                    ok, msg = await self.project_ops.restart(target)
                    status = "ok" if ok else "error"
                    detail = msg
                else:
                    logger.error("unknown project_op operation: %s", op)
            else:
                logger.error("unknown task_type: %s", task["task_type"])
        except Exception:  # noqa: BLE001 — must not crash the job loop
            logger.exception("scheduled task %s raised", task_id)
            status = "error"

        self.sdb.set_last_run(task_id, status)
        if self.sdb.get_notifications_enabled():
            await self._notify(name, status, detail)

    async def _notify(self, task_name: str, status: str, detail: str) -> None:
        icon = "✅" if status == "ok" else "❌"
        text = f"⏰ *{task_name}* · {icon}"
        if detail:
            # Telegram has a 4096-char limit; keep notifications short.
            text += f"\n`{detail[:200]}`"
        for uid in self.user_ids:
            try:
                await self.bot.send_message(
                    chat_id=uid, text=text, parse_mode="Markdown",
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to notify user %s", uid)
```

- [ ] **Step 4: Lancer les tests**

```
pytest tests/test_scheduler_executor.py -v
```

Expected : 8 passed.

- [ ] **Step 5: Commit**

```
git add tgbot/scheduler/executor.py tests/test_scheduler_executor.py
git commit -m "feat(scheduler): Executor with notification + error handling"
```

---

## Task 6: Handlers Telegram (liste, fiche, wizard)

Module le plus volumineux : il expose les renderers et le `ConversationHandler` de création. Toutes les fonctions sont définies dans `register_handlers(...)` (un fonction-factory, comme `register_trading.register_handlers`) pour fermer sur `app`, `cfg`, les DB, l'executor et les helpers wizard.

**Files:**
- Create: `tgbot/scheduler/handlers.py`

- [ ] **Step 1: Implémenter `tgbot/scheduler/handlers.py`**

```python
"""Telegram handlers for the scheduler module."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..auth import restricted
from .db import SchedulerDB
from .executor import Executor
from .triggers import build_trigger, describe_trigger

if TYPE_CHECKING:
    from telegram.ext import Application

logger = logging.getLogger(__name__)

# Conversation states (offset 800 to avoid clashing with bot.py ranges).
SCHED_TYPE, SCHED_TARGET, SCHED_OP, SCHED_TRIGGER, \
    SCHED_INTERVAL_VALUE, SCHED_DAILY_TIME, SCHED_WEEKLY_DAY, \
    SCHED_WEEKLY_TIME, SCHED_CRON_EXPR, SCHED_NAME = range(800, 810)


_WEEKDAYS = [
    ("mon", "Lundi"), ("tue", "Mardi"), ("wed", "Mercredi"),
    ("thu", "Jeudi"), ("fri", "Vendredi"), ("sat", "Samedi"),
    ("sun", "Dimanche"),
]


def register_handlers(
    app: "Application",
    cfg,
    main_db,
    scheduler_db: SchedulerDB,
    executor: Executor,
    *,
    wizard_step: Callable,
    wizard_finish: Callable,
    wizard_escape: Callable,
) -> None:
    """Register all scheduler-related Telegram handlers."""
    auth = restricted(cfg.allowed_user_ids)
    scheduler = app.job_queue.scheduler

    # ─── helpers ────────────────────────────────────────────────────────
    def _job_id(task_id: int) -> str:
        return f"sched:{task_id}"

    def _schedule(task: dict) -> None:
        """(Re)register a task as an APScheduler job. Idempotent."""
        try:
            scheduler.remove_job(_job_id(task["id"]))
        except Exception:  # JobLookupError or similar
            pass
        trigger = build_trigger(task["trigger_kind"], task["trigger_spec"])
        scheduler.add_job(
            executor.run_task,
            trigger=trigger,
            args=[task["id"]],
            id=_job_id(task["id"]),
            misfire_grace_time=None,
            max_instances=1,
            replace_existing=True,
        )

    def _unschedule(task_id: int) -> None:
        try:
            scheduler.remove_job(_job_id(task_id))
        except Exception:
            pass

    # ─── markup builders ────────────────────────────────────────────────
    def _list_markup(tasks: list[dict]) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for t in tasks:
            check = "✓" if t["enabled"] else "✗"
            label = f"{check} {t['name']} — {describe_trigger(t['trigger_kind'], t['trigger_spec'])}"
            rows.append([InlineKeyboardButton(label, callback_data=f"sched:card:{t['id']}")])
        rows.append([InlineKeyboardButton("➕ Nouvelle", callback_data="sched:new")])
        rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")])
        return InlineKeyboardMarkup(rows)

    def _card_markup(task_id: int, enabled: bool) -> InlineKeyboardMarkup:
        toggle = "⏸ Désactiver" if enabled else "▶️ Activer"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle, callback_data=f"sched:toggle:{task_id}")],
            [InlineKeyboardButton("▶️ Exécuter maintenant", callback_data=f"sched:run:{task_id}")],
            [InlineKeyboardButton("🗑 Supprimer", callback_data=f"sched:del:{task_id}")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="menu:scheduled")],
        ])

    def _confirm_delete_markup(task_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Oui, supprimer", callback_data=f"sched:dconfirm:{task_id}"),
                InlineKeyboardButton("❌ Annuler", callback_data=f"sched:card:{task_id}"),
            ],
        ])

    # ─── renderers ──────────────────────────────────────────────────────
    async def _render_list(query) -> None:
        tasks = scheduler_db.list_tasks()
        text = (
            f"*Tâches planifiées ({len(tasks)})*"
            if tasks
            else "*Tâches planifiées*\nAucune tâche."
        )
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=_list_markup(tasks),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def _format_card(t: dict) -> str:
        if t["task_type"] == "project_op":
            target_line = f"Cible       : `{t['target']}` · `{t['operation']}`"
            type_line = "Type        : opération projet"
        else:
            target_line = f"Cible       : action `{t['target']}`"
            type_line = "Type        : action enregistrée"
        recurrence = describe_trigger(t["trigger_kind"], t["trigger_spec"])
        status = "✓ activée" if t["enabled"] else "✗ désactivée"
        if t["last_run_at"]:
            icon = "✅" if t["last_status"] == "ok" else "❌"
            last = f"Dernière    : {t['last_run_at']} · {icon} {t['last_status']}"
        else:
            last = "Dernière    : jamais exécutée"
        return (
            f"*⏰ {t['name']}*\n"
            f"{type_line}\n"
            f"{target_line}\n"
            f"Récurrence  : {recurrence}\n"
            f"Statut      : {status}\n"
            f"{last}"
        )

    async def _render_card(query, task_id: int) -> None:
        t = scheduler_db.get_task(task_id)
        if t is None:
            await query.edit_message_text(
                f"Tâche `{task_id}` introuvable.", parse_mode=ParseMode.MARKDOWN,
            )
            return
        try:
            await query.edit_message_text(
                await _format_card(t),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_card_markup(task_id, bool(t["enabled"])),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    # ─── /scheduled command + non-wizard callbacks (sched:list/card/toggle/run/del) ──
    @auth
    async def cmd_scheduled(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Render the list as a fresh message.
        tasks = scheduler_db.list_tasks()
        text = (
            f"*Tâches planifiées ({len(tasks)})*"
            if tasks
            else "*Tâches planifiées*\nAucune tâche."
        )
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=_list_markup(tasks),
        )

    @auth
    async def on_sched_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        parts = data.split(":", 2)
        if len(parts) < 2:
            return
        sub = parts[1]

        if sub == "list":
            await _render_list(query)
            return

        if sub == "card" and len(parts) == 3:
            try:
                task_id = int(parts[2])
            except ValueError:
                return
            await _render_card(query, task_id)
            return

        if sub == "toggle" and len(parts) == 3:
            task_id = int(parts[2])
            t = scheduler_db.get_task(task_id)
            if t is None:
                return
            new_enabled = not bool(t["enabled"])
            scheduler_db.set_enabled(task_id, new_enabled)
            if new_enabled:
                _schedule(scheduler_db.get_task(task_id))
            else:
                _unschedule(task_id)
            await _render_card(query, task_id)
            return

        if sub == "run" and len(parts) == 3:
            task_id = int(parts[2])
            await query.edit_message_text("⏳ Exécution…", parse_mode=ParseMode.MARKDOWN)
            await executor.run_task(task_id)
            await _render_card(query, task_id)
            return

        if sub == "del" and len(parts) == 3:
            task_id = int(parts[2])
            await query.edit_message_text(
                "🗑 Supprimer cette tâche planifiée ?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_confirm_delete_markup(task_id),
            )
            return

        if sub == "dconfirm" and len(parts) == 3:
            task_id = int(parts[2])
            _unschedule(task_id)
            scheduler_db.delete_task(task_id)
            await _render_list(query)
            return

        if sub == "notifs":  # admin toggle for notifications
            current = scheduler_db.get_notifications_enabled()
            scheduler_db.set_notifications_enabled(not current)
            # Re-render admin menu — delegated to bot.py via wizard_escape pattern;
            # here we just confirm.
            new_state = "ON" if not current else "OFF"
            await query.answer(text=f"Notifications {new_state}", show_alert=False)
            return

    # ─── wizard (create) ────────────────────────────────────────────────
    @auth
    async def wizard_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.callback_query is not None:
            await update.callback_query.answer()
        ctx.user_data["sched"] = {}
        await wizard_step(
            update, ctx,
            "*➕ Nouvelle tâche planifiée*\n\nQuel type ?",
            extra_rows=[[
                InlineKeyboardButton("🚀 Action enregistrée", callback_data="sched:wt:action"),
                InlineKeyboardButton("📂 Opération projet", callback_data="sched:wt:project_op"),
            ]],
        )
        return SCHED_TYPE

    async def wizard_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[2] not in ("action", "project_op"):
            return SCHED_TYPE
        ctx.user_data["sched"]["task_type"] = parts[2]
        if parts[2] == "action":
            actions = main_db.list_actions()
            if not actions:
                await wizard_step(update, ctx, "⚠️ Aucune action enregistrée. Crée d'abord une Action.")
                ctx.user_data.pop("sched", None)
                return ConversationHandler.END
            rows = [
                [InlineKeyboardButton(a["name"], callback_data=f"sched:wtg:{a['name']}")]
                for a in actions
            ]
            await wizard_step(update, ctx, "🚀 *Quelle action ?*", extra_rows=rows)
        else:
            projs = main_db.list_projects()
            if not projs:
                await wizard_step(update, ctx, "⚠️ Aucun projet enregistré. Crée d'abord un Projet.")
                ctx.user_data.pop("sched", None)
                return ConversationHandler.END
            rows = [
                [InlineKeyboardButton(p["name"], callback_data=f"sched:wtg:{p['name']}")]
                for p in projs
            ]
            await wizard_step(update, ctx, "📂 *Quel projet ?*", extra_rows=rows)
        return SCHED_TARGET

    async def wizard_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":", 2)
        if len(parts) != 3:
            return SCHED_TARGET
        ctx.user_data["sched"]["target"] = parts[2]
        if ctx.user_data["sched"]["task_type"] == "project_op":
            await wizard_step(
                update, ctx, "📂 *Quelle opération ?*",
                extra_rows=[[
                    InlineKeyboardButton("▶️ start", callback_data="sched:wop:start"),
                    InlineKeyboardButton("⏹ stop", callback_data="sched:wop:stop"),
                    InlineKeyboardButton("🔄 restart", callback_data="sched:wop:restart"),
                ]],
            )
            return SCHED_OP
        ctx.user_data["sched"]["operation"] = None
        return await _ask_trigger(update, ctx)

    async def wizard_op(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[2] not in ("start", "stop", "restart"):
            return SCHED_OP
        ctx.user_data["sched"]["operation"] = parts[2]
        return await _ask_trigger(update, ctx)

    async def _ask_trigger(update, ctx):
        await wizard_step(
            update, ctx, "⏱ *Récurrence ?*",
            extra_rows=[
                [InlineKeyboardButton("Toutes les X min", callback_data="sched:tr:interval")],
                [InlineKeyboardButton("Quotidien à HH:MM", callback_data="sched:tr:daily")],
                [InlineKeyboardButton("Hebdo : <jour> à HH:MM", callback_data="sched:tr:weekly")],
                [InlineKeyboardButton("Expression cron…", callback_data="sched:tr:cron")],
            ],
        )
        return SCHED_TRIGGER

    async def wizard_trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3:
            return SCHED_TRIGGER
        kind = parts[2]
        ctx.user_data["sched"]["trigger_kind"] = kind
        if kind == "interval":
            await wizard_step(update, ctx, "⏱ Toutes les combien de *minutes* ? (1–1440)")
            return SCHED_INTERVAL_VALUE
        if kind == "daily":
            await wizard_step(update, ctx, "🕓 *Heure quotidienne* au format `HH:MM` (ex: `04:00`)")
            return SCHED_DAILY_TIME
        if kind == "weekly":
            rows = [[InlineKeyboardButton(label, callback_data=f"sched:wd:{code}")]
                    for code, label in _WEEKDAYS]
            await wizard_step(update, ctx, "📅 *Quel jour ?*", extra_rows=rows)
            return SCHED_WEEKLY_DAY
        if kind == "cron":
            await wizard_step(
                update, ctx,
                "🧙 *Expression cron* (format `m h dom mon dow`)\n"
                "Exemple : `0 4 * * 1` = tous les lundis à 04h00.",
            )
            return SCHED_CRON_EXPR
        return SCHED_TRIGGER

    async def wizard_interval_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        text = update.message.text.strip()
        try:
            mins = int(text)
            if not (1 <= mins <= 1440):
                raise ValueError
        except ValueError:
            await wizard_step(update, ctx, "⚠️ Entier entre 1 et 1440 attendu. Réessaie.")
            return SCHED_INTERVAL_VALUE
        ctx.user_data["sched"]["trigger_spec"] = {"minutes": mins}
        return await _ask_name(update, ctx)

    async def wizard_daily_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        spec = _parse_hhmm(update.message.text.strip())
        if spec is None:
            await wizard_step(update, ctx, "⚠️ Format invalide. Envoie `HH:MM` (ex: `04:00`).")
            return SCHED_DAILY_TIME
        ctx.user_data["sched"]["trigger_spec"] = spec
        return await _ask_name(update, ctx)

    async def wizard_weekly_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[2] not in {c for c, _ in _WEEKDAYS}:
            return SCHED_WEEKLY_DAY
        ctx.user_data["sched"]["weekly_day"] = parts[2]
        await wizard_step(update, ctx, "🕓 *Heure* au format `HH:MM` (ex: `03:00`)")
        return SCHED_WEEKLY_TIME

    async def wizard_weekly_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        spec = _parse_hhmm(update.message.text.strip())
        if spec is None:
            await wizard_step(update, ctx, "⚠️ Format invalide. Envoie `HH:MM`.")
            return SCHED_WEEKLY_TIME
        spec["day_of_week"] = ctx.user_data["sched"].pop("weekly_day")
        ctx.user_data["sched"]["trigger_spec"] = spec
        return await _ask_name(update, ctx)

    async def wizard_cron_expr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        expr = update.message.text.strip()
        try:
            build_trigger("cron", {"expr": expr})
        except ValueError as e:
            await wizard_step(update, ctx, f"⚠️ Cron invalide : `{e}`\nRéessaie.")
            return SCHED_CRON_EXPR
        ctx.user_data["sched"]["trigger_spec"] = {"expr": expr}
        return await _ask_name(update, ctx)

    async def _ask_name(update, ctx):
        await wizard_step(update, ctx, "🏷 *Nom de la tâche ?* (libellé affiché dans la liste)")
        return SCHED_NAME

    async def wizard_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.delete()
        except Exception:
            pass
        name = update.message.text.strip()
        if not name:
            await wizard_step(update, ctx, "⚠️ Nom vide. Réessaie.")
            return SCHED_NAME
        s = ctx.user_data.get("sched", {})
        task_id = scheduler_db.add_task(
            name=name,
            task_type=s["task_type"],
            target=s["target"],
            operation=s.get("operation"),
            trigger_kind=s["trigger_kind"],
            trigger_spec=s["trigger_spec"],
        )
        _schedule(scheduler_db.get_task(task_id))
        ctx.user_data.pop("sched", None)
        await wizard_finish(update, ctx)
        return ConversationHandler.END

    def _parse_hhmm(text: str) -> dict | None:
        try:
            hh, mm = text.split(":")
            h, m = int(hh), int(mm)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                return None
            return {"hour": h, "minute": m}
        except (ValueError, AttributeError):
            return None

    # ─── ConversationHandler ────────────────────────────────────────────
    sched_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(wizard_start, pattern=r"^sched:new$"),
        ],
        states={
            SCHED_TYPE: [CallbackQueryHandler(wizard_type, pattern=r"^sched:wt:")],
            SCHED_TARGET: [CallbackQueryHandler(wizard_target, pattern=r"^sched:wtg:")],
            SCHED_OP: [CallbackQueryHandler(wizard_op, pattern=r"^sched:wop:")],
            SCHED_TRIGGER: [CallbackQueryHandler(wizard_trigger, pattern=r"^sched:tr:")],
            SCHED_INTERVAL_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_interval_value),
            ],
            SCHED_DAILY_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_daily_time),
            ],
            SCHED_WEEKLY_DAY: [CallbackQueryHandler(wizard_weekly_day, pattern=r"^sched:wd:")],
            SCHED_WEEKLY_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_weekly_time),
            ],
            SCHED_CRON_EXPR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_cron_expr),
            ],
            SCHED_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_name),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: wizard_finish(u, c) or ConversationHandler.END),
            CallbackQueryHandler(wizard_escape),
        ],
        conversation_timeout=300,
    )

    # Conversation handler must be added BEFORE the global sched:* callback
    # so wizard entry/escape patterns match first.
    app.add_handler(sched_conv)
    app.add_handler(CommandHandler("scheduled", cmd_scheduled))
    app.add_handler(CallbackQueryHandler(on_sched_callback, pattern=r"^sched:"))
```

- [ ] **Step 2: Vérifier l'import du module — pas de syntaxe cassée**

```
python -c "from tgbot.scheduler import handlers"
```

Expected : pas d'erreur.

- [ ] **Step 3: Commit**

```
git add tgbot/scheduler/handlers.py
git commit -m "feat(scheduler): handlers (list, card, wizard create)"
```

---

## Task 7: Façade `register_scheduler` + intégration dans `bot.py`

C'est la tâche d'intégration : on crée la façade `register_scheduler`, on l'appelle dans `build_app`, on ajoute le bouton `⏰ Planifié` au menu principal, on ajoute le toggle notifs dans `_admin_menu_markup`, et on route `menu:scheduled` dans `on_callback`.

**Files:**
- Modify: `tgbot/scheduler/__init__.py`
- Modify: `tgbot/bot.py:37` (import)
- Modify: `tgbot/bot.py:123-134` (_main_menu_markup)
- Modify: `tgbot/bot.py:137-142` (_admin_menu_markup)
- Modify: `tgbot/bot.py:647-672` (`on_callback` menu dispatch — add `scheduled` route + admin notifs handling)
- Modify: `tgbot/bot.py:1700-1731` (BotCommand list — add `/scheduled`)
- Modify: `tgbot/bot.py:1761-1767` (registration order — call `register_scheduler` after `register_trading`)

- [ ] **Step 1: Implémenter `tgbot/scheduler/__init__.py` (façade)**

```python
"""Scheduler module — periodic execution of Actions and project operations.

Entry point: register_scheduler(app, cfg, db, *, wizard_step, wizard_finish,
wizard_escape, run_action, project_ops).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from .db import SchedulerDB
from .executor import Executor, ProjectOps
from .handlers import register_handlers

if TYPE_CHECKING:
    from telegram.ext import Application

    from ..config import Config
    from ..db import DB


logger = logging.getLogger(__name__)


def register_scheduler(
    app: "Application",
    cfg: "Config",
    db: "DB",
    *,
    wizard_step: Callable,
    wizard_finish: Callable,
    wizard_escape: Callable,
    run_action: Callable[[str], Awaitable[tuple[int, str]]],
    project_ops: ProjectOps,
) -> SchedulerDB:
    """Wire the scheduler module into the Telegram Application.

    Returns the `SchedulerDB` so callers (e.g. admin notifs toggle in bot.py)
    can read/write `bot_settings.notifications_enabled` directly.
    """
    scheduler_db = SchedulerDB(cfg.data_dir / "projects.db")
    executor = Executor(
        scheduler_db=scheduler_db,
        run_action=run_action,
        project_ops=project_ops,
        bot=app.bot,
        allowed_user_ids=cfg.allowed_user_ids,
    )

    register_handlers(
        app, cfg, db, scheduler_db, executor,
        wizard_step=wizard_step,
        wizard_finish=wizard_finish,
        wizard_escape=wizard_escape,
    )

    # Replay enabled tasks into APScheduler at startup.
    from .triggers import build_trigger
    scheduler = app.job_queue.scheduler
    for task in scheduler_db.list_tasks(only_enabled=True):
        try:
            trigger = build_trigger(task["trigger_kind"], task["trigger_spec"])
            scheduler.add_job(
                executor.run_task,
                trigger=trigger,
                args=[task["id"]],
                id=f"sched:{task['id']}",
                misfire_grace_time=None,
                max_instances=1,
                replace_existing=True,
            )
        except Exception:
            logger.exception("failed to replay scheduled task %s at boot", task["id"])

    return scheduler_db
```

- [ ] **Step 2: Mettre à jour `tgbot/bot.py:37` — ajouter l'import**

Sous `from .trading import register_trading` (ligne 37), ajouter :

```python
from .scheduler import register_scheduler
```

- [ ] **Step 3: Étendre `_main_menu_markup` (lignes 123-134)**

Remplacer la fonction `_main_menu_markup` par :

```python
def _main_menu_markup(trading_enabled: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("📂 Projets", callback_data="menu:projects"),
        InlineKeyboardButton("🚀 Actions", callback_data="menu:actions"),
    ]]
    rows.append([InlineKeyboardButton("⏰ Planifié", callback_data="menu:scheduled")])
    if trading_enabled:
        rows.append([InlineKeyboardButton("📈 Trading", callback_data="trd:home")])
    rows.append([
        InlineKeyboardButton("⚙️ Admin", callback_data="menu:admin"),
        InlineKeyboardButton("❓ Aide", callback_data="menu:help"),
    ])
    return InlineKeyboardMarkup(rows)
```

- [ ] **Step 4: Étendre `_admin_menu_markup` (lignes 137-142) — accepter l'état des notifs**

Remplacer la fonction `_admin_menu_markup` par :

```python
def _admin_menu_markup(notifications_enabled: bool = True) -> InlineKeyboardMarkup:
    notifs_label = (
        "🔔 Notifs : ON" if notifications_enabled else "🔕 Notifs : OFF"
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(notifs_label, callback_data="bot:notifs")],
        [InlineKeyboardButton("🔄 Redémarrer le bot", callback_data="bot:restart")],
        [InlineKeyboardButton("📥 Update bot", callback_data="bot:update")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")],
    ])
```

- [ ] **Step 5: Préparer une référence partagée à `SchedulerDB` dans `build_app`**

Dans `build_app` (juste après `trading_enabled = bool(cfg.trading and cfg.trading.enabled)` autour de la ligne 311), ajouter :

```python
    scheduler_db_holder: dict = {}  # populated by register_scheduler at end of build_app
```

Et plus loin, à l'appel de `_admin_menu_markup()` (lignes ~671, 739), remplacer chaque occurrence de `_admin_menu_markup()` par :

```python
_admin_menu_markup(
    scheduler_db_holder["sdb"].get_notifications_enabled()
    if scheduler_db_holder.get("sdb")
    else True
)
```

- [ ] **Step 6: Ajouter la route `menu:scheduled` et l'action `bot:notifs` dans `on_callback`**

Dans `on_callback`, à la fin du bloc `if ns == "menu":` (juste avant la ligne `return` finale après `elif target == "admin":`), ajouter :

```python
            elif target == "scheduled":
                sdb = scheduler_db_holder.get("sdb")
                if sdb is None:
                    await query.edit_message_text(
                        "Scheduler indisponible.",
                        reply_markup=_main_menu_markup(trading_enabled),
                    )
                    return
                from .scheduler.handlers import register_handlers  # noqa: F401
                tasks = sdb.list_tasks()
                # Inline minimal version of _render_list to avoid circular wiring:
                rows: list[list[InlineKeyboardButton]] = []
                from .scheduler.triggers import describe_trigger
                for t in tasks:
                    check = "✓" if t["enabled"] else "✗"
                    label = f"{check} {t['name']} — {describe_trigger(t['trigger_kind'], t['trigger_spec'])}"
                    rows.append([InlineKeyboardButton(label, callback_data=f"sched:card:{t['id']}")])
                rows.append([InlineKeyboardButton("➕ Nouvelle", callback_data="sched:new")])
                rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")])
                text = f"*Tâches planifiées ({len(tasks)})*" if tasks else "*Tâches planifiées*\nAucune tâche."
                await query.edit_message_text(
                    text, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(rows),
                )
            return
```

Puis dans le bloc `if ns == "bot":`, ajouter une branche `elif target == "notifs":` AVANT le `elif target == "restart":` (ligne ~677) :

```python
            if target == "notifs":
                sdb = scheduler_db_holder.get("sdb")
                if sdb is not None:
                    sdb.set_notifications_enabled(not sdb.get_notifications_enabled())
                await query.edit_message_text(
                    "*⚙️ Admin*\nOpérations sur le bot lui-même :",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_admin_menu_markup(
                        sdb.get_notifications_enabled() if sdb else True
                    ),
                )
                return
```

- [ ] **Step 7: Ajouter `/scheduled` à la liste `BotCommand` dans `post_init`**

Dans la liste `commands` autour de la ligne 1701, ajouter après `BotCommand("delaction", ...)` :

```python
            BotCommand("scheduled", "List/manage scheduled tasks"),
```

- [ ] **Step 8: Brancher `register_scheduler` à la fin de `build_app` (après `register_trading`)**

Remplacer le bloc final `register_trading(...)` (lignes 1761-1767) par :

```python
    register_trading(
        app, cfg,
        wizard_step=_wizard_step,
        wizard_finish=_wizard_finish,
        wizard_escape=_wizard_escape,
    )

    class _ProjectOps:
        @staticmethod
        async def start(name: str) -> tuple[bool, str]:
            proj = db.get_project(name)
            if not proj or not proj.get("start_command"):
                return False, "project not configured"
            return await runner.start(name, proj["start_command"], proj["path"])

        @staticmethod
        async def stop(name: str) -> bool:
            return await _stop_project(name)

        @staticmethod
        async def restart(name: str) -> tuple[bool, str]:
            return await _restart_project(name)

    scheduler_db_holder["sdb"] = register_scheduler(
        app, cfg, db,
        wizard_step=_wizard_step,
        wizard_finish=_wizard_finish,
        wizard_escape=_wizard_escape,
        run_action=_run_action_by_name,
        project_ops=_ProjectOps,
    )
```

- [ ] **Step 9: Smoke test : lancement du bot, table créée, menu affiche `⏰ Planifié`**

```
python -m tgbot config.toml
```

Vérifier dans les logs : pas d'exception au boot. Dans Telegram, `/start` doit afficher le bouton `⏰ Planifié`. Cliquer dessus doit afficher `*Tâches planifiées (0)*\nAucune tâche.` avec un bouton `➕ Nouvelle`.

- [ ] **Step 10: Lancer toute la test suite**

```
pytest -v
```

Expected : 26 passed (7 + 11 + 8).

- [ ] **Step 11: Commit**

```
git add tgbot/scheduler/__init__.py tgbot/bot.py
git commit -m "feat(scheduler): wire scheduler into bot.py + main menu + admin notifs toggle"
```

---

## Task 8: End-to-end smoke test manuel

Tâche de validation. Aucun code modifié.

- [ ] **Step 1: Préparer une Action simple pour le test**

Dans Telegram :
- `/addaction` → nom `sched-ping`, mode `oneshot`, sans confirmation, commande `echo ping`, pas de cwd.

- [ ] **Step 2: Créer une tâche planifiée "toutes les 1 min" sur `sched-ping`**

- `/start` → `⏰ Planifié` → `➕ Nouvelle`.
- Type : `🚀 Action enregistrée`.
- Action : `sched-ping`.
- Récurrence : `Toutes les X min` → entrer `1`.
- Nom : `Test ping`.
- Confirmer.

- [ ] **Step 3: Observer 2 exécutions consécutives**

Attendre ~2 minutes. Vérifier qu'un message `⏰ Test ping · ✅` apparaît deux fois (notifications ON par défaut). La fiche de la tâche doit afficher `Dernière : <timestamp> · ✅ ok`.

- [ ] **Step 4: Toggle notifications dans Admin**

`/start` → `⚙️ Admin` → `🔔 Notifs : ON` (le bouton bascule à `🔕 Notifs : OFF`). Attendre 1 min. Aucune notification ne doit arriver, mais la fiche montre que `last_run_at` est bien mis à jour.

Rebasculer sur ON.

- [ ] **Step 5: Désactiver la tâche depuis sa fiche**

Ouvrir la fiche, cliquer `⏸ Désactiver`. Attendre 1 min — plus d'exécution. Le bouton est devenu `▶️ Activer`.

- [ ] **Step 6: Redémarrer le bot via Admin**

`⚙️ Admin` → `🔄 Redémarrer le bot` → confirmer. Après redémarrage, le menu principal réapparaît.

Réactiver la tâche `Test ping`. Vérifier qu'elle exécute à nouveau dans la minute suivante → la persistance + replay au boot fonctionnent.

- [ ] **Step 7: Tester une tâche project_op**

Créer une tâche planifiée :
- Type : `📂 Opération projet`.
- Projet : choisir un projet existant.
- Opération : `restart`.
- Récurrence : `Quotidien à HH:MM` → entrer `04:00` (ou une heure utile).
- Nom : `Daily restart`.

Cliquer `▶️ Exécuter maintenant` depuis la fiche. Le projet doit redémarrer immédiatement et la dernière exécution doit s'afficher `✅ ok`.

- [ ] **Step 8: Tester une expression cron invalide**

Lancer un wizard, choisir `Expression cron…`, taper `pas un cron`. Le wizard doit re-prompter avec `⚠️ Cron invalide…`. Taper `*/5 * * * *`, ça doit accepter.

- [ ] **Step 9: Supprimer la tâche de test**

Ouvrir la fiche `Test ping` → `🗑 Supprimer` → confirmer. La liste doit ne plus la contenir.

- [ ] **Step 10: Commit (rien à committer — étape de validation)**

Aucun changement de code à cette étape. Si tout est OK, on est prêt à merger.

---

## Self-review notes

**Spec coverage check :**
- Planification d'Actions ✅ (Task 6 wizard `task_type=action`).
- Planification d'opérations Projet (start/stop/restart) ✅ (Task 6 wizard `project_op` + Task 7 `_ProjectOps`).
- Presets simples + mode cron ✅ (Task 3 + Task 6 `_ask_trigger`).
- Survie aux redémarrages ✅ (Task 7 Step 8 replay au boot).
- Toggle notifications global ✅ (Task 2 `bot_settings` + Task 7 Step 4 admin button).
- Ignorer les exécutions manquées ✅ (Task 6 `misfire_grace_time=None`).
- `max_instances=1` ✅ (Task 6 `_schedule`).
- Table `scheduled_tasks` au schéma exact de la spec ✅ (Task 2 SCHEMA).
- Vue liste / fiche / wizard 5 états ✅ (Task 6 — étendu à 10 états pour couvrir les sous-prompts presets, ce que la spec autorise dans `SCHED_TRIGGER`).
- Bouton `⏰ Planifié` dans MAIN_MENU ✅ (Task 7 Step 3).
- Toggle notifs dans Admin ✅ (Task 7 Steps 4 + 6).
- Tests `test_scheduler_triggers.py` / `test_scheduler_db.py` / `test_scheduler_executor.py` ✅ (Tasks 1, 3, 5).
- Test manuel : 1 min, redémarrer, désactiver/réactiver ✅ (Task 8 Steps 3, 6, 5).

**Hypothèses confirmées (à vérifier au moment de l'exécution) :**
- `app.job_queue.scheduler` est bien l'`AsyncIOScheduler` APScheduler — vrai pour python-telegram-bot v21 (utilisé par le repo).
- `cfg.allowed_user_ids` contient des `user_id` Telegram qui, en chat privé, sont aussi des `chat_id` valides.

