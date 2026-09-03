#!/usr/bin/env python3
"""Rapports de commits de TOUS les dépôts visibles par un token en lecture seule → Slack.

Exécuté depuis un dépôt privé séparé ; rien n'est écrit dans les dépôts surveillés.
Découverte : chaque token SOURCE_TOKEN, SOURCE_TOKEN_2 … SOURCE_TOKEN_9 (un par organisation ou compte)
liste les dépôts qu'il peut lire (GET /user/repos) ; SOURCE_OWNERS restreint aux propriétaires listés,
SOURCE_REPOS impose une liste explicite owner/repo, SOURCE_EXCLUDE retire des dépôts.
Modes :
  - planifié (défaut)   : pour chaque dépôt et chaque branche, un rapport des commits apparus depuis le
                          dernier passage (état dans STATE_FILE, commité dans CE dépôt).
  - BACKFILL=per-day    : reprise de l'historique d'un dépôt (REPO_INPUT) sur une branche (BRANCH_INPUT),
                          un rapport par jour ; per-commit = un rapport par commit.
Chaque rapport est posté sur Slack (sauf POST_SLACK=false) et ajouté à reports/*.md (artefact du run).
Autres env : ANTHROPIC_API_KEY, SLACK_WEBHOOK_URL, REPORT_MODEL (défaut claude-sonnet-5), STATE_FILE,
SINCE, POST_SLACK, MAX_DIFF_CHARS, MAX_COMMITS, FIRST_RUN_WINDOW (défaut 24h), SOURCE_BASE_URL (tests).
DRY_RUN=1 : n'appelle ni Claude ni Slack ; clone, écrit Markdown et état.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Paris")
except Exception:  # tzdata absente : on retombe sur UTC
    TZ = timezone.utc

MODEL = os.environ.get("REPORT_MODEL") or "claude-sonnet-5"
MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "150000"))
MAX_COMMITS = int(os.environ.get("MAX_COMMITS", "40"))
STATE_FILE = os.environ.get("STATE_FILE", "state/last-seen.json")
FIRST_RUN_WINDOW = os.environ.get("FIRST_RUN_WINDOW", "24h")
BASE_URL = os.environ.get("SOURCE_BASE_URL", "https://github.com/")
DEEP_DIVE = {r.strip().lower() for r in (os.environ.get("DEEP_DIVE_REPOS") or "").split(",") if r.strip()}
MAX_TOOL_CALLS = int(os.environ.get("MAX_TOOL_CALLS", "15"))
EFFORT = os.environ.get("REPORT_EFFORT", "medium")   # profondeur de réflexion du modèle (low/medium/high)
BUDGETS = (12000, 24000)                             # max_tokens = réflexion + texte ; second essai si tronqué
TRONQUE = "\n\n_(rapport tronqué : budget de sortie atteint malgré deux essais — voir le log du run)_"
SECTIONS_ATTENDUES = ("*Résumé opérationnel*", "*Interprétation*")
REPORTS_LOG = os.environ.get("REPORTS_LOG", "state/reports.jsonl")   # une ligne par rapport produit (fiche incluse)
SESSION_GAP_H, SESSION_START_H = 2.0, 1.0   # heuristique heures-sessions (git-hours)
WORK = "source"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
POST_SLACK = (os.environ.get("POST_SLACK") or "true").lower() != "false"
SLACK_PAUSE = 1.5  # secondes entre deux posts (limite ~1 msg/s des webhooks)
REPORTED_CAP = 10000  # SHA conservés pour la déduplication

# Le token n'apparaît ni dans une URL, ni dans la config git, ni dans les logs : git le demande
# à ce helper, qui le lit dans l'environnement du processus.
CRED_HELPER = '!f() { printf "username=x-access-token\\npassword=%s\\n" "$SOURCE_CRED"; }; f'

# Fichiers exclus du diff envoyé au modèle (bruit, binaires)
EXCLUDES = [f":(exclude){p}" for p in (
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "*.min.js", "*.min.css", "*.map",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.webp", "*.ico",
    "*.woff", "*.woff2", "*.ttf", "*.mp4", "*.webm", "*.pdf",
)]

SYSTEM = """Tu es le relecteur technique de Payout (payout.fr), SaaS de gestion financière pour \
indépendants en SASU/EURL (facturation, TVA, frais pro, payout mensuel, liasse), stack Next.js / React / TypeScript.
Tu rédiges pour le dirigeant — expert-comptable, à l'aise techniquement mais qui ne lit pas le code au quotidien — \
un rapport court et factuel, en français, à partir des commits et du diff fournis.

