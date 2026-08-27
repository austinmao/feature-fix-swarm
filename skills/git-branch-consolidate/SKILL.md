---
name: git-branch-consolidate
description: "Audit every git worktree and branch for spec/plan/test completeness, then plan the consolidation down to one origin/main. Use for 'audit all worktrees', 'what's unmerged', 'consolidate branches', 'get everything to main', 'branch estate'."
version: "1.0.0"
user-invocable: true
---

# Git Branch Consolidate

## Host dispatch contract

- Codex: `$skill`, Codex collaboration roles, and GPT-5.6 tiers.
- Claude: `/skill`, Agent/Skill tools, and Claude aliases.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

Answers two questions with evidence, never recall:

1. **What is actually unfinished?** Per branch: does its content already live in
   the base branch, what does it still owe, does it have tests, and is its
   spec/plan/tasks trail complete.
2. **What is the shortest ordered path to one `origin/main`?** Merge set, cleanup
   set, and the testing gaps that must close first.

Read-only toward every branch. Never checks out, fetches, merges, or deletes —
it emits the commands and stops. Execution of the deletion set belongs to
`/git-branch-cleanup`, which owns the approval flow.

## Step 1 — Collect (deterministic, ~30s)

```bash
python3 skills/git-branch-consolidate/scripts/collect-estate.py \
  --repo "$(git rev-parse --show-toplevel)" --base main \
  > "${TMPDIR:-/tmp}/estate-$(date -u +%Y%m%d-%H%M).json"
```

Per branch: `landed` · `residual_files` / `residual_code` · `src_files` /
`test_files` / `test_gap` · `spec` artifacts + `tasks.md` counts · `.planning`
ROADMAP+STATE · worktree dirt · PR state · `disposition`.

**`landed` is the load-bearing field.** `git merge-base --is-ancestor` alone
reports every squash-merged branch as unmerged — on a repo with a squash-merge
convention this yields dozens of false positives. `landed` is true when ancestry
OR a merged PR OR zero residual code says the base branch already carries the
content. Trust `landed`, not `ahead`.

`--no-gh` skips the PR lookup (offline / rate-limited). Dispositions then lose
`merge-ready` and `stale-abandoned` — say so in the report.

## Step 2 — Fan out (every spawn model-pinned; contracts per `docs/model-tiers.md`)

| Agent | Tier | Contract | Job |
|---|---|---|---|
| spec-trail scout | volume | scout ≤15 | For each `review-then-land` spec: spec.md/plan.md/tasks.md present? open checkboxes? evidence dir populated? |
| testing analyst | execution | build ≤20 | Every `test_gap` branch + every branch whose residual touches an API surface, auth, access-control, or schema migrations. Name the missing suite, not "add tests". |
| runner analyst | execution | build ≤20 | Worktrees with a live `.planning/run-state/gsd-run.pid` or `status != complete`: progressing / stopped-at-gate / stalled / dead. |
| conflict scout | volume | scout ≤15 | For the proposed merge set only: which branches touch the same files (collision map + suggested order). |
| risk assessor | judgment | deep ≤40 | Conclusion first: what blocks consolidation, what only the operator can decide, the ordered next 3. |

Synthesis reconciles contradictions and names which report won. Producer ≠
reviewer: the synthesizer never re-runs a scout's search.

Run `/spec-status NNN` only for specs the collector flags as **not landed with an
open tasks trail** — it is expensive and most specs are already closed.

## Step 3 — Report

Write `.planning/ESTATE-<UTC yyyymmdd-HHMM>.md`:

- **Headline** — N branches, N landed, N owing work, N testing gaps.
- **Merge set** — ordered, with the reason each is safe to land and its gate command.
- **Testing gaps** — branch → the specific suite missing → the command that would prove it.
- **Cleanup set** — `delete-safe` branches + their worktrees, with the exact
  `git worktree remove` / `git branch -d` lines. Never `-D`.
- **Blocked / operator-reserved** — each with its one-command unblock.
- **Next 3** — from the risk assessor.

Print headline + merge set + Next 3 inline; the file carries the rest.

## Step 4 — Execute (only on explicit approval, one step at a time)

Order matters: **land before cleanup**, or you delete the only copy.

1. Merge the `merge-ready` set (grant-gated; `/review-gate` first on
   auth/access-control/payments/infra diffs).
2. Re-run Step 1. `landed` must have grown by exactly the merged count.
3. Hand the refreshed `delete-safe` set to `/git-branch-cleanup` — it owns the
   deletion approval flow. Remove worktrees before their branches.
4. Detached-HEAD build worktrees whose `head` is an ancestor of the base branch
   are pure scratch: `git worktree remove`, no branch to delete.

