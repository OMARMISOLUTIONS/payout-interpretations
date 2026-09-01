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

MODEL = os.environ.get("REPORT_MODEL") or "claude-sonnet-5"
MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "150000"))
MAX_COMMITS = int(os.environ.get("MAX_COMMITS", "40"))
STATE_FILE = os.environ.get("STATE_FILE", "state/last-seen.json")
FIRST_RUN_WINDOW = os.environ.get("FIRST_RUN_WINDOW", "24h")
BASE_URL = os.environ.get("SOURCE_BASE_URL", "https://github.com/")
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
        for extra in (["--filter=blob:none"], []):  # clone sans blobs (rapide) ; repli en clone complet
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
    """Commits joignables depuis head dont la date d'auteur ≥ borne, du plus ancien au plus récent.
    Filtré en Python : `git --since` coupe le parcours au premier commit trop vieux (rebase, commit antidaté)."""
    commits = parse_log(src.git("log", "--no-merges", f"--format={LOG_FMT}", "--date=iso-strict", head))
    limit = threshold(since)
    if limit is not None:
        commits = [c for c in commits if datetime.fromisoformat(c["iso"]) >= limit]
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
def build_prompt(src: Source, branch: str, label: str, commits: list[dict], stat: str, diff: str, truncated: bool) -> str:
    lines = [f"Dépôt : {src.full_name} — branche : {branch} — plage : {label}",
             f"Commits ({len(commits)}, du plus récent au plus ancien) :"]
    for c in commits:
        lines.append(f"- {c['short']} · {c['author']} · {c['date']} · {c['subject']}")
        if c["body"]:
            lines.append("    " + c["body"].replace("\n", "\n    "))
    lines += ["", "Fichiers modifiés :", stat.strip() or "(aucun fichier texte modifié)", "",
              "Diff" + (" (TRONQUÉ)" if truncated else "") + " :", "```", diff.strip() or "(vide)", "```"]
    return "\n".join(lines)


def summarize(prompt: str) -> str:
    if DRY_RUN:
        return "*Résumé opérationnel*\n• (dry run — aucun appel au modèle)"
    import anthropic  # importé ici pour que DRY_RUN fonctionne sans SDK
    client = anthropic.Anthropic()
    msg = client.messages.create(model=MODEL, max_tokens=1800, system=SYSTEM,
                                 messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in msg.content).strip()


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


def post_slack(payload: dict) -> None:
    if DRY_RUN or not POST_SLACK:
        return
    import requests
    r = requests.post(os.environ["SLACK_WEBHOOK_URL"], json=payload, timeout=30)
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
    report = summarize(prompt)
    post_slack(slack_payload(src, branch, label, commits, report, len(diff), truncated, retro))
    md.add(src, branch, label, commits, report)
    print(f"Rapport : {src.full_name} · {branch} · {label} — {len(commits)} commit(s), diff {len(diff)} caractères.")


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
    state.update(src.full_name, branch, head, [c["sha"] for c in commits])  # évite un doublon au prochain run
    return 0


def main() -> int:
    md, state = Markdown(), State()
    sources = discover()
    print("Dépôts : " + (", ".join(s.full_name for s in sources) or "(aucun)"))
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
