# Design — Healthcheck Pi

Date : 2026-05-14
Branche cible : nouvelle branche `feat/healthcheck-pi` (depuis `main`)

## Contexte

Le bot tourne sur Raspberry Pi 5 et joue un rôle d'orchestrateur (projets, scheduler, trading, shell mode). Quand le Pi rame ou ne répond plus normalement, il faut aujourd'hui SSH puis taper `uptime`, `free -h`, `df -h`, `ps aux | sort` à la main. C'est lent et fastidieux depuis le téléphone.

## Objectif

Ajouter un bouton "🩺 Health" dans le menu admin qui affiche en un message un snapshot complet de l'état système : uptime, CPU, RAM, disque, top processus. Cible : < 1 seconde de rendu.

## Non-objectifs

- Pas de monitoring continu / pas d'alerte automatique (un autre feature future via le scheduler pourra réutiliser ce module).
- Pas de température CPU ni de services systemd dans cette itération (sera ajouté plus tard si besoin).
- Pas d'historique : c'est un snapshot instantané, pas une série temporelle.
- Pas d'action de remédiation (kill, restart) depuis l'écran healthcheck — seulement de la lecture.

## Décisions

| Sujet                  | Choix                                                              |
| ---------------------- | ------------------------------------------------------------------ |
| Bibliothèque           | `psutil` (cross-platform, dispo sur Pi et Windows pour dev)        |
| Métriques              | Uptime + CPU% + load avg + RAM + disques + top 3 CPU + top 3 RAM   |
| Mount points           | Configurables via `config.toml` (`health_mounts = ["/"]` par défaut) |
| Cross-platform         | `load_avg` retourne `None` sur Windows, formaté `n/a`              |
| Placement bouton       | Menu admin uniquement                                              |
| UX                     | Single-message édité en place, bouton 🔄 Refresh                   |
| Auth                   | Hérité de `restricted(cfg.allowed_user_ids)`                       |
| Persistance            | Aucune                                                             |

## UX

1. `/menu` → ⚙️ Admin → `🩺 Health Pi`.
2. Le message admin est édité en panel healthcheck :
   ```
   🩺 Healthcheck

   ⏱  Uptime  : 4j 12h 03m
   📊 CPU     : 23% (load 0.4 / 0.6 / 0.5)
   🧠 RAM     : 1.2 / 4.0 GB (30%)
   💾 /       : 12.3 / 64.0 GB (19%)

   🔥 Top CPU
     1. python3                  18%
     2. node                      4%
     3. systemd-journald          2%

   🐘 Top RAM
     1. python3                245 MB
     2. postgres                80 MB
     3. nginx                   30 MB
   ```
   Boutons : `[🔄 Refresh]` `[⬅️ Retour]`.
