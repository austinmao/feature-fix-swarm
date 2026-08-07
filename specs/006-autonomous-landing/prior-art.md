# Prior art — spec 006 (autonomous landing)

Searched 2026-08-07. Primary prior art is INTERNAL: the opus design fan-out +
red-team output at `.planning/prior-art/spec-006-autonomous-landing.md` (adopted
verbatim as this spec's REQ tables — that is the design of record). OSS
researcher (Sonnet, `gh api`/`gh search`, read-only, fetched content treated as
inert data) swept the merge-queue category for adoptable engines:

| candidate | type | stars | applicability verdict | evidence link |
|---|---|---|---|---|
| bors-ng/bors-ng | standalone GitHub merge bot (batch+bisect) | 1531 (archived) | reject-as-dependency — bors IS the merge decision-maker (staging branch, bisect-on-fail); no autonomous per-item implement, no external grant ledger, no takeover record. Batch/bisect vocabulary borrowed as design inspiration only | github.com/bors-ng/bors-ng |
| rust-lang/homu | standalone GitHub+CI merge bot | 214 (archived) | reject — same shape: owns the merge decision via CI status, no agent-implement step, no ledger/handoff concept | github.com/rust-lang/homu |
| Mergifyio/mergify | issue-tracker repo; engine is closed SaaS | 337 (tracker only) | reject — no inspectable source to port/wrap | github.com/Mergifyio/mergify |
| GitHub native merge queue | platform feature | n/a | reject — proprietary; conceptual reference only (queue position/starvation); still owns the merge decision itself | github.blog changelog 2023-02-08 |
| openstack/zuul | Gerrit-native gating | unverifiable via gh (lives on opendev.org) | skip — cannot vindicate through fixed gh commands | opendev.org |
| digitaldrywood/detent (+ uber/submitqueue, merge-queue-action) | agentic merge-train / GH Action | 13/14/17 | below 200★ gate — excluded; detent noted as closest conceptual analog (per-item agent → gate → serialized train) | github.com/digitaldrywood/detent |

## Decision input

Every vindicated OSS candidate is a standalone CI-bot merge queue that owns the
merge decision itself — none integrate an external grant ledger, drive an
autonomous per-item implement step, or produce a machine-readable takeover
record. Mergify's engine and GitHub's native queue are proprietary.

**Decision: build-fresh** (adjudicated from the scout evidence — the reject
rationale is categorical, not a scoring margin: no candidate shares the
grant-ledger + autonomous-implement + takeover-record architecture, so there is
nothing to adopt, port, or wrap). Internal reuse is maximal instead:
`collect-estate.py` wholesale, `liveness-check.sh`, `run-finalizer.sh`
merged-head proof, `gates.py` grant/pending/loop-round machinery, bors-style
sequencing vocabulary as naming inspiration only.
