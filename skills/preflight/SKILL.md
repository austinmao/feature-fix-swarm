---
name: preflight
description: "Prove every env var, secret, and service the run will need is present and reachable BEFORE starting an unattended or overnight run. Use at the planning stage — after decompose, before /feature-implement — so the run never stalls at 3am on a missing variable or dead endpoint."
version: "2.0.0"
---

# /preflight

## Host dispatch contract

- Codex: `$skill`, Codex collaboration roles, and GPT-5.6 tiers.
- Claude: `/skill`, Agent/Skill tools, and Claude aliases.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

## Init gate

Before any other step, run the advisory init guard and relay its output
verbatim:

```bash
bash "$(git rev-parse --show-toplevel)/scripts/gsd/init-guard.sh" || true
```

If it printed `INIT-GUARD:` warnings, offer `/ffs-init` before proceeding in
interactive sessions (declining proceeds anyway); headless, spawned, and
autonomous runs relay the warnings once and continue. Advisory only — never
a block, never an exit-code change.

At entry, make one opportunistic, fail-soft `bash scripts/gsd/reconcile.sh` pass; never block on its result.

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

   **Seed from the environment registry** (REQ-401 Seam 2): when
   `config/environments.yaml` exists, run

   ```bash
   bash scripts/gsd/env-registry.sh seed
   ```

   It emits candidate rows (names only — every `secret_names` entry as an
   env row, every non-`none` `base_url` as a cheap probe row) as a JSON
   array on stdout and writes nothing. Merge them into the authored
   manifest yourself — additive, never authoritative: per the scan rule
   below, the authored manifest is the contract.

   ```json
   [
     {"kind": "env",   "name": "PGHOST"},
     {"kind": "env",   "name": "PGDATABASE"},
     {"kind": "env",   "name": "PGUSER"},
     {"kind": "env",   "name": "PGPASSWORD"},
     {"kind": "env",   "name": "N8N_WEBHOOK_HMAC_SECRET"},
     {"kind": "probe", "name": "db-reachable",
      "argv": ["psql", "-c", "select 1", "-qtA"]},
     {"kind": "probe", "name": "gateway-health",
      "argv": ["curl", "-sf", "-m", "10", "http://127.0.0.1:18789/health", "-o", "/dev/null"]},
     {"kind": "probe", "name": "vercel-auth", "argv": ["vercel", "whoami"]}
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
- **Probes are real executions**, not greps. `argv` is a non-empty JSON array
  and is executed directly without a shell. Environment placeholders are
  rejected because expanding secrets into process arguments exposes them to OS
  process inspection; invoke a program that reads the inherited environment
  instead. Shell command strings, pipes, redirects, and operators are rejected.
  "The var is set" does not prove the service answers; probe the service.
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
