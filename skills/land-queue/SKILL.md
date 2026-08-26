---
name: land-queue
description: "Serially land finished branches: collect from takeover records, estate discovery, and explicit args; rebase, implement, review, watch CI, merge under exact grant, finalize, and journal every step. v1 is strictly serial."
version: "1.0.0"
user-invocable: true
---

# Land Queue

## Host dispatch contract

- Codex: `$skill`, Codex collaboration roles, and GPT-5.6 tiers.
- Claude: `/skill`, Agent/Skill tools, and Claude aliases.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

Drain a queue of finished branches into the base branch, one item at a time,
with every side effect journaled before it starts and observed after it
finishes. The queue never guesses: authorization, merge truth, and deletion
truth live in their existing authorities and are only *called* from here.

## Invocation

```
bash scripts/gsd/land-queue.sh [--repo DIR] [--base NAME] [--run-id ID] [BRANCH...]
bash scripts/gsd/land-queue.sh --drain
bash scripts/gsd/land-queue.sh --resume QUEUE-ID
```

- **New queue:** run with zero or more explicit `BRANCH` arguments. Intake
  unions three sources — takeover records, landable `collect-estate.py`
  dispositions, and the explicit arguments — after a bounded fetch-first.
  The queue clock starts at that bounded intake call.
- **Explicit inputs:** positional branch names. Identity is
  `(branch, head SHA)`; any head/run/spec disagreement between sources is a
  typed `BLOCKED:identity-conflict`, never a merged record.
- **`--resume QUEUE-ID`:** re-enter a crashed queue from its journal. LANDED
  entries are never re-executed; nonterminal entries are reconciled against
  merge authority before any retry. (The reconciliation engine ships with
  the plan 02-02 failure machine; until then the flag refuses loudly.)
- **`--drain`:** creates the well-known `<store>/land-queue/DRAIN` marker
  file. A running queue honors it once at its next item boundary, consumes
  it (unlinks under the queue lock), and finishes with
  `QUEUE-DRAINED:operator-drain`.
- **STOP:** touch `<store>/land-queue/STOP` to abort. The runner checks the
  marker immediately before starting every external effect — item start,
  rebase, implementation, push, review, CI, merge, finalization — finishes
  in-flight work, starts nothing new, and records
  `QUEUE-ABORTED:operator-stop`.
- **`--parallel`:** parsed and always refused with
  `PARALLEL-UNSUPPORTED:v1-serial-only`. No lane is ever launched; v1 is
  deliberately serial.

`<store>` is the directory printed by `python3 lib/gates.py store-dir`; all
queue state is store-adjacent and worktree/host neutral.

## Verdict grammar

Item terminals (one per queue item, recorded in the journal and the report):

| Terminal | Meaning |
|---|---|
| `LANDED` | merged, asserted, finalized; the record carries the merge commit SHA |
| `BLOCKED:<reason>` | item stopped; e.g. `identity-conflict`, `conflict`, `source-missing`, `head-drift`, `ci-timeout`, `grant-missing` |
| `SKIPPED:<reason>` | item needed no work; e.g. `already-landed` |

Queue-level terminals (at most one per run):

| Terminal | Meaning |
|---|---|
| `QUEUE-ABORTED:systemic:<class>` | consecutive systemic failure classes tripped the breaker |
| `QUEUE-ABORTED:operator-stop` | STOP marker observed before an effect |
| `QUEUE-DRAINED:operator-drain` | DRAIN marker honored at an item boundary |
| `QUEUE-REFUSED:queue-live` | another live queue holds `<store>/land-queue/queue.lock` |

Infrastructure: `QUEUE-ERROR:store` — the journal is corrupt, hostile, or
unwritable; the queue fails closed rather than run without durable state.

Every non-`LANDED` terminal carries a reason and a **one-command Human-inbox
unblock** in the report; an empty inbox prints `HUMAN-INBOX: empty`.

## Authority boundaries

This skill orchestrates; it never re-implements a privileged decision:

- `skills/git-branch-consolidate/scripts/collect-estate.py` — estate
  discovery; queue eligibility is its landable `disposition` set, never the
  `landed` boolean.
- `run_bounded` (`scripts/gsd/run-bounded.sh`) — every external CLI call is
  wall-clock bounded; CI waits use exactly
  `run_bounded 1200 gh pr checks PR --watch --interval 10`.
- `python3 lib/gates.py check-grant RUN_ID --action merge:pr-N` — the exact,
  unexpired, run-bound merge authority. Merges are pinned with
  `--match-head-commit` to the review/CI-time head OID; any head drift is a
  typed abort, never a re-pin.
- `scripts/gsd/assert-merged.sh` — the only merged-state truth; LANDED is
  recorded only after it passes.
- `scripts/gsd/run-finalizer.sh` — the only deletion/cleanup truth; it runs
  after the merged assertion and before the LANDED terminal.

## Operating constraints

- Queue worktrees must be clean: each item is rebased and pushed from a
  private worktree; dirty state is never stashed or discarded.
- Bounds are wall clock plus rounds — 90 minutes per item, 8 hours per
  queue from intake, 2 rounds per item, 10 items per queue — because no
  usable token total exists across vendor CLIs.
- Zero live vendor calls occur in the test suites; only the queue's own
  runtime touches `gh`, reviewer CLIs, or implementation children.

```bash verify
for artifact in \
  skills/land-queue/SKILL.md \
  skills/land-queue/scripts/collect-queue.py \
  skills/land-queue/scripts/queue-journal.py \
  scripts/gsd/queue-guard.sh \
  scripts/gsd/land-queue.sh; do
  [ -f "$REPO_ROOT/$artifact" ]
done
bash "$REPO_ROOT/scripts/gsd/queue-guard.sh" --contract-probe
bash "$REPO_ROOT/scripts/gsd/land-queue.sh" --contract-probe
```
