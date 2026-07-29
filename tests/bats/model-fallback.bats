#!/usr/bin/env bats
# model-fallback.sh — probe-and-substitute lever tests (synthetic probes only)

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LEVER="$REPO_ROOT/scripts/gsd/model-fallback.sh"
  TMP="$(mktemp -d)"
  export GSD_FALLBACK_CACHE="$TMP/cache"
  mkdir -p "$TMP/.planning"
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "model_overrides": {
    "gsd-planner": "claude-fable-5",
    "gsd-plan-checker": "claude-fable-5",
    "gsd-verifier": "claude-opus-5"
  },
  "dynamic_routing": {
    "enabled": true,
    "tier_models": { "light": "claude-haiku-4-5-20251001", "standard": "claude-sonnet-5", "heavy": "claude-opus-5" }
  }
}
JSON
  cat > "$TMP/probe-ok.sh" <<'SH'
#!/bin/sh
exit 0
SH
  cat > "$TMP/probe-fail.sh" <<'SH'
#!/bin/sh
exit 1
SH
  chmod +x "$TMP/probe-ok.sh" "$TMP/probe-fail.sh"
}

teardown() { rm -rf "$TMP"; }

@test "FALLBACK-001: fable unavailable -> rewritten to opus in all overrides" {
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"UNAVAILABLE -> claude-opus-5 (2 override(s) rewritten)"* ]]
  fable_count="$(grep -c -- "claude-fable-5" "$TMP/.planning/config.json" || true)"
  [ "$fable_count" -eq 0 ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-opus-5" ]
}

@test "FALLBACK-002: fable available -> config byte-identical" {
  cp "$TMP/.planning/config.json" "$TMP/before.json"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"available — config unchanged"* ]]
  diff -q "$TMP/before.json" "$TMP/.planning/config.json"
}

@test "FALLBACK-003: probe cached — second run does not re-probe" {
  PROBE_LOG="$TMP/probe.log"
  cat > "$TMP/probe-log.sh" <<SH
#!/bin/sh
echo probed >> "$PROBE_LOG"
exit 1
SH
  chmod +x "$TMP/probe-log.sh"
  GSD_MODEL_PROBE_CMD="$TMP/probe-log.sh" bash "$LEVER" "$TMP/.planning"
  GSD_MODEL_PROBE_CMD="$TMP/probe-log.sh" bash "$LEVER" "$TMP/.planning" || true
  [ "$(wc -l < "$PROBE_LOG" | tr -d ' ')" -eq 1 ]
}

@test "FALLBACK-004: config without fable is a no-op (no probe)" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{ "model_overrides": { "gsd-verifier": "claude-opus-5" } }
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ ! -f "$GSD_FALLBACK_CACHE/claude-fable-5.status" ]
}

@test "FALLBACK-005: missing config errors" {
  run bash "$LEVER" "$TMP/nope"
  [ "$status" -eq 1 ]
  [[ "$output" == *"not found"* ]]
}

@test "FALLBACK-006: fable down + codex sol up -> opus substituted, marker mode=codex-sol with correct paths" {
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"UNAVAILABLE -> claude-opus-5 (2 override(s) rewritten)"* ]]
  [ -f "$TMP/.planning/fable-fallback.json" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/fable-fallback.json'))['mode'])")" = "codex-sol" ]
  # paths maps each rewritten JSON path to its exact prior value (form-preserving restore)
  [ "$(python3 -c "import json;print(sorted(json.load(open('$TMP/.planning/fable-fallback.json'))['paths'].items()))")" = "[('model_overrides.gsd-plan-checker', 'claude-fable-5'), ('model_overrides.gsd-planner', 'claude-fable-5')]" ]
}

@test "FALLBACK-007: both fable and codex sol down -> opus substituted, marker mode=opus-only" {
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-fail.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/fable-fallback.json'))['mode'])")" = "opus-only" ]
}

@test "FALLBACK-008: fable back + marker -> restores only marker paths, intentional opus pins untouched, marker deleted" {
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  [ -f "$TMP/.planning/fable-fallback.json" ]
  # gsd-verifier was an intentional pre-existing opus pin, not a fable rewrite
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-verifier'])")" = "claude-opus-5" ]

  # simulate the 24h cache expiring so the "fable is back" probe actually re-runs
  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"restored 2 path(s) — marker deleted"* ]]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-fable-5" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-plan-checker'])")" = "claude-fable-5" ]
  # intentional opus pin untouched by recovery — not blanket-flipped back to fable
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-verifier'])")" = "claude-opus-5" ]
}

