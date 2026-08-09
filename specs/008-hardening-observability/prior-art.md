# Spec 008 prior art — OSS + skills search (2026-08-07)

Primary prior art: `.planning/prior-art/spec-008-hardening-observability.md`
(opus red-team output — REQ tables fed verbatim into spec.md). This file
records the OSS/skill vindication search (threshold: 200 stars).

| candidate | type | stars | applicability verdict | evidence |
|---|---|---|---|---|
| danielfm/pybreaker | Python lib | 689 | REJECT — new pip dependency violates zero-dep constraint; state-machine pattern worth reading, not vendoring | github.com/danielfm/pybreaker |
| fabfuel/circuitbreaker | Python lib | 521 | REJECT — new dependency | github.com/fabfuel/circuitbreaker |
| fluxcd/flagger | K8s operator | 5385 | REJECT — requires Kubernetes + CRDs + controller daemon; PATTERN ported (metric-breach rollback → digest binding + dry-run rehearsal) | github.com/fluxcd/flagger |
| argoproj/argo-rollouts | K8s controller | 3540 | REJECT — Kubernetes-bound | github.com/argoproj/argo-rollouts |
| protectai/rebuff | Python/TS lib | 1517 | REJECT — needs VectorDB + LLM detection calls; not applicable to static doc fencing | github.com/protectai/rebuff |
| tldrsec/prompt-injection-defenses | reference doc | 719 | APPLICABLE as read-only pattern reference (delimiter/instruction-hierarchy taxonomy) — not a library | github.com/tldrsec/prompt-injection-defenses |
| tox-dev/filelock | Python lib | 974 | REJECT — stdlib flock + existing claim_pidfile already cover it | github.com/tox-dev/filelock |
| caronc/apprise | Python lib | 17010 | REJECT — dependency + per-service plugins; overkill for a 2-mode digest script | github.com/caronc/apprise |
| healthchecks/healthchecks | Django app | 10224 | REJECT — DB + web daemon | github.com/healthchecks/healthchecks |
| waiver/audit-trail candidates | — | all <200 | no vindicated prior art at threshold | — |
| token-budget degradation candidates | — | all <200 | no vindicated prior art at threshold | — |

In-repo prior art (the decisive kind — every sub-system extends an EXISTING
seam rather than adopting external code): plan-wall.sh waiver recorder +
untrusted-data fence idiom; gsd-run.sh `claim_pidfile`; run-finalizer.sh
report patterns; `_StoreLock`/`_store_path` git-common-dir pin;
`_evidence_resolves` digest comparison; fallback-rehearsal.sh.

## Decision input

Zero-dependency bash + python-stdlib repo. Every vindicated external
candidate either drags a new runtime dependency or a daemon/K8s — all
rejected. Verdict (no judge dispatched — no candidate is both vindicated AND
adoptable, so the adjudication precondition is unmet): **build-fresh for all
seven sub-systems**, porting two PATTERNS: flagger/argo's
rollback-on-metric-breach concept into G7's digest binding + dry-run
rehearsal, and tldrsec's delimiter-defense taxonomy into G9's fence checks.
