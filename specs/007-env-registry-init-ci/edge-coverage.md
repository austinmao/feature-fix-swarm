# Edge coverage — spec 007 (8-category probe over REQ-101..403)

Format: REQ · category · status · reason/AC.

| REQ | category | status | reason / AC |
|---|---|---|---|
| REQ-101 registry | empty | resolved | no prod surfaces → surfaces block omitted, soft mode unchanged; bare repo → local-only registry (REQ-202) |
| REQ-101 registry | encoding | resolved | tabs/CRLF/dup keys in surfaces → REJECT (REQ-103); prose fields never machine-parsed |
| REQ-102 resolution | precedence | resolved | env var > environments.yaml > parity-manifest; explicit --manifest wins; empty --manifest REJECTED (EDGE-011) |
| REQ-102 resolution | io | resolved | set-but-broken env var → REJECTED prod (EDGE-010); unparseable resolved file → REJECTED both modes (EDGE-005) |
| REQ-102 trust | concurrency/identity | resolved | worktree-local uncommitted registry → governs nothing (implicit lookups read main HEAD only → absent + advisory; caller-supplied paths REJECTED — main-checkout anchor, EDGE-012, wall round-3) |
| REQ-102 matching | encoding | resolved | casefold surface both sides; sentinel casefold+strip (EDGE-014) |
| REQ-103 parser | ordering | resolved | surfaces-not-last parses; single-entry — later surface-shaped lines inert (EDGE-013) |
| REQ-103 parser | adjacency | resolved | nested surfaces: at indent>0 ignored (EDGE-001) |
| REQ-104 reasons | empty | resolved | every refusal carries remedy text; CLI + pending record both assert |
| REQ-201 modes | idempotency | resolved | --update preserves operator fields; --yes over existing registry does not regenerate; declines keyed to evidence re-propose on new evidence |
| REQ-201 answers | empty | resolved | missing required key → nonzero naming key + expected shape, nothing written (EDGE-008) |
| REQ-202 detect | io | resolved | unreadable .env → evidence: unreadable, no crash (EDGE-003); gh unauth → probe skipped (EDGE-004) |
| REQ-202a scan | precision | resolved | thresholds named (hex≥32, b64≥40); uses:@/sha256: whitelist; output contract bans matched bytes |
| REQ-202a scan | boundary | resolved | leak fixture dir path-exempted from AC-011 grep gate |
| REQ-203 apply | io | resolved | atomic all-or-nothing; sole writer |
| REQ-301 deploy | ordering | resolved | deploy→smoke(artifact-bound)→promote; smoke-fail writes no promotion |
| REQ-301 rollback | idempotency | resolved | rollback emitted never executed; job fails |
| REQ-302 render | adjacency | resolved | collision → .github/ffs-proposals/ (never executable); byte-identical existing → up-to-date no proposal (EDGE-007) |
| REQ-302 render | empty | resolved | empty repo → exactly 5 ffs-*.yml + dependabot entry when absent |
| REQ-303 tiers | ordering | resolved | repeated tier rows run in declaration order (EDGE-006) |
| REQ-303 tiers | empty | resolved | unknown tier → 2; missing registry → 3 + reason; suite matching no covers glob → check fails naming suite |
| REQ-401 seams | idempotency | resolved | promote command EMITTED not run (INT-002); preflight seeding additive-never-authoritative (INT-003) |
| REQ-402 compat | boundary | resolved | absent registry non-prod → exit codes pinned unchanged (AC-010) |
| REQ-403 hermetic | io | resolved | zero network in tests; fixtures/stubs only (AC-011) |

Unresolved: none.