3. `🔄 Refresh` ré-exécute `collect()` et `edit_message_text` (callback `admin:health:show`, donc même chemin que l'entrée).
4. `⬅️ Retour` → callback `menu:admin` (re-render le menu admin).

### Rendu Markdown

- Format `<pre>...</pre>` HTML (parse_mode HTML) pour préserver l'alignement en colonnes.
- Les noms de processus tronqués à 24 caractères pour éviter le wrap sur mobile.

## Architecture

### Nouveau module : `tgbot/healthcheck.py`

```python
"""System healthcheck snapshot for the admin menu."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

import psutil


@dataclass
class DiskUsage:
    mount: str
    used_bytes: int
    total_bytes: int
    percent: float


@dataclass
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float
    rss_bytes: int


@dataclass
class HealthSnapshot:
    uptime_seconds: int
    cpu_percent: float
    load_avg: Optional[tuple[float, float, float]]  # None sur Windows
    ram_used_bytes: int
    ram_total_bytes: int
    ram_percent: float
    disks: list[DiskUsage] = field(default_factory=list)
    top_cpu: list[ProcessInfo] = field(default_factory=list)
    top_ram: list[ProcessInfo] = field(default_factory=list)


def collect(mounts: list[str], *, top_n: int = 3) -> HealthSnapshot:
    """Snapshot système synchrone. Retourne dans tous les cas (skip silencieusement
    les erreurs Pi-specific sur Windows)."""
    ...


def format_snapshot(snap: HealthSnapshot) -> str:
    """Format HTML/pre prêt pour Telegram."""
    ...


def _format_duration(seconds: int) -> str:
    """123456 -> '1j 10h 17m'."""
    ...


def _format_bytes(n: int) -> str:
    """1610612736 -> '1.5 GB'."""
    ...
```

**Pureté** : `collect()` est la seule fonction "impure" (lit `psutil`). `format_snapshot()` et helpers sont purs et testables sans mock.

**Top processus** : itération via `psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info'])`, deux passes :
1. Première lecture pour amorcer les compteurs CPU (`cpu_percent` retourne 0.0 au premier appel).
2. Sleep court (~100 ms) puis seconde lecture → tri par CPU% (top_cpu) et par RSS (top_ram).

Coût : ~100 ms total. Acceptable pour un snapshot à la demande.

### Intégration dans `bot.py`

**Config** :
```python
# tgbot/config.py — ajouts au dataclass Config
health_mounts: list[str] = field(default_factory=lambda: ["/"])
```

Loader :
```python
health_mounts=list(raw.get("health_mounts", ["/"])),
```

**Bouton dans `_admin_menu_markup`** : nouvelle ligne après Shell, avant Restart :
```python
[InlineKeyboardButton("🩺 Health Pi", callback_data="admin:health:show")],
```

**Branche de callback dans `on_callback`** : nouvelle exact-match avant le split générique :
```python
if data == "admin:health:show":
    snap = collect(cfg.health_mounts)
    text = format_snapshot(snap)
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_health_markup(),
    )
    return
```

**Markup helper** :
```python
def _health_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin:health:show")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu:admin")],
    ])
```

### Sécurité

- Callback gardé par le décorateur `@auth` du `on_callback` existant.
- Aucune information sensible exposée : noms de processus, chiffres système. Pas d'arguments de commande, pas de variables d'environnement.
- Pas d'écriture sur le système : lecture seule via psutil.

### Gestion des erreurs

- `psutil.getloadavg()` raise `AttributeError` sur Windows → caught, `load_avg = None`, format affiche `load n/a`.
- `psutil.disk_usage(mount)` raise `OSError` sur mount invalide → caught par mount, ce mount est sauté (warning loggé), les autres continuent.
- `psutil.process_iter` peut raise `NoSuchProcess` / `AccessDenied` par process → filtrés via `try/except` dans la boucle.

## Tests

Fichier : `tests/test_healthcheck.py`. Tout est synchrone et indépendant de Telegram.

- **`test_format_duration`** : 0 → `"0s"`, 90 → `"1m 30s"`, 3700 → `"1h 1m"`, 90061 → `"1j 1h 1m"`.
- **`test_format_bytes`** : 0, 1024, 1.5 GB, edge cases.
- **`test_format_snapshot_contains_essential_metrics`** : un `HealthSnapshot` fait main → output contient "Uptime", "CPU", "RAM", "💾 /", noms top processus.
- **`test_format_snapshot_no_load_avg`** : `load_avg=None` → texte contient `n/a`.
- **`test_format_snapshot_empty_top`** : `top_cpu=[]` et `top_ram=[]` → output n'inclut pas les sections vides (ou affiche `(aucun)`).
- **`test_collect_returns_snapshot`** (smoke) : `collect(["/"])` (ou un mount qui existe sur la plateforme courante) retourne un `HealthSnapshot` cohérent (uptime > 0, ram_total > 0). Pas de mock, pour valider que l'intégration psutil fonctionne sur la plateforme du test.

## Fichiers modifiés

| Action | Fichier                                                            | Pourquoi                                              |
| ------ | ------------------------------------------------------------------ | ----------------------------------------------------- |
| ➕     | `tgbot/healthcheck.py`                                             | Module collecte + formattage                          |
| ➕     | `tests/test_healthcheck.py`                                        | Tests unitaires                                       |
| ✏️     | `tgbot/config.py`                                                  | Champ `health_mounts`                                 |
| ✏️     | `config.example.toml`                                              | Documenter `health_mounts`                            |
| ✏️     | `requirements.txt`                                                 | Ajouter `psutil>=5.9`                                 |
| ✏️     | `tgbot/bot.py`                                                     | Bouton admin, callback `admin:health:show`            |
| ➕     | `docs/superpowers/specs/2026-05-14-healthcheck-pi-design.md`       | Ce document                                           |
| ➕     | `docs/superpowers/plans/2026-05-14-healthcheck-pi.md`              | Plan d'exécution                                      |