Règles :
- Ne rien inventer. Si le diff est tronqué, partiel ou ambigu, le dire explicitement.
- Parler produit d'abord (écrans, flows, modules, API), puis technique ; citer les fichiers clés entre backticks.
- Distinguer ce qui est livré / en cours / potentiellement cassant.
- Signaler tout ce qui touche à : auth, données financières, paiements, secrets, dépendances, config Vercel, migrations.
- Format Slack mrkdwn uniquement : gras *texte*, puces "•", code `inline`. Pas de titres markdown (#), pas de tableaux, \
pas de gras avec **.
- 2 500 caractères maximum.

Structure imposée (omettre une section seulement si elle est vide) :
*Résumé opérationnel*
• ce qui a changé, regroupé par domaine fonctionnel
*Interprétation*
• ce que ça signifie : avancement, qualité, cohérence avec l'existant, risques de régression, dette
*À tester*
• vérifications concrètes, une par puce
*Points d'attention*
• questions à poser à l'auteur, décisions à prendre
"""


# ----------------------------------------------------------------------------- dépôts
class Source:
    """Un dépôt surveillé : owner/repo + token qui le voit + clone local."""

    def __init__(self, full_name: str, token: str) -> None:
        self.full_name, self.token = full_name, token
        self.dir = os.path.join(WORK, full_name.replace("/", "__"))
        self.deep = "all" in DEEP_DIVE or full_name.lower() in DEEP_DIVE

    @property
    def name(self) -> str:
        return self.full_name.split("/")[-1]

    def git(self, *args: str, check: bool = True) -> str:
        env = dict(os.environ, SOURCE_CRED=self.token, GIT_TERMINAL_PROMPT="0")
        r = subprocess.run(["git", "-c", f"credential.helper={CRED_HELPER}", "-C", self.dir, *args],
                           capture_output=True, text=True, env=env)
        if check and r.returncode != 0:
            raise RuntimeError(f"[{self.full_name}] git {' '.join(args)} → {r.stderr.strip()[:300]}")
        return r.stdout

    def clone(self) -> bool:
        url = f"{BASE_URL}{self.full_name}.git"
        env = dict(os.environ, SOURCE_CRED=self.token, GIT_TERMINAL_PROMPT="0")
        base = ["git", "-c", f"credential.helper={CRED_HELPER}", "clone", "--quiet", "--no-checkout"]
        filtres = ([],) if self.deep else (["--filter=blob:none"], [])  # investigation : clone complet d'office
        for extra in filtres:
            shutil.rmtree(self.dir, ignore_errors=True)
            r = subprocess.run(base + extra + [url, self.dir], capture_output=True, text=True, env=env)
            if r.returncode == 0:
                return True
        print(f"::warning::[{self.full_name}] clone impossible : {r.stderr.strip()[:200]}")
        return False

    def rev_exists(self, rev: str) -> bool:
        return subprocess.run(["git", "-C", self.dir, "cat-file", "-e", f"{rev}^{{commit}}"], capture_output=True).returncode == 0

    def is_ancestor(self, a: str, b: str) -> bool:
        return subprocess.run(["git", "-C", self.dir, "merge-base", "--is-ancestor", a, b], capture_output=True).returncode == 0

    def checkout(self, sha: str) -> None:
        """Matérialise l'arbre au commit rapporté (pour les outils d'investigation)."""
        self.git("checkout", "--quiet", "--force", sha)

    def branches(self) -> list[str]:
        out = self.git("branch", "-r", "--format=%(refname:short)")
        return sorted(b.split("/", 1)[1] for b in out.split() if b.startswith("origin/") and b != "origin/HEAD")


def tokens() -> list[str]:
    names = ["SOURCE_TOKEN"] + [f"SOURCE_TOKEN_{i}" for i in range(2, 10)]
    return [os.environ[n].strip() for n in names if os.environ.get(n, "").strip()]


def discover() -> list[Source]:
    """Dépôts à surveiller : SOURCE_REPOS explicite, sinon tout ce que chaque token peut lire."""
    toks = tokens()
    if not toks:
        raise RuntimeError("aucun token : définir SOURCE_TOKEN (et SOURCE_TOKEN_2… pour d'autres organisations)")
    owners = {o.strip().lower() for o in (os.environ.get("SOURCE_OWNERS") or "").split(",") if o.strip()}
    exclude = {r.strip().lower() for r in (os.environ.get("SOURCE_EXCLUDE") or "").split(",") if r.strip()}
    explicit = [r.strip() for r in (os.environ.get("SOURCE_REPOS") or "").split(",") if r.strip()]
    if explicit:  # sans découverte : le premier token sert pour tous (tests, ou une seule organisation)
        return [Source(r, toks[0]) for r in explicit if r.lower() not in exclude]

    import requests
    found: "OrderedDict[str, Source]" = OrderedDict()
    for tok in toks:
        page = 1
        while True:
            r = requests.get("https://api.github.com/user/repos",
                             params={"per_page": 100, "page": page, "affiliation": "owner,organization_member,collaborator"},
                             headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                      "X-GitHub-Api-Version": "2022-11-28"}, timeout=30)
            if r.status_code != 200:
                print(f"::warning::découverte : GitHub {r.status_code} — {r.text[:150]}")
                break
            batch = r.json()
            for repo in batch:
                full = repo["full_name"]
                if repo.get("archived") or repo.get("disabled") or repo.get("size", 1) == 0:
                    continue
                if owners and repo["owner"]["login"].lower() not in owners:
                    continue
                if full.lower() in exclude or full.lower() in found:
                    continue
                found[full.lower()] = Source(full, tok)
            if len(batch) < 100:
                break
            page += 1
    return list(found.values())


LOG_FMT = "%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1f%b%x1e"
IA_RE = re.compile(r"co-authored-by:[^\n]*(claude|copilot|cursor|aider|gpt|codex|gemini|devin)|generated with (claude|copilot|cursor|aider|chatgpt|codex|gemini|ai\b)|🤖|claude code|written by ai", re.I)


def marqueur_ia(c: dict) -> str:
    m = IA_RE.search(c["subject"] + "\n" + c["body"])
    return m.group(0).strip()[:60] if m else ""


def humanise(sec: float) -> str:
    if sec < 3600:
        return f"{int(sec // 60)} min"
    if sec < 86400:
        return f"{sec / 3600:.1f} h".replace(".0 h", " h")
    return f"{sec / 86400:.1f} j".replace(".0 j", " j")


def ecarts_auteur(src: Source, head: str, commits: list[dict]) -> dict:
    """sha → écart avec le commit précédent du même auteur dans l'historique (borne haute du temps passé)."""
    tous = parse_log(src.git("log", "--no-merges", f"--format={LOG_FMT}", "--date=iso-strict", head))
    precedent: dict[str, str] = {}
    dern: dict[str, str] = {}
    for c in reversed(tous):  # du plus ancien au plus récent
        if c["author"] in dern:
            precedent[c["sha"]] = dern[c["author"]]
        dern[c["author"]] = c["iso"]
    out = {}
    for c in commits:
        if c["sha"] in precedent:
            delta = (datetime.fromisoformat(c["iso"]) - datetime.fromisoformat(precedent[c["sha"]])).total_seconds()
            if delta > 0:
                out[c["sha"]] = humanise(delta)
    return out


