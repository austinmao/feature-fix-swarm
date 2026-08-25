# Cross-session messaging — shared files, advisory locks, no mailbox

Several Claude Code or Codex sessions work the same repository at once. They
have to agree on who owns a spec, who may edit a path, which artifact was
promoted, and what the last session was in the middle of.

**There is no mailbox.** No session can address another one. Every channel is
the same shape: a file on disk that one session writes under a lock and another
session reads when it happens to look. That is a deliberate design point — a
polling reader cannot lose a message it never had to receive — but it means
"send a message to the other session" is not an operation that exists.

| Channel | Carrier | Reference |
|---|---|---|
| Who owns this spec / this path | `coord/registry.json` | [Coordination layer](coordination.md) |
| Grants, promotions, loop counters | one `evidence.json` | this page |
| Only-one-of-me | runner pidfile, finisher lock | this page |
| What the last session was doing | handoff / STATUS documents | this page |
| Something happened | `digest.sh` event lines | [Digest](digest.md) |

## Claims and leases

`scripts/coord/coord.py` is the registry a session claims a spec in and leases
paths from. Store is one directory per repository —
`<git-common-dir parent>/.feature-fix-swarm/coord/` — so a claim taken inside a
linked worktree is visible from the main checkout and vice versa.

The spec-stable join between a run and a session is
`sessions/by-run/<run_id>`: a plain file holding one session UUID, published
with `O_CREAT|O_EXCL|O_NOFOLLOW` at 0600 in a single unbuffered write, with
readers retrying briefly against a torn read.

Full CLI, exit codes, staleness and reclaim semantics live in
[Coordination layer](coordination.md). The one thing to carry over here: a
claim's holder is judged dead by **process liveness plus a start token**, not by
a wall-clock timeout, so a paused session does not lose its claim to a slow
afternoon.

Edit-time enforcement is `scripts/hooks/path-reservation-gate.sh`, a PreToolUse
hook that blocks `Edit`/`Write`/`MultiEdit` inside another session's live
exclusive lease. It is a pure reader — it never writes the registry and never
takes the lock. Its stated gaps: writes performed through the Bash tool and
`NotebookEdit` are not intercepted.

## The evidence store

`lib/gates.py` keeps one JSON ledger that every session reads and writes. Its
path is pinned to the **main checkout** via `git rev-parse --git-common-dir`
(`lib/gates.py:1908-1930`), which is the fix for a real incident: four separate
`evidence.json` files were live at audit time, one per worktree, each session
happily reading its own decoy. `$GATES_STORE` still wins when set.

One store also makes the lock mean something. `_StoreLock` takes an exclusive
`flock` on `<store>.lock` around every read-modify-write, and `_save_store`
writes a same-directory tempfile then `os.replace`s it, so a concurrent reader
never sees a torn file.

Reserved namespaces, each with a shape guard that refuses rather than trusting a
malformed foreign write:

| Namespace | Carries |
|---|---|
| `_autonomy[run_id]` | granted actions and their TTLs |
| `_promotions[run_id]` | `surface`, `artifact`, `evidence_ids`, `recorded_at` — the only run+surface binding the schema has |
| `_loops[run_id][loop]` | round counters behind the plan-wall cap |
| `_degradation.mappings` | the runstore→ledger `run_id` join `digest.sh` uses |

Treat the store as potentially-corrupt input, because another session — or an
older version of this one — wrote it.

## Single-flight locks

Two locks stop two sessions from doing the same irreversible thing.

**Runner pidfile** (`scripts/gsd/lib-lock.sh:54-114`). An atomic `set -C` claim
plus a heartbeat file. A duplicate launch exits **75**. Reclaim requires a
reclaim-mutex and a machine identity check; a symlinked lock path refuses with
78. The coord claim is taken after pidfile ownership is confirmed and *before*
the heartbeat subshell forks — a claim taken after the fork would leave the
subshell without a generation for the run's whole life.

**Finisher lock** (`scripts/gsd/run-finalizer.sh:98-143`). Per **user**, not per
repo: `$HOME/.cache/feature-fix-swarm/finisher.lock`. Losing it is a visible
yield, and this is the closest thing in the system to a directed message: the
loser writes a `finisher-skipped` evidence event carrying `run_id`, `pr`,
`lock_path`, and `holder_pid`. That row is how the session that stood down tells
the one that proceeded.

