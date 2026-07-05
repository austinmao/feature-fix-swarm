# open-gsd/gsd-core Research Report

**Date:** 2026-07-05
**Confidence:** High for release-notes/issue-tracker facts (primary source, gh CLI + GitHub search API); Medium for "does 1.6.1 actually behave as we assume" claims (inferred from maintainer triage comments, not independently reproduced against our pinned build).
**Repo:** stars 5932 / forks 374 / open issues 76 (67 `type:issue` open, 968 closed) / npm `@opengsd/gsd-core`, pinned 1.6.1.

## Executive Summary

gsd-core is a single-maintainer-dominated project (trek-e: 2374 commits vs. #2 glittercowboy at 945, ~2.5:1) shipping extremely fast (1.5.0→1.7.0-rc.2 in ~3 weeks) with same-day AI-assisted issue triage. Our 1.6.1 pin sits right before a maintainer-acknowledged architectural gap closes in 1.7.0: a maintainer comment on #2004 states plainly "there is no generic, data-driven gate evaluator in 1.6.1 — all gate enforcement is hardcoded per built-in capability," meaning third-party `test_command`/`code_review_command`-style command-exec gates are not natively supported at our pin. STATE.md progress-counter unreliability is a known, actively-being-fixed upstream defect class (ADR-1769 "STATE.md Transition Module," 8+ related closed issues across 1.6.0–1.7.0-rc.2) with a new instance (#2012, #2022) still open as of today.

## Release Timeline (1.5.0 → 1.7.0-rc.2)

| Version | Date | Headline theme |
|---|---|---|
| v1.5.0 | 2026-06-17 | Capability Registry pilot, Loop Host Contract generation, federated config merge, Kimi CLI runtime |
| v1.6.0-rc.1..rc.3 | 2026-06-20–24 | Capability manifest v2 + registry overlay + trust gate/upgrade (ADR-1244 Phases 1-5), capability mgmt CLI |
| **v1.6.1 (our pin)** | 2026-07-01 | Hotfix only: `fix(#1847) add Claude Sonnet 5 + curated 1.6.1 hotfix fixes` |
| v1.7.0-rc.1 | 2026-06-30 | ADR-1239 host-integration interface Phase A/B begins (portability, write-confinement, AST lint rules) |
| v1.7.0-rc.2 | 2026-07-03 | ADR-1239 Phases A–6 complete (OpenCode/VS Code/pi host bindings, capability-exchange handshake), ADR-1769 STATE.md Transition Module Phases 0–7, Claude Sonnet 5 model support, `honest verifier` abstain-on-non-inferable, `cmdStateRebuild` CLI |

## Per-Category Findings

**(a) STATE.md / progress / percent bugs** — matches our known local finding; upstream confirms it as a recurring bug class, not fixed once but repeatedly re-discovered:
- [#1514 "retired/folded phase counted in progress.total_phases denominator → milestone stuck below 100%"](https://github.com/open-gsd/gsd-core/issues/1514) (closed)
- [#1446 "shouldPreserveExistingProgress ratchet cements a stale-high total_phases"](https://github.com/open-gsd/gsd-core/issues/1446) (closed)
- [#1264 "state patch silently resets curated progress.* counters when patching an unrelated field"](https://github.com/open-gsd/gsd-core/issues/1264) (closed)
- [#274 "phase.complete leaves completed_phases/percent stale when progress-table rows have name-bearing headers"](https://github.com/open-gsd/gsd-core/issues/274) (closed)
- [#1580 "999 backlog sentinel blocks milestone complete + mis-routes roadmap analyze next_phase"](https://github.com/open-gsd/gsd-core/issues/1580) (closed, fixed in 1.7.0-rc.2)
- [#1836/#1838/#1839 milestone-op disk-dir scan blind to `project_code` prefixes / counts backlog `999.x` phases](https://github.com/open-gsd/gsd-core/issues/1836) (closed, 1.7.0-rc.2)
- **[#2012 "phase.complete silently skips ## Progress rollup row when an earlier phase-numbered table precedes it; roadmap_updated masks the failure"](https://github.com/open-gsd/gsd-core/issues/2012) — OPEN, filed today (2026-07-04), reproduced against `next` HEAD.**
- **[#2022 "roadmap update-plan-progress checks phase-level checkbox with no verification gate — freezes premature completion date"](https://github.com/open-gsd/gsd-core/issues/2022) — OPEN, filed 2026-07-05, maintainer-triaged same day.**

**(b) test_command / code_review_command** — no direct hits on the config keys by name; closest relevant thread is (c)/gate findings below plus [#1296/#1216 "Config audit: settings docs/prompts disagree with consumers (units, types, dead keys, unenforced)"](https://github.com/open-gsd/gsd-core/issues/1216) (closed) — config-schema drift between docs and actual consumers was a real, acknowledged class of bug.

**(c) model_overrides / dynamic_routing** —
- [#1688 "warn (or auto-rebake) when model_overrides changes without on static-frontmatter runtimes"](https://github.com/open-gsd/gsd-core/issues/1688) (closed, shipped 1.7.0-rc.2) — confirms model_overrides silently stale-bakes on static-frontmatter runtimes was a real defect, only fixed in 1.7.0.
- [#1650 "model profile not working in OpenCode"](https://github.com/open-gsd/gsd-core/issues/1650) (closed) — runtime-specific model routing gaps existed.
- [#1133 "Fable 5 is never used in 1.4.4"](https://github.com/open-gsd/gsd-core/issues/1133) (closed) — historical pattern of model-catalog lag; Sonnet 5 support was likewise only added in the 1.6.1 hotfix / 1.7.0 forward-port (#1847, #1851, #1853).

**(d) capability / consent bugs since 1.6.0** — the most consequential finding for our pin:
- **[#2004 "Third-party command-based blocking gates don't work end-to-end on 1.6.1"](https://github.com/open-gsd/gsd-core/issues/2004) (closed 2026-07-04)** — maintainer investigation quote: *"there is no generic, data-driven gate evaluator in 1.6.1. All gate enforcement is hardcoded per built-in capability."* Split into 4 sub-issues; sub-issue on version-detection already fixed, remainder split out.
- **[#2009 "Load-failed capability injects blocking synthetic gates at every declared point — no fail-open, blocks unrelated phases project-wide"](https://github.com/open-gsd/gsd-core/issues/2009) — OPEN**, split from #2004, verified against 1.6.1 AND `next` (post-1.7.0-rc.2) — i.e. not fixed by 1.7.0.
- [#1167 "declared capability gates never fire — ship:pre (security) and execute:wave:post (ui_safety)"](https://github.com/open-gsd/gsd-core/issues/1167) (closed)
- [#1858 "Flat commands/gsd-*.md install layout not detected by _resolveManifest → all skill-bearing capabilities lost"](https://github.com/open-gsd/gsd-core/issues/1858) — OPEN.

**(e) wave parallelism / worktree** — mostly resolved before our pin:
- [#1297 "Parallel wave dispatch silently degrades to sequential — gsd-executor never self-reports it"](https://github.com/open-gsd/gsd-core/issues/1297) (closed)
- [#1369 "execute-phase worktree agents fork from stale base after wave merge"](https://github.com/open-gsd/gsd-core/issues/1369) (closed)
- [#1689 "Per-plan executor routing: native agent_hint: or execute:wave:pre render seam"](https://github.com/open-gsd/gsd-core/issues/1689) — OPEN feature request: no supported way to route a specific plan to a specialist executor subagent without hand-editing core files that `/gsd-update` overwrites.

**(f) context overflow / prompt too long / MCP** — no hits for those exact terms, but a related, currently-open, high-signal defect: [#2020 "gsd-executor.md references a nonexistent sdk/src/query/QUERY-HANDLERS.md path → some runtimes shell out to `find /` to resolve it, leaking orphaned find.exe processes (4M+ handles, 14+ hrs) on Windows"](https://github.com/open-gsd/gsd-core/issues/2020) — OPEN, filed today, tagged `upstream-bug` (not GSD-fixable directly; fix = remove the dead doc reference). Also [#2017 "context7 tool grants never match plugin-marketplace installs → 8 agents silently lose doc lookup, falls back to WebSearch"](https://github.com/open-gsd/gsd-core/issues/2017) — OPEN, MCP-adjacent.

**(g) 1.7.0 breaking changes** — see watchlist below.

## 1.7.0 Breaking-Changes Watchlist (bears on our 1.6.1 pin)

- **ADR-1239 host-integration interface**: entire install/write path refactored (`copyWithPathReplacement`, `installCodexConfig`, `destSubpath` write-confinement, `getRuntimeLabel`/`getGlobalConfigHomeFragment` collapses). Config-dir resolution changed shape; re-verify any code that reads gsd-core's install layout directly.
- **ADR-1769 STATE.md Transition Module**: `beginPhase`/`advancePlan`/`completePhase`/`plannedPhase`/`milestoneSwitch`/`milestoneComplete`/`patch`/`sync`/`prune`/`update` all migrated onto a new substrate across 7 phases — if we have any code parsing STATE.md directly (vs. calling `gsd-tools`), expect field/shape drift.
- **New `gsd capability` CLI verbs** (`install/update/remove/list/disable/enable/outdated`) shipped 1.6.0 — if not yet adopted, worth adopting once we move off 1.6.1 since 1.7.0 hardens the capability trust/gate model further.
- **`cmdStateRebuild`** (new in 1.7.0-rc.2, #1826/#1827) — a rebuild-from-derivable-state CLI; potentially a mitigation for our local STATE.md-counter-unreliable finding once we upgrade.
- AST portability lint rules (G1–G6, no-path-literal-in-assert, no-posix-mode-bit-assert, no-unguarded-nonportable-exec) are new gates in CI — irrelevant to runtime behavior but could affect any fork/vendoring workflow.

## Community Health

| Metric | Value |
|---|---|
| Open issues (type:issue) | 67 |
| Closed issues (type:issue) | 968 |
| Closed:Open ratio | ~14:1 (healthy velocity) |
| Stars / Forks / Watchers | 5932 / 374 / 17 |
| Top contributor share | trek-e 2374 commits (dominant single maintainer); #2 glittercowboy 945; #3 Tibsfox 127 — **bus factor risk: effectively one maintainer for triage + most fixes** |
| Maintainer response time (5 most recent issues, 2026-07-04/05) | Same-day AI-assisted triage comment on all 5 sampled issues (#2022, #2020, #2019, #2018, #2017) — typically within ~4-8 hours of filing |

## Confidence Assessment

- Release-note contents, issue numbers/titles/states, contributor commit counts: **high confidence** — pulled directly from GitHub API/gh CLI, not summarized from memory.
- The #2004 "no generic gate evaluator in 1.6.1" claim: **medium-high confidence** — it's a maintainer's own triage statement (not our independent code read), but maintainer explicitly says they verified it against the codebase.
- Whether our specific FFS `test_command`/`code_review_command`/`verify:post`/`ship:pre` wiring is *actually* broken today: **not verified in this pass** — this report identifies the risk; confirming it requires reading our actual gsd-core integration code against the 1.6.1 gate-evaluator behavior described in #2004/#2009 (recommend as an immediate follow-up).

## Migration-Relevant Verdicts (our 9 dependency areas)

| Area | Verdict |
|---|---|
| `workflow.test_command` | **KNOWN-BUGGY** (#2004, #2009) — no generic command-exec gate evaluator in 1.6.1; third-party blocking gates confirmed non-functional end-to-end |
| `workflow.code_review_command` | **KNOWN-BUGGY** (#2004, #1167) — same hardcoded-per-builtin-capability gate limitation |
| `model_overrides` | **KNOWN-BUGGY at 1.6.1, CHANGING-IN-1.7** (#1688) — stale-bake on static-frontmatter runtimes without rebake warning; fix ships 1.7.0-rc.2 |
| `dynamic_routing` | **SAFE** — no defects found specific to dynamic routing itself; adjacent model-profile issues (#1650, #1872) are runtime-specific (OpenCode) docs/behavior gaps, not core routing bugs |
| **Capability system (gate hooks + consent model)** | **KNOWN-BUGGY** (#2004, #2009 open even post-1.7.0-rc.2, #1858 open) — the consent/trust-gate model has an unfixed fail-closed-everywhere defect when a third-party capability fails to load |
| **Wave-parallel executors** | **SAFE** (as of 1.6.1) — #1297 and #1369 (sequential-degrade, stale-worktree-base) both closed before our pin; open #1689 is a feature gap (no per-plan executor routing), not a correctness bug |
| **STATE.md pause/resume (progress counters)** | **KNOWN-BUGGY, CHANGING-IN-1.7** — matches our local finding exactly; ADR-1769 Transition Module (1.7.0-rc.2) is the fix vehicle, but #2012 and #2022 show new instances still surfacing post-1.7.0-rc.2 — do not expect 1.7.0 to fully close this class |
| `global_learnings` | **SAFE**, with a doc-only defect (#2019: docs say `~/.gsd/learnings/`, code uses `~/.gsd/knowledge/` — cosmetic, not behavioral) |
| `mempalace` | **CHANGING-IN-1.7-ADJACENT** — active feature development (#1964 semantic recall via MemPalace, #2007 closed "implement forward-declared mempalace.memory_mode modes") — treat as pre-1.0-stability, not yet hardened |

## Report Location
`/Users/luminamao/Documents/Github/feature-fix-swarm/.claude/worktrees/gsd-ruflo/spike-results/gsd-ruflo/research_gsd-core_20260705.md`

## Post-research reconciliation (main session, 2026-07-05)

The `workflow.test_command` / `code_review_command` KNOWN-BUGGY verdicts above are
OVERBROAD: #2004/#2009 concern the CAPABILITY-system gate evaluator (third-party
`gates:` hooks in capability fragments), not the config seams. The FFS spike
(`spike-results/gsd-core-eval/report.md`, criterion (a)) DIRECTLY proved
`workflow.test_command` blocks-then-proceeds at 1.6.1, opus-adversarially verified.
Standing verdicts for our adoption:
- Config seams (test_command / code_review_command): SAFE (spike-proven live).
- ffs-gates as an installable capability with blocking gates (plan Phase B):
  BLOCKED at 1.6.1 per maintainer on #2004 — re-design Phase B to config seams +
  gsd hook events, or defer capability-native gates to a post-1.7.0 conformance run.
