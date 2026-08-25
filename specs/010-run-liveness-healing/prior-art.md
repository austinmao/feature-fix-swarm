# Prior art — spec 010 run-liveness healing

Searched 2026-08-10 (local skill scout + gh repo/code search, PRIOR_ART_MIN_STARS=200).

| candidate | type | stars | applicability verdict | evidence |
|---|---|---|---|---|
| terryso/claude-auto-resume | bash script (repo) | 813 | vindicated but REJECTED for adoption — architecture inverted (interactive wrapper owning the CLI vs our callee-with-capture-file contract); fit ~15-25%; license unverified | `claude-auto-resume.sh` greps `(limit reached\|hit your limit).*resets`, parses reset HH:MM, sleeps, re-runs |
| EveryInc/compound-engineering-plugin (`ce-babysit-pr`) | Claude Code skill | 24157 | reference only — validates infra-vs-test classify + `gh run rerun --failed` design; coupled to its own state machine, too heavy for bash-only toolkit | skill step 4: flaky/infra → rerun; real failure → debug |
| camunda/camunda `.claude/skills/ci-fix-failure` | skill md | 4239 | reference only — same 2-way infra/test taxonomy | "No silent reruns… Recommend `gh run rerun --failed`" |
| microsoft/onnxruntime `.agents/skills/ort-ci` | skill md | 21329 | reference only — 3-tier taxonomy incl. "transient/infra… re-run, escalate on 2-3x recurrence" | matches bounded-rerun intent |
| AnandChowdhary/continuous-claude | bash script | 1367 | reference only — loop+PR conductor pattern, not usage-limit or CI specific | while-loop conductor |
| gh CLI built-ins (`gh run watch/rerun`) | built-in | n/a | partial only — `watch --exit-status` polls fixed 3s interval, no backoff, no classification, no rerun trigger; `rerun --failed` is the rerun primitive we invoke | `gh run watch --help`, `gh run rerun --help` |
| andborth/RoboPhD, ShootingKing-AM/claude-code-orchestrator | scripts | 25 / 0 | reject — below star threshold | usage-limit sleep pattern, low signal |
| local: gsd-run.sh heartbeat/staleness loop (`:545-625`) | in-repo | n/a | REUSED — bounded-sleep + staleness discipline pattern for item 3 | spec-009 heartbeat |
| local: plan-wall.sh durable verdict records | in-repo | n/a | REUSED — item 1's `--await` polls these; no new state file | `.planning/run-state/` records, `verdict` field |

## Decision input

Local scout: items 1 (await verb) and 4 (ci-watch) greenfield; item 2 has quarantine/refusal
(BUDGET-BREACHED, `gsd-run.sh:367`) but no healing layer; item 3's bounded-sleep discipline
exists in the spec-009 heartbeat loop; item 5 has TTL timeouts but no plan-length ceiling.
OSS researcher: no adoptable library for any item; `gh run watch` inadequate for item 4;
infra/test classification keyword-table approach precedented by three ≥4k-star repos' skills.

## Adjudication (judgment tier, 2026-08-10)

**DECISION: build-fresh** (all five items; item 3 explicitly adjudicated against
terryso/claude-auto-resume). Rationale: (1) license unverified — disqualifying for
adopt/port under the no-vendored-third-party convention, and diligence cost exceeds an
~80-line artifact; (2) unknown maintenance — a one-time copy inherits all future banner
drift anyway; (3) fit ~15-25% — upstream owns the CLI invocation (interactive foreground
wrapper); ours is a callee handed a capture file + resume argv, and none of the cap env,
typed lines, fail-closed unparseable path, kill switch, or codex banner variant exists
upstream; (4) integration cost of porting ≥ writing ~80 lines fresh to house conventions.

**Borrowed (attribution via comment only, no license obligation — observed vendor-output
fact, not expression):** the banner detection shape `(limit reached|hit your limit).*resets`
+ reset-time capture. Comment above the regex:
`# banner pattern shape observed in terryso/claude-auto-resume (prior art)`.

Edge cases a blind port would miss, now spec'd: reset time earlier than now → roll forward
to tomorrow (never negative sleep); am/pm + timezone-suffix banner variants → anchor the
parse on digits, not a 24-hour assumption.
