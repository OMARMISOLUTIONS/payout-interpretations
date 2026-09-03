---
name: reportgit
description: Dispositif privé de rapports automatiques de commits du dépôt OMARMISOLUTIONS/payout-interpretations (résumé opérationnel + interprétation + audit rédigés par Claude via l'API Anthropic, postés sur Slack) couvrant les dépôts GitHub de Mike sans rien installer dedans. Utiliser SYSTÉMATIQUEMENT dès qu'on touche à ce dispositif ou qu'on parle de « rapport de commits », « commit report », « payout-interpretations », « backfill », « SOURCE_TOKEN », « WEBHOOK_URL », « prompts par dépôt », « canal Slack par repo », ajout d'un dépôt surveillé, modification du cron, échec d'un run Actions du dépôt, ou reprise d'historique. Contient l'architecture, les noms de secrets, les modes, les décisions FIGÉES et les pièges déjà rencontrés (off booléen YAML, null==false sur schedule, git --since). NE PAS réinventer : corriger à partir de l'existant.
---

# Reportgit — rapports de commits interprétés

## Architecture (FIGÉE)
- Dépôt d'exécution : `OMARMISOLUTIONS/payout-interpretations` (privé, compte perso — jamais dans l'org PAYOUT-fr).
- Rien dans les dépôts surveillés : ni workflow, ni secret, ni run. Clone en lecture seule (`--no-checkout`,
  `--filter=blob:none`, repli clone complet), token via credential helper lisant l'env — jamais en URL/config/log.
- Fichiers : `.github/workflows/commit-report.yml` (cron `7 6-20/2 * * 1-5` UTC + workflow_dispatch),
  `.github/scripts/commit_report.py`, `prompts/<owner>__<repo>.md` sinon `prompts/default.md`,
  `requirements.txt` (hashes, `pip --require-hashes`), `state/last-seen.json` (commité par le run),
  artefact Markdown `commit-reports-<run_id>` (90 j).
- Rapport Slack : header `repo · branche · N commits` (⏪ si backfill), liste des commits liés, puis sections
  *Résumé opérationnel* / *IA — prompts, modèles, coûts* (payoutUI, conditionnelle) / *Interprétation* /
  *Audit & préconisations* ([bloquant]/[recommandé]/[optionnel]) / *À tester* / *Points d'attention*.

## Secrets (repo payout-interpretations)
`SOURCE_TOKEN` = PAT fine-grained, resource owner PAYOUT-fr, Contents **Read-only** (+ Metadata auto) ;
`SOURCE_TOKEN_2/3` pour d'autres owners (un fine-grained ne couvre qu'un owner). `INTERPRETATION_API_KEY` = clé
Anthropic (workspace plafonné). `WEBHOOK_URL` = canal Slack par défaut ; `WEBHOOK_URL_PAYOUTUI` = canal payoutUI.
Jamais de token classic scope `repo` (lecture-écriture globale).

## Réglages courants (env du yml)
- `SOURCE_REPOS: "PAYOUT-fr/payoutWebsite,PAYOUT-fr/payoutUI"` — vider = tous les dépôts visibles par les tokens ;
  `SOURCE_OWNERS` / `SOURCE_EXCLUDE` en complément.
- Routage : `SLACK_WEBHOOK_URL__<OWNER_REPO>` (majuscules, tout sauf A-Z0-9 → `_`) → secret dédié ; défaut en repli ;
  URLs multiples par virgules ; dépôt sans webhook : pas de post, Markdown conservé.
- Modèle : `claude-sonnet-5` par défaut, sélecteur par run (fable-5 ≈ ×5 le coût, haiku-4-5 ≈ ÷2).