def parse_log(out: str) -> list[dict]:
    items = []
    for rec in out.split("\x1e"):
        if not rec.strip():
            continue
        h, short, author, iso, subject, body = (rec.strip("\n").split("\x1f") + [""] * 6)[:6]
        day, hm = iso[:10], iso[11:16]
        items.append(dict(sha=h, short=short, author=author, iso=iso, day=day,
                          date=f"{day[8:10]}/{day[5:7]} {hm}", subject=subject.strip(), body=body.strip()))
    return items


def threshold(since: str) -> datetime | None:
    """Borne basse : all / Nh / Nd / YYYY-MM-DD → datetime UTC (None = pas de borne)."""
    since = (since or "").strip()
    if not since or since == "all":
        return None
    m = re.fullmatch(r"(\d+)([hd])", since)
    if m:
        n = int(m.group(1))
        return datetime.now(timezone.utc) - (timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n))
    return datetime.fromisoformat(since).replace(tzinfo=timezone.utc)


def commits_since(src: Source, head: str, since: str) -> list[dict]:
    """Commits joignables depuis head dont la date d'auteur ≥ borne (et < borne haute si since = "A..B"),
    du plus ancien au plus récent. Filtré en Python : `git --since` coupe le parcours au premier commit trop
    vieux (rebase, commit antidaté)."""
    commits = parse_log(src.git("log", "--no-merges", f"--format={LOG_FMT}", "--date=iso-strict", head))
    bas, _, haut = (since or "").partition("..")
    limit, plafond = threshold(bas), (threshold(haut) if haut else None)
    if limit is not None:
        commits = [c for c in commits if datetime.fromisoformat(c["iso"]) >= limit]
    if plafond is not None:
        commits = [c for c in commits if datetime.fromisoformat(c["iso"]) < plafond]
    return sorted(commits, key=lambda c: c["iso"])


def commits_diff(src: Source, commits: list[dict]) -> tuple[str, str, bool]:
    """Patch propre à chaque commit (indépendant de la linéarité de l'historique)."""
    stats, patches = [], []
    for c in commits:
        stats.append(f"{c['short']} {c['subject']}\n"
                     + src.git("show", "--stat=110", "--format=", c["sha"], "--", ".", *EXCLUDES))
        patches.append(f"### {c['short']} · {c['subject']} ({c['author']}, {c['date']})\n"
                       + src.git("show", "--unified=3", "--no-color", "--format=", c["sha"], "--", ".", *EXCLUDES))
    full = "\n".join(patches)
    truncated = len(full) > MAX_DIFF_CHARS
    if truncated:
        full = full[:MAX_DIFF_CHARS] + "\n\n[... DIFF TRONQUÉ — la suite n'a pas été transmise ...]"
    return "\n".join(stats), full, truncated


# ----------------------------------------------------------------------------- modèle
def build_prompt(src: Source, branch: str, label: str, commits: list[dict], stat: str, diff: str, truncated: bool,
                 head: str = "") -> str:
    ecarts = ecarts_auteur(src, head or commits[0]["sha"], commits)
    lines = [f"Dépôt : {src.full_name} — branche : {branch} — plage : {label}",
             f"Commits ({len(commits)}, du plus récent au plus ancien) — métadonnées factuelles incluses :"]
    for c in commits:
        meta = []
        if marqueur_ia(c):
            meta.append(f"MARQUEUR IA : « {marqueur_ia(c)} »")
        if c["sha"] in ecarts:
            meta.append(f"écart depuis le commit précédent de l'auteur : {ecarts[c['sha']]}")
        lines.append(f"- {c['short']} · {c['author']} · {c['date']} · {c['subject']}" + (f"  [{' · '.join(meta)}]" if meta else ""))
        if c["body"]:
            lines.append("    " + c["body"].replace("\n", "\n    "))
    lines += ["", "Fichiers modifiés :", stat.strip() or "(aucun fichier texte modifié)", "",
              "Diff" + (" (TRONQUÉ)" if truncated else "") + " :", "```", diff.strip() or "(vide)", "```"]
    return "\n".join(lines)


