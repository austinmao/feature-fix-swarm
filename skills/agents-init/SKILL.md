---
name: agents-init
description: "Scan this repo's sub-agent roster into .feature-fix-swarm/agents.json so swarm stages (spec-decompose specialists, feature-implement [agent:] tags) assign real agents. Run once per repo, and again after adding or removing agents."
version: "1.0.0"
---

# /agents-init

Swarms are only as good as the roster they draw from. This skill builds the
machine-readable agent manifest that `/spec-decompose` (specialist fan-out) and
`/feature-implement` (`[agent:]` tag execution) consume.

## When to run

- Once after installing feature-fix-swarm into a repo (setup.sh best-efforts
  this for you at install time).
- After adding, removing, or renaming agents in `.claude/agents/` or
  `.codex/agents/`.
- Whenever `agents_manifest.py check` fails with unknown `[agent:]` tags.

## Procedure

1. **Resolve the lib path** (same shapes as gates.py):

   ```bash
   MANIFEST_PY=""
   for c in \
     "$(git rev-parse --show-toplevel 2>/dev/null)/packages/feature-fix-swarm/lib/agents_manifest.py" \
     "$HOME/.claude/lib/feature-fix-swarm/agents_manifest.py" \
     "$(git rev-parse --show-toplevel 2>/dev/null)/lib/agents_manifest.py"; do
     [ -f "$c" ] && MANIFEST_PY="$c" && break
   done
   [ -z "$MANIFEST_PY" ] && { echo "[agents-init] FATAL: agents_manifest.py not found — run setup.sh"; exit 1; }
   ```

2. **Scan:**

   ```bash
   python3 "$MANIFEST_PY" scan --repo . --out .feature-fix-swarm/agents.json
   ```

   Prints `AGENTS-MANIFEST: <N> agents (<M> repo-local) / <D> domains` plus the
   per-domain roster table. Show that table to the user — it IS the deliverable.

3. **Seed plugin agents (optional).** Plugin-provided agents (`ecc:*`,
   marketplace namespaces) are valid `subagent_type` values but not files, so
   the scan cannot see them. Declare them once in
   `.feature-fix-swarm/agents.local.json`:

   ```json
   {
     "extra_agents": [
       {"name": "ecc:typescript-reviewer", "description": "TS/JS code review"},
       {"name": "ecc:security-reviewer",   "description": "OWASP security review"}
     ],
     "domain_overrides": {"review": ["ecc:typescript-reviewer"]}
   }
   ```

   Re-run the scan after editing the seed — it merges over discovery.

4. **Validate a tasks.md against the roster** (used by spec-decompose as a
   gate; also useful standalone):

   ```bash
   python3 "$MANIFEST_PY" check specs/NNN/tasks.md   # exit 1 on unknown [agent:] tags
   ```

## Rules

- **Names are canonical kebab-case.** `Brand Designer` (codex TOML) and
  `brand-designer` (claude frontmatter) dedupe to one entry.
- **Regenerate-anytime.** The scan is cheap and idempotent; the manifest is a
  cache of the repo state, never hand-edited (hand edits go in the seed file).
- **Bare repos still work.** With zero local agents the manifest carries the
  ruflo built-in roles + generic floor (planner/coder/reviewer/tester/researcher),
  so swarm stages never come up empty.
- **Domain buckets are advisory** for specialist selection; `[agent:]` tag
  validation uses `all_agents` (exact or `dept/role` last-segment match).

## Anti-patterns

- Hand-editing `agents.json` — it's regenerated; use `agents.local.json`.
- Tagging tasks with agents from memory instead of the manifest — that's the
  drift this skill exists to kill; run `check` before handing tasks.md to
  `/feature-implement`.