@test "FALLBACK-009: no-fable config -> still no marker written" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{ "model_overrides": { "gsd-verifier": "claude-opus-5" } }
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-fail.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
}

@test "FALLBACK-010: alias-form config (template-shaped 'fable') -> substituted to 'opus', marker records path->'fable'" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{ "model_overrides": { "gsd-planner": "fable", "gsd-verifier": "opus" } }
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"(1 override(s) rewritten)"* ]]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "opus" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/fable-fallback.json'))['paths']['model_overrides.gsd-planner'])")" = "fable" ]
}

@test "FALLBACK-011: mixed-form config -> both forms substituted, recovery restores exact form per path, opus pins untouched" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{ "model_overrides": { "gsd-planner": "fable", "gsd-plan-checker": "claude-fable-5", "gsd-verifier": "opus" } }
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "opus" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-plan-checker'])")" = "claude-opus-5" ]

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "fable" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-plan-checker'])")" = "claude-fable-5" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-verifier'])")" = "opus" ]
}

@test "FALLBACK-012: real-CLI probes are wall-clock bounded (dead-CLI hang guard)" {
  # No GSD_MODEL_PROBE_CMD override -> the lever hits its real-CLI branch.
  # Shim claude/codex hang (sleep 30); GSD_MODEL_PROBE_TIMEOUT=1 must reap them
  # and read the probes as "unavailable" (fallback applied), never block.
  # This wall runs at the top of /feature-implement — pre-fix it had NO timeout
  # on any branch (2026-07-12 dead-codex forensics).
  mkdir -p "$TMP/shim"
  cat > "$TMP/shim/claude" <<'SH'
#!/bin/sh
sleep 30
SH
  cp "$TMP/shim/claude" "$TMP/shim/codex"
  chmod +x "$TMP/shim/claude" "$TMP/shim/codex"
  run env PATH="$TMP/shim:$PATH" GSD_MODEL_PROBE_TIMEOUT=1 bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"UNAVAILABLE"* ]]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-opus-5" ]
}

@test "FALLBACK-013: fable inside a JSON array is substituted, not a TypeError crash" {
  # Regression: sub() recursed only into dicts, so a list value fell through to
  # `v in forms` -> hashing a list -> TypeError. The rewrite aborted entirely and
  # left a live fable pin in config.json, silently defeating the fallback chain.
  # Every FALLBACK-001..012 fixture is a flat dict, which is why this shipped green.
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "model_overrides": { "gsd-planner": "fable" },
  "review": { "default_reviewers": ["claude-fable-5", "opus", "fable"] },
  "nested": { "deep": [{ "model": "claude-fable-5" }, ["fable"]] }
}
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" != *"TypeError"* ]]
  [[ "$output" == *"(5 override(s) rewritten)"* ]]
  ! grep -q "fable" "$TMP/.planning/config.json"
}

@test "FALLBACK-014: array substitution is form-preserving and marker indexes list elements" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "review": { "default_reviewers": ["claude-fable-5", "opus", "fable"] },
  "nested": { "deep": [{ "model": "claude-fable-5" }, ["fable"]] }
}
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  # alias stays alias, full ID stays full ID — even inside arrays
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['review']['default_reviewers'][0])")" = "claude-opus-5" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['review']['default_reviewers'][2])")" = "opus" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['nested']['deep'][1][0])")" = "opus" ]
  # an untouched non-fable array element must survive verbatim
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['review']['default_reviewers'][1])")" = "opus" ]
  # list elements are addressed by integer index, including list-inside-list
  [ "$(python3 -c "import json;p=json.load(open('$TMP/.planning/fable-fallback.json'))['paths'];print('review.default_reviewers.0' in p and 'nested.deep.1.0' in p)")" = "True" ]
}

@test "FALLBACK-015: recovery round-trips an array-bearing config byte-identical" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "model_overrides": { "gsd-planner": "fable", "gsd-verifier": "opus" },
  "review": { "default_reviewers": ["claude-fable-5", "opus", "fable"] },
  "nested": { "deep": [{ "model": "claude-fable-5" }, ["fable"]] }
}
JSON
  python3 -c "import json;json.dump(json.load(open('$TMP/.planning/config.json')),open('$TMP/before.json','w'),indent=2)"
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"restored 5 path(s) — marker deleted"* ]]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
  run python3 -c "
