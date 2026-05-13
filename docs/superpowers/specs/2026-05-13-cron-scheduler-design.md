# Design — Planificateur de tâches (cron) pour teleProjectManager

**Date :** 2026-05-13
**Statut :** Validé, prêt pour implémentation
**Auteur :** theo-bggtt

## Contexte

Le bot Telegram `teleProjectManager` gère déjà :
- des **Projets** (start / stop / restart / logs)
- des **Actions** (commandes nommées et réutilisables)
- un module **Trading** (alertes MC, watch wallets)
- une section **Admin** (redémarrage du bot)

On ajoute un **planificateur** permettant d'exécuter automatiquement à heure fixe (ou à intervalle) une Action enregistrée ou une opération sur un Projet (start/stop/restart).

Ce design fait partie d'un sous-projet plus large « Workflow dev intégré » (cron, git intégré, backup, déploiement). Le planificateur est la première brique, choisie en premier car elle devient le moteur des autres features (un backup planifié = une Action backup + une tâche planifiée).

## Objectifs

- Planifier l'exécution d'**Actions enregistrées** et d'**opérations sur Projet** (start/stop/restart).
- Supporter à la fois des **presets** simples (toutes les X min, quotidien à HH:MM, hebdo jour J à HH:MM) et un mode **expression cron** pour les cas avancés.
- **Survivre aux redémarrages** du bot.
- Notifier l'utilisateur de chaque exécution si un toggle global « notifications » est activé dans Admin.
- **Ignorer** les exécutions manquées pendant que le bot était arrêté (pas de rattrapage).

## Non-objectifs (YAGNI)

- Historique complet des exécutions (on garde seulement le dernier statut).
- Notifications par tâche individuelle (un seul toggle global suffit pour le moment).
- Déclencheurs « git pull » / « backup auto » natifs : ces features seront livrées plus tard sous forme d'Actions, qui pourront alors être planifiées via ce système.
- Commandes shell brutes planifiables : passer par une Action est plus propre et auditable.

## Approche technique retenue

**PTB JobQueue + persistance SQLite.**

`python-telegram-bot` v20+ expose un `JobQueue` qui utilise `APScheduler` (`AsyncIOScheduler`) en interne. On a donc gratuitement le scheduler et les triggers (`IntervalTrigger`, `CronTrigger`) sans nouvelle dépendance externe.

La persistance des jobs est gérée par nous : la table `scheduled_tasks` dans `projects.db` est l'unique source de vérité. Au démarrage du bot, on rejoue cette table dans le scheduler. Toute modification (CREATE / UPDATE / TOGGLE / DELETE) écrit d'abord en DB, puis synchronise le scheduler.

Alternative écartée : APScheduler avec jobstore SQLite séparé. Rejetée car elle introduit deux sources de vérité (DB des projets/actions vs jobstore APScheduler), ce qui complique la migration de schéma et les opérations atomiques.

## Architecture

Nouveau module `tgbot/scheduler/` :

```
tgbot/scheduler/
├── __init__.py        # façade : register_scheduler(app, db, cfg)
├── db.py              # CRUD des tâches planifiées
├── triggers.py        # parsing presets → triggers APScheduler
├── executor.py        # exécution d'une tâche + notification
└── handlers.py        # callbacks Telegram (wizard, liste, fiche)
```

`register_scheduler(app, db, cfg)` est appelé depuis `tgbot/bot.py` au démarrage. Il :
1. Crée la table `scheduled_tasks` si absente (idempotent).
2. Récupère toutes les tâches `enabled=1` et les ajoute au `app.job_queue.scheduler` via `add_job(executor.run_task, trigger, args=[task_id], id=f"sched:{task_id}")`.
3. Enregistre les `CallbackQueryHandler` / `ConversationHandler` du wizard.

## Modèle de données

Une seule nouvelle table dans `projects.db` :