## Step 4-A — Autonomous consolidation (spec-006 Phase 3, REQ-301/302/305)

`--autonomous` closes the queue-to-estate loop WITHOUT widening MAX-AUTH:
authority is an exact queue-derived `consolidate:estate:<sha256(target
tuples)>` grant minted by the landed queue itself (land-queue.sh at its
all-items-terminal boundary, via `queue-journal.py read-landed-tuples` and
`gates.py grant-consolidate`), never prose, scouts, or shell arguments.

**Report-only is the default.** Every run rebuilds the canonical target set
`(branch ref, expected tip OID, PR#, observed merge commit)` from the queue
manifest, refreshes `collect-estate.py`, requires `landed==true`, checks the
exact scope-aware grant, and runs `assert-merged.sh` once per target PR —
then prints the evidence and the PLANNED `run-finalizer.sh` delegation and
deletes nothing.  The operational `--execute` code path is gated by the
Phase 3 activation checkpoint and is NOT yet active: until it lands,
`--execute` refuses with `CONSOLIDATE-REFUSED:execute-not-activated`.  When
activated it may only delegate to `bash scripts/gsd/run-finalizer.sh`
(the sole deletion owner) after immediate grant and OID rechecks.

Environment contract: `CONSOLIDATE_RUN_ID` (queue run id owning the grant),
`CONSOLIDATE_EVIDENCE_DIR` (must contain `grant`, `fresh-estate`,
`target-set`, `assert-merged`), optional `CONSOLIDATE_REPO` (default `.`)
and `CONSOLIDATE_BASE` (default `main`).  Run from the repository root.

<!-- PHASE3_CONSOLIDATE_BEGIN -->
```bash
set -euo pipefail

# ── mode: report-only unless an EXPLICIT --execute is passed ─────────────
MODE="report-only"
for ARG in "$@"; do
  case "$ARG" in
    --execute) MODE="execute" ;;
    --autonomous) : ;;
    *) echo "CONSOLIDATE-REFUSED:unknown-flag $ARG"; exit 1 ;;
  esac
done

RUN_ID="${CONSOLIDATE_RUN_ID:?CONSOLIDATE_RUN_ID (queue run id) is required}"
EVIDENCE_DIR="${CONSOLIDATE_EVIDENCE_DIR:?CONSOLIDATE_EVIDENCE_DIR is required}"
REPO="${CONSOLIDATE_REPO:-.}"
BASE="${CONSOLIDATE_BASE:-main}"

# ── evidence conjunction: every member present, refused BY NAME ──────────
for EV in grant fresh-estate target-set assert-merged; do
  if [ ! -f "$EVIDENCE_DIR/$EV" ]; then
    echo "CONSOLIDATE-REFUSED:missing-evidence $EV"
    exit 1
  fi
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/consolidate-4a.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# ── refreshed deterministic inputs: queue manifest + fresh estate ────────
skills/land-queue/scripts/collect-queue.py collect   --repo "$REPO" --base "$BASE" > "$WORK/queue.json"
skills/git-branch-consolidate/scripts/collect-estate.py   --repo "$REPO" --base "$BASE" > "$WORK/estate.json"

# ── canonical tuple validation + exact target-set digest (no shell JSON) ─
if ! python3 - "$WORK/queue.json" "$WORK/estate.json" "$BASE" "$WORK/plan" <<'PYVALIDATE'
import hashlib, json, re, sys
queue_path, estate_path, base, out_path = sys.argv[1:5]
OID = re.compile(r"^[0-9a-f]{40}$")
PRN = re.compile(r"^[0-9]{1,9}$")
def refuse(reason):
    print("CONSOLIDATE-REFUSED:" + reason)
    sys.exit(3)
try:
    doc = json.load(open(queue_path))
except Exception:
    refuse("target-set queue manifest unreadable")
items = doc.get("items") if isinstance(doc, dict) else None
if not isinstance(items, list) or not items:
    refuse("target-set empty target set")
seen, tuples = set(), []
for it in items:
    if not isinstance(it, dict):
        refuse("target-set malformed target entry")
    b, h, pr, m = it.get("branch"), it.get("head"), it.get("pr"), it.get("merge_sha")
    if not isinstance(b, str) or not b or any(c.isspace() for c in b):
        refuse("target-set malformed branch ref")
    if b == base:
        refuse("target-set base branch is never a deletion target: " + b)
    if not isinstance(h, str) or not OID.match(h):
        refuse("target-set malformed expected tip OID for " + b)
    if isinstance(pr, bool) or not PRN.match(str(pr)):
        refuse("target-set malformed PR number for " + b)
    if not isinstance(m, str) or not OID.match(m):
        refuse("not-landed no observed merge commit for " + b)
    key = (b, h)
    if key in seen:
        refuse("duplicate-target " + b)
    seen.add(key)
    tuples.append((b, h, str(int(str(pr))), m))
try:
    est = json.load(open(estate_path))
except Exception:
    refuse("fresh-estate estate output unreadable")
by_branch = {}
for rec in (est.get("branches") or []) if isinstance(est, dict) else []:
    if isinstance(rec, dict) and isinstance(rec.get("branch"), str):
        by_branch[rec["branch"]] = rec
for b, h, pr, m in tuples:
    rec = by_branch.get(b)
    if rec is not None and not rec.get("landed"):
        refuse("fresh-estate landed!=true for " + b)
digest = hashlib.sha256(json.dumps(sorted([[b, h] for b, h, _p, _m in tuples]),
                                   separators=(",", ":")).encode()).hexdigest()
with open(out_path, "w") as fh:
    fh.write(digest + "\n")
    for b, h, pr, m in tuples:
        fh.write("\t".join((b, h, pr, m)) + "\n")
PYVALIDATE
then
  exit 1
fi

read -r DIGEST < "$WORK/plan"
SCOPE="consolidate:estate:$DIGEST"

# ── exact scope-aware grant check (queue-derived scope, never substituted)
if ! lib/gates.py check-grant "$RUN_ID" --action "$SCOPE"; then
  echo "CONSOLIDATE-REFUSED:grant no exact unexpired queue-derived grant for scope $SCOPE"
  exit 1
fi

# ── one green assert-merged per canonical target PR ──────────────────────
while IFS="$(printf '\t')" read -r T_BRANCH T_OID T_PR T_MERGE; do
  if ! scripts/gsd/assert-merged.sh "$T_PR"; then
    echo "CONSOLIDATE-REFUSED:assert-merged PR $T_PR is not proven merged (branch $T_BRANCH)"
    exit 1
  fi
done < <(tail -n +2 "$WORK/plan")

# ── evidence report + planned (never self-executed) finalizer delegation ─
echo "CONSOLIDATE REPORT mode=$MODE run=$RUN_ID"
echo "EVIDENCE grant=ok scope=$SCOPE"
echo "EVIDENCE fresh-estate=ok landed=true for every target"
echo "EVIDENCE target-set=ok digest=$DIGEST"
while IFS="$(printf '\t')" read -r T_BRANCH T_OID T_PR T_MERGE; do
  echo "EVIDENCE assert-merged=ok pr=$T_PR"
  echo "TARGET branch=$T_BRANCH oid=$T_OID pr=$T_PR merge=$T_MERGE"
  echo "PLANNED-FINALIZER: bash scripts/gsd/run-finalizer.sh --run-id $RUN_ID $T_PR"
done < <(tail -n +2 "$WORK/plan")

if [ "$MODE" = "execute" ]; then
  echo "CONSOLIDATE-REFUSED:execute-not-activated the --execute code path is gated by the Phase 3 activation checkpoint; report-only evidence above"
  exit 1
fi

echo "report-only: no deletion performed; run-finalizer.sh not invoked"
```
<!-- PHASE3_CONSOLIDATE_END -->

