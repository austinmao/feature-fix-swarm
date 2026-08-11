---
phase: 02-filing-consent-seams
plan: 01
status: implemented
---

Implemented the fixed-HOME consent state helper and locked filing tracer.
`retro.sh` now gates disabled and unconsented runs before collection, preserves
the Phase 1 scanner barrier, performs auth only after that barrier, and sends
only scanned private payloads into the locked coordinator. Consent mutations,
auth records, and filing share the secure lock.

Validation: Phase 1 scrub regression suite and ShellCheck pass. The Wave 0
copied-runtime Bats harness needs to copy `lib/retro_state.py` alongside the
already copied `retro.sh` and `retro_scrub.py`; this summary records that
fixture dependency without modifying the immutable acceptance suite.
