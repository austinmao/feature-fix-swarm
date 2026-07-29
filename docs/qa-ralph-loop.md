# QA Ralph Loop

Per-phase quality assurance that runs automatically during `/feature-implement`,
plus a narrower background auto-QA hook for in-flight edits.

## What it does (the real per-phase gate)

After every phase's tasks complete, `/gsd-execute-phase N` (invoked by
`/feature-implement`) runs an ordered gate ladder, cheapest checks first:

```
compile/typecheck → lint → unit → integration → e2e smoke → LLM review dims
```

- A rung failure skips later rungs and retries the task (fail-fast).
- Same failure signature twice in a row = no-progress → STOP and report
  (`gates.py note-failure`).
- Phase truth score below `TRUTH_THRESHOLD` (default `0.95`) after retries →
  rollback to the phase-start checkpoint (`gates.py phase-score`).
- After each impl task, `gates.py scan-tamper` flags deleted asserts, added
  skips, `exit 0`, or CI edits as CRITICAL — blocks the phase.
- `lib/gates.py` (the evidence store) is the completion authority, never
  self-report. See [Commands](commands.md) and [Configuration](configuration.md).

There is no `--qa-loop`/`--no-qa-loop` flag on `/feature-implement` — this gate
always runs. The flags that actually exist are `--autonomous`, `--dry-run`,
`--adhoc`, `--no-finish` ([Commands](commands.md)).

## The background auto-QA hook (a separate, narrower mechanism)

`scripts/hooks/post-implement-batch.sh` is a PostToolUse hook that fires only
on edits under `.claude/worktrees/phase-*`. It debounces (`RALPH_DEBOUNCE_SECS`,
default `30`), then runs:

```bash
bash scripts/qa-swarm.sh --phase "auto-qa" --diff "<changed files>" \
  --spec-dir "." --qa-only "review"
```

— a single LLM review pass on the changed files, not the full gate ladder
above. Disable with `RALPH_AUTO_QA=0`.

`scripts/qa-swarm.sh` itself (2 deterministic hooks — `vitest run` / `pytest -x`
— plus up to 4 LLM dims: `e2e`, `review`, `security`, `design`) accepts its own
`--qa-skip`, `--qa-only`, and `--max-retries` (default `3`) flags. These are
script flags, not `/feature-implement` flags — the auto-QA hook above is
currently the only caller.

## Retry loop

`scripts/ralph-retry.sh` runs the investigate → fix → re-QA cycle on gate
failure, up to `--max-retries` (default `3`) attempts.

## Artifacts

Failure artifacts land in `.ralph/<phase-slug>/` (gitignored): logs,
`results.json`, per-dimension result files. Used by `/investigate` for root
cause analysis.

## Cost

~$0.15 per phase for the 3-4 LLM QA agents at sonnet tier (see the cost table
in [README](../README.md)).

## Related

- [Commands](commands.md) — the gsd-core gate ladder and `gates.py` subcommands
- [Configuration](configuration.md) — `GATES_STORE`, `TRUTH_THRESHOLD`,
  `GATES_STRICT`, and the `RALPH_*` env vars
- [Browser proof](browser-proof.md) — the e2e dimension's evidence gate
