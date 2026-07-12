---
name: preflight
description: "Prove every env var, secret, and service the run will need is present and reachable BEFORE starting an unattended or overnight run. Use at the planning stage — after decompose, before /feature-implement — so the run never stalls at 3am on a missing variable or dead endpoint."
version: "1.1.0"
---

# /preflight

An unattended run that stops on a missing env var wasted the whole night.
Requirements are proven at plan time — while the operator is still present —
never discovered at run time.

## When to run

- Before any `--auto` / `--autonomous` `/feature-implement` run.
- Before an overnight or operator-absent run of any kind.
- After decompose, when the task list makes the runtime footprint knowable.

## Procedure

1. **Author the manifest** — `specs/NNN/preflight.json`. Enumerate what the
   RUN will touch, not what the repo could touch:
   - every env var / secret name the tasks read (`process.env.*`,
     `os.environ`, `doppler secrets get` names) — scan the diff-scope AND the
     plan's deploy/QA steps;
   - every external service the run must reach (DB, gateway, deploy target,
     MCP server), each as a cheap real probe.

   ```json
   [
     {"kind": "env",   "name": "DATABASE_URL"},
     {"kind": "env",   "name": "N8N_WEBHOOK_HMAC_SECRET"},
     {"kind": "probe", "name": "db-reachable",
      "cmd": "psql \"$DATABASE_URL\" -c 'select 1' -qtA"},
     {"kind": "probe", "name": "gateway-health",
      "cmd": "curl -sf -m 10 http://127.0.0.1:18789/health -o /dev/null"},
     {"kind": "probe", "name": "vercel-auth", "cmd": "vercel whoami"}
   ]
   ```

2. **Run it, fail closed:**

   ```bash
   python3 lib/gates.py preflight specs/NNN/preflight.json --run "$RUN_ID"
   ```

   Exit 1 on ANY missing var / failed probe / empty manifest. Fix while the
   operator is present (fetch from Doppler, start the service, re-auth), then
   re-run until `PREFLIGHT-PASS`.

3. **The loop verifies mechanically.** An unattended start requires
   `gates.py check-preflight "$RUN_ID"` → exit 0 (recorded PASS, < 24h old).
   No fresh pass, no unattended run.

### Harness audit (advisory)

Separate from the run manifest: `python3 scripts/harness-audit.py [--json]`
scores the `~/.claude` harness itself 0-100 (dangling skill symlinks,
vendored-copy version drift, dead model pins in `.planning/config.json`,
unregistered hooks). Run it alongside step 2 and surface a low score as an
advisory finding in the preflight report.

This section is advisory only — it NEVER blocks preflight. The script always
exits 0; a low score is a heads-up, not a `PREFLIGHT-PASS` gate, and a
machine without `~/.claude/skills` scores 100 with a skip-note (absence is
not drift). Do not wire its exit code or score into `gates.py preflight` —
that gate stays fail-closed on the manifest only.

## Rules

See `docs/promotion-protocol.md` for the full 12-rule dev→staging→production
promotion protocol; staging-proof preflight checks are one of its enforcement points.

- **Env checks are presence-only.** A secret VALUE never appears in the
  manifest, the output, or the evidence store — names only.
- **Probes are real executions**, not greps. "The var is set" does not prove
  the service answers; probe the service.
- **Empty manifest fails.** A run with nothing declared is undeclared, not
  requirement-free.
- **Scan is additive, never authoritative.** Grepping the diff for env reads
  seeds the manifest; the authored manifest is the contract.

## Anti-patterns

- Skipping preflight because "the last run worked" — env drifts between runs
  (rotated secret, sleeping service, expired auth).
- Probing with `echo $VAR` — leaks the value AND proves nothing about
  reachability.
- Declaring only the happy path. If the plan has a deploy step, the deploy
  target's auth belongs in the manifest.
