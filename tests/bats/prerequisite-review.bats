#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

@test "PRH001 binds the resolved fallback tuple at the managed host-request authority seam" {
  root="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  run env PYTHONPATH="$root/lib" python3 -m pytest -q \
    "$root/tests/test_host_request.py::test_codex_host_request_resolves_tier_without_accepting_argv" \
    "$root/tests/test_managed_qualification.py::test_helper_launches_only_four_supervised_probes_then_promotes_and_receipts"
  [ "$status" -eq 0 ]
}