def system_for(src: Source) -> str:
    """Prompt système par dépôt : prompts/<owner>__<repo>.md, sinon prompts/default.md, sinon l'intégré."""
    for p in (f"prompts/{src.full_name.replace('/', '__')}.md", "prompts/default.md"):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
    return SYSTEM


def summarize(prompt: str, system: str) -> str:
    if DRY_RUN:
        return "*Résumé opérationnel*\n• (dry run — aucun appel au modèle)"
    import anthropic  # importé ici pour que DRY_RUN fonctionne sans SDK
    client = anthropic.Anthropic()
    text = ""
    for max_tok in BUDGETS:
        msg = client.messages.create(model=MODEL, max_tokens=max_tok, system=system,
                                     output_config={"effort": EFFORT},
                                     messages=[{"role": "user", "content": prompt}])
        text = "".join(getattr(b, "text", "") for b in msg.content).strip()
        out = getattr(getattr(msg, "usage", None), "output_tokens", "?")
        print(f"modèle : stop_reason={msg.stop_reason} · effort={EFFORT} · max_tokens={max_tok} · sortie={out} tokens · texte={len(text)} caractères")
        if msg.stop_reason != "max_tokens" and text:
            return verifier_structure(text)
    if not text:
        return ("*Interprétation indisponible* — le modèle n'a renvoyé aucun texte "
                "(budget consommé par la réflexion interne ; voir le log du run).")
    print("::warning::rapport tronqué malgré deux essais — posté avec mention")
    return verifier_structure(text) + TRONQUE


def verifier_structure(text: str) -> str:
    manquantes = [sec for sec in SECTIONS_ATTENDUES if sec not in text]
    if manquantes:
        print(f"::warning::sections absentes du rapport : {', '.join(manquantes)}")
    return text


# ----------------------------------------------------------------------------- investigation (tool use)
DEEP_INSTR = """

Tu disposes d'outils de lecture du dépôt, figé au commit rapporté. AVANT d'écrire le rapport : vérifie dans le code
chacun de tes points d'attention et tout doute levé par le diff (valeur codée en dur ? config existante ? autre module
qui fait déjà X ? tests présents ?). Une question à laquelle le code répond n'est PAS un point d'attention : mets la
réponse dans la section concernée, fichier à l'appui. Ne laisse en *Points d'attention* que ce qui exige une décision
humaine ou une information absente du dépôt. Maximum {n} lectures — cible-les.""".format(n=MAX_TOOL_CALLS)

TOOLS = [
    {"name": "lister", "description": "Liste le contenu d'un répertoire du dépôt (les répertoires se terminent par /).",
     "input_schema": {"type": "object", "properties": {"chemin": {"type": "string", "description": "Chemin relatif, '' ou '.' pour la racine"}}, "required": []}},
    {"name": "chercher", "description": "Recherche un motif (regex git grep) dans les fichiers suivis du dépôt. Retourne fichier:ligne:extrait.",
     "input_schema": {"type": "object", "properties": {"motif": {"type": "string"}, "chemin": {"type": "string", "description": "Limiter à un sous-répertoire ou motif de chemin (optionnel)"}}, "required": ["motif"]}},
    {"name": "lire_fichier", "description": "Lit un fichier du dépôt (lignes numérotées). Par défaut les 200 premières lignes.",
     "input_schema": {"type": "object", "properties": {"chemin": {"type": "string"}, "ligne_debut": {"type": "integer"}, "ligne_fin": {"type": "integer"}}, "required": ["chemin"]}},
]


def _safe_path(src: Source, chemin: str) -> str:
    racine = os.path.realpath(src.dir)
    p = os.path.realpath(os.path.join(racine, (chemin or ".").lstrip("/")))
    if p != racine and not p.startswith(racine + os.sep) or f"{os.sep}.git" in p[len(racine):]:
        raise ValueError(f"chemin hors dépôt : {chemin}")
    return p


