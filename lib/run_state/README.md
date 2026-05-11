# run_state

Persistent run state for `/feature` and `/fix` skills.

## What it adds

Four capabilities to long-running pipeline skills:

1. **Persistent state** across Claude Code sessions (SQLite at `~/.claude/state/runs.db`).
2. **Stop-hook auto-continuation:** when Claude tries to stop while a run is `active`, the hook injects a continuation prompt so the pipeline resumes.
3. **Adversarial completion audit:** before declaring done, spawn `codex` GPT-5 in a hostile prompt that tries to prove the work is NOT done. Three verdicts: pass / fail / error.
4. **Token budget tracking:** runs flip to `budget_limited` when used > budgeted; operator resumes manually.

## States

| state | meaning |
|---|---|
| `active` | Pipeline running; Stop hook injects continuation. |
| `pending_audit` | Adversarial audit in progress. |
| `paused` | Operator paused; marker cleared; resume manually. |
| `budget_limited` | Token budget hit; Stop hook allows stop; resume manually. |
| `complete` | Audit passed AND (for /feature) canary passed. |
| `failed` | 3 audit attempts exhausted, or unrecoverable error. |
| `aborted` | Operator killed via `run-state abort`. |

## CLI

```bash
run-state start --skill <feature|fix> --objective "<text>" [--tokens N] [--worktree PATH] [--session-id ID]
run-state status <run_id>
run-state list [--state STATE]
run-state update <run_id> --phase <name> | --tokens <delta> | --state <state>
run-state audit <run_id> --kind <fix|feature> --context KEY=VALUE [--cwd PATH]
run-state complete <run_id>
run-state pause <run_id>
run-state resume <run_id>
run-state abort <run_id>
```

## Hooks (auto-installed by setup.sh)

`~/.claude/settings.json` registers:
- `SessionStart`: `python3 ~/.claude/hooks/run-state-session.py` — writes `CLAUDE_SESSION_ID` to `~/.claude/state/session.env`
- `Stop`: `python3 ~/.claude/hooks/run-state-stop.py` — checks marker, blocks stop and injects continuation if active

## Files

| Path | Purpose |
|---|---|
| `~/.claude/state/runs.db` | SQLite database. Tables: `runs`, `events`. |
| `~/.claude/state/.active-run` | Single-line text file with current run_id. O(1) stat-check by Stop hook. |
| `~/.claude/state/session.env` | `CLAUDE_SESSION_ID=...` for skill subprocesses to source. |
| `~/.claude/lib/run_state/prompts/{fix,feature}_audit.txt` | Adversarial audit prompt templates. |
| `~/.claude/bin/run-state` | Bash shim → `python3 -m run_state.cli`. |

## How /fix uses it

```bash
RUN_ID=$(run-state start --skill fix --objective "$BUG_DESC" --tokens 300000 | jq -r .run_id)
run-state update "$RUN_ID" --phase investigate
run-state update "$RUN_ID" --phase implement
run-state update "$RUN_ID" --phase qa

# Before declaring complete: hostile audit
run-state audit "$RUN_ID" --kind fix \
  --context "BUG_DESCRIPTION=$BUG_DESC" \
  --context "MODIFIED_FILES=$(git diff main...HEAD --name-only)"
# Exit 0 (verdict=pass) → marker cleared, state=complete
# Exit 1 (verdict=fail or error) → state=active, Stop hook continues loop
```

## How /feature uses it

Same lifecycle plus:
- Higher default budget (1.5M vs 300K tokens — longer pipeline)
- Audit kind = `feature` with spec content + diff
- After audit pass, skill explicitly re-sets state to `active` (overriding audit's auto-complete) so the Stop hook continues through canary phase. Only after canary passes does the skill mark `complete`.

## Debugging

```bash
run-state list --state active
sqlite3 ~/.claude/state/runs.db \
  "SELECT created_at, event_type, payload_json FROM events WHERE run_id = '<id>' ORDER BY created_at;"
run-state abort <id>
rm -f ~/.claude/state/.active-run
```

## Tests

```bash
cd ~/Documents/Github/feature-fix-swarm/lib/run_state
python3 -m pytest -v
```

## Edge cases

- **Concurrent /feature and /fix:** Current marker holds only one run_id. If both run simultaneously, the second overwrites — first still queryable by id but Stop hook only continues the most recent. Documented as "one long-running skill per session" for now.
- **codex CLI missing:** `audit` subcommand returns `verdict=error` with reasoning `"codex CLI not installed"`. Run state reverts to `active`; operator must install codex (`npm install -g @openai/codex`) or run with `--no-audit`.
- **Audit false-pass risk:** codex may declare pass when bug still exists in untouched code path. Mitigation: aggressive prompt + 3-attempt hard cap. Track verdict accuracy in postmortem.