import json
a=json.load(open('$TMP/before.json')); b=json.load(open('$TMP/.planning/config.json'))
assert a==b, ('restore mismatch', a, b)
print('ok')"
  [ "$status" -eq 0 ]
}

@test "FALLBACK-016: a marker path that no longer resolves is skipped, not fatal — the rest restore" {
  # The lever is the FIRST wall in /feature-implement. Recovery used to walk every
  # marker path with no guard, so ONE unresolvable path (config hand-edited, list
  # shortened, key renamed between rewrite and recovery) raised out of the python
  # heredoc, and `set -euo pipefail` turned that into a nonzero exit for the whole
  # run — with json.dump never reached, so even the RESOLVABLE paths stayed on opus.
  # Contract: fail-soft. Restore what resolves, name what did not, keep the marker
  # so a later run can retry, exit 0.
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "model_overrides": { "gsd-planner": "fable" },
  "review": { "default_reviewers": ["claude-fable-5", "opus"] }
}
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  [ -f "$TMP/.planning/fable-fallback.json" ]

  # hand-edit between rewrite and recovery: the list the marker indexes is now empty,
  # so `review.default_reviewers.0` cannot resolve (IndexError on list assignment).
  python3 -c "
import json
p='$TMP/.planning/config.json'
c=json.load(open(p)); c['review']['default_reviewers']=[]
json.dump(c,open(p,'w'),indent=2)"

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  # the unresolvable path is named, not swallowed
  [[ "$output" == *"review.default_reviewers.0"* ]]
  [[ "$output" == *"WARN"* ]]
  # the resolvable path DID restore — this is what the old hard-fail lost
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "fable" ]
  # marker retained so a later run can retry the skipped path
  [ -f "$TMP/.planning/fable-fallback.json" ]
  # config is still valid JSON, not a truncated write
  python3 -c "import json;json.load(open('$TMP/.planning/config.json'))"
}

@test "FALLBACK-017: installed copy of the lever has not drifted from the packaged source" {
  # setup.sh:643 installs gsd/model-fallback.sh into the consumer's scripts/gsd/,
  # so the packaged copy under test here is the SOURCE and scripts/gsd/ is the
  # installed artifact. A fix hand-applied to only one side ships a lever whose
  # tests pass while the copy /feature-implement actually invokes still has the bug.
  # FAIL-CLOSED: an absent installed copy is a real finding in a consumer repo, so
  # only an explicit GSD_FFS_STANDALONE=1 (packaged repo, nothing installed) skips.
  CONSUMER_LEVER="$(cd "$REPO_ROOT/../.." && pwd)/scripts/gsd/model-fallback.sh"
  if [ ! -f "$CONSUMER_LEVER" ]; then
    [ "${GSD_FFS_STANDALONE:-0}" = "1" ] || {
      echo "no installed copy at $CONSUMER_LEVER — set GSD_FFS_STANDALONE=1 if intentional" >&2
      return 1
    }
    skip "GSD_FFS_STANDALONE=1: packaged repo with no installed copy"
  fi
  cmp -s "$LEVER" "$CONSUMER_LEVER"
}

@test "FALLBACK-018: an unreadable marker warns and leaves config alone, it does not fail the run" {
  # The marker write is not the last thing that can be interrupted, and this lever
  # is the FIRST wall in /feature-implement. json.load(marker) used to sit outside
  # every guard, so a marker truncated by a SIGTERM mid-write raised a traceback
  # and exited 1 — taking the whole run down over a recovery hint.
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  cp "$TMP/.planning/config.json" "$TMP/config-before.json"
  printf '{"mode":"opus-only","pa' > "$TMP/.planning/fable-fallback.json"   # truncated

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"WARN"* ]]
  [[ "$output" == *"unreadable"* ]]
  # config must be byte-identical — a failed recovery may not half-write it
  cmp "$TMP/config-before.json" "$TMP/.planning/config.json"
  # marker preserved for inspection rather than silently discarded
  [ -f "$TMP/.planning/fable-fallback.json" ]
}