```sql
CREATE TABLE scheduled_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,           -- libellé affiché ("Restart trading bot")
    task_type    TEXT NOT NULL,           -- 'action' | 'project_op'
    target       TEXT NOT NULL,           -- nom de l'action OU nom du projet
    operation    TEXT,                    -- 'start'|'stop'|'restart' si task_type='project_op', sinon NULL
    trigger_kind TEXT NOT NULL,           -- 'interval' | 'daily' | 'weekly' | 'cron'
    trigger_spec TEXT NOT NULL,           -- JSON, voir ci-dessous
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run_at  TEXT,                    -- ISO 8601, NULL tant que pas exécuté
    last_status  TEXT,                    -- 'ok'|'error'|NULL
    created_at   TEXT NOT NULL
);
```

Format de `trigger_spec` selon `trigger_kind` :

| `trigger_kind` | `trigger_spec` exemple                      | Trigger APScheduler                      |
|----------------|---------------------------------------------|------------------------------------------|
| `interval`     | `{"minutes": 10}`                           | `IntervalTrigger(minutes=10)`            |
| `daily`        | `{"hour": 4, "minute": 0}`                  | `CronTrigger(hour=4, minute=0)`          |
| `weekly`       | `{"day_of_week": "mon", "hour": 3, "minute": 0}` | `CronTrigger(day_of_week='mon', hour=3, minute=0)` |
| `cron`         | `{"expr": "0 4 * * 1"}`                     | `CronTrigger.from_crontab("0 4 * * 1")`  |

Nouveau toggle global pour les notifications : ajouté à une mini-table `bot_settings (key TEXT PRIMARY KEY, value TEXT)` avec la clé `notifications_enabled` (valeurs `'1'`/`'0'`). Cette table accueillera d'autres réglages globaux plus tard sans nouvelle migration.

## Flux UI

### Bouton dans le menu principal

Ajouter `⏰ Planifié` à `MAIN_MENU` dans `tgbot/bot.py` (`_main_menu_markup`).

### Vue liste

```
Tâches planifiées (3)
─────────────────────
✓ Restart trading bot     quotidien 04:00         [⚙️]
✓ Backup nightly action   hebdo lundi 03:00       [⚙️]
✗ Stress test             toutes les 10 min       [⚙️]

[➕ Nouvelle]  [← Retour]
```

`✓`/`✗` indique `enabled`. `[⚙️]` ouvre la fiche de la tâche.

### Fiche d'une tâche

```
⏰ Restart trading bot
─────────────────────
Type        : opération projet
Cible       : trading-bot · restart
Récurrence  : quotidien 04:00
Statut      : ✓ activée
Dernière    : 2026-05-13 04:00 · ✅ ok

[⏯ Activer/Désactiver]
[▶️ Exécuter maintenant]
[🗑 Supprimer]
[← Retour]
```

### Wizard de création (`ConversationHandler`)

5 états :

1. **`SCHED_TYPE`** — boutons `Action enregistrée` / `Opération sur projet`.
2. **`SCHED_TARGET`** — liste paginée (réutilise `_actions_list_markup` ou `_projects_list_markup`) selon le type choisi.
3. **`SCHED_OP`** *(uniquement si `project_op`)* — boutons `start` / `stop` / `restart`.
4. **`SCHED_TRIGGER`** — 4 boutons presets + `Expression cron…` :
   - `Toutes les X min` → sous-prompt nombre (entier 1–1440).
   - `Quotidien à HH:MM` → saisie texte format `HH:MM`.
   - `Hebdo : <jour> à HH:MM` → choix du jour (boutons) puis HH:MM.
   - `Expression cron` → saisie libre, validée par `CronTrigger.from_crontab(expr)` ; sur exception → message d'erreur et redemande.
5. **`SCHED_NAME`** — saisie libre, puis écran de confirmation listant tous les choix, bouton `Confirmer` → `INSERT` en DB + `add_job` dans le scheduler.

### Toggle notifications dans Admin

Ajouter un bouton `🔔 Notifs : ON` / `🔕 Notifs : OFF` dans `_admin_menu_markup`. Toggle écrit dans `bot_settings`.

## Exécution

`executor.run_task(task_id: int)` :

