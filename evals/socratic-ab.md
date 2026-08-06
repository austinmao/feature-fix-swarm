# Socratic A/B evidence pass — plan-wall reviewer prompt delta

Status: **advisory**, not a ship gate (ROADMAP Phase 4 criterion 3, REQ-11 /
AC-011).

## What was measured, and what was not

This pass records the **reviewer PROMPT delta** — the byte/line difference
between the text `plan-wall.sh` sends to the reviewer host with the socratic
slice unarmed versus armed — not a two-dispatch model findings diff. AC-011's
original wording imagines comparing findings from two real adversary
dispatches. That comparison was not run here: two frontier dispatches would
cost real tokens and produce a non-reproducible artifact whose difference
could not be attributed to the slice alone (model sampling variance, not the
prompt). Measuring the prompt text instead is reproducible, free, and
attributable to exactly one variable.

**The empty-diff un-arm trigger AC-011 describes is NOT exercised by this
pass** — the stub reviewer returns an empty findings array `[]` on both the
unarmed and armed captures (no real model was invoked), so there are no
findings to diff in either arm. Whether an armed reviewer's findings would
actually differ from an unarmed one — the question AC-011's un-arm trigger is
about — remains open for the retro. No conclusion about un-arming
`plan-wall.sh`, `plan-decompose` Step 3, or `review-gate`'s honest verifier
may be drawn from this document.

## Provenance

The real pinned question bank was used (`bash scripts/install-socratic.sh
--dest <mktemp>/vendor/socratic`, network available), not the offline
`make_vendor_tree` bats fixture. The qualitative note below therefore
describes actual question and pack content, not fixture sentinel tokens. Had
the clone failed, the pass would have fallen back to `make_vendor_tree`
(`tests/bats/helpers/socratic-fixtures.bash`) and the qualitative note would
be marked structural-only — a sentinel fixture can prove the slice arrives
and how many bytes it adds, and can prove nothing about what a reviewer
additionally reads.

## Fixture

`specs/005-socratic-integration/socratic.md`, validated with `bash
scripts/gsd/socratic-slice.sh --validate specs/005-socratic-integration`
at exit 0 before either capture (an unvalidated fixture emits nothing on the
fail-soft emission path too, which would collapse both captures to an
identical, empty diff):

```yaml
---
domains: [security, testing, api]
depth: core
packs: [threat-modeling]
---
## Self-answered highlights

- The wall dispatches to an opposite-vendor reviewer whenever both hosts are
  installed, falling back to a same-vendor distinct-model rung otherwise.
- Every reviewer dispatch is a brand-new adversary-host invocation with no
  session reuse.

## Assumed (flag if wrong)

- ASSUME: the review brief plus the plan file content is the entire payload
  sent to the reviewer, with no additional repository context attached.
- ASSUME: a WAIVED verdict from PLAN_WALL=off still writes a durable
  run-state record so a later audit can see the phase was skipped, not
  silently unreviewed.

## Open questions → grants

- None outstanding; this fixture spec has no unresolved socratic gate.

## Top risks

- A reviewer selection tie between two same-vendor distinct-model rungs
  could pick a less capable model than intended if the diversity ladder is
  misordered.
```

The reviewed artifact was the real landed
`.planning/phases/03-arm-the-three-consumers/03-01-PLAN.md`, copied verbatim
into the scratch repo as the phase plan.

## Measurements

| Quantity | Value |
| --- | --- |
| Unarmed prompt size | 46,482 bytes |
| Armed prompt size | 53,726 bytes |
| Delta | +7,244 bytes (+15.6%) |
| Added lines (unified diff) | 67 |
| Delimited `SOCRATIC_DATA_START`…`SOCRATIC_DATA_END` block | 105 lines |
| Armed status line (stderr) | `socratic: armed domains=api,security,testing packs=threat-modeling` |

## Qualitative note

