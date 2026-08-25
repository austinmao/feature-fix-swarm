# Effort-hygiene audit — spec-004 AC-011 / US9

Grep-driven review of `skills/*/SKILL.md` guard prose for redundant "final
verification step" instructions ahead of Opus 5 landing in the judgment
seat (`judgment` tier: `claude-opus-5` / `gpt-5.6-sol@high`). Rationale
(US9): a strong judgment-tier model does not need hand-holding prose
telling it to re-verify its own conclusion before responding — that
instruction burns turns/tokens without adding signal, and is exactly the
kind of prompt cruft that survives a model swap unnoticed.

## Scope

All 19 `skills/*/SKILL.md` files in this repo (`ls skills/`):
adopt-wip, autonomy-grant, code-uplift, continue-compact, feature,
feature-implement, feature-spec, fix, goal-wrap, plan-decompose, preflight,
review-gate, spec-decompose, spec-guide, spec-status, swarm, task-swarm,
testing-policy, verify-review.

## Method

Sequential grep passes over `skills/*/SKILL.md`, widening the net each round
(case-insensitive, extended regex):

```bash
grep -rniE "final verification|verify (again|once more|a second time)|double.?check|re-?verify|second pass to (verify|confirm)|make (absolutely )?sure (you|to) verify|one more (verification|check)|extra verification|additional verification pass" skills/*/SKILL.md
grep -rniE "before (finalizing|submitting|responding),? (verify|check|confirm|make sure)|re-?check your|verify your own|confirm your (own )?findings|triple.?check|be (extra|especially) careful|sanity.?check your|review your own (work|output|answer)|check (it|this|your work) (again|twice)" skills/*/SKILL.md
grep -rniE "final (verification|check|review|pass|step)" skills/*/SKILL.md
grep -rniE "before (you )?(respond|finish|conclude|submit|answer)|one (more|last) (time|check|pass)|make sure (you|to) (verify|check|confirm)|check (your|its) (own )?(work|answer|output)|sanity.?check" skills/*/SKILL.md
```

Then a manual read of every `judgment`-tier dispatch/guard section surfaced
by `grep -rniE "opus|judgment" skills/*/SKILL.md` (25 hits across 10 files),
to catch redundant-verification prose that the keyword passes above would
miss because it's phrased without any of the literal trigger words.

## Findings

**Zero literal matches** on all four grep passes above — no skill file
contains a "please verify your own answer again before responding"-shaped
instruction anywhere in the guard prose.

**One candidate surfaced by the manual read, adjudicated as NOT redundant
(explicit waiver):**

- `skills/review-gate/SKILL.md:513` — "### Verify-the-reviewer (v3.17.0)"
  section: after a judgment-tier reviewer returns a verdict, a SEPARATE step
  re-opens the cited files at current HEAD and classifies the verdict's
  load-bearing claims as `REAL_BLOCKER`/`STALE`/`WRONG`/etc.
  - **Waived, not fixed.** This is architecturally the opposite of the
    anti-pattern US9 targets: it is a fresh-context, cross-checking pass
    against a DIFFERENT failure mode (a reviewer reading stale code /
    inventing line numbers / misreading intent) — not the same agent being
    told to re-verify its own conclusion. `.claude/rules/common/agents.md`
    §"Model Delegation — MANDATORY" ("Producer ≠ reviewer") and this
    project's own fresh-context-verifier doctrine explicitly endorse this
    shape (same-model self-review is documented there as the anti-pattern,
    citing arxiv 2603.12123: a fresh reviewer scores materially better than
    a self-review). Removing it would remove a documented, evidence-backed
    safeguard, not a redundant one. No prose change.

## Disposition

No fixes required. One near-miss candidate explicitly waived above with
rationale. `skills/*/SKILL.md` guard prose is clean of redundant
final-verification-step instructions as of this audit — safe for the
judgment tier to move onto Opus 5 without an over-verification regression
from existing prose.

Re-run the four grep passes above whenever a new `skills/*/SKILL.md` guard
is added or a judgment-tier dispatch prompt is edited; this file is a
point-in-time audit, not a standing gate.
