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
bash scripts/gsd/land-queue.sh [--repo DIR] [--base NAME] [--run-id ID] [--posture zero|floor] [BRANCH...]
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
  entries are never re-executed; every nonterminal entry is reconciled
  against merge authority (`gh pr view` by its recorded idempotency key)
  before anything replays. A satisfied merge key becomes reconciled LANDED
  with zero merge calls; a finalizer intent without a terminal re-runs
  run-finalizer idempotently and only then appends LANDED; anything else is
  `BLOCKED:resume-incomplete` with a re-run remedy.
- **`--posture zero|floor`** (default `zero`): review posture for this run.
  `floor` blocks without an opposite-vendor reviewer
  (`BLOCKED:no-cross-vendor-reviewer`) — executable presence never counts; a
  satisfied review is rc 0 plus a recorded findings artifact under
  `<store>/land-queue/reviews/`. `zero` permits the merge to continue when
  the reviewer is missing, but ONLY after `gates.py note-degraded` durably
  records the degradation bound to run, invocation, branch, full head SHA,
  baseline, the Git-computed changed-file set, its production subset, and
  `production_touch`; an unrecordable degradation fails closed
  (`BLOCKED:degradation-unrecorded`). Production promotion later refuses
  when degraded reviews exceed 50% of the run or when ANY degraded review
  touched production. In zero posture a conflict-quarantined item requeues
  exactly once after the base advances; its second quarantine parks
  permanently (EDGE-010).
- **`--drain`:** creates the well-known `<store>/land-queue/DRAIN` marker
  file. A running queue honors it once at its next item boundary, consumes
  it (unlinks under the queue lock), and finishes with
  `QUEUE-DRAINED:operator-drain`.
- **STOP:** touch `<store>/land-queue/STOP` to abort. The runner checks the
  marker immediately before starting every external effect — item start,
  rebase, implementation, push, review, degradation recording, CI, merge,
  finalization — finishes in-flight work, starts nothing new, and records
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
| `LANDED` | merged, asserted, finalized; the record carries the merge commit SHA and the report adds a `REVERT: <item> git revert <sha>` row |
| `BLOCKED:<reason>` | item stopped; e.g. `identity-conflict`, `conflict`, `source-missing`, `no-progress`, `head-drift`, `head-moved`, `ci-timeout`, `ci-empty`, `merge-conflict`, `gh-auth`, `no-cross-vendor-reviewer`, `degradation-unrecorded`, `grant-missing`, `resume-incomplete` |
| `SKIPPED:<reason>` | item needed no work or never started; e.g. `already-landed`, `queue-aborted` (materialized for every untouched item after a systemic abort) |

Queue-level terminals (at most one per run):

| Terminal | Meaning |
|---|---|
| `QUEUE-ABORTED:systemic:<class>` | two consecutive enumerated systemic failures tripped the breaker (class-agnostic), or a guard cap/wall verdict named the class (`max-items` — including an intake whose true item count exceeded the 10-item cap — `round-cap`, `item-wall`, `queue-wall`); untouched items become `SKIPPED:queue-aborted` |
| `QUEUE-ABORTED:operator-stop` | STOP marker observed before an effect |
| `QUEUE-DRAINED:operator-drain` | DRAIN marker honored at an item boundary |
| `QUEUE-REFUSED:queue-live` | another live queue holds `<store>/land-queue/queue.lock` |

Infrastructure: `QUEUE-ERROR:store` — the journal is corrupt, hostile, or
unwritable; the queue fails closed rather than run without durable state.

Every non-`LANDED` terminal record carries a separate nonempty `reason` and
a **one-command `unblock`**, rendered as
`HUMAN-INBOX: <item> <status> reason: <reason> unblock: <command>`; an empty
inbox prints `HUMAN-INBOX: empty`.

## One-command remedies

| Block class | Remedy |
|---|---|
| `BLOCKED:conflict` | `git -C <repo> rebase origin/<base> <branch>` |
| `BLOCKED:no-progress` | fix the repeated gate failure named in the reason, then re-run the queue |
| `BLOCKED:no-cross-vendor-reviewer` | install an opposite-vendor reviewer CLI (codex or claude), then re-run |
| `BLOCKED:degradation-unrecorded` | re-run with a ledger-shaped `--run-id` (`spec-NNN`, `run-N`, `adhoc-*`) |
| `BLOCKED:ci-timeout` / `BLOCKED:ci-empty` | fix or configure required checks on the PR, then re-run |
| `BLOCKED:merge-conflict` | `git -C <repo> rebase origin/<base> <branch>`, push, re-run |
| `BLOCKED:gh-auth` | `gh auth login`, then re-run |
| `BLOCKED:head-drift` / `BLOCKED:head-moved` | re-run so the current head is reviewed before merge |
| `BLOCKED:grant-missing` | `python3 lib/gates.py grant <run-id> --action merge:pr-<N> --reason '<why>'` |
| `BLOCKED:resume-incomplete` | re-run `land-queue.sh` for the item |
| `PARALLEL-UNSUPPORTED:v1-serial-only` | drop `--parallel`; v1 is strictly serial |

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
