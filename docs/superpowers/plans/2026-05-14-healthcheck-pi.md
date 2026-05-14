# Healthcheck Pi Implementation Plan

**Goal :** Ajouter un bouton "🩺 Health Pi" au menu admin qui affiche un snapshot système (uptime, CPU, RAM, disque, top processus) en un seul message éditable.

**Architecture :** Nouveau module pur `tgbot/healthcheck.py` exposant `collect()` (lecture psutil) et `format_snapshot()` (rendu HTML). `tgbot/bot.py` reçoit un bouton et une branche de callback. `tgbot/config.py` reçoit un champ `health_mounts` configurable.

**Tech Stack :** Python 3.12+, `psutil>=5.9`, python-telegram-bot v21, pytest.

**Spec :** `docs/superpowers/specs/2026-05-14-healthcheck-pi-design.md`

---

## File Structure

| Action | Fichier                         | Responsabilité                                 |
| ------ | ------------------------------- | ---------------------------------------------- |
| ➕     | `tgbot/healthcheck.py`          | `collect`, `format_snapshot`, helpers          |
| ➕     | `tests/test_healthcheck.py`     | Tests unitaires (helpers + format + smoke)     |
| ✏️     | `tgbot/config.py`               | `health_mounts: list[str]`                     |
| ✏️     | `config.example.toml`           | Documenter `health_mounts`                     |
| ✏️     | `requirements.txt`              | `psutil>=5.9`                                  |
| ✏️     | `tgbot/bot.py`                  | Bouton admin + callback                        |

---

## Task 1 : Ajouter la dépendance psutil

- [ ] **Step 1.1 : Mettre à jour `requirements.txt`**
  - Ajouter `psutil>=5.9` après les deps trading.
- [ ] **Step 1.2 : Installer dans le venv local**
  ```
  .venv\Scripts\python -m pip install -r requirements.txt
  ```
  Vérifier `python -c "import psutil; print(psutil.__version__)"`.

---

## Task 2 : Helpers purs (`_format_duration`, `_format_bytes`)

- [ ] **Step 2.1 : Écrire les tests d'abord (TDD)**

Créer `tests/test_healthcheck.py` avec les tests des deux helpers :

```python
import pytest
from tgbot.healthcheck import _format_duration, _format_bytes

def test_format_duration_seconds():
    assert _format_duration(0) == "0s"
    assert _format_duration(45) == "45s"

def test_format_duration_minutes():
    assert _format_duration(90) == "1m 30s"

def test_format_duration_hours():
    assert _format_duration(3700) == "1h 1m"

def test_format_duration_days():
    assert _format_duration(90061) == "1j 1h 1m"

def test_format_bytes_zero():
    assert _format_bytes(0) == "0 B"

def test_format_bytes_kb():
    assert _format_bytes(2048) == "2.0 KB"

def test_format_bytes_mb():
    assert _format_bytes(1_572_864) == "1.5 MB"

def test_format_bytes_gb():
    assert _format_bytes(1_610_612_736) == "1.5 GB"
```

- [ ] **Step 2.2 : Implémenter les helpers**

Créer `tgbot/healthcheck.py` avec `_format_duration` et `_format_bytes`. Lancer `pytest tests/test_healthcheck.py -k "format_duration or format_bytes"` → vert.

---

## Task 3 : Dataclasses + `collect()`

- [ ] **Step 3.1 : Définir les dataclasses**
  - `DiskUsage`, `ProcessInfo`, `HealthSnapshot` selon la spec.

- [ ] **Step 3.2 : Implémenter `collect(mounts, top_n=3)`**
  - `uptime_seconds = int(time.time() - psutil.boot_time())`
  - `cpu_percent = psutil.cpu_percent(interval=0.1)`
  - `load_avg` : `try: psutil.getloadavg() except (AttributeError, OSError): None`
  - RAM : `vm = psutil.virtual_memory()` → used/total/percent
  - Disques : pour chaque mount, `try: psutil.disk_usage(mount)` (skip si OSError, log warning)
  - Processus :
    1. Premier passage `for p in psutil.process_iter(['pid','name'])` pour amorcer `p.cpu_percent()`.
    2. `time.sleep(0.1)`.
    3. Second passage pour lire `cpu_percent()` et `memory_info().rss`, en `try/except (NoSuchProcess, AccessDenied)`.
    4. Trier par CPU descendant → top N → `top_cpu`. Idem par RSS → `top_ram`.

- [ ] **Step 3.3 : Test smoke**
  ```python
  def test_collect_returns_snapshot(tmp_path):
      # mount qui existe sur win et linux : la racine du dossier de test
      snap = collect([str(tmp_path)])
      assert snap.uptime_seconds > 0
      assert snap.ram_total_bytes > 0
      assert isinstance(snap.cpu_percent, float)
      # top peut être vide en CI bridé mais le type est correct
      assert isinstance(snap.top_cpu, list)
  ```

---

## Task 4 : `format_snapshot()`

- [ ] **Step 4.1 : Tests**