1. Recharge la tâche depuis la DB (en cas de modification entre temps).
2. Si `enabled=0` → no-op (sécurité, le job devrait déjà être absent du scheduler).
3. Dispatch :
   - `task_type='action'` → appelle la même logique que `/runaction <target>` (factoriser dans `_run_action_by_name` réutilisable).
   - `task_type='project_op'` → appelle `runner.start/stop/restart(target)` via le `make_runner()` existant.
4. Capture exit code + temps écoulé.
5. `UPDATE scheduled_tasks SET last_run_at=?, last_status=?` (`'ok'` si code 0, sinon `'error'`).
6. Si `bot_settings.notifications_enabled='1'` → envoyer au chat admin (`cfg.admin_chat_id`) :
   ```
   ⏰ <name> · ✅/❌ · <durée>
   ```
   Pas d'envoi de stdout dans la notification ; pour le détail, l'utilisateur ouvre la fiche de la tâche (qui pourra afficher le dernier output si on ajoute un champ `last_output` plus tard — pas dans ce design).

## Cycle de vie & persistance

| Évènement                | DB                                                | Scheduler                               |
|--------------------------|---------------------------------------------------|-----------------------------------------|
| Bot boot                 | `SELECT * WHERE enabled=1`                        | `add_job` pour chaque ligne             |
| Création                 | `INSERT`                                          | `add_job(id="sched:<id>")`              |
| Activer                  | `UPDATE enabled=1`                                | `add_job`                               |
| Désactiver               | `UPDATE enabled=0`                                | `remove_job("sched:<id>")`              |
| Modifier la récurrence   | `UPDATE trigger_spec`                             | `remove_job` puis `add_job`             |
| Supprimer                | `DELETE`                                          | `remove_job` (try/except `JobLookupError`) |
| Bot redémarré (admin)    | Aucune action côté DB                             | Le scheduler est recréé → re-register   |

**Tâches manquées :** `misfire_grace_time=None` à la création des jobs APScheduler → les exécutions manquées sont silencieusement ignorées (comportement validé).

**Concurrence :** `max_instances=1` par job → si une exécution déborde sur la suivante, la prochaine est skip (log warning).

## Tests

- `tests/test_scheduler_triggers.py`
  - `interval` → `IntervalTrigger` aux bons paramètres.
  - `daily` / `weekly` → `CronTrigger` aux bons paramètres.
  - `cron` valide → trigger OK ; cron invalide → `ValueError` propre.
- `tests/test_scheduler_db.py`
  - Insertion + relecture, round-trip JSON `trigger_spec`.
  - Toggle `enabled` persiste.
- `tests/test_scheduler_executor.py`
  - Action mock → met à jour `last_run_at` + `last_status='ok'`.
  - Action qui échoue → `last_status='error'`.
  - Toggle notifications OFF → pas d'envoi Telegram (mock du bot).
- **Test manuel** : créer une tâche "toutes les 1 min" sur une action triviale, attendre 2 exécutions, redémarrer le bot via le bouton admin, vérifier que les exécutions reprennent. Désactiver/réactiver, vérifier que le job disparaît/réapparaît sans relancer le bot.

## Plan d'implémentation (haut niveau)

1. Migration DB : créer `scheduled_tasks` et `bot_settings` (avec valeur par défaut `notifications_enabled='1'`).
2. `tgbot/scheduler/db.py` + tests.
3. `tgbot/scheduler/triggers.py` + tests.
4. `tgbot/scheduler/executor.py` + tests (avec mocks PTB).
5. Factoriser `_run_action_by_name` extrait de `bot.py` pour réutilisation.
6. `tgbot/scheduler/handlers.py` : wizard ConversationHandler + vues liste/fiche.
7. Bouton `⏰ Planifié` dans `MAIN_MENU` + toggle notifs dans menu admin.
8. `register_scheduler()` + branchement dans `bot.py` au boot.
9. Tests manuels end-to-end + commit.

Le détail (étapes atomiques, fichiers touchés ligne par ligne) sera produit par le skill `writing-plans` à l'étape suivante.
