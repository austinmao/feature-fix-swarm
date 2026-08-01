# Ruflo function curation — SUPERSEDED (spec 002)

Ruflo was removed as FFS's orchestrator. gsd-core (`@opengsd/gsd-core@1.9.1`,
pinned) now owns orchestration; gates.py remains the sole completion authority.
See `docs/commands.md` § "gsd-core (Orchestration)" and
`spike-results/gsd-ruflo/` for the adoption evidence.

The historical curation table (which ruflo functions FFS wired, and why) lives in
git history: `git show <pre-spec-002>:docs/ruflo-curation.md`.