def run_tool(src: Source, name: str, args: dict) -> str:
    try:
        if name == "lister":
            p = _safe_path(src, args.get("chemin", ""))
            entries = sorted(os.listdir(p))[:200]
            return "\n".join(e + ("/" if os.path.isdir(os.path.join(p, e)) else "") for e in entries if e != ".git") or "(vide)"
        if name == "chercher":
            cmd = ["grep", "-nI", "--max-count", "5", "-e", str(args["motif"])]
            if args.get("chemin"):
                cmd += ["--", str(args["chemin"])]
            out = src.git(*cmd, check=False)
            return "\n".join(out.splitlines()[:80])[:8000] or "(aucun résultat)"
        if name == "lire_fichier":
            p = _safe_path(src, args["chemin"])
            d, f = int(args.get("ligne_debut", 1)), int(args.get("ligne_fin", 0)) or int(args.get("ligne_debut", 1)) + 199
            with open(p, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            sel = [f"{i}\t{l.rstrip()}" for i, l in enumerate(lines[d - 1:f], start=d)]
            return ("\n".join(sel))[:12000] or "(vide)"
        return f"outil inconnu : {name}"
    except Exception as exc:
        return f"erreur : {exc}"


def deep_summarize(src: Source, head_sha: str, prompt: str, system: str) -> str:
    if DRY_RUN:
        return "*Résumé opérationnel*\n• (dry run — aucun appel au modèle)"
    import anthropic
    client = anthropic.Anthropic()
    src.checkout(head_sha)
    messages = [{"role": "user", "content": prompt}]
    calls = 0
    for _tour in range(MAX_TOOL_CALLS + 3):  # plafond dur d'allers-retours, quoi que fasse le modèle
        msg = client.messages.create(model=MODEL, max_tokens=BUDGETS[0], system=system + DEEP_INSTR,
                                     output_config={"effort": EFFORT}, tools=TOOLS, messages=messages)
        if msg.stop_reason == "tool_use" and calls < MAX_TOOL_CALLS:
            messages.append({"role": "assistant", "content": msg.content})
            results = []
            for block in msg.content:
                if getattr(block, "type", "") == "tool_use":
                    calls += 1
                    print(f"investigation : {block.name} {json.dumps(block.input, ensure_ascii=False)[:120]}")
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": run_tool(src, block.name, dict(block.input))})
            messages.append({"role": "user", "content": results})
            continue
        text = "".join(getattr(b, "text", "") for b in msg.content).strip()
        print(f"modèle (investigation) : stop_reason={msg.stop_reason} · effort={EFFORT} · {calls} lecture(s) · texte={len(text)} caractères")
        if text and msg.stop_reason == "max_tokens":
            print("::warning::rapport (investigation) tronqué — posté avec mention")
            return verifier_structure(text) + TRONQUE
        if text:
            return verifier_structure(text)
        if msg.stop_reason == "tool_use":  # plafond de lectures atteint sans texte : on force la rédaction
            messages.append({"role": "assistant", "content": msg.content})
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b.id,
                             "content": "Plafond de lectures atteint — rédige le rapport maintenant."}
                             for b in msg.content if getattr(b, "type", "") == "tool_use"]})
            continue
        return ("*Interprétation indisponible* — le modèle n'a renvoyé aucun texte (voir le log du run).")
    print("::warning::investigation : plafond d'allers-retours atteint sans rapport — repli sur le mode simple")
    return summarize(prompt, system)


# ----------------------------------------------------------------------------- sorties
def chunks(text: str, limit: int = 2900) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if cur and len(cur) + len(line) > limit:
            out.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out


def slack_payload(src: Source, branch: str, label: str, commits: list[dict], report: str,
                  diff_len: int, truncated: bool, retro: bool = False) -> dict:
    authors = sorted({c["author"] for c in commits})
    header = f"{src.name} · {branch} · {len(commits)} commit{'s' if len(commits) > 1 else ''}"
    if retro:
        header = "⏪ " + header
    commit_lines = [f"• <https://github.com/{src.full_name}/commit/{c['sha']}|`{c['short']}`> {c['subject']} — {c['author']}"
                    for c in commits]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": f"{src.full_name} · plage {label} · auteur(s) : {', '.join(authors)}"}]},
    ]
    for part in chunks("\n".join(commit_lines)):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": part}})
    blocks.append({"type": "divider"})
    for part in chunks(report):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": part}})
    foot = f"Modèle {MODEL} · diff {diff_len // 1000} Ko{' (tronqué)' if truncated else ''}"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": foot}]})
    return {"text": f"{header} — rapport de commits", "blocks": blocks[:50]}


def webhooks_for(src: Source) -> list[str]:
    """Canal par dépôt : SLACK_WEBHOOK_URL__<OWNER_REPO en majuscules, tout caractère hors A-Z0-9 → _>,
    sinon SLACK_WEBHOOK_URL (défaut). Chaque valeur accepte plusieurs URLs séparées par des virgules."""
    key = "SLACK_WEBHOOK_URL__" + re.sub(r"[^A-Z0-9]", "_", src.full_name.upper())
    raw = os.environ.get(key) or os.environ.get("SLACK_WEBHOOK_URL", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def post_slack(src: Source, payload: dict) -> None:
    if DRY_RUN or not POST_SLACK:
        return
    urls = webhooks_for(src)
    if not urls:
        print(f"::warning::[{src.full_name}] aucun webhook Slack configuré, rapport non posté (Markdown conservé)")
        return
    import requests
    for url in urls:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Slack {r.status_code} : {r.text[:300]}")
    time.sleep(SLACK_PAUSE)


def mrkdwn_to_md(text: str) -> str:
    text = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])", r"**\1**", text)
    return re.sub(r"^\s*•\s*", "- ", text, flags=re.M)


class Markdown:
    def __init__(self) -> None:
        os.makedirs("reports", exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.path = f"reports/commit-reports-{stamp}.md"
        self.parts = ["# Rapports de commits\n"]

    def add(self, src: Source, branch: str, label: str, commits: list[dict], report: str) -> None:
        lines = [f"\n## {src.full_name} · {branch} — {label} — {len(commits)} commit{'s' if len(commits) > 1 else ''}\n"]
        for c in commits:
            lines.append(f"- [`{c['short']}`](https://github.com/{src.full_name}/commit/{c['sha']}) {c['subject']} — {c['author']}, {c['date']}")
        lines += ["", mrkdwn_to_md(report), ""]
        self.parts.append("\n".join(lines))

    def save(self) -> str:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.parts))
        return self.path


