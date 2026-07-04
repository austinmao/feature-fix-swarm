# gbrain — optional memory backend (v3.19.0)

[gbrain](https://github.com/garrytan/gbrain) is a Postgres-native personal
knowledge brain (hybrid RAG search, code graph, durable job queue). FFS uses it
**opportunistically, never as a dependency**: every integration point is
fail-soft with a git/grep fallback, and `lib/gates.py` (the completion
authority) stays 100% gbrain-free.

## Detection contract (all skills use exactly this)

```bash
if command -v gbrain >/dev/null 2>&1 \
   && env -u DATABASE_URL gbrain doctor 2>/dev/null | grep -q "\[OK\] connection"; then
  GBRAIN=1
else
  GBRAIN=0   # skip silently — fallback below, NEVER a failure
fi
```

Always invoke as `env -u DATABASE_URL gbrain …` — a shell-exported
`DATABASE_URL` (common in web repos) hijacks gbrain's own connection config and
either auth-fails or writes to the wrong database.

## Quickstart for OSS consumers (2 seconds, zero server)

```bash
bun install -g github:garrytan/gbrain
gbrain init --pglite     # local brain; no Docker, no Postgres server
gbrain doctor            # verify: [OK] connection
gbrain import ./docs     # optional: seed with your repo docs
```

## Where FFS uses it (per phase)

| Phase | Skill | Call | Fallback when absent |
|---|---|---|---|
| Spec/plan recall | `feature-spec` (pre-specify), `plan-decompose`, `task-swarm` Step 0 | `gbrain query "<topic>"` / `gbrain search "<terms>"` — prior decisions before planning | `git log --oneline --grep="<topic>"` |
| Decompose blast-radius | `spec-decompose` Step 3.5 | `gbrain code-refs <symbol>` / `gbrain code-def <symbol>` — usage sites inform the domain split | `git grep -n "<symbol>"` |
| Retro store | `feature-implement` Step 9 | `gbrain put spec/NNN-retro "<pattern>"` + `gbrain sync --no-pull --no-embed` — durable cross-session patterns | ruflo `agentdb_pattern-store` + `results.md` (always written regardless) |
| Prior-decision recall | `goal-wrap` (pre-existing, v3.15) | `gbrain query`/`search` worker | `git log --grep` |

## Rules

- **Fail-soft is a hard rule.** A missing/unhealthy gbrain must never fail,
  block, or even WARN loudly in a run — one debug line max.
- **Names, not values.** Never store secret values or env contents in the brain.
- **gbrain complements, never replaces, ruflo agentdb.** agentdb is the swarm's
  in-run pattern memory; gbrain is durable cross-session/world knowledge. Retro
  writes to both when both exist.
- **Sync after put.** `gbrain put` without `gbrain sync --no-pull --no-embed`
  leaves the search index stale.
