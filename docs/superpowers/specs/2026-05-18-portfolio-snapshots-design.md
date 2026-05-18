# Portfolio Snapshots + Graph — Design Spec

**Date** : 2026-05-18
**Statut** : design validé, prêt pour planification
**Module concerné** : `tgbot/trading/`

## 1. Contexte & objectif

Le module trading actuel expose les holdings d'un wallet à un instant T, mais ne conserve aucune historique. Cette feature ajoute un **snapshot quotidien automatique** de la valeur agrégée USD de tous les wallets surveillés, plus un **graph d'évolution sur 30 jours** accessible depuis le menu Trading.

Objectif utilisateur : voir en un clic l'évolution de la valeur totale du portfolio sans ouvrir DexScreener / Debank, et garder un historique perpétuel pour mesurer la perf à long terme.

## 2. Décisions principales

| Décision | Choix | Raison |
|---|---|---|
| Fréquence snapshot | Quotidien (1×/jour) | Équilibre coût API / résolution |
| Vue par défaut | Total USD agrégé (tous wallets confondus) | Vision macro, plus simple, suffisante pour V1 |
| Période affichée par défaut | 30 jours | Bonne granularité de lecture, boutons 7j/90j/all dispo |
| Rétention historique | Totale (pas de purge) | Coût stockage négligeable (~1.8 MB/an) |
| Déclenchement Telegram | Bouton "📊 Portfolio" dans menu Trading | Pas de push auto |
| Lib graph | `matplotlib` | Génère PNG offline, déjà standard Python |
| Réutilisation scheduler | Oui (`tgbot/scheduler/`) | Évite un loop async dédié |

## 3. Architecture & emplacement

Trois fichiers touchés / créés dans `tgbot/trading/` :

- **`portfolio.py`** *(nouveau)* — logique métier :
  - `take_snapshot(db, monitor) -> dict` — appelle les fetchers existants, agrège, persiste.
  - `load_history(db, period: str) -> list[Snapshot]` — lit avec filtre temporel.
  - `render_chart(snapshots, period_days) -> bytes` — PNG via matplotlib.
- **`db.py`** *(extension)* — table `portfolio_snapshots` + méthodes `add_snapshot()` / `list_snapshots(since)`.
- **`handlers.py`** *(extension)* — bouton `trd:portfolio` dans `_home_markup()`, callbacks `trd:portfolio[:period]`, `trd:portfolio:force`.

**Découpe :** `portfolio.py` isole la logique testable (calcul, rendu) du transport Telegram. `db.py` reste l'unique point d'accès SQLite. Le scheduler existant évite de réinventer un loop.

## 4. Modèle de données

Nouvelle table dans `trading.db` :

```sql
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT    NOT NULL,       -- ISO 8601 UTC, ex "2026-05-18T08:00:00Z"
    total_usd   REAL    NOT NULL,       -- somme USD agrégée tous wallets
    wallets_ok  INTEGER NOT NULL,       -- nb wallets fetchés OK
    wallets_ko  INTEGER NOT NULL,       -- nb wallets en échec
    raw_json    TEXT                    -- détail par wallet/token, JSON, pour debug + vues futures
);
CREATE INDEX IF NOT EXISTS idx_snapshots_taken_at ON portfolio_snapshots(taken_at);
```

**Choix clés :**
- `total_usd` matérialisé → vue par défaut très rapide, pas d'agrégation à la lecture.
- `raw_json` conserve la composition par wallet/token. Permet plus tard des vues "par token" ou "par wallet" sans nouveau backfill.
- `wallets_ok` / `wallets_ko` → badge "snapshot partiel" si un RPC était down.
- Pas de table `portfolio_holdings` séparée : YAGNI tant qu'une seule vue agrégée existe.

## 5. Flux du snapshot quotidien

**Trigger** : job cron `0 8 * * *` enregistré via le scheduler existant au premier démarrage du bot (si pas déjà présent). Modifiable par l'utilisateur via les commandes scheduler standard. Fuseau : **Europe/Paris**.

**Algorithme `take_snapshot()` :**

1. `wallets = trading_db.list_wallets()`
2. Init `total_usd = 0.0`, `detail = {}`, `ok = 0`, `ko = 0`.
3. Pour chaque wallet, sous `asyncio.Semaphore(4)` :
   - Try : appeler `fetch_solana_holdings` ou `fetch_evm_holdings` (existants).
   - Succès → `total_usd += w_total`, ajouter au `detail`, `ok += 1`.
   - Échec → `logger.warning(...)`, `ko += 1`.