class State:
    """Dernier SHA vu par dépôt et branche + SHA déjà rapportés (déduplication)."""

    def __init__(self) -> None:
        self.data = {"repos": {}, "reported": []}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                self.data = json.load(f)
        self.data.setdefault("repos", {})
        self.reported = set(self.data.get("reported", []))

    def last(self, repo: str, branch: str) -> str | None:
        return self.data["repos"].get(repo, {}).get(branch)

    def update(self, repo: str, branch: str, head: str, shas: list[str]) -> None:
        self.data["repos"].setdefault(repo, {})[branch] = head
        for s in shas:
            if s not in self.reported:
                self.reported.add(s)
                self.data["reported"].append(s)
        self.data["reported"] = self.data["reported"][-REPORTED_CAP:]

    def save(self) -> None:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        self.data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, ensure_ascii=False)


def run_one(src: Source, branch: str, label: str, commits: list[dict], md: Markdown, retro: bool = False) -> None:
    """commits : du plus récent au plus ancien."""
    stat, diff, truncated = commits_diff(src, commits)
    prompt = build_prompt(src, branch, label, commits, stat, diff, truncated)
    if DRY_RUN:
        print("=== PROMPT ===\n" + prompt[:800] + ("\n[...]" if len(prompt) > 800 else ""))
    report = (deep_summarize(src, commits[0]["sha"], prompt, system_for(src)) if src.deep
              else summarize(prompt, system_for(src)))
    post_slack(src, slack_payload(src, branch, label, commits, report, len(diff), truncated, retro))
    md.add(src, branch, label, commits, report)
    log_report(src, branch, label, commits, report)
    print(f"Rapport : {src.full_name} · {branch} · {label} — {len(commits)} commit(s), diff {len(diff)} caractères.")


def extraire_fiche(report: str) -> str:
    m = re.search(r"\*Fiche\*\s*\n(.*?)(?=\n\*[^*\n]+\*\s*\n|\Z)", report, re.S)
    return m.group(1).strip() if m else ""


