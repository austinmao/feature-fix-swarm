# Prior art — spec 004 model routing

Pattern-mined 2026-08-03. Verdict: **PORT** (adopt patterns, no new dependency).

| Source | What it is | What we take | What we reject | Citation |
|---|---|---|---|---|
| consort (siimvene/consort) | Two-vendor Claude Code lifecycle: fable principal orchestrates/judges, `codex exec -m gpt-5.6-sol` implements; no model's work ships on its own word | (1) Principal-never-drafts blind panel — but at SPEC authoring only; (2) shared per-artifact JSON schema with the codex `--output-schema` vs plugin prompt-contract backend split; (3) "a finding is a lead, not a verdict" adversarial verify | Panel at plan phase (consort's own plan phase is single-author+refute — matches FFS's existing gauntlet shape); same-vendor panel as degrade (shared blind spots); whole-lifecycle adoption (competing architecture) | SPEC.md §panel ("The principal does not author a panel draft — it holds the REQUEST framing… converges with the synthesis instead of diverging"), §risks ("measure on the fixture before deciding the panel earns its keep vs plain author+refuter"); build order puts panel in Slice 2-of-3, after the spine is proven |
| consort scoreboard | `scoreboard.md` read on boot to bias routing (telemetry-driven model routing seed) | DEFERRED to a follow-up spec — FFS seed exists (`evals/gpt56/results.json`, `rehearsal.json`) but wiring live routing bias is out of this spec's blast radius | — | AGENTS.md §"Routing scoreboard" |
| spec-kit discussion #513 | Multi-model plan review discussion in github/spec-kit | Confirms ecosystem direction (plan review wants a second model family); no reusable artifact shipped | — | github.com/github/spec-kit/discussions/513 |
| gsd-core v1.9.x line | Pinned dependency's own model machinery | v1.9.1 stays exact-pinned (confirmed latest, released Jul 31); `effortSurface` (#2481/#2490) + `stale-bake-guard` (#1688/#1692) present in installed build — doctor SURFACES the latter rather than rebuilding it | Upgrading (no need); re-plumbing effort onto effortSurface (per-call `-c model_reasoning_effort` already invocation-time) | github.com/open-gsd/gsd-core/releases (v1.9.0/v1.9.1 notes); `node_modules/@opengsd/gsd-core/gsd-core/bin/lib/stale-bake-guard.cjs:4-6` |
| humanlayer SlopCodeBench run | 17-checkpoint inherited-codebase benchmark (Opus 5 strict 24% vs Opus 4.8 6% vs Sonnet 5 6%; verbosity accretion 65%→80% for every model) | The eval SHAPE for EVAL-A (hand-off across checkpoints under FFS gates) + the thesis: no cheap model inherits a codebase across phases un-gated | Treating its absolute numbers as routing evidence (different harness, no Fable arm) | github.com/humanlayer/advanced-context-engineering-for-coding-agents — benchmarking-opus-5-on-slop-code-bench.md |

Model-landscape sources for tier-table justifications (labelled, not
cross-vendor comparable): Anthropic + OpenAI vendor cards, vals.ai +
Artificial Analysis + llm-stats independent leaderboards, vellum GPT-5.6
explainer, thenewstack Fable-5 reception roundup, cloudzero Opus-5 pricing —
as compiled in the operator brief 2026-08-03; each table row tags its source
class ([vendor]/[independent]/[labelled]/[contested]/[GUESS→EVAL]).
