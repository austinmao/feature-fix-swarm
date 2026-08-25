# Socratic — the pinned question bank FFS slices into reviewer prompts

[m4vic/socratic](https://github.com/m4vic/socratic) is a curated bank of the
questions a senior engineer asks before and during a build: fifteen domain files
(requirements, frontend, backend, data, API, security, infra, testing,
observability, AI/LLM, mobile, product/UX, cost/performance, compliance,
team/maintenance) plus ten decision-card packs derived from named source
material. Its default mode is self-interrogation, not interviewing the user.

FFS does not run socratic as a tool. It **slices** the relevant subset of that
bank into the prompts its adversarial reviewers already receive, so a reviewer
reads the questions a domain expert would have asked about this specific change.

## Why it was adopted

`specs/005-socratic-integration/prior-art.md` records the search honestly.
Socratic sat **below** the 200-star vindication gate that spec's prior-art rule
normally requires. Four higher-starred candidates were examined and rejected as
the wrong shape — a general topic-exploration Q&A tool, a prompt-engineering
course, a customization marketplace, and a crowd-sourced prompt collection —
none of them a discrete, consumable engineering question bank. No in-repo
equivalent existed.

It was adopted anyway, after a direct read of its `SKILL.md`, question files,
and packs. The gate's actual purpose — do not take on an unvetted dependency —
was satisfied by content review plus commit pinning rather than by star count.
The recorded risk is bus-factor 1, mitigated two ways: the pin means upstream
abandonment costs nothing, and the MIT license makes fork-and-carry viable.

## How FFS uses it

Everything routes through one emitter, `scripts/gsd/socratic-slice.sh`. A spec
declares what it wants in `specs/NNN/socratic.md` frontmatter:

```yaml
---
domains: [security, testing, api]
depth: core          # core | full
packs: [threat-modeling]
---
```

`domains` is a closed 15-slug enum, `packs` a closed 10-slug enum, both mirrored
from the pin in `socratic-slice.sh:72-113`. **At most two packs are ever
emitted**; a third is skipped with a warning, and an unknown or missing pack
never consumes a cap slot.

```bash
socratic-slice.sh <spec_dir|socratic.md> [--mode plan|arm|verify]
socratic-slice.sh --validate <spec_dir|socratic.md>
socratic-slice.sh --record-pendings <socratic.md> <run-id>
```

Four seams consume it:

| Seam | Mode | Effect |
|---|---|---|
| `/feature-spec` Step 1.5 | `--mode arm`, then `--validate` | Authors and validates the spec's `socratic.md`. Validation is **fail-closed** — exit 3 stops the step. |
| `/feature-spec` grants step | `--record-pendings` | Writes unresolved socratic questions into the ledger as PENDING. Never as grants. |
| `plan-wall.sh` | `--mode arm` | Folds the slice into the adversarial reviewer's prompt. Memoized once per process; the socratic sha is folded into the plan cache key so editing `socratic.md` invalidates the zero-dispatch fast path. |
| `plan-decompose` Step 3 and `/review-gate`'s honest verifier | `--mode arm` / `--mode verify` | Verify-mode extracts the spec's `## Verification` items; a violated item enters the merged findings at **HIGH**. |

### Containment

The slice is untrusted text folded into a prompt, and it is treated that way.
It is wrapped in `SOCRATIC_DATA_START`/`SOCRATIC_DATA_END` with fixed lead
**and** trail text on both sides, so the slice is never the last bytes a
reviewer reads. Counterfeit delimiters — in the plan or in the socratic content
— are rewritten to `SOCRATIC_DATA_ESCAPED` rather than dropped, so neither can
fake an early fence close. Same mechanism as every other fenced payload; see
[Cross-session messaging](cross-session-messaging.md#trust-boundary).

### Failure posture

Split on purpose:

- **Authoring is fail-closed.** `--validate` rejects an unknown domain, pack, or
  depth and reports every offending value. It needs no vendor tree and ignores
  `SOCRATIC=off`.
- **Consumption is fail-soft.** The arming signal is stdout **emptiness**, never
  an exit code. No `socratic.md`, no vendor tree, or `SOCRATIC=off` all produce
  an empty slice and a prompt byte-identical to its unarmed form.

### Controls

| Variable | Effect |
|---|---|
| `SOCRATIC=off` | Emission kill switch, checked before any filesystem access. Empty stdout, exit 0, one status line. Ignored by `--validate`. |
| `FFS_SOCRATIC_DIR` | Authoritative vendor-tree override — no fall-through to the ladder. |
| `FFS_SKIP_SOCRATIC=1` | Skip installation entirely. |
| `PLAN_WALL=off` | The wall's waiver path never shells the helper at all. |

Vendor-tree resolution ladder: `$REPO/.agents/skills/socratic` →
`$REPO/.claude/skills/socratic` → `~/.agents/skills/socratic` →
`~/.claude/skills/socratic`.

## Pin and updates

FFS vendors a **pin, not a tree**: `vendor/socratic/pin.json` names the
repository and commit `8c7e1fdda5ff6f7755d4855907ddf0022a755493`, and
`scripts/install-socratic.sh` clones at that commit into
`.agents/skills/socratic` as a managed external skill. No submodule, no copied
question files in this repo. The same convention `prompt-master` uses, including
the optional bare-filename patch and the upstream-PR channel — no patch is
pinned at this commit.

The bump runbook is in [Dependencies and integrations](dependencies.md).

## What was measured, and what was not

`evals/socratic-ab.md` recorded the **prompt** delta: +7,244 bytes (+15.6%) of
reviewer prompt when the slice arms, against the real pinned question bank.

It did **not** measure a findings delta. Both captures used a stub reviewer
returning an empty findings array, so no conclusion about whether an armed
reviewer finds different things — and therefore no conclusion about un-arming
any seam — may be drawn from that document.

## Known gaps

**Grades are not modelled.** Upstream v1.2.0 added a `grades/` surface —
`mvp` → `production` → `enterprise` readiness gates, cumulative through a
`Supersedes:` chain, where the grade's gate replaces the generic stopping
condition and names which domains go full and which packs become mandatory.
FFS's frontmatter reads `domains`, `depth`, and `packs` only; a `grades:` key
is not read today. The surface is inert unless a grade is named, so the pin bump
that brought it in was additive.

**CI cannot catch enum drift.** Every socratic bats suite runs against
`make_vendor_tree` fixtures, so no suite exercises the real pin.
`tests/bats/socratic-enum-drift.bats` is the one that would — it extracts the
two enums from the production script rather than hardcoding a copy, and asserts
each maps 1:1 to a real vendor tree — but it is opt-in via `FFS_SOCRATIC_DIR`
and skips wherever no tree resolves, including CI. Run it deliberately after a
pin bump.

The failure mode this leaves is asymmetric: a vanished upstream domain degrades
silently (warn, thinner slice, still arms), while a genuinely-added upstream
domain is rejected fail-closed at `--validate` against an otherwise correct
spec.

## Related

- [Dependencies and integrations](dependencies.md)
- [Model tiers](model-tiers.md)
- [Commands](commands.md)