def log_report(src: Source, branch: str, label: str, commits: list[dict], report: str) -> None:
    os.makedirs(os.path.dirname(REPORTS_LOG) or ".", exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "repo": src.full_name, "branch": branch,
           "label": label, "shas": [c["sha"] for c in commits], "dates": [c["iso"] for c in commits],
           "authors": sorted({c["author"] for c in commits}), "ia": sum(1 for c in commits if marqueur_ia(c)),
           "fiche": extraire_fiche(report)}
    with open(REPORTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------------------- récapitulatif hebdo / mensuel
def periode(kind: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """Période complète précédente : semaine ISO (lun→dim) ou mois civil. Retourne (début, fin exclue, libellé)."""
    now = (now or datetime.now(timezone.utc)).astimezone(TZ)
    if kind == "month":
        fin = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        debut = (fin - timedelta(days=1)).replace(day=1)
        return debut, fin, f"mois de {debut.strftime('%m/%Y')}"
    fin = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    debut = fin - timedelta(days=7)
    return debut, fin, f"semaine {debut.isocalendar()[1]} ({debut.strftime('%d/%m')} → {(fin - timedelta(days=1)).strftime('%d/%m')})"


def heures_sessions(isos: list[str]) -> float:
    ts = sorted(datetime.fromisoformat(i) for i in isos)
    total, prev = 0.0, None
    for t in ts:
        if prev is not None and (t - prev).total_seconds() <= SESSION_GAP_H * 3600:
            total += (t - prev).total_seconds() / 3600
        else:
            total += SESSION_START_H
        prev = t
    return round(total, 1)


def hors_ouvrees(t: datetime) -> bool:
    """Week-end, ou nuit (22h–6h) en heure de Paris."""
    l = t.astimezone(TZ)
    return l.weekday() >= 5 or l.hour >= 22 or l.hour < 6


def stats_periode(src: Source, debut: datetime, fin: datetime) -> dict:
    """Compteurs factuels sur toutes les branches : par auteur, commits / +lignes / −lignes / heures-sessions /
    jours actifs / commits hors heures ouvrées / marqués IA. Deux temps : la liste (sans blobs) puis le numstat
    des seuls commits de la période — un clone sans blobs ne télécharge ainsi que ce qui est nécessaire."""
    commits = parse_log(src.git("log", "--remotes=origin", "--no-merges", f"--format={LOG_FMT}", "--date=iso-strict"))
    par_auteur: dict[str, dict] = {}
    for c in commits:
        try:
            t = datetime.fromisoformat(c["iso"])
        except ValueError:
            continue
        if not (debut <= t < fin):
            continue
        add = dele = 0
        for l in src.git("show", "--numstat", "--format=", c["sha"], check=False).splitlines():
            m = re.match(r"^(\d+|-)\t(\d+|-)\t", l)
            if m:
                add += int(m.group(1)) if m.group(1).isdigit() else 0
                dele += int(m.group(2)) if m.group(2).isdigit() else 0
        a = par_auteur.setdefault(c["author"], {"commits": 0, "add": 0, "del": 0, "isos": [], "ia": 0, "hors": 0, "jours": set()})
        a["commits"] += 1; a["add"] += add; a["del"] += dele; a["isos"].append(c["iso"])
        a["ia"] += 1 if marqueur_ia(c) else 0
        a["hors"] += 1 if hors_ouvrees(t) else 0
        a["jours"].add(t.astimezone(TZ).date())
    for a in par_auteur.values():
        a["heures"] = heures_sessions(a["isos"])
        a["jours"] = len(a["jours"])
    return par_auteur


def tableau(stats: dict[str, dict[str, dict]]) -> str:
    en_tete = f"{'dépôt':<15}{'auteur':<13}{'commits':>8}{'+lignes':>8}{'−lignes':>8}{'h-sess':>7}{'j.act':>6}{'hors-h':>7}{'IA':>4}"
    lignes = [en_tete]
    fusion: dict[str, dict] = {}
    for repo, auteurs in stats.items():
        for auteur, a in sorted(auteurs.items(), key=lambda kv: -kv[1]["commits"]):
            lignes.append(f"{repo.split('/')[-1][:14]:<15}{auteur[:12]:<13}{a['commits']:>8}{a['add']:>8}{a['del']:>8}"
                          f"{a['heures']:>7}{a['jours']:>6}{a['hors']:>7}{a['ia']:>4}")
            f = fusion.setdefault(auteur, {"commits": 0, "add": 0, "del": 0, "isos": [], "hors": 0, "ia": 0, "jours": set()})
            for k in ("commits", "add", "del", "hors", "ia"):
                f[k] += a[k]
            f["isos"] += a["isos"]
            f["jours"].update(datetime.fromisoformat(i).astimezone(TZ).date() for i in a["isos"])
    if len(stats) > 1:
        lignes.append("— par auteur, tous dépôts (sessions fusionnées, sans double compte) —")
        for auteur, f in sorted(fusion.items(), key=lambda kv: -kv[1]["commits"]):
            lignes.append(f"{'':<15}{auteur[:12]:<13}{f['commits']:>8}{f['add']:>8}{f['del']:>8}"
                          f"{heures_sessions(f['isos']):>7}{len(f['jours']):>6}{f['hors']:>7}{f['ia']:>4}")
    tot_h = round(sum(heures_sessions(f["isos"]) for f in fusion.values()), 1)
    lignes.append(f"{'TOTAL':<28}{sum(f['commits'] for f in fusion.values()):>8}{sum(f['add'] for f in fusion.values()):>8}"
                  f"{sum(f['del'] for f in fusion.values()):>8}{tot_h:>7}{'':>6}{sum(f['hors'] for f in fusion.values()):>7}{sum(f['ia'] for f in fusion.values()):>4}")
    return "\n".join(lignes)


RECAP_SYSTEM = """Tu rédiges pour le dirigeant de Payout (expert-comptable, à l'aise techniquement) le récapitulatif {kind} \
de l'activité de développement, en français, à partir : (1) de COMPTEURS FACTUELS déjà calculés — ne les recalcule \
pas, ne les contredis pas ; « h-sess » = heures-sessions, ESTIMATION heuristique du temps actif (méthode git-hours : commits espacés de \
moins de 2 h comptés en continu, +1 h forfaitaire par session — ni plancher ni plafond, un ordre de grandeur) ; \
« j.act » = jours avec au moins un commit ; « hors-h » = commits le week-end ou entre 22h et 6h (Paris) ; (2) des FICHES des rapports de la période (nature, \
origine IA, effort estimé) ; (3) des compteurs de la période précédente pour la tendance.
Format Slack mrkdwn (gras *texte*, puces •), 2 500 caractères max, rien d'inventé. Structure :
*Lecture de la période*
• 3 puces max : ce qui ressort (rythme, répartition, faits marquants)
*Heures*
• heures-sessions vs effort estimé cumulé des fiches : convergence ou écart, et ce qu'on peut en conclure ; \
hypothèses explicites ; jamais présenté comme un pointage
*Livré / en cours*
• par feature (nom), d'après les fiches : livré, en cours, abandonné
*Signal IA*
• part de commits marqués IA, tendance, effet visible sur le rythme ou la qualité (prudence)
*Vigilance*
• rythme (nuits, week-ends, pics), gros commits monolithiques, absence de tests, dépôts silencieux
"""


def recap(sources: list[Source], kind: str, md: Markdown) -> int:
    debut, fin, libelle = periode(kind)
    p_debut, p_fin = debut - (fin - debut), debut
    stats, prev = {}, {}
    for src in sources:
        if not src.clone():
            continue
        cur = stats_periode(src, debut, fin)
        if cur:
            stats[src.full_name] = cur
        old = stats_periode(src, p_debut, p_fin)
        if old:
            prev[src.full_name] = old
    fiches = []
    if os.path.exists(REPORTS_LOG):
        with open(REPORTS_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if any(debut <= datetime.fromisoformat(d) < fin for d in r.get("dates", [])) and r.get("fiche"):
                    fiches.append(f"[{r['repo'].split('/')[-1]} · {r['branch']} · {r['label']} · {', '.join(r['authors'])}]\n{r['fiche']}")
    if not stats and not fiches:
        print(f"Récap {kind} : aucune activité sur la {libelle}.")
        return 0
    table_cur, table_prev = tableau(stats) if stats else "(aucun commit)", tableau(prev) if prev else "(aucun commit)"
    titre = f"📊 Récap {'mensuel' if kind == 'month' else 'hebdo'} · {libelle}"
    prompt = (f"{titre}\n\nCOMPTEURS DE LA PÉRIODE :\n{table_cur}\n\nPÉRIODE PRÉCÉDENTE (tendance) :\n{table_prev}\n\n"
              f"FICHES DES RAPPORTS DE LA PÉRIODE ({len(fiches)}) :\n" + ("\n\n".join(fiches) if fiches else "(aucune fiche enregistrée)"))
    texte = summarize(prompt, RECAP_SYSTEM.format(kind="mensuel" if kind == "month" else "hebdomadaire"))
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": titre[:150]}},
              {"type": "section", "text": {"type": "mrkdwn", "text": "```\n" + table_cur[:2800] + "\n```"}}]
    for part in chunks(texte):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": part}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Modèle {MODEL} · {len(fiches)} fiche(s) · h-sess = estimation sessions (gap 2 h, +1 h/session) · hors-h = week-end ou 22h–6h"}]})
    if not DRY_RUN and POST_SLACK:
        import requests
        for url in [u.strip() for u in os.environ.get("SLACK_WEBHOOK_URL", "").split(",") if u.strip()]:
            r = requests.post(url, json={"text": titre, "blocks": blocks[:50]}, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"Slack {r.status_code} : {r.text[:300]}")
    md.parts.append(f"\n## {titre}\n\n```\n{table_cur}\n```\n\n{mrkdwn_to_md(texte)}\n")
    print(f"Récap {kind} posté : {libelle} — {sum(a['commits'] for r in stats.values() for a in r.values())} commits, {len(fiches)} fiche(s).")
    return 0


