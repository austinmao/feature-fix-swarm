# QA Ralph Loop

Per-phase quality assurance that runs automatically during `/feature-implement`.

## What it does

After every phase in tasks.md completes, the QA Ralph loop:

1. **Deterministic hooks** ($0, always run):
   - `vitest run` on changed TypeScript/JavaScript files
   - `pytest -x` on changed Python files

2. **LLM QA agents** (~$0.15/phase, Ruflo-coordinated and host-CLI executed):
   - **qa-e2e** — browser tests user stories (skipped if no dev server)
   - **qa-review** — code review for logic errors, CRITICAL/HIGH only
   - **qa-security** — OWASP Top 10 scan, CRITICAL only

3. **On failure**: investigate root cause → fix → re-test (up to 3 retries)

## Flags

| Flag | Effect |
|------|--------|
| `--qa-loop` | Enable (default ON) |
| `--no-qa-loop` | Disable entirely |
| `--qa-skip e2e,security` | Skip specific dimensions |
| `--qa-only review` | Run only these dimensions |

## Environment variables

| Var | Default | Effect |
|-----|---------|--------|
| `RALPH_MAX_RETRIES` | `3` | Max retry attempts per phase |

## Artifacts

Failure artifacts stored in `.ralph/` (gitignored):
- Logs, screenshots, diffs per phase
- Ruflo manifest and per-agent logs when available
- Used by `/investigate` for root cause analysis

## Cost

~$0.15 per phase (3 LLM agents at sonnet tier).
A 5-phase feature adds ~$0.75 in QA overhead.

## Disabling

Set `RALPH_MAX_RETRIES=0` or pass `--no-qa-loop` to skip entirely.

## Gate ladder (feature-implement v1.9.0)

Per-phase QA runs as an ordered ladder, cheapest deterministic gates first:

```
compile/typecheck → lint → unit → integration → e2e smoke → LLM review dims
```

- A rung failure skips all later rungs and retries the task (fail-fast).
- Deterministic rungs retry up to `RALPH_MAX_RETRIES`; LLM review rounds cap at 2.
- Same failure signature twice in a row = no-progress → STOP and report.
- Phase truth score < 0.95 after retries → rollback to phase-start checkpoint.
- After each impl task: `gates.py scan-tamper` on the diff — deleted asserts,
  added skips, `exit 0`, or CI edits are CRITICAL and block the phase.
- Every gate outcome feeds `hooks_model-outcome` so the model router learns.