```bash verify
# Phase 3 autonomous-consolidation levers exist and stay wired
test -f "$REPO_ROOT/skills/git-branch-consolidate/SKILL.md"
test -f "$REPO_ROOT/scripts/gsd/land-queue.sh"
test -f "$REPO_ROOT/skills/land-queue/scripts/queue-journal.py"
test -x "$REPO_ROOT/lib/gates.py"
test -x "$REPO_ROOT/skills/land-queue/scripts/collect-queue.py"
test -x "$REPO_ROOT/skills/git-branch-consolidate/scripts/collect-estate.py"
test -f "$REPO_ROOT/scripts/gsd/assert-merged.sh"
test -f "$REPO_ROOT/scripts/gsd/run-finalizer.sh"
grep -q "PHASE3_CONSOLIDATE_END" "$REPO_ROOT/skills/git-branch-consolidate/SKILL.md"
grep -q "read-landed-tuples" "$REPO_ROOT/skills/land-queue/scripts/queue-journal.py"
grep -q "grant-consolidate" "$REPO_ROOT/scripts/gsd/land-queue.sh"
grep -q "consolidate:estate" "$REPO_ROOT/lib/gates.py"
```

## Constraints

- READ-ONLY until Step 4, which requires explicit per-step approval.
- Never `git add -A`; never `-D`; never delete an unmerged branch or one checked
  out in a worktree; never delete a worktree with uncommitted changes without
  showing the diff first.
- A dirty worktree is a finding, not an obstacle — report the file count, and
  route stalled work to `/adopt-wip` rather than discarding it.
- Secrets: filenames only, never content.