```python
from tgbot.healthcheck import (
    HealthSnapshot, DiskUsage, ProcessInfo, format_snapshot,
)

def _sample(load_avg=(0.4, 0.6, 0.5), top_cpu=None, top_ram=None):
    return HealthSnapshot(
        uptime_seconds=90061,
        cpu_percent=23.0,
        load_avg=load_avg,
        ram_used_bytes=1_288_490_188,
        ram_total_bytes=4_294_967_296,
        ram_percent=30.0,
        disks=[DiskUsage(mount="/", used_bytes=13_207_960_780, total_bytes=68_719_476_736, percent=19.0)],
        top_cpu=top_cpu if top_cpu is not None else [
            ProcessInfo(pid=1, name="python3", cpu_percent=18.0, rss_bytes=257_000_000),
        ],
        top_ram=top_ram if top_ram is not None else [
            ProcessInfo(pid=1, name="python3", cpu_percent=18.0, rss_bytes=257_000_000),
        ],
    )

def test_format_snapshot_contains_essentials():
    text = format_snapshot(_sample())
    assert "Uptime" in text
    assert "CPU" in text
    assert "RAM" in text
    assert "1j 1h 1m" in text  # uptime formaté
    assert "/" in text  # mount
    assert "python3" in text

def test_format_snapshot_no_load_avg():
    text = format_snapshot(_sample(load_avg=None))
    assert "n/a" in text.lower()

def test_format_snapshot_empty_top():
    text = format_snapshot(_sample(top_cpu=[], top_ram=[]))
    # pas de KeyError, sections gracieusement omises ou marquées
    assert "Top CPU" in text  # section présente
    # mais pas de ligne process
```

- [ ] **Step 4.2 : Implémentation**

Rendu en `<pre>...</pre>` HTML escapé. Aligner colonnes avec padding manuel. Tronquer les noms process à 24 chars.

Lancer toute la suite : `pytest tests/test_healthcheck.py` → vert.

---

## Task 5 : Config `health_mounts`

- [ ] **Step 5.1 : Étendre `Config` dataclass dans `tgbot/config.py`**
  - Ajouter `health_mounts: list[str] = field(default_factory=lambda: ["/"])`.
  - Dans `load()` : `health_mounts=list(raw.get("health_mounts", ["/"]))`.

- [ ] **Step 5.2 : Documenter dans `config.example.toml`**
  - Bloc commenté :
    ```toml
    # Mount points surveillés par le healthcheck admin (bouton 🩺 Health Pi).
    # Sur Windows en dev, utiliser ["C:\\"] ou ["."].
    # health_mounts = ["/"]
    ```

---

## Task 6 : Wire dans `bot.py`

- [ ] **Step 6.1 : Import**
  ```python
  from .healthcheck import collect as health_collect, format_snapshot as health_format
  ```

- [ ] **Step 6.2 : Helper markup**
  Près des autres `_*_markup` :
  ```python
  def _health_markup() -> InlineKeyboardMarkup:
      return InlineKeyboardMarkup([
          [InlineKeyboardButton("🔄 Refresh", callback_data="admin:health:show")],
          [InlineKeyboardButton("⬅️ Retour", callback_data="menu:admin")],
      ])
  ```

- [ ] **Step 6.3 : Bouton dans `_admin_menu_markup`**
  Ajouter une ligne après Shell, avant Restart :
  ```python
  [InlineKeyboardButton("🩺 Health Pi", callback_data="admin:health:show")],
  ```

- [ ] **Step 6.4 : Branche dans `on_callback`**
  Avant le `parts = data.split(":", 2)`, ajouter :
  ```python
  if data == "admin:health:show":
      snap = health_collect(cfg.health_mounts)
      text = health_format(snap)
      try:
          await query.edit_message_text(
              text,
              parse_mode=ParseMode.HTML,
              reply_markup=_health_markup(),
          )
      except BadRequest as e:
          if "not modified" not in str(e).lower():
              raise
      return
  ```

- [ ] **Step 6.5 : Vérification manuelle**
  - Lancer `python -m tgbot` (ou la commande de boot du repo).
  - Telegram : `/start` → ⚙️ Admin → 🩺 Health Pi → snapshot s'affiche en < 1 s.
  - 🔄 Refresh → uptime augmente, autres valeurs cohérentes.
  - ⬅️ Retour → menu admin.

---

## Task 7 : Vérification finale

- [ ] **Step 7.1 : Tests**
  ```
  pytest -q
  ```
  Tous les tests (anciens + healthcheck) doivent passer.

- [ ] **Step 7.2 : Lint manuel** : relire `tgbot/healthcheck.py` pour types et docstrings minimales.

- [ ] **Step 7.3 : Commit**
  ```
  feat(healthcheck): admin button showing uptime/CPU/RAM/disk/top procs

  Adds tgbot/healthcheck.py (psutil-based, cross-platform), config field
  health_mounts, and an admin-menu entry "🩺 Health Pi" with a Refresh button.
  ```

- [ ] **Step 7.4 : Push + PR `feat/healthcheck-pi` → `main`** (sur demande utilisateur).