With the real pinned bank installed, the armed prompt's delimited block adds
the full question text for the `api`, `security`, and `testing` domains (the
core files' short forms, plus the `Core API, SDK, and Connectors` / `Core
Security` / `Core Testing and Quality` headings) and the `threat-modeling`
pack's core content (`## What are we protecting...`, `## Where are the trust
boundaries...`, `## How could an attacker impersonate...`, `## Can an
untrusted input cross into a privileged interpreter...`, `## If one identity,
secret, or component is compromised...`, `## Which threats remain...`). None
of this appears in the unarmed prompt, which is byte-identical to
`plan-wall.sh`'s pre-feature output. This is what "additionally reads" means
in practice for the domains and pack this fixture selected — it is not a
claim that a reviewer weighs this material correctly or that findings
improve; no model reviewed anything in this pass.

## Reproducible capture recipe

Run from the repo root, in a scratch directory, with no writes outside it:

```bash
REPO_ROOT="$(pwd)"
SCRATCH="$(mktemp -d)"

# 1. vendor tree — real bank preferred, make_vendor_tree fallback if offline
VENDOR="$SCRATCH/vendor/socratic"
bash "$REPO_ROOT/scripts/install-socratic.sh" --dest "$VENDOR" \
  || { source tests/bats/helpers/socratic-fixtures.bash; make_vendor_tree "$VENDOR"; }

# 2. scratch git repo mirroring tests/bats/socratic-plan-wall.bats's setup()
REPO="$SCRATCH/repo"
mkdir -p "$REPO/packages/feature-fix-swarm/lib" "$REPO/schemas" "$REPO/bin"
cp "$REPO_ROOT/lib/gates.py" "$REPO/packages/feature-fix-swarm/lib/gates.py"
cp "$REPO_ROOT/schemas/review-finding.schema.json" "$REPO/schemas/"
cd "$REPO"
git init -q -b main
git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
mkdir -p .planning/phases/1-foo
echo '{"model_overrides": {"gsd-planner": "fable"}, "dynamic_routing": {"escalate_on_failure": true}}' \
  > .planning/config.json
cp "$REPO_ROOT/.planning/phases/03-arm-the-three-consumers/03-01-PLAN.md" \
  .planning/phases/1-foo/PLAN.md
mkdir -p specs/005-socratic-integration
# write specs/005-socratic-integration/socratic.md — see Fixture above
git checkout -q -b 005-socratic-integration

# 3. validate the fixture BEFORE capturing anything
bash "$REPO_ROOT/scripts/gsd/socratic-slice.sh" --validate \
  specs/005-socratic-integration   # must exit 0

# 4. capture stub reviewer + inert opposite-vendor binary — no model invoked
export PATH="$REPO/bin:$PATH"
export FFS_ADVERSARY_MODEL_PROBE=off
export GATES_PY="$REPO/packages/feature-fix-swarm/lib/gates.py"
export ADVERSARY_BIN_CODEX=nonexistent-codex-binary-xyz
cat > bin/stub-claude <<'EOF'
#!/usr/bin/env bash
cat > "$PROMPT_CAPTURE"
printf '%s\n' '[]'
EOF
chmod +x bin/stub-claude
export ADVERSARY_BIN_CLAUDE="$REPO/bin/stub-claude"
LEVER="$REPO_ROOT/scripts/gsd/plan-wall.sh"

# 5. capture 1 — unarmed (SOCRATIC=off), the single varied condition
export PROMPT_CAPTURE="$SCRATCH/unarmed.prompt"
export SOCRATIC=off
export FFS_SOCRATIC_DIR="$SCRATCH/no-such-vendor-tree"
bash "$LEVER" .planning/phases/1-foo
unset SOCRATIC
rm -rf .planning/run-state   # unconditional re-dispatch on capture 2

# 6. capture 2 — armed
export PROMPT_CAPTURE="$SCRATCH/armed.prompt"
export FFS_SOCRATIC_DIR="$VENDOR"
bash "$LEVER" .planning/phases/1-foo

# 7. derive the numbers — never commit the captures themselves
wc -c "$SCRATCH/unarmed.prompt" "$SCRATCH/armed.prompt"
diff -u "$SCRATCH/unarmed.prompt" "$SCRATCH/armed.prompt" | grep -c '^+[^+]'
```

The captured prompt files (`unarmed.prompt`, `armed.prompt`) are large
copies of a real phase plan plus the reviewer brief and are never committed;
they stayed in the scratch directory and were discarded after the numbers
above were derived.