# ----------------------------------------------------------------------------- modes
def scheduled(sources: list[Source], md: Markdown, state: State) -> int:
    for src in sources:
        if not src.clone():
            continue
        for br in src.branches():
            head = src.git("rev-parse", f"origin/{br}").strip()
            last = state.last(src.full_name, br)
            if last and src.rev_exists(last) and src.is_ancestor(last, head):
                commits = parse_log(src.git("log", "--no-merges", f"--format={LOG_FMT}", "--date=iso-strict", f"{last}..{head}"))
                label = f"{last[:7]}..{head[:7]}"
            else:  # premier passage, ou branche réécrite (force-push / rebase) : fenêtre glissante
                commits = commits_since(src, head, FIRST_RUN_WINDOW)[::-1]
                label = f"{'premier passage' if not last else 'branche réécrite'} · dernières {FIRST_RUN_WINDOW}"
            commits = [c for c in commits if c["sha"] not in state.reported]
            if not commits:
                state.update(src.full_name, br, head, [])
                continue
            if len(commits) > MAX_COMMITS:
                print(f"{src.full_name}/{br} : {len(commits)} commits, seuls les {MAX_COMMITS} plus récents sont analysés.")
                commits = commits[:MAX_COMMITS]
            run_one(src, br, label, commits, md)
            state.update(src.full_name, br, head, [c["sha"] for c in commits])
        print(f"{src.full_name} : {len(src.branches())} branche(s) passée(s).")
    return 0


def backfill(sources: list[Source], mode: str, since: str, repo: str, branch: str, md: Markdown, state: State) -> int:
    """Un rapport par jour (per-day) ou par commit (per-commit), du plus ancien au plus récent."""
    match = [s for s in sources if s.full_name.lower() == repo.lower() or (("/" not in repo) and s.name.lower() == repo.lower())]
    if not match:
        raise RuntimeError(f"dépôt introuvable parmi les dépôts visibles : {repo}")
    src = match[0]
    if not src.clone():
        return 1
    if branch not in src.branches():
        raise RuntimeError(f"branche introuvable dans {src.full_name} : {branch} (dispo : {', '.join(src.branches())})")
    head = src.git("rev-parse", f"origin/{branch}").strip()
    commits = commits_since(src, head, since)
    if not commits:
        print("Aucun commit dans la période demandée.")
        return 0
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for c in commits:
        groups.setdefault(c["sha"] if mode == "per-commit" else c["day"], []).append(c)
    print(f"Backfill {mode} sur {src.full_name}/{branch} : {len(commits)} commits, {len(groups)} rapport(s).")
    for key, group in groups.items():
        label = f"{group[0]['date']} · {group[0]['short']}" if mode == "per-commit" \
            else f"journée du {key[8:10]}/{key[5:7]}/{key[:4]}"
        run_one(src, branch, label, sorted(group, key=lambda c: c["iso"], reverse=True), md, retro=True)
        state.update(src.full_name, branch, head, [c["sha"] for c in group])  # progressif : survit à un timeout
        state.save()
        md.save()
    return 0


def main() -> int:
    md, state = Markdown(), State()
    sources = discover()
    print("Dépôts : " + (", ".join(s.full_name for s in sources) or "(aucun)"))
    recap_kind = (os.environ.get("RECAP") or "").strip().lower()
    sched = (os.environ.get("SCHEDULE") or "").strip()
    if recap_kind not in ("week", "month"):
        recap_kind = {"0 6 * * 1": "week", "0 6 1 * *": "month"}.get(sched, "")
    if recap_kind:
        code = recap(sources, recap_kind, md)
        if len(md.parts) > 1:
            print(f"Markdown : {md.save()}")
        return code
    mode = (os.environ.get("BACKFILL") or "off").strip()
    if mode in ("per-day", "per-commit"):
        code = backfill(sources, mode, os.environ.get("SINCE", "all"), os.environ.get("REPO_INPUT") or "",
                        os.environ.get("BRANCH_INPUT") or "main", md, state)
    else:
        code = scheduled(sources, md, state)
    state.save()
    if len(md.parts) > 1:
        print(f"Markdown : {md.save()}")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # message lisible dans le log Actions
        print(f"::error::{exc}")
        sys.exit(1)
