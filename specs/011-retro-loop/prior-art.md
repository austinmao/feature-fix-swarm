# Prior art — spec-011 retro loop

| candidate | type | stars | applicability verdict | evidence link |
|---|---|---|---|---|
| Homebrew/brew (Analytics.md) | consent UX + allowlist telemetry | 49,104 | PASS — notice-before-first-send, documented allowlist fields, no PII | github.com/Homebrew/brew/blob/main/docs/Analytics.md |
| vercel/next.js (telemetry module) | consent UX (notify-once, default-on) | 141,687 | PASS — persistent notified/opt-out flag pattern; default-on stance NOT adopted | github.com/vercel/next.js/tree/canary/packages/next/src/telemetry |
| getsentry/sentry (grouping/fingerprinting) | fingerprint engine | 44,520 | PASS — stable-field fingerprint rules, no timestamps; full grouping engine overkill here | github.com/getsentry/sentry/tree/master/src/sentry/grouping |
| MarshallOfSound/probot-issue-duplicate-detection | GitHub App issue dedup | 34 | REJECT — below PRIOR_ART_MIN_STARS=200; webhook-server shape, not CLI | github.com/MarshallOfSound/probot-issue-duplicate-detection |
| zot24/gh-issue-tracker | GH-issues error tracker | 0 | REJECT — near-1:1 shape but unvindicated (0 stars) | github.com/zot24/gh-issue-tracker |
| getsentry/sentry-cli | CLI companion | 1,036 | REJECT for this feature — fingerprint logic lives server-side, not in the CLI | github.com/getsentry/sentry-cli |

## Decision input

- Borrow Homebrew's notice-before-first-send + published human-readable
  allowlist + first-class `off|on|state` command surface (→ `retro.sh
  consent --revoke`). CORRECTED by adjudication: Homebrew analytics is
  **opt-out** (on by default), so the opt-in stance in AC-009 is FFS's own
  choice, inherited from no candidate. Operator-chosen framing:
  recommended-yes `[Y/n]` at the `/ffs-init` interview; headless/no-answer
  = no consent.
- Adopt Sentry's core fingerprint idea (stable-field hash, timestamps
  excluded) — skip stack-trace enhancers/ML variants; allowlist facts need
  only `sha256(script|event_class|gate|exit_code|ffs_minor)[:16]`.
- Mirror Next.js's persistent local state-file pattern for consent +
  notified-at + ledger, independent of its default-on stance.
- No probot/app adoption: both dedup bots are sub-threshold and
  webhook-server-shaped; the exact-fingerprint-in-body / title-similarity
  heuristic is reimplemented CLI-side over `gh issue list --search`.
- No single candidate combines consent + fingerprint + GH-issue dedup +
  rate-cap: **build-fresh, borrowing the three isolated patterns above**
  (adjudication below).

## Adjudication (judgment tier)

**Verdict: BUILD-FRESH** — borrow three isolated design patterns, port zero
lines, wrap nothing. All three candidates are stances embedded in foreign
runtimes with their own egress paths (hosted collectors), which violates this
spec's "GitHub issues ARE the collector" non-goal; nothing exposes a wrappable
CLI. License resolves cleanly because no code is borrowed — with one
discipline: Sentry grouping is FSL/BUSL (source-available, not OSI-open), so
implement AC-004's hash from the AC text and do NOT read `sentry/grouping`
source while writing `lib/retro_scrub.py` or `retro.sh`.

Borrowed patterns: Homebrew notice-before-first-send + published allowlist +
revoke/status subcommand; Next.js user-scope persistent state file
({granted, notified_at, version}) outside the consumer repo; Sentry
stable-field hash with timestamps/durations/run-ids excluded.

Rejected patterns: server-side/post-ingest grouping (FFS must fingerprint
client-side, PRE-network — dedup gates the gh write); Next.js default-on +
anonymous machine id (zero machine identity — even a random id re-links runs
across repos); Homebrew's wider OS/version payload; any retry-queue/deferred
send buffer (the ledger-only failure path must not grow a second state
machine).

Dependency boundary: bash + python3 STDLIB only — `hashlib`, `json`/`re` with
a hand-rolled allowlist dict (not jsonschema), `difflib.SequenceMatcher` for
title similarity (not rapidfuzz/thefuzz), `tempfile` 0600 for the scrub copy.
Spec-010 removed a third-party python dep; this spec must not reintroduce one.