## Investigation (deep dive)
`DEEP_DIVE_REPOS` (env du yml, actuellement `PAYOUT-fr/payoutUI` ; `all` = tous, vide = aucun) : avant d'écrire,
le modèle dispose de trois outils lecture seule sur l'arbre du dépôt FIGÉ au commit rapporté (`lister`, `chercher`
= git grep, `lire_fichier`), plafond `MAX_TOOL_CALLS` (15). Consigne : vérifier ses propres points d'attention dans
le code — ce que le code résout passe dans les sections avec la réponse, seuls les vrais arbitrages humains restent
en *Points d'attention*. Un dépôt en investigation est cloné COMPLET (pas de --filter). Coût ×3 à ×10 par rapport.
Plafond dur de MAX_TOOL_CALLS+3 allers-retours puis repli sur le mode simple (pas de boucle possible).
Génération : `max_tokens` 12 000 puis 24 000 si `stop_reason=max_tokens`, `output_config.effort` = REPORT_EFFORT
(medium), mention _(rapport tronqué…)_ si encore coupé, avertissement si une section obligatoire manque.

## Récapitulatifs hebdo / mensuel
Crons dédiés `0 6 * * 1` (lundi, semaine ISO précédente) et `0 6 1 * *` (le 1er, mois précédent) ; manuel via input
`recap` (week|month). Compteurs FACTUELS calculés par le script sur toutes les branches (`git log --remotes --numstat`,
filtrage des dates en Python, périodes en heure de Paris, numstat demandé pour les seuls commits de la période) :
par dépôt × auteur = commits, +lignes, −lignes, heures-sessions (heuristique git-hours : gap ≤ 2 h compté en continu,
+1 h par session — ESTIMATION du temps actif, ni plancher ni plafond), jours actifs, commits hors ouvrées (week-end
ou 22h–6h Paris), commits marqués IA ; bloc « par auteur, tous dépôts » avec sessions fusionnées (pas de double
compte dans le TOTAL) ; tableau de la période précédente pour la tendance. Le modèle rédige seulement la lecture (*Lecture de
la période* / *Heures* / *Livré / en cours* / *Signal IA* / *Vigilance*) à partir de ces compteurs et des FICHES
journalisées dans `state/reports.jsonl` (une ligne par rapport : repo, branche, SHA, dates, auteurs, fiche).
Posté sur le canal par défaut (`WEBHOOK_URL`) + Markdown. Le run commite tout `state/`.

## Modes
- Planifié : par dépôt/branche, commits depuis le dernier SHA vu ; premier passage ou branche réécrite → fenêtre
  `FIRST_RUN_WINDOW` (24h) ; dédup globale des SHA (merge preview→main non re-rapporté).
- Backfill (Run workflow) : `backfill` per-day|per-commit · `repo` (owner/repo ou nom seul) · `branch` · `since`
  (all | 30d | YYYY-MM-DD). payoutUI : reprendre `preview`.
- `DRY_RUN=1` en local : ni Claude ni Slack, mais clone + Markdown + état.

## Pièges DÉJÀ RENCONTRÉS (ne pas retomber dedans)
1. YAML 1.1 : `off` nu dans `options:` = booléen false → toujours quoter `"off"`.
2. Expressions GitHub : coercition numérique, sur un cron `inputs.*` est null et `null == false` est VRAI →
   `POST_SLACK: ${{ (github.event_name == 'schedule' || inputs.slack == true) && 'true' || 'false' }}`.
   Contrôler tout nouveau yml avec actionlint.
3. `git log --since` coupe l'historique au premier commit trop vieux (rebase/antidaté) → filtrage des dates en Python
   (`commits_since`), jamais `--since` pour délimiter.
4. Secrets d'org ≠ secrets de repo perso ; un secret n'est jamais réaffiché ; une clé Anthropic n'est visible qu'à la création.
5. Durcissement en place à préserver : actions épinglées par SHA (checkout v4.4.0 `11d5960a326750d5838078e36cf38b85af677262`,
   setup-python v5.6.0 `a26af69be951a213d495a4c3e4e4022e16d87065`, upload-artifact v4.6.2 `ea165f8d65b6e75b540449e92b4886f43607fa02`),
   `--require-hashes`, réglage repo « Allow actions created by GitHub » ; toute nouvelle dépendance passe par la
   régénération de requirements.txt avec hashes.

## Contexte org (décisions du 01/09/2026)
PAYOUT-fr : *Base permissions* = **No permission** (accès directs conservés), création de repos par les membres coupée,
fork des privés désactivé. Externes → *outside collaborator*. L'app GitHub pour Slack (`/github subscribe … commits:*`)
reste utilisable pour des notifications brutes, indépendante de ce dispositif.
