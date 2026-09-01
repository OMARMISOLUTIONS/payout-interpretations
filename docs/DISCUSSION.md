# Genèse du dispositif — récapitulatif de la discussion (01/09/2026)

## Point de départ
Besoin initial : « alerte git dans Slack ». Première réponse : app GitHub officielle pour Slack
(`/github subscribe owner/repo pulls reviews comments commits:*`, `commits:*` = toutes branches,
`/github unsubscribe … issues releases` pour couper le bruit, `/github subscribe list features` pour contrôler).
Toujours valable pour des notifications brutes, indépendamment du dispositif ci-dessous.

## Évolution du besoin
1. Notifications brutes → **rapports interprétés** : résumé opérationnel + interprétation rédigés par Claude
   (API Anthropic) à partir des commits et du diff, postés sur Slack.
2. Couvrir **tous les commits, toutes branches, tous auteurs** — aucun paramétrage nominatif.
3. Pouvoir **reprendre l'historique** (backfill par jour ou par commit, artefact Markdown consolidé).
4. **Confidentialité totale** : personne ne doit voir le dispositif. Un workflow dans le dépôt source expose
   commit, runs et secrets à quiconque a le droit d'écriture → architecture déplacée dans un dépôt privé séparé
   (`payout-interpretations`, hors org), qui clone les sources en lecture seule. Rien n'apparaît dans les dépôts
   surveillés : ni fichier, ni run, ni secret.
5. **Multi-dépôts** : découverte automatique de tout ce que voit le token (ou liste explicite `SOURCE_REPOS`),
   état par dépôt/branche, déduplication des SHA (un merge preview→main ne re-rapporte pas).
6. **Prompts par dépôt** : `prompts/default.md` (sites) et `prompts/PAYOUT-fr__payoutUI.md` (app IA native :
   radars IA/argent/banque/données/sécurité/infra, section conditionnelle *IA — prompts, modèles, coûts*),
   puis section *Audit & préconisations* ([bloquant]/[recommandé]/[optionnel]) ajoutée aux deux.
7. **Routage Slack** : un canal par dépôt (`SLACK_WEBHOOK_URL__<OWNER_REPO>` → secret dédié), défaut en repli,
   plusieurs URLs par valeur possibles (virgules).

## Sécurité mise en place
- Token fine-grained **Contents : Read-only** (resource owner PAYOUT-fr), révocable ; jamais dans une URL, une
  config git ou un log (credential helper lisant l'environnement) ; clone sans blobs.
- Clé Anthropic dans un workspace à plafond de dépense ; webhook qui ne sait que poster ; canal privé.
- Chaîne d'exécution sans tiers : actions officielles GitHub épinglées par SHA de commit
  (checkout v4.4.0 `11d5960a…`, setup-python v5.6.0 `a26af69b…`, upload-artifact v4.6.2 `ea165f8d…`),
  paquets PyPI figés par empreintes (`--require-hashes`), et réglage repo « Allow actions created by GitHub ».
- Données sortantes : diff (≤ 150 k caractères) vers l'API Anthropic — pas d'entraînement sur les contenus API,
  pas de rétention par défaut hors « Covered Models » (Fable 5 : 30 j ; Sonnet 5 : non) ; rapports vers Slack.
- Côté org PAYOUT-fr : *Base permissions* passé à **No permission** (le CTO garde ses accès directs — constat :
  10 → 8, les 2 perdus étaient implicites), création de repos par les membres coupée, fork des privés désactivé ;
  recommandé : *outside collaborator* pour les externes, retirer aux membres le droit de supprimer/transférer.

## Pièges rencontrés (consignés dans le skill)
- YAML 1.1 : `off` non quoté dans une liste d'options = booléen `false` (affiché tel quel par GitHub).
- Coercition des expressions GitHub : sur un run planifié `inputs.slack` est null et `null == false` est VRAI
  (cast numérique) → `POST_SLACK` retombait à false sur tous les crons. Corrigé :
  `(github.event_name == 'schedule' || inputs.slack == true) && 'true' || 'false'`. Contrôle final actionlint 1.7.12 : 0 erreur.
- `git log --since` coupe le parcours au premier commit trop vieux (rebase, commit antidaté) → filtrage des dates
  en Python.
- Les secrets d'organisation ne sont pas lisibles depuis un dépôt personnel ; un secret GitHub n'est jamais
  réaffiché (remplaçable seulement) ; une clé Anthropic n'est montrée qu'à la création.
- GitHub Desktop : les fichiers cachés (`.github`, `.gitignore`) sont bien commités même si l'explorateur les masque.

## État au 01/09/2026
Dépôt `OMARMISOLUTIONS/payout-interpretations` (privé, hors org PAYOUT-fr). Surveillés : `PAYOUT-fr/payoutWebsite`
(canal par défaut) et `PAYOUT-fr/payoutUI` (canal dédié, prompt IA enrichi). Cron `7 6-20/2 * * 1-5` (UTC).
Backfill payoutUI recommandé sur la branche `preview` (contient l'historique de main + le travail en cours).
