#!/usr/bin/env bats
# Opt-in vendor-boundary contract (PR #88 follow-up, named in its own body):
# a real `codex --output-schema` round trip against the COMMITTED
# schemas/review-finding.schema.json. The array-root schema failed 100% of
# live codex dispatches for weeks while CI stayed green, because every suite
# stubs the vendor CLI — this is the one test that exercises the real wire.
#
# Opt-in like socratic-enum-drift.bats: skips when no codex CLI is on PATH
# (CI has none). On machines with codex installed it distinguishes the ONE
# failure it guards — OpenAI structured-outputs rejecting our schema — from
# environmental failures (auth, network, quota), which skip rather than
# false-fail.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCHEMA="$REPO_ROOT/schemas/review-finding.schema.json"
  LIB="$REPO_ROOT/scripts/gsd/adversary-host.sh"
  [ -f "$SCHEMA" ] || skip "review-finding schema missing"
  [ -f "$LIB" ] || skip "adversary-host.sh missing"
  command -v "${ADVERSARY_BIN_CODEX:-codex}" >/dev/null 2>&1 \
    || skip "codex CLI not installed (opt-in test)"
}

@test "codex accepts schemas/review-finding.schema.json as --output-schema and returns the object shape" {
  # Production dispatch path, not a hand-rolled invocation — the contract
  # under test is adversary_invoke's exact codex+schema wiring.
  run bash -c ". '$LIB'; adversary_invoke codex 120 gpt-5.6-luna low \
    'Return the empty findings object: {\"findings\":[]}. Output only JSON.' \
    '$SCHEMA' 2>&1"

  if [ "$status" -ne 0 ]; then
    # The regression this test exists to catch presents as the OpenAI API
    # rejecting the schema itself. Anything else (auth, network, quota,
    # unknown model) is environmental — skip, do not false-fail.
    if [[ "$output" == *"must be a JSON Schema"* ]] \
      || [[ "$output" == *"output-schema"* && "$output" == *"invalid"* ]] \
      || [[ "$output" == *"'required' is required"* ]]; then
      echo "SCHEMA REJECTED BY VENDOR:" >&2
      printf '%s\n' "$output" >&2
      false
    fi
    skip "codex present but call failed environmentally (rc=$status) — not a schema rejection"
  fi

  # rc=0: the returned payload must be the object root the schema mandates.
  printf '%s' "$output" | jq -e 'type == "object" and (.findings | type) == "array"' >/dev/null
}
