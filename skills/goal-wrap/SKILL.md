---
name: goal-wrap
version: "1.0.0"
description: Bundle current work into a self-contained autonomous-execution prompt with tracked done-when criteria, best-effort architecture grounding, and full autonomy by default (pass --gates to enable operator confirmation prompts).
argument-hint: "[--gates] What should the next session accomplish?"
allowed-tools:
  - Read
  - Bash
  - Glob
  - Grep
  - Skill
  - Agent
---

# /goal-wrap — Grounded, tracked goal bundle

Pack current conversation state into a self-contained execution bundle:
1. **Best-effort research first** — a small research pass grounds the goal in real
   architecture before handoff (codebase search + prior-decision recall, using
   whatever tools are actually available — see "Soft dependencies" below)
2. **Tracked DONE WHEN** — each criterion has a proof command; executor runs it and
   updates status; goal clears only when all show `[✓]`
3. **Full autonomy by default** — commits, push, merge, DB migrations, prod deploys,
   schema changes all pre-approved; pass `--gates` to revert to ask-first behavior
4. **Parallel-execution hint baked in** — goal instructs the executor to use this
   package's own `/feature-implement` (or a manual swarm) for parallel work

Use before: `/clear`, agent handoff, `/loop`, `/schedule`, switching machines.

## Soft dependencies (degrade gracefully — do not block on absence)

This skill was originally authored inside a monorepo with a codebase-index MCP
server (`repowise`), a cross-session memory store (`gbrain`), and two sibling
skills (`/prompt-master`, `/handoff`). None of those are guaranteed to exist for
a standalone `feature-fix-swarm` install. Check availability once at the top of
Phase 0 and Step 4, then follow the stated fallback — never hard-block the whole
skill on one missing tool, matching this package's existing `RUFLO_REQUIRED=auto`
pattern for Ruflo elsewhere.

| Tool | If present | If absent |
|---|---|---|
| `repowise` MCP (`get_overview`, `get_answer`) | Use for Worker A (architecture map) | Fall back to `grep -rl` against `README*`, `ARCHITECTURE*`, `docs/`, and the top-level directory listing |
| `gbrain` (`gbrain query`/`gbrain search`) | Use for Worker B (prior-decision recall) | Fall back to `git log --oneline --grep="<key terms>" -20` and skip decision recall entirely |
| `/prompt-master` skill | Invoke it for Step 4's goal-body synthesis | Synthesize the goal body inline yourself, following the same 2000-2800 char budget and the constraints list in Step 4 |
| `/handoff` skill | Invoke it for Step 6 | Already has a documented fallback below (inline minimal handoff to OS temp dir) |

## Inputs

`$ARGUMENTS` — task description + optional `--gates` flag.

**Parse flags:**
- `--gates` present → `AUTONOMY=supervised` (ask before irreversible/high-blast actions)
- `--gates` absent → `AUTONOMY=full` (all production actions pre-approved, no confirmation prompts)

## Autonomy Modes

| Mode | Trigger | Pre-approved actions |
|---|---|---|
| `full` (default) | no flag | commits, push, merge to main, DB migrations, prod deploys, schema changes, file deletes, branch ops, env changes |
| `supervised` | `--gates` | none — ask before each irreversible action |

The AUTONOMY declaration appears in the goal prompt header. The executor reads and honors it with no further prompting.

## Workflow

### Phase 0: Best-effort architecture research

Run before all other steps. Try a small parallel research pass; degrade gracefully per the table above.

