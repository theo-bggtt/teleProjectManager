# Single-Message Wizard — Design Spec

**Date :** 2026-05-11
**Branche :** `feat/trading-module` (à dériver — voir Implementation Notes)
**Auteur :** Water
**Statut :** Validé, prêt pour planification

## Contexte

Aujourd'hui, les flux multi-étapes du bot (ajout de projet, ajout d'action, configuration) s'étalent sur plusieurs messages : le bot pose une question, l'utilisateur répond (nouveau message), le bot répond à nouveau, etc. Le fil de conversation se remplit rapidement de messages intermédiaires.

L'objectif est de transformer ces flux en un **wizard à message unique** : un seul message du bot évolue d'étape en étape, les réponses textuelles de l'utilisateur sont supprimées dès traitement, et à la fin le message redevient le menu principal — laissant le fil ultra-propre.

## Comportement attendu

1. L'utilisateur clique sur un bouton du menu ou tape une commande qui démarre un flux.
2. Un **unique message wizard** apparaît avec :
   - Les boutons du menu principal en haut (inchangés)
   - Le texte de la question courante en dessous
   - Un bouton `❌ Annuler` en dernière ligne du markup
3. L'utilisateur répond par texte → son message est supprimé immédiatement, le message wizard est édité pour afficher la question suivante.
4. Pour les étapes à choix discrets (mode oneshot/longrun, oui/non), des boutons inline remplacent la saisie texte — l'édition se fait sur callback, sans message utilisateur à supprimer.
5. À la fin du flux (succès ou annulation), le message wizard est édité une dernière fois pour **redevenir le menu principal propre**. Aucune confirmation persistante, aucun nouveau message envoyé.

## Architecture

### Helper central

Un nouvel helper `_wizard_step(update, context, text, extra_buttons=None)` est ajouté comme closure dans `build_app()`. Il :

- Cherche `context.user_data["wizard_msg_id"]` et `wizard_chat_id`.
- Construit le markup : boutons du menu principal (`_main_menu_markup`) + lignes `extra_buttons` optionnelles + ligne `[❌ Annuler → wiz:cancel]`.
- Si `wizard_msg_id` existe : `bot.edit_message_text(chat_id, msg_id, text, reply_markup)`. En cas d'échec (message trop ancien, supprimé), fallback `reply_text` et mise à jour de l'id.
- Sinon : envoie un nouveau message via `reply_text`, stocke `wizard_msg_id` et `wizard_chat_id`.

### Helper de fin

`_wizard_finish(update, context, success=True)` :

- Édite le message wizard pour qu'il redevienne le menu principal (mêmes boutons que `_main_menu_markup`, texte d'accueil standard).
- Nettoie `user_data` : pop de `wizard_msg_id`, `wizard_chat_id`, et de toutes les clés `addact_*`, `addproj_*`, `cfg_*` selon le flux.
- Retourne `ConversationHandler.END`.

### Suppression du message utilisateur

Chaque `MessageHandler` qui traite une réponse texte fait en première ligne :
```python
try:
    await update.message.delete()
except Exception:
    pass  # silencieux : permissions, message déjà supprimé, etc.
```

Puis utilise `_wizard_step` pour avancer (et non `reply_text`).

### Démarrage du wizard

| Source | Action |
|---|---|
| Bouton inline (CallbackQuery) | `wizard_msg_id = query.message.message_id` ; `wizard_chat_id = query.message.chat_id` ; on édite directement le message du menu courant |
| Commande slash (`/addpath`, `/addaction`, `/config`) | `_wizard_step` est appelé sans id stocké → il fait `reply_text` et capture l'id du message envoyé |

### Annulation

Nouveau callback `wiz:cancel` routé dans `on_callback` :
- Appelle `_wizard_finish(update, context, success=False)`
- Retourne `ConversationHandler.END`

Le callback est aussi enregistré comme `fallback` sur chaque `ConversationHandler` (en plus du fallback `/cancel` existant).

### Edge cases