4. Si `ok == 0 and wallets != []` → **ne pas écrire** le snapshot (éviter d'inscrire un 0 trompeur), log error, retry au prochain run.
5. Sinon → `trading_db.add_snapshot(taken_at=utc_now_iso(), total_usd, ok, ko, raw_json=json.dumps(detail))`.
6. Log info final.

**Concurrence & idempotence :**
- Sémaphore 4 → évite de saturer Helius/Alchemy si beaucoup de wallets.
- Pas de protection anti-doublon journée (YAGNI). Deux snapshots à 30s d'écart même jour = data correcte, non critique.
- Pas de lock entre snapshot daily et "force snapshot" UI : indépendants au pire on a 2 entrées proches.

**Pas de notification Telegram** au snapshot (silent, conforme au choix UX).

## 6. Rendu graphique

**Fonction `render_chart(snapshots, period_days=30) -> bytes`**

1. Filtre la fenêtre `[now - period_days, now]` (ou tout si `period == "all"`).
2. Si `len(snapshots) == 0` → image "Pas encore de données".
3. Figure 10×5 inches @ 100 DPI (~1000×500 px), style `dark_background`.
4. Plot : ligne + marqueurs sur chaque point.
5. Axe Y formaté `$K`/`$M` (`$1.2K`, `$850`, `$12.3K`).
6. Axe X formaté en dates courtes (`12 May`).
7. Annotations haut-droite :
   - `Actuel : $X`
   - `30j : +X.X%` (vert si positif, rouge sinon)
   - `ATH : $X (date)`
8. Footer "⚠ Snapshot partiel" si `wallets_ko > 0` sur le dernier point.
9. `fig.savefig(BytesIO, format='png', bbox_inches='tight')` → bytes.

**Calcul perf :** `pct = (last.total_usd / ref.total_usd - 1) * 100` où `ref` = premier snapshot ≥ `(now - period_days)`. Si moins de snapshots que la période demandée, on prend le premier dispo et le label devient "depuis le début : +X%".

## 7. UX inline keyboard

**Ajout dans `_home_markup()` :**

```python
[InlineKeyboardButton("📊 Portfolio", callback_data="trd:portfolio")]
```

**Flow utilisateur :**

1. Clic `📊 Portfolio` → callback `trd:portfolio` (period par défaut = 30j).
2. Réponse immédiate `⏳ Génération du graph…` (edit du message courant).
3. Charge snapshots, génère PNG, envoie via `send_photo(chat_id, photo=BytesIO, caption=...)`.
4. Caption :
   ```
   📊 Portfolio (30j)
   Actuel: $12,345
   30j: +12.3% 🟢
   Snapshots: 28 (2 partiels)
   ```
5. Reply markup :
   ```
   [ 7j ]  [ 30j ✓ ]  [ 90j ]  [ All ]
   [ ⬅️ Retour Trading ]
   ```
6. Clic période → callback `trd:portfolio:7|30|90|all` → re-génère et `edit_message_media()` pour remplacer l'image.

**Cas DB vide (aucun snapshot encore pris) :**
Image "Pas encore de données — le premier snapshot sera pris à 08:00 demain" + bouton "🔄 Snapshot maintenant" (callback `trd:portfolio:force`). Ce bouton disparaît dès qu'au moins 1 snapshot existe en DB.

**Callback namespace** : tout sous `trd:portfolio[:period|:force]`, routé via `on_trading_callback` existant. Pas de nouveau dispatcher.

## 8. Gestion d'erreurs

| Cas | Comportement |
|---|---|
| Snapshot daily : RPC fail sur un wallet | Wallet KO, snapshot enregistré avec `wallets_ko > 0`, log warning. |
| Snapshot daily : tous les wallets KO | **Aucune écriture** DB, log error, retry au prochain run quotidien. |
| Aucun wallet surveillé (`list_wallets() == []`) | Snapshot avec `total_usd=0, wallets_ok=0`. Graph affiche "Aucun wallet — ajoute-en un dans 👛 Wallets". |
| Bouton Portfolio cliqué, DB vide | Image placeholder + bouton "🔄 Snapshot maintenant". |
| `render_chart()` plante | Try/except → edit_message "❌ Erreur lors du rendu : `<msg>`" + bouton retour. Log exception. |
| Force snapshot : échec | Edit "❌ Snapshot échoué : `<error>`" + bouton retry. Pas d'écriture DB. |

**Logging :**
- `logger.info("snapshot ok", extra={"total_usd", "ok", "ko"})`
- `logger.warning("snapshot wallet failed", extra={"wallet", "chain", "error"})`
- `logger.error("snapshot fully failed, skipping")` si `ok == 0` et wallets non vides.

## 9. Tests

- **`test_portfolio_db.py`** : add/list snapshots, filtres temporels (7/30/90/all), round-trip sérialisation `raw_json`.
- **`test_portfolio_chart.py`** : rendu avec 0 / 1 / N snapshots, PNG non vide (taille > 1KB), pas de comparaison pixel.
- **`test_portfolio_snapshot.py`** : `take_snapshot()` avec mocks des fetchers — succès complet, échec partiel, échec total (assert pas d'écriture).
- Pas de tests d'intégration on-chain (fetchers existants déjà testés).

## 10. Hors-scope V1

Explicitement reporté à une V2 si besoin émerge :

- Vues alternatives (par wallet, par token, par chaîne).
- PnL réel/non-réalisé (nécessite reconstruction historique on-chain — gros projet séparé).
- Push notifications quotidiennes/hebdomadaires.
- Bouton "Snapshot now" persistant (uniquement présent quand DB vide en V1).
- Export CSV.
- Configuration utilisateur de l'heure du cron via UI dédiée (reste éditable via commandes scheduler standard).

## 11. Estimation effort

~1-2 jours de dev pour un développeur familier avec le codebase. La plus grosse pièce inconnue est le tuning matplotlib pour un rendu lisible — prévoir une demi-journée d'itération visuelle.