**Liveness** (`scripts/gsd/liveness-check.sh`) answers "is that other run still
alive?" from three ORed signals — pid alive, state-file mtime inside the window
(default 30 min), or a valid gates grant. It deliberately never trusts a shell
exit code or a `state=completed` marker alone.

## Handoff documents

The actual session→session payload is a document, and there are two of them.

**Live**: `.planning/run-state/HANDOFF.json`, written by
`hooks/gsd-checkpoint.sh` after tool calls, throttled to at most once per 60s
off the file's own mtime, so resume state is never more than a minute stale.
The hook is inert until a consumer wires it as a `PostToolUse` matcher, and its
kill switch is `GSD_CHECKPOINT=off`. It is surfaced by
`hooks/gsd-handoff-resume.sh` at SessionStart, which prints it plus the resume
pointer. Nothing auto-consumes it; it persists until the resume flow or
`run-finalizer.sh` retires it.

**Durable**: `.planning/STATUS-<UTC>.md` plus a `/handoff` document, written by
`/spec-status`. Its coordination section reads `coord.py status` and treats a
claim held by another live session as *do not proceed*.

### Publishing order is the whole point

Handoff documents are the one artifact most likely to contain a pasted
credential, so `scripts/gsd/publish-scanned-handoff.sh` fixes the order:

```bash
publish-scanned-handoff.sh <artifact> --commit "<message>"
publish-scanned-handoff.sh <artifact> --publish-to <dest>
```

1. Copy the artifact to a private 0600 temp file.
2. Scan **that copy** — never a git index blob, because a blob means the bytes
   already reached the object database before detection.
3. Only a clean scan proceeds. A finding touches neither the index nor the
   destination.

The commit path is race-free by construction: it refuses to run if unrelated
paths are already staged, builds the tree in a private `GIT_INDEX_FILE` so a
concurrent `git add` cannot inject unscanned bytes, then `commit-tree` and a
compare-and-swap `update-ref HEAD <new> <parent>` that fails rather than
clobbering. Two post-conditions verify the commit touched exactly one path with
exactly the scanned blob, with a CAS-guarded undo if either fails. The working
tree is synced from the scanned copy only after that verification.

## Trust boundary

Read this before treating any of the above as a security control.

- **Coordination is cooperative, not enforced.** Single-UID, advisory locks.
  Any process running as the same user can edit `registry.json` or export a
  borrowed identity; the borrowed-identity check catches the accidental case,
  not the deliberate one. See [Coordination layer](coordination.md).
- **Handoff content is untrusted data when it re-enters a prompt.** Every
  consumer fences it through `scripts/gsd/fence-data.sh`: `fence_data <TAG>`
  wraps the body in `<TAG>_DATA_START`/`_DATA_END`, and `fence_neutralize`
  rewrites counterfeit markers inside the body to `<TAG>_DATA_ESCAPED` rather
  than dropping them, so a document cannot fake an early fence close. Grants and
  budgets are always read from the store by `run_id`, never from the document.
- **`DIGEST_NOTIFY_CMD` is the operator's own shell authority**, executed on the
  operator's machine, and accepted as such — not an injection surface. See
  [Digest](digest.md).

## Notification

`scripts/gsd/digest.sh` is the read side: a no-daemon poll that emits nine event
classes exactly once each, plus a daily summary. Cursors are tie-safe positions,
never timestamps, and a transport failure retains only the failing class's
cursor so redelivery is guaranteed per class. Full contract in
[Digest](digest.md).

## What does not exist yet

Do not go looking for these; they are designed and unbuilt, owned by spec 006:

- `<store>/takeover/*.json` — the machine-readable takeover record.
- `takeover-check.sh` — the deterministic re-verify wall.
- `/land-queue` — the N-items-to-`origin/main` drain.

The only takeover machinery that exists today is the intra-runner reclaim lease
(`GSD_RECLAIM_LEASE_SECS`, see [Configuration](configuration.md)), which is a
lock-reclaim timeout and not a takeover record.

Known limitation worth planning around: leases have no waiter queue, so shared
holders can starve an exclusive waiter indefinitely.

## Related

- [Coordination layer](coordination.md)
- [Digest](digest.md)
- [Configuration](configuration.md)