| Cas | Comportement |
|---|---|
| `edit_message_text` échoue (msg ancien/supprimé) | Fallback : nouveau `reply_text`, met à jour `wizard_msg_id` |
| `delete_message` du message user échoue | Log silencieux, on continue (UX dégradée mais fonctionnelle) |
| Nom dupliqué (projet ou action) | Réaffiche la même étape avec préfixe `⚠️ Ce nom existe déjà.\n\n` au-dessus de la question |
| Choix invalide via callback boutons | Réaffiche la même étape (pas d'erreur visible) |
| Timeout ConversationHandler (300s) | Le `conversation_timeout` callback (à ajouter si absent) appelle `_wizard_finish` ; si échec d'édition, on laisse tomber silencieusement |
| Wizard démarré alors qu'un autre est en cours | Le nouveau démarrage écrase `wizard_msg_id` ; l'ancien message reste inerte (ses boutons mènent à un état nettoyé). Acceptable. |

## Refonte des trois flux

### `add_conv` (ajout de projet)

| État | Question | Markup | Validation |
|---|---|---|---|
| `ADD_NAME` | "📂 Nom du projet ?" | menu + Annuler | non vide, pas de `:`, pas en doublon |
| `ADD_PATH` | "📄 Chemin du fichier d'entrée pour `{name}` ?" | menu + Annuler | non vide |
| (succès) | retour menu | — | — |

### `action_add_conv` (ajout d'action)

| État | Question | Markup |
|---|---|---|
| `ADD_A_NAME` | "🚀 Nom de l'action ?" | menu + Annuler |
| `ADD_A_MODE` | "Mode d'exécution ?" | menu + `[⚡ Oneshot]` `[🔁 Long-running]` + Annuler |
| `ADD_A_CONFIRM` | "Demander confirmation avant exécution ?" | menu + `[✅ Oui]` `[❌ Non]` + Annuler |
| `ADD_A_CMD` | "Commande shell à exécuter ?" | menu + Annuler |
| `ADD_A_CWD` | "📁 Répertoire de travail (optionnel) ?" | menu + `[⏭️ Passer]` + Annuler |
| (succès) | retour menu | — |

Pour `ADD_A_CWD`, l'utilisateur peut soit cliquer `⏭️ Passer` (callback `addact:cwd:skip`), soit taper un chemin. Le bouton `Passer` route vers la finalisation directement.

### `config_conv` (configuration projet)

L'entrée se fait via `/config` qui affiche d'abord une liste de projets (mécanisme existant à conserver, en éditant le message courant pour devenir le wizard).

| État | Question | Markup |
|---|---|---|
| (entrée via `/config`) | "Sélectionne un projet à configurer" | menu + boutons projets + Annuler |
| `CFG_START_CMD` | "Commande de démarrage pour `{name}` ?" | menu + Annuler |
| `CFG_ENTRY_FILE` | "Nom du fichier de log d'entrée pour `{name}` ?" | menu + Annuler |
| (succès) | retour menu | — |

## Impact sur le code existant

- **`tgbot/bot.py`** :
  - Ajout de `_wizard_step` et `_wizard_finish` comme closures dans `build_app`.
  - Réécriture des handlers : `add_name`, `add_path`, `cmd_config`/`config_select`, `cfg_entry_file`, `cfg_cancel`, `action_add_start`, et tous les états du `action_add_conv`.
  - Ajout de la route `wiz:cancel` dans `on_callback`.
  - Suppression des appels à `_send_main_menu` dans les chemins terminaux des 3 flux (le helper reste pour d'éventuels autres appels).
  - Toutes les réponses texte des handlers concernés font un `update.message.delete()` en première ligne.
- **Aucun changement de schéma DB.**
- **Aucun changement de config.**

## Critères de succès

- Démarrer `/addpath` puis répondre aux deux questions ne laisse **qu'un seul message** dans le fil (le menu principal final).
- Démarrer le flux via bouton inline depuis le menu n'envoie **aucun nouveau message** — le message courant est édité tout du long.
- Le bouton `❌ Annuler` ferme proprement le wizard à n'importe quelle étape.
- Le bouton `⏭️ Passer` saute l'étape `cwd` sans envoyer de message texte.
- Un échec de `delete_message` sur le message utilisateur ne casse pas le flux.

## Non-objectifs

- Pas de refonte des flux Trading (`/holdings`, alertes MC, etc.) — ils n'utilisent pas ConversationHandler.
- Pas de modification des flux de visualisation (`/projects`, `/actions`).
- Pas d'animation/confirmation éphémère type "✅ pendant 2 secondes".
- Pas de prise en charge des messages utilisateur non-texte (photos, fichiers) pendant le wizard — comportement actuel conservé.

## Implementation Notes

- La branche actuelle `feat/trading-module` contient déjà des changements non commités sur `tgbot/bot.py` (le wiring `_send_main_menu`). À la planification, décider si on dérive une nouvelle branche `feat/wizard-single-message` ou si on étend la branche courante.
- Aucune nouvelle dépendance.
- Tests manuels en chat privé Telegram suffisants — pas de framework de tests automatisés en place sur le projet.
