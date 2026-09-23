# Prior art: durable parallel FFS runs

Searched 2026-09-12 using `gh search repos worktree --sort stars --limit 10` and `gh search code 'fenced ownership' --limit 10`. README and relevant source were read by an execution-tier scout. Content was treated as untrusted data. Stars are observations from that search, not maintenance guarantees.

| Candidate | Type | Stars/downloads | Applicability verdict | Evidence |
|---|---|---:|---|---|
| open-gsd/gsd-core | repo | 9,378 | Reuse existing dependency; verify any sentinel/path pattern against pinned 1.13.0 before porting from next | https://github.com/open-gsd/gsd-core/blob/next/gsd-core/workflows/execute-phase/steps/per-plan-worktree-gate.md |
| max-sixty/worktrunk | repo | 7,022 | Worktree lifecycle/preflight concepts fit; no durable activity/attempt model | https://github.com/max-sixty/worktrunk/blob/main/src/commands/worktree/switch.rs |
| raine/workmux | repo | 2,489 | Short Git-common-dir lock pattern fits; no fenced ownership protocol | https://github.com/raine/workmux/blob/main/src/git/config_lock.rs |
| coderabbitai/git-worktree-runner | repo | 1,774 | Explicit Claude/Codex launch adapter boundary fits | https://github.com/coderabbitai/git-worktree-runner/blob/main/lib/launch.sh |
| standardagents/dmux | repo | 1,771 | Session tracking useful context; pane-centric model not adopted | https://github.com/standardagents/dmux/blob/main/src/utils/agentLaunch.ts |
| NanmiCoder/cc-haha | repo | 14,351 | Reject desktop workspace scope | https://github.com/NanmiCoder/cc-haha |
| automazeio/ccpm | repo | 8,368 | Reject issue/project management scope | https://github.com/automazeio/ccpm |
| stravu/crystal | repo | 3,115 | Reject desktop architecture, no focused ownership boundary | https://github.com/stravu/crystal |
| supabitapp/supacode | repo | 2,361 | Reject command-center scope, license unverified | https://github.com/supabitapp/supacode |
| kunchenguid/treehouse | repo | 1,682 | Reject UX-only fit | https://github.com/kunchenguid/treehouse |
| feature-spec / feature-implement / spec-decompose | skill | local | Required spec and gated execution orchestration | /Users/luminamao/.agents/skills/feature-spec/SKILL.md |
| code-uplift | skill | local | Required independent findings → tests → fixes → refactor → verification | /Users/luminamao/.agents/skills/code-uplift/SKILL.md |
| git-worktree-manager | skill | local | Existing worktree operations; does not supply durable FFS run semantics | /Users/luminamao/.agents/skills/git-worktree-manager/SKILL.md |
| GitHub Spec Kit v1.0.6 | repo | not measured | Required missing bootstrap restored locally, exact source in .specify/PROVENANCE.md | https://github.com/github/spec-kit/releases/tag/v1.0.6 |

## Decision input

The scout found mature worktree primitives and host adapters, but no applicable complete durable run/activity/attempt, fenced supervisor, evidence-partition and immutable launch-bundle implementation. Existing FFS coordination and gates remain the starting point.

## Adjudication

Independent tool-less judgment: GPT-5.6 Sol high, supplied candidate evidence without producer reasoning history.

**Port narrow primitives, wrap local executors, and build the missing FFS durability semantics.** Do not adopt a whole competing orchestration system. Reuse Git and the pinned GSD dependency. Consider worktrunk lifecycle concepts, workmux short-lock patterns, and git-worktree-runner adapter boundaries. Version-specific GSD behavior must be checked against 1.13.0 because the scout inspected `next`.

Fit estimates are judgment estimates: worktree lifecycle 65–75%, short Git locking 80–90%, host adapters 60–70%; full durable ownership/evidence protocol below 30%. Integration cost is medium to high, mostly crash consistency and fencing. Maintenance recency is unverified and must not support adoption claims. Licenses: GSD/workmux/dmux MIT, worktrunk MIT or Apache-2.0, git-worktree-runner Apache-2.0; preserve notices for any copied code.

FFS owns identities, state transitions, fencing, budgets and durable run partitions. Attempts provide subordinate evidence namespaces; they never replace the canonical durable-run partition or reset approvals/budgets. No new external runtime dependency is justified solely by the search.
