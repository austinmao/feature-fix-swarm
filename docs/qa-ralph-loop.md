# QA Ralph Loop

Per-phase quality assurance that runs automatically during `/feature-implement`.

## What it does

After every phase in tasks.md completes, the QA Ralph loop:

1. **Deterministic hooks** ($0, always run):
   - `vitest run` on changed TypeScript/JavaScript files
   - `pytest -x` on changed Python files

2. **LLM QA agents** (~$0.15/phase, via ruflo swarm):
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
- Used by `/investigate` for root cause analysis

## Cost

~$0.15 per phase (3 LLM agents at sonnet tier).
A 5-phase feature adds ~$0.75 in QA overhead.

## Disabling

Set `RALPH_MAX_RETRIES=0` or pass `--no-qa-loop` to skip entirely.