**If `repowise`/`gbrain` MCP tools are available**, spawn up to 3 parallel workers (via `Agent` or Ruflo if configured — see `skills/swarm/SKILL.md` for this package's own swarm conventions):

```
Worker A (arch-map):
  → repowise get_overview()
  → repowise get_answer("key entry points, services, data flows, and hotspot files for this codebase")
  → Returns: architecture summary, hotspot file list, service topology

Worker B (prior-decisions):
  → env -u DATABASE_URL gbrain query "<task description from $ARGUMENTS>"
  → env -u DATABASE_URL gbrain search "<key noun phrases from $ARGUMENTS>"
  → Returns: prior decisions, resolved blockers, known patterns, spec history

Worker C (spec-scan):
  → find specs -maxdepth 3 -name 'spec.md' | xargs ls -t 2>/dev/null | head -3
  → git log -10 --oneline
  → git diff origin/main...HEAD --stat
  → Returns: active spec list, recent change summary
```

**If `repowise`/`gbrain` are absent**, run the fallback commands from the Soft dependencies table directly (no agent spawn needed — these are cheap, run them yourself) plus Worker C's commands, and note `RESEARCH: degraded (repowise/gbrain unavailable)` in the bundle output instead of `SWARM_FAILED`.

**Synthesize → ARCH_CONTEXT block (max 600 chars):**
```
ARCH:
entry: <key files/routes>
services: <list>
spec: <active spec + SHA>
decisions: <1-2 relevant prior decisions, or "none recalled (gbrain unavailable)">
hotspots: <flagged files, or "not scanned (repowise unavailable)">
tasks: <count> (<ids>)
```

If a parallel spawn was attempted and failed for a reason other than tool-absence: fall back to running the same queries sequentially yourself. Log `SWARM_FAILED` in bundle output only for a genuine spawn failure, not for a soft-dependency absence (use `RESEARCH: degraded` for that, per above).

### Step 1: Gather context (no writes yet)

Detect:
- Primary spec: `find specs -maxdepth 2 -name 'spec.md' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-`
- Primary plan: same dir, `plan.md`
- Active branch: `git branch --show-current`
- Worktree: `git worktree list | grep "$(pwd)"`
- Recent commits: `git log -5 --oneline`
- Spike/research artifacts: `find docs/artifacts \( -name '*-spike-*.md' -o -name '*-research-*.md' -o -name '*-findings-*.md' \) -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -5 | cut -d' ' -f2-`
- ADRs touched: `git diff origin/main...HEAD -- 'docs/adr/**' --name-only`
- Skills invoked this session: scan conversation for `Skill` tool calls
- Outstanding operator decisions: review last 10 `AskUserQuestion` answers

If NO `spec.md` + `plan.md` found:
- ASK operator: "No spec/plan found. Anti-drift baseline cannot be established. (A) run `/feature-spec` first (this package's spec-first pipeline), (B) point to an existing reference doc, (C) override 'no baseline' — autonomous run has nothing to drift-check against."

### Step 2: Capture anti-drift SHAs

```bash
SPEC_SHA=$(git log -1 --format=%h -- "$SPEC_PATH" 2>/dev/null || echo "uncommitted")
PLAN_SHA=$(git log -1 --format=%h -- "$PLAN_PATH" 2>/dev/null || echo "uncommitted")
# AUTO-COMMIT if uncommitted or dirty — on feature branch only, never ask
if [ "$SPEC_SHA" = "uncommitted" ] || [ "$PLAN_SHA" = "uncommitted" ] || ! git diff --quiet -- "$SPEC_PATH" "$PLAN_PATH"; then
  CURRENT_BRANCH=$(git branch --show-current)
  if [ "$CURRENT_BRANCH" = "main" ] || [ "$CURRENT_BRANCH" = "master" ]; then
    echo "STOP: on $CURRENT_BRANCH — branch first, then re-run /goal-wrap"
    exit 1
  fi
  git add "$(dirname "$SPEC_PATH")"
  git commit -q -m "docs(spec): commit spec/plan for goal-wrap anti-drift baseline"
  SPEC_SHA=$(git log -1 --format=%h -- "$SPEC_PATH")
  PLAN_SHA=$(git log -1 --format=%h -- "$PLAN_PATH")
fi
```

### Step 3: Build tracked DONE WHEN criteria

Extract acceptance criteria from `spec.md` (success_criteria, acceptance criteria, or DONE WHEN section).
Build a structured list — each item: `STATUS | DESCRIPTION | proof: BASH_COMMAND`.

**Proof command rules:**
- Must exit 0 when the criterion is proven true
- Must be runnable non-interactively from repo root
- Must NOT be "I verified X" — always a real command

**Status states (executor updates these):**
- `[ ] pending` → `[→] in_progress` → `[✓] verified` (proof ran + exit 0 + output captured)
- `[✗] blocked` — proof failed or prerequisite missing

**Template:**
```
DONE WHEN:
[ ] pending | <measurable outcome 1 from spec> | proof: <bash cmd>
[ ] pending | <measurable outcome 2 from spec> | proof: <bash cmd>
[ ] pending | All e2e tests pass | proof: <e2e test command for this repo>
[ ] pending | All unit + integration tests pass | proof: <unit test command for this repo>
[ ] pending | No spec/plan drift | proof: git diff <SPEC_SHA>..HEAD -- <SPEC_PATH> <PLAN_PATH> | wc -l | grep -qx 0
```

Goal clears ONLY when ALL items show `[✓]`. Executor must run proof commands; never self-report done without exit 0.

### Step 4: Draft the goal body

**If `/prompt-master` is available**, call `Skill(prompt-master, args=<below spec>)`:

```
Draft a /goal-runnable prompt under 2800 chars for autonomous execution. Context:
- Repo: <repo-name>
- Branch: <branch>
- AUTONOMY: <full|supervised>
- Spec baseline: <SPEC_PATH> @ <SPEC_SHA>
- Plan baseline: <PLAN_PATH> @ <PLAN_SHA>
- Objective: <$ARGUMENTS task description>
- Architecture context: <ARCH_CONTEXT from Phase 0>
- Hard constraints:
  * AUTONOMY=<mode>: [full = all prod/DB/merge/deploy pre-approved, no confirmation prompts] [supervised = ask before irreversible actions]
  * For parallel work (3+ independent tasks): use `/feature-implement` if a tasks.md exists (this package's own executor), otherwise spawn Agent calls directly
  * Full test suite (unit + integration + e2e) MUST pass before marking any DONE WHEN [✓]
  * Run proof command from DONE WHEN; capture exit code + output before updating status
  * Anti-drift: every action traces to spec @ <SPEC_SHA>. Drift = STOP + emit blocker checkpoint
  * Checkpoint after each DONE WHEN status change: {changed_item, new_status, proof_output, blockers, next_action}

Output ONLY the prompt body (no /goal wrapper, no markdown fence). Target 2000-2800 chars.
```

**If `/prompt-master` is unavailable**, synthesize the same content yourself directly — same structure, same 2000-2800 char target, same hard-constraints list above. This is a straightforward compression task; no external skill is required to do it well.

Verify returned/synthesized body is under 2800 chars. Compress (max 2 retries) if over.

### Step 5: Assemble /goal body

```
PROJECT: <repo>
BRANCH: <branch>  AUTONOMY: <full|supervised>
SPEC: <SPEC_PATH> @ <SPEC_SHA>  [anti-drift baseline]
PLAN: <PLAN_PATH> @ <PLAN_SHA>  [anti-drift baseline]

<ARCH_CONTEXT — max 600 chars>

<Step 4 output — max 2800 chars>

HANDOFF DOC: <path from Step 6>

DONE WHEN (executor: run proof cmd, capture exit+output, then update status):
<DONE WHEN items from Step 3>

PARALLEL WORK: use /feature-implement if specs/NNN/tasks.md exists, otherwise spawn Agent calls directly for independent work
```

Verify total ≤4000 chars: `printf '%s' "$GOAL_BODY" | wc -c`

Final form: `/goal "<GOAL_BODY>"` ready to paste.

### Step 6: Handoff doc

**If `/handoff` is available**, call `Skill(handoff, args=<$ARGUMENTS>)`. Capture the path it writes to (OS temp dir per its own spec). Inject into GOAL_BODY at `HANDOFF DOC:` line.

Read the handoff doc and verify it references (does not duplicate): spec+plan by SHA, ADRs by path, commits by sha+subject, spike findings by path+1-line summary, task count+ids, outstanding decisions 1-line each.

**If `/handoff` is unavailable**, write a minimal handoff doc yourself to the OS temp dir (`mktemp` or equivalent) covering the same references list above, and note `HANDOFF: inlined (skill unavailable)` in the bundle output.

If either path inlines large content from referenceable artifacts instead of pointing at them: flag as a bug to the operator.

### Step 7: E2E framework check

```bash
HAS_E2E=0
{ [ -f playwright.config.ts ] || [ -f playwright.config.js ] || \
  [ -f cypress.config.ts ] || [ -f cypress.config.js ] || \
  grep -q '"test:e2e"' package.json 2>/dev/null || \
  [ -d web/e2e ] || [ -d tests/e2e ] || [ -d e2e ]; } && HAS_E2E=1
```

If absent: ASK operator: "(A) add an e2e setup as part of goal work (adds scope), (B) replace the e2e gate with manual operator verification in DONE WHEN, (C) abort bundle until e2e is committed."

### Step 8: Surface bundle

```
=== /goal-wrap bundle ready ===
AUTONOMY: <full — all prod/DB/merge/deploy pre-approved | supervised — ask before irreversible>
RESEARCH: <ok — N tools available | degraded — repowise/gbrain unavailable, used grep/git fallback>

GOAL PROMPT (<NNNN>/4000 chars):
──────────────────────────────────────
/goal "<GOAL_BODY>"
──────────────────────────────────────

HANDOFF DOC: <temp_path>

ANTI-DRIFT BASELINE:
  spec: <SPEC_PATH> @ <SPEC_SHA>
  plan: <PLAN_PATH> @ <PLAN_SHA>

DONE WHEN (<N> items, all pending):
  [ ] <criterion 1>  (proof: <cmd>)
  [ ] <criterion 2>  (proof: <cmd>)
  [ ] All e2e tests pass  (proof: <cmd>)
  [ ] All unit+integration tests pass  (proof: <cmd>)
  [ ] No spec/plan drift  (proof: git diff ...)

ARCHITECTURE GROUNDING:
  <ARCH_CONTEXT brief — entry, services, hotspots, prior decisions recalled>

E2E STATUS: <ok | gap-flagged>

NEXT:
  Paste /goal in a fresh session → executor runs proof cmds, updates DONE WHEN
```

## Hard Rules

- Goal body ≤4000 chars. Budgets: ARCH_CONTEXT 600, DONE WHEN 400, prompt body 2800, headers 200.
- DONE WHEN items MUST have proof commands. No proof = flag item; do not emit goal until fixed.
- AUTONOMY=full is default. Executor honors it — no confirmation for commits, push, merge, DB, deploys. `--gates` is the only opt-in to prompts.
- Phase 0 research MUST be attempted before Step 4, even in degraded mode. Architecture grounding is not optional — only its *source* (MCP tools vs grep/git fallback) is conditional.
- Anti-drift SHA = committed state. AUTO-COMMIT on feature branch, never ask. STOP only on main/master.
- NEVER include secrets. Redact API keys, passwords, PII in both goal and handoff doc.
- Prefer `/prompt-master` and `/handoff` when available; synthesize inline when they're not — never silently skip the content they'd produce.
- Goal references the handoff path. Executor fetches full context from handoff, not from the goal body.

## Failure Modes

| Failure | Response |
|---|---|
| No spec/plan | BLOCK + ask (A/B/C) |
| repowise/gbrain unavailable | Degrade to grep/git fallback; log `RESEARCH: degraded`, continue |
| Phase 0 parallel spawn fails for another reason | Sequential fallback; log `SWARM_FAILED` in bundle |
| /prompt-master unavailable | Synthesize inline; log `SYNTH: inline` |
| Synthesized/returned body over 2800 chars (2 retries) | Surface with WARNING; operator decides |
| DONE WHEN item missing proof cmd | Flag each; block goal emission until fixed |
| /handoff unavailable | Inline minimal handoff to OS temp dir + warn |
| E2E framework absent | ASK (A/B/C) |
| On main/master with uncommitted spec | STOP; tell operator to branch first |
| Secrets in goal or handoff | REDACT; log count |

## What this skill does NOT do

- Does NOT execute the goal (handoff to a fresh session or runner)
- Does NOT run proof commands (executor does, inside the goal loop)
- Does NOT modify spec/plan (read-only)
- Does NOT push (operator action)
- Does NOT trigger `/loop` or `/schedule` (suggests only)

## Related skills

- `/feature-spec` — upstream (create spec.md/plan.md if missing)
- `/spec-decompose` — produces the `tasks.md` this skill references for parallel-work hints
- `/feature-implement` — the executor this skill's goal body points parallel work at
- `/prompt-master`, `/handoff` — optional dependencies (Step 4, Step 6) — degrade gracefully if absent
