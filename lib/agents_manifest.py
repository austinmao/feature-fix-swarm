#!/usr/bin/env python3
"""Agent roster manifest for feature-fix-swarm (v3.19.0 Stream A).

Discovers the CONSUMING repo's sub-agent roster and writes
`.feature-fix-swarm/agents.json` so swarm stages (spec-decompose specialists,
feature-implement `[agent:]` tags) assign REAL agents instead of guessing.

Discovery order (additive, deduped, sorted):
  1. `.claude/agents/**/*.md` frontmatter `name:` (local Claude Code sub-agents)
  2. `.codex/agents/*.toml` `name = "..."` (curated Codex mirrors, where present)
  3. Seed file `.feature-fix-swarm/agents.local.json` — `extra_agents` for
     plugin-provided agents (e.g. `ecc:typescript-reviewer`) that are valid
     `subagent_type` values but NOT files, plus `domain_overrides`.
  4. Ruflo built-in roles (always available as swarm fallback)
  5. Generic floor so a bare repo still swarms

Usage:
    python3 lib/agents_manifest.py scan [--repo .] [--out .feature-fix-swarm/agents.json]
    python3 lib/agents_manifest.py check <tasks.md> [--manifest .feature-fix-swarm/agents.json]

`check` exits 1 if any `[agent:X]` tag in tasks.md names an agent absent from
the manifest (accepts both bare `role` and `dept/role` grammar forms). Fails
closed on a missing/corrupt manifest. Stdlib-only, like gates.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

RUFLO_BUILTIN_ROLES = [
    "coordinator", "architect", "coder", "tester",
    "reviewer", "researcher", "security-architect", "planner",
]
GENERIC_FLOOR = ["planner", "coder", "reviewer", "tester", "researcher"]

# Keyword → domain buckets. First match wins; checked in this order so the
# more specific buckets (security, database) beat the generic ones (review).
DOMAIN_KEYWORDS: list[tuple[str, list[str]]] = [
    # "authent/authoriz/oauth" not bare "auth" — 'SOUL.md authoring' is not security.
    ("security", ["security", "owasp", "vulnerab", "authent", "authoriz",
                  "oauth", "injection", "pii"]),
    ("database", ["database", "postgres", "sql", "schema", "migration", "rls"]),
    # testing before frontend so 'qa-engineer (Playwright, WCAG)' lands in testing.
    ("testing", ["test", "tdd", "e2e", "qa ", "qa-", "playwright", "coverage"]),
    ("frontend", ["frontend", "react", "next.js", "nextjs", "ui build", " ui ",
                  "css", "component", "design system", "tailwind"]),
    ("backend", ["backend", "api ", "fastapi", "server", "endpoint", "rest"]),
    ("architecture", ["architect", "system design", "adr", "bounded context"]),
    ("planning", ["plann", "decompos", "roadmap", "implementation plan"]),
    ("review", ["review"]),
    ("docs", ["documentation", "docs", "docum"]),
]
ALL_DOMAINS = [d for d, _ in DOMAIN_KEYWORDS] + ["other"]

AGENT_TAG_PAT = re.compile(r"\[agent:([A-Za-z0-9_:./-]+)\]")
FRONTMATTER_FIELD = re.compile(r"^(name|description):\s*(.*)$")


def normalize_name(name: str) -> str:
    """Canonicalize agent names to kebab-case for dedup: codex TOML display
    names ('Brand Designer') and claude frontmatter names ('brand-designer')
    are the same agent. Namespaced plugin names (ecc:x) pass through lowered."""
    return re.sub(r"\s+", "-", name.strip()).lower()


def classify(name: str, description: str = "") -> str:
    """Bucket an agent into a domain by keyword over name+description."""
    hay = f" {name} {description} ".lower()
    for domain, keywords in DOMAIN_KEYWORDS:
        if any(k in hay for k in keywords):
            return domain
    return "other"


def _parse_frontmatter(path: Path) -> dict:
    """Minimal frontmatter reader: name/description between the first two ---.

    An UNCLOSED block yields nothing (codex v3.19 round 1: without this, body
    text `name: planner` in a fence-less file injects/shadows an agent)."""
    fields: dict[str, str] = {}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return fields
    if not lines or lines[0].strip() != "---":
        return fields
    closed = False
    for line in lines[1:200]:
        if line.strip() == "---":
            closed = True
            break
        m = FRONTMATTER_FIELD.match(line)
        if m:
            fields[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fields if closed else {}


def discover_local_agents(repo: Path) -> list[dict]:
    agents = []
    root = repo / ".claude" / "agents"
    if root.is_dir():
        root_resolved = root.resolve()
        for f in sorted(root.rglob("*.md")):
            # codex v3.19 round 1 (HIGH): rglob follows symlinks — refuse any
            # entry that resolves outside the roster tree (pwn.md -> /etc/passwd)
            try:
                f.resolve().relative_to(root_resolved)
            except ValueError:
                print(f"WARNING: skipping {f} — resolves outside {root}",
                      file=sys.stderr)
                continue
            fm = _parse_frontmatter(f)
            if fm.get("name"):
                agents.append({"name": fm["name"],
                               "description": fm.get("description", ""),
                               "source": "claude-agents"})
    return agents


def discover_codex_agents(repo: Path) -> list[dict]:
    agents = []
    root = repo / ".codex" / "agents"
    if root.is_dir():
        for f in sorted(root.glob("*.toml")):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            name = None
            desc = ""
            for line in text.splitlines():
                nm = re.match(r'^\s*name\s*=\s*"([^"]+)"', line)
                dm = re.match(r'^\s*description\s*=\s*"([^"]*)"', line)
                if nm and name is None:
                    name = nm.group(1)
                if dm and not desc:
                    desc = dm.group(1)
            if name:
                agents.append({"name": name, "description": desc,
                               "source": "codex-agents"})
    return agents


def load_seed(repo: Path) -> dict:
    seed_path = repo / ".feature-fix-swarm" / "agents.local.json"
    if not seed_path.is_file():
        return {}
    try:
        data = json.loads(seed_path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        print(f"WARNING: unreadable seed file {seed_path} — ignored",
              file=sys.stderr)
        return {}


def build_manifest(repo: Path) -> dict:
    repo = Path(repo)
    seed = load_seed(repo)

    entries: dict[str, dict] = {}

    def add(name: str, description: str, source: str) -> None:
        name = normalize_name(name)
        if not name:
            return
        if name in entries:
            # codex v3.19 round 1 (MED): dedup was silent first-wins. CROSS-source
            # same-slug is the designed fallback-tier dedup (claude file + its
            # codex mirror, builtin vs generic floor) — stays quiet. The true
            # collision is the same slug claimed twice WITHIN one source (two
            # distinct agent files fighting over a name) — warn loudly; first
            # wins (discovery order is the contract).
            prior = entries[name]
            if source == prior["source"] and description and prior["description"] \
                    and description.strip().lower() != prior["description"].strip().lower():
                print(f"WARNING: roster collision on '{name}' within {source}: "
                      f"two agents claim this name with different descriptions — "
                      f"keeping the first; rename one if they are distinct agents",
                      file=sys.stderr)
            return
        entries[name] = {"name": name, "description": description,
                         "source": source}

    for a in discover_local_agents(repo):
        add(a["name"], a["description"], a["source"])
    for a in discover_codex_agents(repo):
        add(a["name"], a["description"], a["source"])
    for a in seed.get("extra_agents", []) or []:
        if isinstance(a, dict) and a.get("name"):
            add(a["name"], a.get("description", ""), "seed")
        elif isinstance(a, str):
            add(a, "", "seed")
    for role in RUFLO_BUILTIN_ROLES:
        add(role, f"ruflo built-in role: {role}", "ruflo-builtin")
    for role in GENERIC_FLOOR:
        add(role, f"generic role: {role}", "generic-floor")

    domains: dict[str, list[str]] = {d: [] for d in ALL_DOMAINS}
    for name in sorted(entries):
        e = entries[name]
        domains[classify(e["name"], e["description"])].append(name)

    for domain, names in (seed.get("domain_overrides", {}) or {}).items():
        if domain not in domains:
            domains[domain] = []
        for n in names:
            n = normalize_name(n)
            if n not in domains[domain]:
                domains[domain].append(n)
            if n not in entries:  # override may introduce an agent
                entries[n] = {"name": n, "description": "", "source": "seed"}

    return {
        "version": 1,
        "generated_at": time.time(),
        "all_agents": sorted(entries),
        "domains": {d: sorted(v) for d, v in domains.items() if v},
        "sources": {n: entries[n]["source"] for n in sorted(entries)},
    }


def scan(repo: Path, out: Path) -> dict:
    manifest = build_manifest(repo)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def check_tasks(manifest_path: Path, tasks_path: Path) -> tuple[bool, list[str]]:
    """Validate every [agent:X] tag in tasks.md against the manifest.

    Accepts both bare `role` and `dept/role` grammar forms (matches on the
    full string OR the final path segment). Fails closed on unreadable inputs.
    """
    try:
        manifest = json.loads(Path(manifest_path).read_text())
        known = set(manifest.get("all_agents", []))
    except (OSError, json.JSONDecodeError):
        return False, ["<manifest missing or corrupt>"]
    if not known:
        return False, ["<manifest empty>"]
    try:
        text = Path(tasks_path).read_text(errors="replace")
    except OSError:
        return False, ["<tasks.md unreadable>"]

    unknown = []
    for tag in AGENT_TAG_PAT.findall(text):
        # codex v3.19 round 1 (MED): grammar allows at most ONE slash (dept/role);
        # '..' or deeper paths must FAIL, never pass via last-segment match.
        malformed = ".." in tag or tag.count("/") > 1
        if not malformed and (tag in known or tag.split("/")[-1] in known):
            continue
        if tag not in unknown:
            unknown.append(tag)
    return not unknown, unknown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="discover roster, write agents.json")
    p_scan.add_argument("--repo", default=".")
    p_scan.add_argument("--out", default=".feature-fix-swarm/agents.json")

    p_check = sub.add_parser("check", help="validate [agent:] tags in tasks.md")
    p_check.add_argument("tasks_md")
    p_check.add_argument("--manifest", default=".feature-fix-swarm/agents.json")

    args = parser.parse_args(argv)

    if args.cmd == "scan":
        manifest = scan(Path(args.repo), Path(args.out))
        n_local = sum(1 for s in manifest["sources"].values()
                      if s in ("claude-agents", "codex-agents", "seed"))
        print(f"AGENTS-MANIFEST: {len(manifest['all_agents'])} agents "
              f"({n_local} repo-local) / {len(manifest['domains'])} domains "
              f"-> {args.out}")
        for domain in sorted(manifest["domains"]):
            print(f"  {domain}: {', '.join(manifest['domains'][domain])}")
        return 0

    if args.cmd == "check":
        ok, unknown = check_tasks(Path(args.manifest), Path(args.tasks_md))
        if ok:
            print(f"AGENTS-CHECK-PASS: all [agent:] tags in {args.tasks_md} "
                  f"resolve against {args.manifest}")
            return 0
        print(f"AGENTS-CHECK-FAIL: unknown agents {unknown} — "
              f"refresh the roster (python3 lib/agents_manifest.py scan) or fix the tags")
        return 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
