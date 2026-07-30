---
name: autonomy-grant
description: "Pre-approve the operator gates an unattended run will hit — prod push, deploy, merge, DNS — so the run proceeds through them without stopping. Use at plan approval, while the operator is present, to build the typed approval ledger that /feature-implement --autonomous checks mechanically."
version: "1.2.0"
---

# /autonomy-grant

The operator approves the run's gate list ONCE, up front. The loop then checks
the ledger mechanically instead of stopping to ask. A gate NOT in the ledger
still stops — that is the safety floor, not a bug.

Ported in spirit from the fable-agent-orchestration `autonomous-finish-loop`
skill (Apache-2.0): proceed on approved work; hold only at genuinely
unapproved external gates.

## When to run

- At plan approval, before an `--autonomous` `/feature-implement` run.
- Before any overnight/operator-absent run that will push, deploy, or merge.
- Morning after: to approve `pending` actions a run stopped on, then resume.

## Procedure

1. **Enumerate the gates.** Walk the plan/tasks and list EVERY
   operator-gated action the run will perform. Typed form only —
   `type:target` — because free prose never matches at run time:

   | Type | Example |
   |---|---|
   | `push` | `push:origin/main` |
   | `merge` | `merge:pr` |
   | `deploy` | `deploy:vercel-web` |
   | `flip` | `flip:HOLALUMINA_301_TO_GETGLANCE` |
   | `restart` | `restart:tenant-gateway` |
   | `secret-use` | `secret-use:RAILWAY_API_TOKEN` |
   | `migrate` | `migrate:add-column-x` |

2. **Present the list to the operator** — one screen, one decision. Include
   the rollback for each action. Wait for explicit yes.

   **MAX-AUTH mode (v1.1.0 — the /feature-spec and /task-swarm default):**
   when the operator pre-authorized at launch (ran the pipeline without
   `--gated`), skip this stop — record the full enumerated list immediately
   and put the list + rollbacks in the completion report instead of a
   blocking screen. This is NOT a wildcard: the ledger still holds exact
   typed entries walked from the plan, and an action not enumerated still
   stops. The launch flag moves the approval MOMENT to minute 1; it does not
   remove the floor.

3. **Record the grant:**

   ```bash
   python3 lib/gates.py grant "$RUN_ID" \
     --action push:origin/main \
     --action deploy:vercel-web \
     --action merge:pr \
     --ttl-hours 12
   ```

   TTL default 72h — sized for multi-day agentic runs (24h expired
   mid-run). Trim with `--ttl-hours` for short runs; cap 168h. Grants are
   run-bound: they never leak into the next run.

4. **At each gate, the loop checks — never asks:**

   ```bash
   python3 lib/gates.py check-grant "$RUN_ID" --action deploy:vercel-web \
     && <proceed + log> \
     || { python3 lib/gates.py pending "$RUN_ID" \
            --action deploy:vercel-web --reason "hit unlisted gate"; STOP; }
   ```

   Granted → proceed and log the action in the run report. Not granted →
   record pending + STOP that action (other independent tasks continue).

5. **Morning resume.** `gates.py pending "$RUN_ID"` lists what stopped.
   Approve with `grant`, resume. One command, not an archaeology session.

## Rules

- **Exact typed match only.** `push:origin/main` does not authorize
  `push:origin/release`. Wildcards do not exist — deliberately.
- **Expired = not granted.** Fail closed; re-grant if the run overruns TTL.
- **Novel irreversible actions always stop.** The ledger encodes what the
  operator FORESAW. Anything else is by definition unreviewed.
- **Every consumed grant appears in the final report** with its artifact
  (commit sha, deploy URL, PR number) — approval is not amnesty from
  evidence.

## Anti-patterns

- Blanket grants (`push:*`) — that is `SKIP_OPERATOR_GATES=1` with extra
  steps; if you want no floor, set that env var and accept its blast radius.
  (MAX-AUTH mode is not this: it pre-approves an enumerated list at launch —
  the recorded entries stay exact and unlisted actions still stop.)
- Granting mid-run over chat ("yes push it") without recording — the next
  gate check still fails and the run stalls again; record via `grant`.
- Enumerating gates from memory instead of the plan — walk the tasks; a
  missed gate is a 3am stop.
