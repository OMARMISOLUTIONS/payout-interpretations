# payout-interpretations

Rapports automatiques de commits (résumé opérationnel + interprétation rédigés par Claude, postés sur Slack)
pour les dépôts GitHub surveillés — exécutés depuis CE dépôt privé, sans rien installer dans les dépôts sources.

## Contenu
- `.github/workflows/commit-report.yml` — cron (2 h, heures ouvrées, lun-ven) + lancement manuel avec backfill
- `.github/scripts/commit_report.py` — découverte des dépôts, clone lecture seule, état, rapports, Slack, Markdown
- `prompts/` — prompt système par dépôt (`<owner>__<repo>.md`, sinon `default.md`) : modifiable sans toucher au code
- `requirements.txt` — versions et empreintes SHA-256 figées (`pip --require-hashes`)
- `state/last-seen.json` — dernier SHA vu par dépôt/branche + SHA déjà rapportés ; `state/reports.jsonl` — journal des rapports (fiches) pour les récaps
- `docs/DISCUSSION.md` — genèse, décisions et pièges rencontrés
- `skills/reportgit/SKILL.md` — skill Claude décrivant le dispositif (à copier dans les skills utilisateur)

## Secrets attendus (Settings → Secrets and variables → Actions)
`SOURCE_TOKEN` (fine-grained, Contents Read-only, resource owner PAYOUT-fr) · `INTERPRETATION_API_KEY` (Anthropic)
· `WEBHOOK_URL` (canal Slack par défaut) · `WEBHOOK_URL_PAYOUTUI` (canal dédié payoutUI)
Optionnels : `SOURCE_TOKEN_2/3` (autres organisations), autres `WEBHOOK_URL_*` (routage par dépôt).

## Opérations courantes
- Ajouter un dépôt : l'ajouter au token, à `SOURCE_REPOS` dans le yml (ou vider la ligne = tous les dépôts visibles),
  et si canal dédié : webhook + secret + ligne `SLACK_WEBHOOK_URL__<OWNER_REPO>` dans le yml.
- Récap hebdo/mensuel : automatique (lundi 08h Paris / le 1er) ou Run workflow → `recap` week|month.
- Reprendre l'historique : Actions → Run workflow → backfill per-day · repo · branch · since (all/30d/date).
- Adapter un rapport : éditer le fichier dans `prompts/`.
- Investigation du code avant rapport : liste `DEEP_DIVE_REPOS` dans le yml (le modèle lit le dépôt pour répondre lui-même aux points d'attention).
