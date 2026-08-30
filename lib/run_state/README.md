# run_state

Adversarial audit recorder for `/feature` and `/fix` skills. v3.0.

## What it provides (v3.0)

**Cross-model adversarial audit** of work done by `/feature` and `/fix`. The native Claude `/goal` command ([docs](https://code.claude.com/docs/en/goal)) owns the continuation loop. This library owns the verification gate:

1. **Adversarial audit**: spawn `codex` GPT-5 in a hostile prompt that tries to prove the work is NOT done. Three verdicts: `pass` / `fail` / `error`.
2. **Audit record**: persist verdict + missing[] to SQLite at `~/.claude/state/runs.db` so native `/goal` condition checker can grep audit history.
3. **CLI** (`run-state`) to start/inspect/audit/complete runs.

What v2.x had but v3.0 dropped (now handled by native `/goal`):
- Stop hook with continuation prompt injection
- `.active-run` marker file
- continuation_count + max_continuations runaway guard
- pause/resume (use `/goal clear` then re-issue `/goal`)
- `budget_limited` auto-state-flip (events still emitted for analytics)

## States

| state | meaning |
|---|---|
| `active` | Pipeline running; native /goal loops |
| `pending_audit` | Adversarial audit in progress |
| `complete` | Audit passed (kind=fix); for kind=feature/phase, set by skill after canary |
| `failed` | Operator-marked unrecoverable error |
| `aborted` | Operator killed via `run-state abort` |

(`paused` and `budget_limited` removed in v3.0.)

## CLI

```bash
run-state start --skill <feature|fix> --objective "<text>" [--tokens N] [--worktree PATH] [--session-id ID]
run-state status <run_id>
run-state list [--state STATE]
run-state update <run_id> --phase <name> | --tokens <delta> | --state <state>
run-state audit <run_id> --kind <fix|feature|phase> --context KEY=VALUE [--cwd PATH]
run-state complete <run_id>
run-state abort <run_id>
```

`--tokens` accepts K/M/B/T suffix: `250K`, `1.5M`, `1B`, `2T`.

`update`, `complete`, `abort`, and `audit` exit nonzero with a single JSON
error line on stderr (`{"error": "not_found", "run_id": "<id>"}`) when
`run_id` names a run that does not exist.

## Files

| Path | Purpose |
|---|---|
| `~/.claude/state/runs.db` | SQLite database. Tables: `runs`, `events`. |
| `~/.claude/lib/run_state/prompts/{fix,feature,phase}_audit.txt` | Hostile audit prompt templates. |
| `~/.claude/bin/run-state` | Bash shim → `python3 -m run_state.cli`. |

## How /fix uses it (v3.0)

```bash
# Operator first sets a native /goal:
#   /goal "bug fixed: run-state audit verdict=pass on this issue AND qa green"

# Skill records audit at end of pipeline:
RUN_ID=$(run-state start --skill fix --objective "$BUG_DESC" | jq -r .run_id)
run-state audit "$RUN_ID" --kind fix \
  --context "BUG_DESCRIPTION=$BUG_DESC" \
  --context "MODIFIED_FILES=$(git diff main...HEAD --name-only)"
# Exit 0 (pass) → state=complete; native /goal sees verdict=pass and clears
# Exit 1 (fail) → state=active; native /goal continues loop
```

## How /feature uses it (v3.0)

```bash
# Operator first sets a native /goal:
#   /goal "spec NNN done: all phase audits pass, review-gate PASS, canary 200"

# Per-wedge audit between every implemented wedge:
run-state audit "$RUN_ID" --kind phase \
  --context "PHASE_NAME=backend-wedge" \
  --context "PRIOR_PHASES=none" \
  --context "PHASE_SPEC=$WEDGE_SPEC" \
  --context "PHASE_DIFF=$WEDGE_DIFF"
# kind=phase pass keeps state=active; advance to next wedge
```

## Debugging

```bash
run-state list --state active
sqlite3 ~/.claude/state/runs.db \
  "SELECT created_at, event_type, payload_json FROM events WHERE run_id = '<id>' ORDER BY created_at;"
run-state abort <id>
```

## Tests

```bash
cd ~/Documents/Github/feature-fix-swarm/lib/run_state
python3 -m pytest -v
# 36 passed
```

## Native /goal integration

v3.0 assumes Claude Code 2.1.139+ with native `/goal`. To use:

```
/goal "<verifiable condition>"
```

Native /goal runs a small/fast model after each turn to check the condition. When true, /goal auto-clears. When false, Claude continues without operator prompt.

Good conditions for our pipelines:

- `/goal "specs/130 done: ~/.claude/state/audits.jsonl shows verdict=pass for every phase audit, review-gate PASS, canary returns 200"`
- `/goal "bug X fixed: latest run-state audit --kind fix verdict=pass, npx vitest run exits 0"`

See https://code.claude.com/docs/en/goal for native /goal semantics.

## Edge cases

- **Concurrent /feature and /fix**: no marker lock anymore. Each call creates its own `runs` row; SQLite handles concurrent writes via WAL. Operator must reference the right run_id in `/goal` conditions.
- **codex CLI missing**: `audit` returns `verdict=error` with reasoning `"codex CLI not installed"`. State stays `active`; operator can install codex or use `--no-audit` on the skill.
- **Audit false-pass risk**: codex may declare pass when a bug still exists in untouched code. Mitigation: aggressive prompt + 3-attempt cap (per skill convention). `/review-gate` (Step 5.7 of /feature) is the final hard gate.
