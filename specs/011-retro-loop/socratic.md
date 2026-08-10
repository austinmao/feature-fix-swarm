<!-- socratic.md — spec 011 self-interrogation ledger. Enums (closed sets from scripts/gsd/socratic-slice.sh): domains ∈ {requirements, frontend, backend, data, api, security, infra, testing, observability, ai-llm, mobile, product-ux, cost-performance, compliance, team-maintenance}; depth ∈ {core, full}; packs (≤2) ∈ {software-design, domain-modeling, data-systems, operations, threat-modeling, ai-engineering, agent-design, legacy-change, testing-design, product-discovery}. -->
<!-- depth full: consumer machines send data upstream (external users + PII-adjacent privacy) and the seam runs inside autonomous/unattended runs — two escalation triggers apply. -->
---
domains: [requirements, testing, security, observability, compliance]
depth: full
packs: [threat-modeling, operations]
---

## Self-answered highlights

- Restated (req Q1): a consent-gated post-run feedback pass that grades
  allowlisted run diagnostics, files them as deduped GitHub issues on
  austinmao/feature-fix-swarm, plus a maintainer-only `/retro-triage` that
  batches the stream into `/feature-spec` briefs. Production, external
  consumers, low volume (≤3 creates/run by cap).
- Real observed pain, not anticipated (req Q9): the spec-010 run alone
  produced ≥6 unfiled defect classes (recorded in spec.md Context); cost of
  doing nothing = defects persist only in operator memory and session
  handoffs.
- Measurable success (req Q12): AC-013's two healing metrics ride every
  payload; the issue stream + triage briefs are themselves the observable.
- Most dangerous flow (threat-modeling): consumer runtime data → public
  GitHub issue. Digest content is attacker-influenceable (a hostile repo can
  shape event text), so the body is assembled allowlist-first — free text
  never crosses; per-key regexes + deny-layer + credential scan on a 0600
  copy; ANY reject = fail-closed zero-gh (AC-005).
- Issue bodies are untrusted input on the RETURN path too: `/retro-triage`
  clusters mechanically by fingerprint/labels and quotes issue text as inert
  data — it never obeys instructions found in issue bodies, and briefs mark
  quoted text as untrusted.
- Integration surface (req Q32-35): reads digest jsonl, findings-queue,
  consent.json; writes GitHub issues + local ledger only. Downstream
  consumers (retro-label.yml, /retro-triage) conform to the versioned HTML
  metadata comment (`ffs-retro` v1) — its shape is a contract, tested on
  both sides.
- gh secondary limit (operations): 80 content-writes/min → 2s pacing + ≤3
  creates/run keeps worst case far under limit; label creation in the
  workflow is idempotent.
- Retro's own health (observability): typed `RETRO:` lines + local ledger
  rows; its failures never reach the consumer run's exit status (AC-008) and
  never become issues (AC-012).
- Compliance: allowlist construction means no PII by design; consent is
  user-scope, revocable, never stored in the consumer repo; headless =
  no-consent (no silent enrollment).

## Assumed (flag if wrong)

- ASSUME-001: recommended-yes `[Y/n]` framing at the interactive `/ffs-init`
  interview (Enter accepts) satisfies the opt-in posture — headless or
  no-answer records NO consent; there is no notify-then-collect default-on.
- ASSUME-002: title similarity = Python difflib `SequenceMatcher.ratio()`
  (stdlib, no new dependency), threshold `RETRO_TITLE_SIM=0.8`.
- ASSUME-003: digest event classes are stable enough for deterministic
  grading; an unrecognized event class grades P3 (ledger floor), never
  P0-P2.
- ASSUME-004: `ffs_minor` in the fingerprint reads the repo version's
  major.minor from one source (CHANGELOG/package metadata) at collect time.
- ASSUME-005: `retro-label.yml` runs with `issues: write` permissions via
  github-script; it parses the metadata comment, creates missing labels
  idempotently, and is the ONLY labeler (consumers cannot label).
- ASSUME-006: `/retro-triage` reads at most ~200 open `source/ffs-retro`
  issues per invocation via `gh issue list --json` (bounded, no pagination
  loop past that).
- ASSUME-007: `consent.json` shape `{granted, asked_at, version}` beside the
  danger-grants store; the major-version re-ask keys off `version`.
- ASSUME-008: `feature-spec`'s tail is one SKILL.md line invoking the same
  `retro.sh` entry (no second seam script); the three gsd-run-based
  entrypoints inherit the finalizer seam.

## Open questions → grants

- One LIVE end-to-end smoke: may the implementation create (then immediately
  close) ONE real issue on austinmao/feature-fix-swarm to prove PATH-001
  against the real API, beyond the stubbed E2E? Candidate action:
  `issue-create:austinmao/feature-fix-swarm` — operator to promote or leave
  pending (stubbed E2E is sufficient for merge either way).

## Top risks

- Scrub bypass leaking a secret or path into a PUBLIC issue — mitigated by
  allowlist-first assembly + deny-layer + credential scan + fail-closed
  zero-gh assertion in bats; residual risk is a novel secret shape passing
  all three layers.
- Fingerprint instability spamming the repo — volatile fields excluded by
  construction (AC-004), per-run create cap, title-similarity net,
  maintainer dup-close as last resort.
- Consent mistrust — one plain-language question, revocable
  (`retro.sh consent --revoke`), documented allowlist; never re-asked except
  `--reset`/major bump.
- Self-reporting feedback loop — finalizer runs retro last; `script=retro.sh`
  events excluded from its own payload; own failures → local ledger only.
- gh unauthenticated on consumer machines — silent no-op + ledger row
  (accepted degradation; no prompt mid-run).