@test "FALLBACK-018b: a marker with no usable paths map warns instead of raising KeyError" {
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  echo '{"mode":"opus-only"}' > "$TMP/.planning/fable-fallback.json"
  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"WARN"* ]]
}

@test "FALLBACK-019: a dot-bearing config key is refused, never restored into the wrong node" {
  # The marker joins path segments with ".", so key "a.b" and nested a->b collide.
  # Rewriting through such a key made recovery write the original into the OTHER
  # node — leaving the dead fable pin AND flipping an intentional opus pin to
  # fable, which is precisely what the per-path marker exists to prevent.
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "a.b": "fable",
  "a": { "b": "claude-opus-5" },
  "model_overrides": { "gsd-planner": "fable" }
}
JSON
  cp "$TMP/.planning/config.json" "$TMP/before.json"
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  # the ambiguous key is named, and NOT rewritten
  [[ "$output" == *"a.b"* ]]
  [[ "$output" == *"WARN"* ]]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['a.b'])")" = "fable" ]
  # the unambiguous pin still got substituted
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "opus" ]

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  # THE point: the intentional opus pin at a->b was never touched
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['a']['b'])")" = "claude-opus-5" ]
  run python3 -c "
import json
a=json.load(open('$TMP/before.json')); b=json.load(open('$TMP/.planning/config.json'))
assert a==b, ('round-trip mismatch', a, b)
print('ok')"
  [ "$status" -eq 0 ]
}

@test "FALLBACK-020: a pin edited since the fallback is left alone and named, not clobbered" {
  # Recovery assigned the recorded original unconditionally, so a deliberate edit
  # made while fable was down was silently reverted to a stale value.
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  python3 -c "
import json
p='$TMP/.planning/config.json'
c=json.load(open(p)); c['model_overrides']['gsd-planner']='claude-sonnet-5'
json.dump(c,open(p,'w'),indent=2)"

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"changed since fallback"* ]]
  [[ "$output" == *"model_overrides.gsd-planner"* ]]
  # the deliberate edit survives
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-sonnet-5" ]
  # the OTHER path (untouched by hand) still restored
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-plan-checker'])")" = "claude-fable-5" ]
}

@test "FALLBACK-021: the kept marker is pruned to ONLY the unresolved paths" {
  # Retaining the full path set meant already-restored entries were re-applied on
  # every later run and the marker could never converge to absent.
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "model_overrides": { "gsd-planner": "fable" },
  "review": { "default_reviewers": ["claude-fable-5", "opus"] }
}
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  python3 -c "
import json
p='$TMP/.planning/config.json'
c=json.load(open(p)); c['review']['default_reviewers']=[]
json.dump(c,open(p,'w'),indent=2)"

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  [ "$(python3 -c "import json;print(sorted(json.load(open('$TMP/.planning/fable-fallback.json'))['paths']))")" = "['review.default_reviewers.0']" ]
}

@test "FALLBACK-022: a second fable flap after a partial restore keeps the unresolved original" {
  # The rewrite replaced the marker wholesale, so one flap after a partial restore
  # destroyed the skipped path's recorded original permanently.
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "model_overrides": { "gsd-planner": "fable" },
  "review": { "default_reviewers": ["claude-fable-5", "opus"] }
}
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  python3 -c "
import json
p='$TMP/.planning/config.json'
c=json.load(open(p)); c['review']['default_reviewers']=[]
json.dump(c,open(p,'w'),indent=2)"
  # partial restore: gsd-planner comes back, review.default_reviewers.0 is skipped
  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  # fable flaps back out — the rewrite must MERGE, not replace
  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  [ "$(python3 -c "import json;p=json.load(open('$TMP/.planning/fable-fallback.json'))['paths'];print(p.get('review.default_reviewers.0'))")" = "claude-fable-5" ]
  # and this run's own rewrite is recorded too
  [ "$(python3 -c "import json;p=json.load(open('$TMP/.planning/fable-fallback.json'))['paths'];print(p.get('model_overrides.gsd-planner'))")" = "fable" ]
}

