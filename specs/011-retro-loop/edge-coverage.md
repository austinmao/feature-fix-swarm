# Edge coverage — spec-011 (Step 2.5 probe over REQ-01..15)

Format: REQ · category · status · reason/AC

- REQ-01 · empty · resolved · no consent file = not granted (unit row; EDGE-003 covers symlink)
- REQ-01 · idempotency · dismissed · check-consent is a pure read; N passes identical
- REQ-01 · concurrency · dismissed · gate chain reads only; mutation lives in REQ-07's flock
- REQ-02 · empty · resolved · empty/absent digest → RETRO:no-events rc 0 (EDGE-001)
- REQ-02 · encoding · resolved · digest read utf-8 errors=replace; torn final line skipped (EDGE-002)
- REQ-02 · ordering · dismissed · payload is a set keyed by fingerprint; source order immaterial
- REQ-03 · adjacency · resolved · an event matching multiple grade rules takes the HIGHEST severity (P0>P1>P2>P3) — rule added to plan Edge resolutions
- REQ-03 · empty · resolved · zero gradable events → no-events path (EDGE-001)
- REQ-04 · encoding · dismissed · joined fields are enum/numeric post-scrub; the `|` separator cannot occur in any field value (allowlist regexes exclude it)
- REQ-04 · idempotency · resolved · same facts → same 16-hex across runs/machines (unit trio)
- REQ-05 · boundary · resolved · 500 passes / 501 rejects (EDGE-008)
- REQ-05 · encoding · resolved · percent-encoded, file://, Windows-drive representations all rejected (AC-005 bypass unit row)
- REQ-05 · empty · resolved · empty payload = reject (unit row)
- REQ-06 · adjacency · resolved · similarity exactly 0.8 → comment (≥ is inclusive, AC-006)
- REQ-06 · ordering · resolved · multiple exact-fingerprint matches → comment on the LOWEST issue number (oldest, stable) — plan Edge resolutions
- REQ-06 · concurrency · resolved · same-machine flock + pre-create re-query; cross-machine survivor accepted + merged upstream (AC-006/007)
- REQ-07 · boundary · resolved · cap=3 allows exactly 3 creates, 4th blocked; cap=0 comments-only (EDGE-009)
- REQ-07 · idempotency · resolved · re-run over filed payload → comments only (INT-002)
- REQ-07 · concurrency · resolved · INT-006 concurrent filers
- REQ-08 · idempotency · resolved · double finalize → second pass dedups to comments; tail always rc 0
- REQ-09 · idempotency · resolved · asked_at guard: interview skips when consent.json exists (re-ask only --reset/major bump)
- REQ-09 · concurrency · resolved · revoke vs in-flight filing serialized by the same flock (EDGE-012)
- REQ-10 · empty · resolved · zero source/ffs-retro issues → typed "no issues to triage" brief, rc 0 — plan Edge resolutions
- REQ-10 · ordering · resolved · equal-priority clusters order by lowest issue number (stable) — plan Edge resolutions
- REQ-11 · idempotency · resolved · label creation + apply idempotent (AC-011); re-delivered events safe
- REQ-11 · concurrency · dismissed · two near-simultaneous occurrence edits: last-writer-wins on the metadata comment is acceptable — occurrences is advisory, the comments themselves are the ground truth
- REQ-12 · idempotency · dismissed · exclusion filter is a pure predicate
- REQ-13 · precision · resolved · wall/active ratio rounded half-even to 2 decimals — plan Edge resolutions
- REQ-13 · boundary · resolved · wall < active (skew) → ratio omitted (EDGE-011)
- REQ-14 · concurrency · resolved · atomic tmp+rename writes under the flock (AC-014)
- REQ-15 · boundary · resolved · missing/Unreleased-only CHANGELOG → 0.0 (EDGE-010)

Unresolved: none.
