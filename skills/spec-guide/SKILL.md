---
name: spec-guide
description: "Create role-based instructions for a feature delivered by a spec, split into developer, admin, and user guidance, then verify every numbered step through its real delivery vehicle. Use after or near spec completion when someone asks how to configure, operate, or use the feature and needs browser, API/MCP, Telegram, email, CLI, webhook, worker, or design evidence rather than untested prose."
---

# /spec-guide [NNN] [--no-live] [--output PATH]

## Host dispatch contract

- Codex: invoke skills as `$skill`; use Codex collaboration roles and GPT-5.6 model tiers.
- Claude: invoke skills as `/skill`; use Claude Agent/Skill tools and Claude model aliases.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.
- Resolve `volume` to Codex Luna low / Claude Haiku, `execution` to Codex Terra medium / Claude Sonnet, and `judgment` to Codex Sol high / Claude Opus.

Produce instructions that have survived the same journey the reader will take.
Do not treat a unit test, screenshot, or prior completion claim as a substitute
for exercising the documented surface.

This is an operating guide for the shipped feature, not a retelling of its
implementation or release history. Include a build, migration, promotion,
cutover, or rollback step only when a developer or administrator must still
perform it to configure, use, recover, or retire the feature today. Otherwise
cite it as background at most. A role with no current operating surface gets a
source-backed `N/A`; do not fill the section with historical delivery tasks.

## Step 1 — Resolve and collect

Resolve the spec from the argument, otherwise from the branch numeric prefix.
Fail closed if zero or multiple `specs/<NNN>-*` directories match.

```bash
bash "$(dirname <this skill>)/scripts/collect-usage-facts.sh" "$SPEC_ID"
```

Read the resolved spec, plan, tasks, linked production source, existing tests,
and evidence filenames. Never print evidence contents. Before any ledger read,
export `GSD_RUN_ID=spec-<NNN>` and pin `GATES_STORE` to the main checkout's
authoritative evidence store.

Default output is `<spec-dir>/usage-guide.md`; `--output` overrides it.
`--no-live` permits drafting but every unexecuted step must remain `BLOCKED` or
`PARTIAL`, never `VERIFIED`.

## Step 2 — Build the role and surface inventory

Fan out source-backed analysis when agents are available:

| Role | Tier | Contract |
|---|---|---|
| developer analyst | execution | Setup, SDK/API, local test, observability, rollback, and failure-mode instructions |
| admin analyst | judgment | Provisioning, permissions, configuration, policy, recovery, and audit instructions |
| user-journey analyst | execution | Every user-visible happy path, correction path, destructive action, and recovery path |
| surface auditor | judgment | Enumerate every delivery vehicle and reject missing or indirect proof |

The surface auditor is the reviewer, not a producer. Producer and reviewer must
be different agents or models. Reconcile findings into one matrix containing:

`role · instruction step · production surface · vehicle · prerequisites · side effects · cleanup · proof · status`.

Search explicitly for browser pages, admin pages, APIs, MCP tools, mobile
clients, Telegram or other chat channels, email, CLI, webhooks, workers/queues,
database effects, files, scheduled jobs, and visual design. Absence must be a
source-backed `N/A`, not an omission.

Collector vehicle signals are search candidates only. Confirm each against the
current production source before adding it to the matrix. Prefer current shipped
behavior over a stale plan or unchecked acceptance table; record material
source/spec conflicts as known limits and never turn an unsafe workaround into
user guidance.

## Step 3 — Match each vehicle to its verifier

Every numbered instruction gets an end-to-end proof using the same vehicle:

| Vehicle | Required proof |
|---|---|
| Browser or admin UI | Run host-native QA (Codex: `$qa`; Claude: `/qa`) against the real route with realistic auth. Exercise the action, expected state, error state, authorization boundary, and cleanup. Use the repository's browser-auth skill when present. |
| Visual design | If UI/design files, design acceptance criteria, or screenshots changed, run host-native design review (Codex: `$design-review`; Claude: `/design-review`) on deployed desktop and mobile captures plus the implementation. A functional browser pass is not a design pass. |
| API or MCP | Call the real API/MCP operation with a valid request and the relevant invalid, unauthenticated, unauthorized, tenant-isolation, idempotency, and cleanup cases. Assert response and resulting state. |
| Telegram or another chat channel | Use the channel's E2E skill. For Telegram, use `e2e-testing-telegram` and prove the actual send/receive or command roundtrip through the sanctioned fixture, including replay/refusal and cleanup where applicable. |
| Email | Send through the configured delivery path, observe the real test inbox/provider event, follow links or replies, and verify suppression/error behavior. Never accept render-only proof for delivery instructions. |
| CLI | Run the exact documented command in a representative environment; assert exit code, safe output, state change, repeat behavior, and cleanup. |
| Webhook | Deliver an actually signed event to the endpoint, verify authentication/replay handling and downstream state, then clean up the fixture. |
| Worker, queue, cron, or database effect | Trigger through the documented producer, observe the real consumer and final state, and test retry/idempotency. A direct database edit cannot prove an application instruction. |
| Other | Name the vehicle and build an equivalent real roundtrip. Do not silently downgrade it to a unit test. |

Existing machine evidence may be reused only when it is newer than the code it
tests, binds the same environment and fixture, records the exact vehicle, and
contains enough detail to reproduce the result. Otherwise rerun it.

## Step 4 — Draft instructions before live execution

Copy `assets/spec-usage-guide-template.md` to the output path and complete it
from sources. Keep these exact top-level sections:

1. `Developer instructions`
2. `Admin instructions`
3. `User instructions`

Every numbered instruction must include:

- actor and prerequisites;
- exact action without secret values;
- expected observable outcome;
- an `E2E verification` subsection using the same vehicle;
- cleanup/rollback when the step mutates state;
- status: `VERIFIED`, `PARTIAL`, `BLOCKED`, or source-backed `N/A`;
- evidence path, command, timestamp/environment, and result.

Write for the reader's altitude. User steps describe product language, not
internal IDs. Admin steps name permissions and blast radius. Developer steps
name interfaces, environment names, failure modes, and test commands.

## Step 5 — Execute the verification matrix

Run from a clean, isolated worktree. Do not stash, commit, or overwrite foreign
changes merely to satisfy a QA tool. Execute non-mutating proofs first, then
fixture-bound mutations with preflight, cleanup, and the repository's required
operator grants. Secret names may appear; values may not.

For each result:

- `VERIFIED`: the documented action and expected outcome passed on the named vehicle.
- `PARTIAL`: part passed, but a named boundary or state was not exercised.
- `BLOCKED`: proof could not run; record the exact blocker and one-command or operator unblock.
- `N/A`: source evidence proves the role/surface does not exist for this feature.

Never convert `PARTIAL` or `BLOCKED` to `VERIFIED` because another layer passed.
If a documented action fails, correct the implementation or the instruction,
then rerun that action and adjacent authorization/error cases.

## Step 6 — Independent completeness review

Give the completed guide, collector output, source inventory, and proof results
to a fresh judgment reviewer. It must check:

- every feature capability appears under at least one role;
- every numbered instruction maps to exactly one or more real vehicles;
- every non-`N/A` vehicle has current evidence;
- admin and tenant boundaries include negative tests;
- all mutations have cleanup or rollback evidence;
- design review ran when design signals exist;
- no secret value or credential-shaped content appears in the guide.

Fix supported findings and re-review. The overall guide status is `VERIFIED`
only when every non-`N/A` instruction is verified. Otherwise publish honestly
as `PARTIAL` or `BLOCKED` with the remaining actions.

## Step 7 — Report

Write the guide and print:

- overall status;
- role step counts (`verified/partial/blocked/N/A`);
- surface coverage table;
- QA and design-review verdicts;
- output path;
- remaining operator actions, if any.

## Constraints

- Do not change spec checkboxes or claim spec completion.
- Do not invent admin/user capabilities that production source does not expose.
- Do not use production identities or data outside an approved fixture.
- Do not run destructive or production-mutating proof without the repository's grants.
- Never include credentials, session cookies, bearer tokens, link codes, or secret values in the guide or evidence excerpts.