@test "FALLBACK-023: a recovery that restores nothing does not rewrite config.json at all" {
  # json.dump ran unconditionally, so an all-skipped recovery truncated and
  # rewrote a config it had no edits for — a corruption window for zero benefit.
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  # make every marker path unresolvable
  python3 -c "
import json
p='$TMP/.planning/config.json'
c=json.load(open(p)); del c['model_overrides']
json.dump(c,open(p,'w'),indent=2)"
  cp "$TMP/.planning/config.json" "$TMP/config-before.json"
  # Comparing CONTENT cannot detect this: an unconditional json.dump of an
  # unchanged object reproduces byte-identical output. The observable is the write
  # itself — atomic_write goes tmp + os.replace, so any write changes the inode.
  INO_BEFORE="$(python3 -c "import os;print(os.stat('$TMP/.planning/config.json').st_ino)")"
  MTIME_BEFORE="$(python3 -c "import os;print(os.stat('$TMP/.planning/config.json').st_mtime_ns)")"

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"unresolvable"* ]]
  cmp "$TMP/config-before.json" "$TMP/.planning/config.json"
  [ "$(python3 -c "import os;print(os.stat('$TMP/.planning/config.json').st_ino)")" = "$INO_BEFORE" ]
  [ "$(python3 -c "import os;print(os.stat('$TMP/.planning/config.json').st_mtime_ns)")" = "$MTIME_BEFORE" ]
}

@test "FALLBACK-024: merge-forward must not overwrite what THIS run installed (form change)" {
  # Round-2 HIGH. The merge-forward loop copied the PRIOR marker's entry over a
  # path this run had just rewritten. When the pin's form changed between runs
  # (alias 'fable' vs full 'claude-fable-5' — both are live, see FALLBACK-011),
  # 'installed' then described a value this lever never wrote, so the CAS refused
  # on every later run and the pin was stranded on opus PERMANENTLY.
  # The reachable route is a SKIPPED path that later comes back in the other form:
  # a skipped path stays in the marker, so the next rewrite sees BOTH a prior entry
  # and a live literal at the same path.
  cat > "$TMP/.planning/config.json" <<'JSON'
{ "model_overrides": { "gsd-planner": "claude-fable-5" } }
JSON
  # 1. rewrite -> paths{p: claude-fable-5}, installed{p: claude-opus-5}
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  # 2. the whole container disappears, so the marker path cannot resolve
  echo '{ "other": "claude-fable-5" }' > "$TMP/.planning/config.json"
  # 3. fable returns -> path skipped, marker RETAINED carrying gsd-planner
  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  [ "$(python3 -c "import json;print('model_overrides.gsd-planner' in json.load(open('$TMP/.planning/fable-fallback.json'))['paths'])")" = "True" ]
  # 4. operator re-adds the pin, in the OTHER form (alias, not full ID)
  echo '{ "model_overrides": { "gsd-planner": "fable" } }' > "$TMP/.planning/config.json"
  # 5. fable flaps out -> this run installs "opus" and must record THAT
  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "opus" ]
  # pre-fix these were the PRIOR run's stale values (claude-fable-5 / claude-opus-5)
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/fable-fallback.json'))['installed']['model_overrides.gsd-planner'])")" = "opus" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/fable-fallback.json'))['paths']['model_overrides.gsd-planner'])")" = "fable" ]
  # 6. THE point: fable returns and the pin actually comes back, in the form the
  #    operator last wrote. Pre-fix the CAS compared "opus" against a stale
  #    "claude-opus-5", refused forever, and the pin was stranded on opus.
  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" != *"changed since fallback"* ]]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "fable" ]
  # nothing unresolved remains, so the marker is gone (it converged)
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
}

@test "FALLBACK-025: a corrupt config.json fails loudly and named, in BOTH directions" {
  # A bare JSONDecodeError traceback used to escape the rewrite heredoc. The run
  # cannot proceed on an unparseable config whatever this lever does, so this is
  # deliberately FATAL rather than fail-soft — but it must be named, not a trace.
  printf '{ "model_overrides": { "gsd-planner": "fable" ' > "$TMP/.planning/config.json"
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -ne 0 ]
  [[ "$output" == *"FATAL"* ]]
  [[ "$output" == *"not valid JSON"* ]]
  [[ "$output" != *"Traceback"* ]]

  # same in the recovery direction (marker present, fable available)
  echo '{"mode":"opus-only","paths":{"model_overrides.gsd-planner":"fable"}}' > "$TMP/.planning/fable-fallback.json"
  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -ne 0 ]
  [[ "$output" == *"FATAL"* ]]
  [[ "$output" != *"Traceback"* ]]
}

@test "FALLBACK-027: a LEGACY marker path with two readings is refused, not mis-restored" {
  # Round-3 HIGH. Refusing dot-bearing keys at REWRITE time does nothing for a
  # marker an older version already wrote. Recovery split such a path on "." and
  # could write the original into the nested node instead of the literal-key node,
  # flipping an intentional opus pin — the corruption the marker exists to prevent.
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "a.b": "opus",
  "a": { "b": "claude-opus-5" }
}
JSON
  # hand-written legacy marker: no "installed" map, ambiguous dot-joined path
  echo '{"mode":"opus-only","paths":{"a.b":"fable"}}' > "$TMP/.planning/fable-fallback.json"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ambiguous"* ]]
  # NEITHER reading was written: the nested opus pin is untouched...
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['a']['b'])")" = "claude-opus-5" ]
  # ...and the literal key is untouched too
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['a.b'])")" = "opus" ]
  # marker retained so an operator can resolve it by hand
  [ -f "$TMP/.planning/fable-fallback.json" ]
}

@test "FALLBACK-028: an unreadable EXISTING marker is never overwritten by a rewrite" {
  # Round-3 HIGH. The rewrite treated an unparseable prior marker as absent and
  # then replaced it — destroying the only record of an earlier fallback's
  # originals. Refuse instead, so the damaged file survives for hand-repair.
  printf '{"mode":"opus-only","pa' > "$TMP/.planning/fable-fallback.json"
  cp "$TMP/.planning/fable-fallback.json" "$TMP/marker-before.json"
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -ne 0 ]
  [[ "$output" == *"FATAL"* ]]
  [[ "$output" != *"Traceback"* ]]
  # the damaged marker is byte-identical, not replaced
  cmp "$TMP/marker-before.json" "$TMP/.planning/fable-fallback.json"
  # and the config was not rewritten either (marker-before-config ordering)
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-fable-5" ]
}

@test "FALLBACK-029: a malformed 'installed' is an unusable marker, not a silent CAS bypass" {
  # Round-3 HIGH. A present-but-non-dict "installed" was coerced to {}, which
  # silently disabled the compare-and-swap and let a stale original overwrite a
  # deliberate edit — degrading safety without saying anything.
  python3 -c "
import json
p='$TMP/.planning/config.json'
c=json.load(open(p)); c['model_overrides']['gsd-planner']='claude-sonnet-5'
json.dump(c,open(p,'w'),indent=2)"
  echo '{"mode":"opus-only","paths":{"model_overrides.gsd-planner":"fable"},"installed":[]}' > "$TMP/.planning/fable-fallback.json"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"WARN"* ]]
  # the deliberate edit survives — pre-fix it was overwritten with "fable"
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-sonnet-5" ]
}

@test "FALLBACK-030: zero rewrites must not claim a fallback mode or name a missing marker" {
  # Round-3 MEDIUM, a direct consequence of the dot-key guard: the shell printed
  # "fallback mode ... (marker: ...)" unconditionally, so a run that rewrote
  # NOTHING and wrote NO marker still read as a successful fallback while the
  # unavailable pin was in fact still live.
  echo '{ "group.a": { "model": "fable" } }' > "$TMP/.planning/config.json"
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"0 override(s) rewritten"* ]]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
  [[ "$output" == *"NO fallback marker written"* ]]
  [[ "$output" != *"fallback mode:"* ]]
  # the pin really is still live, which is what the message now admits
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['group.a']['model'])")" = "fable" ]
}

@test "FALLBACK-026: the dot-key guard skips the whole subtree, and says so" {
  # Documented consequence, pinned so it cannot change silently: a pin nested
  # UNDER a dot-bearing key is also left un-substituted. Safe direction (a live
  # fable pin is loud and recoverable) but it must not surprise a reader.
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "group.a": { "nested": { "model": "claude-fable-5" } },
  "model_overrides": { "gsd-planner": "fable" }
}
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"group.a"* ]]
  # nested pin survives un-substituted, and is NOT recorded in the marker
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['group.a']['nested']['model'])")" = "claude-fable-5" ]
  [ "$(python3 -c "import json;print(sorted(json.load(open('$TMP/.planning/fable-fallback.json'))['paths']))")" = "['model_overrides.gsd-planner']" ]
}
