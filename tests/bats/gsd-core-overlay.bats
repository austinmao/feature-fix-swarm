#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  OVERLAY="$ROOT/scripts/gsd/apply-gsd-core-overlay.py"
  TOOLS="$ROOT/node_modules/@opengsd/gsd-core/gsd-core/bin/gsd-tools.cjs"
}

record() {
  node - "$1" "$2" <<'NODE'
const fs = require("fs");
fs.writeFileSync(process.argv[2], JSON.stringify({
  command: "npx vitest run", exitCode: 1,
  targetTest: "charges buyer the configured USD micro-cap",
  targetFile: "budget.test.ts", output: process.argv[3],
}));
NODE
}

pytest_record() {
  node - "$1" "$2" "$3" "$4" <<'NODE'
const fs = require("fs");
fs.writeFileSync(process.argv[2], JSON.stringify({
  command: "python3 -m pytest -q", exitCode: 1,
  targetTest: process.argv[3], targetFile: process.argv[4],
  output: process.argv[5],
}));
NODE
}

@test "exact-pin overlay accepts truthful Vitest nested TAP RED without a Node summary" {
  run python3 "$OVERLAY" verify --repo "$ROOT"
  [ "$status" -eq 0 ]
  REC="$BATS_TEST_TMPDIR/nested.json"
  record "$REC" $'TAP version 13\n1..1\nnot ok 1 - budget.test.ts {\n    1..5\n    not ok 1 - charges buyer the configured USD micro-cap\n    not ok 2 - reserve failure\n    not ok 3 - settle failure\n    not ok 4 - release failure\n    not ok 5 - cap failure\n}\n'
  run node "$TOOLS" check tdd-red-evidence "$REC" --raw
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict": "RED_EVIDENCE_OK"'* ]]
}

@test "exact-pin overlay accepts the real two-level Vitest 4 TAP hierarchy" {
  REC="$BATS_TEST_TMPDIR/two-level.json"
  record "$REC" $'TAP version 13\n1..1\nnot ok 1 - budget.test.ts # time=7.21ms {\n    1..1\n    not ok 1 - budget static authority assertions # time=6.55ms {\n        1..5\n        not ok 1 - charges buyer the configured USD micro-cap # time=4.36ms\n        not ok 2 - reserve failure # time=0.57ms\n        not ok 3 - settle failure # time=0.44ms\n        not ok 4 - release failure # time=0.38ms\n        not ok 5 - cap failure # time=0.39ms\n    }\n}\n'
  run node "$TOOLS" check tdd-red-evidence "$REC" --raw
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict": "RED_EVIDENCE_OK"'* ]]
}

@test "exact-pin overlay accepts a collected named pytest failure, including an asserted missing module" {
  REC="$BATS_TEST_TMPDIR/pytest-red.json"
  pytest_record "$REC" "test_imports_missing_media_module" "test_media_plan_store.py" $'============================= test session starts ==============================\ncollected 1 item\n\ntests/test_media_plan_store.py::test_imports_missing_media_module FAILED [100%]\n\n=================================== FAILURES ===================================\n______________________ test_imports_missing_media_module ______________________\n\n    def test_imports_missing_media_module():\n>       import media_plan_store\nE       ModuleNotFoundError: No module named \'media_plan_store\'\n\ntests/test_media_plan_store.py:4: ModuleNotFoundError\n=========================== short test summary info ============================\nFAILED tests/test_media_plan_store.py::test_imports_missing_media_module - ModuleNotFoundError: No module named \'media_plan_store\'\n============================== 1 failed in 0.04s ===============================\n'
  run node "$TOOLS" check tdd-red-evidence "$REC" --raw
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict": "RED_EVIDENCE_OK"'* ]]
}

@test "exact-pin overlay accepts pytest collection progress followed by the verified item count" {
  REC="$BATS_TEST_TMPDIR/pytest-collection-progress.json"
  pytest_record "$REC" "test_imports_missing_media_module" "test_media_plan_store.py" $'============================= test session starts ==============================\ncollecting ... collected 1 item\n\ntests/test_media_plan_store.py::test_imports_missing_media_module FAILED [100%]\n\n=================================== FAILURES ===================================\nE       ModuleNotFoundError: No module named \'media_plan_store\'\n=========================== short test summary info ============================\nFAILED tests/test_media_plan_store.py::test_imports_missing_media_module - ModuleNotFoundError: No module named \'media_plan_store\'\n============================== 1 failed in 0.04s ===============================\n'
  run node "$TOOLS" check tdd-red-evidence "$REC" --raw
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict": "RED_EVIDENCE_OK"'* ]]
}

@test "pytest adapter rejects collection/config errors, zero collection, wrong-file same-name, unrelated, and malformed summaries" {
  for CASE in collection config zero wrong_file unrelated malformed; do
    REC="$BATS_TEST_TMPDIR/pytest-$CASE.json"
    case "$CASE" in
      collection) DATA=$'============================= test session starts ==============================\ncollected 0 items / 1 error\n\n==================================== ERRORS ====================================\n___________ ERROR collecting tests/test_media_plan_store.py ___________\nImportError while importing test module\n=========================== short test summary info ============================\nERROR tests/test_media_plan_store.py\n!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n=============================== 1 error in 0.04s ===============================\n' ;;
      config) DATA=$'ERROR: usage: pytest [options] [file_or_dir] [file_or_dir] [...]\npytest: error: unrecognized arguments: --bad-flag\n  inifile: None\n  rootdir: /tmp\n' ;;
      zero) DATA=$'============================= test session starts ==============================\ncollected 0 items\n\n============================ no tests ran in 0.01s =============================\n' ;;
      wrong_file) DATA=$'============================= test session starts ==============================\ncollected 1 item\n\ntests/test_other_store.py::test_imports_missing_media_module FAILED [100%]\n\n=================================== FAILURES ===================================\n______________________ test_imports_missing_media_module ______________________\n\nE       AssertionError\n=========================== short test summary info ============================\nFAILED tests/test_other_store.py::test_imports_missing_media_module - AssertionError\n============================== 1 failed in 0.04s ===============================\n' ;;
      unrelated) DATA=$'============================= test session starts ==============================\ncollected 1 item\n\ntests/test_media_plan_store.py::test_other_case FAILED [100%]\n\n=================================== FAILURES ===================================\n_______________________________ test_other_case _______________________________\n\nE       AssertionError\n=========================== short test summary info ============================\nFAILED tests/test_media_plan_store.py::test_other_case - AssertionError\n============================== 1 failed in 0.04s ===============================\n' ;;
      malformed) DATA=$'============================= test session starts ==============================\ncollected 1 item\n\ntests/test_media_plan_store.py::test_imports_missing_media_module FAILED [100%]\n=========================== short test summary info ============================\nFAILED tests/test_media_plan_store.py::test_imports_missing_media_module - ModuleNotFoundError\n' ;;
    esac
    pytest_record "$REC" "test_imports_missing_media_module" "test_media_plan_store.py" "$DATA"
    run node "$TOOLS" check tdd-red-evidence "$REC" --raw
    [ "$status" -eq 0 ]
    [[ "$output" == *'"verdict": "INVALID_RED"'* ]]
  done
}

@test "pytest adapter binds directory-qualified targets by normalized path, not basename" {
  TARGET="src/tests/test_media_plan_store.py"
  BAD="$BATS_TEST_TMPDIR/pytest-same-basename-wrong-directory.json"
  pytest_record "$BAD" "test_imports_missing_media_module" "$TARGET" $'============================= test session starts ==============================\ncollected 1 item\n\nother/tests/test_media_plan_store.py::test_imports_missing_media_module FAILED [100%]\n\n=================================== FAILURES ===================================\nE       AssertionError\n=========================== short test summary info ============================\nFAILED other/tests/test_media_plan_store.py::test_imports_missing_media_module - AssertionError\n============================== 1 failed in 0.04s ===============================\n'
  run node "$TOOLS" check tdd-red-evidence "$BAD" --raw
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict": "INVALID_RED"'* ]]

  GOOD="$BATS_TEST_TMPDIR/pytest-normalized-target.json"
  pytest_record "$GOOD" "test_imports_missing_media_module" "$TARGET" $'============================= test session starts ==============================\ncollected 1 item\n\nsrc/./tests/../tests/test_media_plan_store.py::test_imports_missing_media_module FAILED [100%]\n\n=================================== FAILURES ===================================\nE       AssertionError\n=========================== short test summary info ============================\nFAILED src/./tests/../tests/test_media_plan_store.py::test_imports_missing_media_module - AssertionError\n============================== 1 failed in 0.04s ===============================\n'
  run node "$TOOLS" check tdd-red-evidence "$GOOD" --raw
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict": "RED_EVIDENCE_OK"'* ]]

  WINDOWS="$BATS_TEST_TMPDIR/pytest-windows-normalized-target.json"
  pytest_record "$WINDOWS" "test_imports_missing_media_module" $'src\\tests\\test_media_plan_store.py' $'============================= test session starts ==============================\ncollected 1 item\n\nsrc/tests/./test_media_plan_store.py::test_imports_missing_media_module FAILED [100%]\n\n=================================== FAILURES ===================================\nE       AssertionError\n=========================== short test summary info ============================\nFAILED src/tests/./test_media_plan_store.py::test_imports_missing_media_module - AssertionError\n============================== 1 failed in 0.04s ===============================\n'
  run node "$TOOLS" check tdd-red-evidence "$WINDOWS" --raw
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict": "RED_EVIDENCE_OK"'* ]]
}

@test "nested TAP adapter rejects zero, malformed, every-level plan mismatch, directives, loader, and unrelated evidence" {
  for CASE in zero malformed count number root_count root_number level_one_count level_two_number todo skip root_todo root_skip loader unrelated; do
    REC="$BATS_TEST_TMPDIR/$CASE.json"
    case "$CASE" in
      zero) DATA=$'TAP version 13\n1..0\n' ;;
      malformed) DATA=$'TAP version 13\nnot ok 1 - budget.test.ts\n1..1\n' ;;
      count) DATA=$'TAP version 13\n# Subtest: budget.test.ts\n    1..2\n    not ok 1 - charges buyer the configured USD micro-cap\nnot ok 1 - budget.test.ts\n1..1\n' ;;
      number) DATA=$'TAP version 13\n# Subtest: budget.test.ts\n    1..2\n    not ok 1 - charges buyer the configured USD micro-cap\n    not ok 1 - reserve failure\nnot ok 1 - budget.test.ts\n1..1\n' ;;
      root_count) DATA=$'TAP version 13\n# Subtest: budget.test.ts\n    1..1\n    not ok 1 - charges buyer the configured USD micro-cap\nnot ok 1 - budget.test.ts\n1..2\n' ;;
      root_number) DATA=$'TAP version 13\n# Subtest: budget.test.ts\n    1..1\n    not ok 1 - charges buyer the configured USD micro-cap\nnot ok 2 - budget.test.ts\n1..1\n' ;;
      level_one_count) DATA=$'TAP version 13\n1..1\nnot ok 1 - budget.test.ts {\n    1..2\n    not ok 1 - budget static authority assertions {\n        1..1\n        not ok 1 - charges buyer the configured USD micro-cap\n    }\n}\n' ;;
      level_two_number) DATA=$'TAP version 13\n1..1\nnot ok 1 - budget.test.ts {\n    1..1\n    not ok 1 - budget static authority assertions {\n        1..2\n        not ok 1 - charges buyer the configured USD micro-cap\n        not ok 1 - reserve failure\n    }\n}\n' ;;
      todo) DATA=$'TAP version 13\n# Subtest: budget.test.ts\n    1..1\n    not ok 1 - charges buyer the configured USD micro-cap # TODO pending provider\nnot ok 1 - budget.test.ts\n1..1\n' ;;
      skip) DATA=$'TAP version 13\n# Subtest: budget.test.ts\n    1..1\n    not ok 1 - charges buyer the configured USD micro-cap # SKIP unsupported runtime\nnot ok 1 - budget.test.ts\n1..1\n' ;;
      root_todo) DATA=$'TAP version 13\n# Subtest: budget.test.ts\n    1..1\n    not ok 1 - charges buyer the configured USD micro-cap\nnot ok 1 - budget.test.ts # TODO pending provider\n1..1\n' ;;
      root_skip) DATA=$'TAP version 13\n# Subtest: budget.test.ts\n    1..1\n    not ok 1 - charges buyer the configured USD micro-cap\nnot ok 1 - budget.test.ts # SKIP unsupported runtime\n1..1\n' ;;
      loader) DATA=$'TAP version 13\n# Subtest: budget.test.ts\nnot ok 1 - budget.test.ts\n  ---\n  error: Cannot find module config\n  ...\n1..1\n' ;;
      unrelated) DATA=$'TAP version 13\n# Subtest: budget.test.ts\n    1..1\n    not ok 1 - unrelated reserve test\nnot ok 1 - budget.test.ts\n1..1\n' ;;
    esac
    record "$REC" "$DATA"
    run node "$TOOLS" check tdd-red-evidence "$REC" --raw
    [ "$status" -eq 0 ]
    [[ "$output" == *'"verdict": "INVALID_RED"'* ]]
  done
}

@test "executor overlay permits a sequential linked-worktree branch but retains the isolated-worktree gate" {
  EXECUTOR="$ROOT/node_modules/@opengsd/gsd-core/agents/gsd-executor.md"
  grep -F 'workflow.use_worktrees --raw' "$EXECUTOR"
  grep -F 'skip this isolated-agent namespace check' "$EXECUTOR"

  mkdir -p "$BATS_TEST_TMPDIR/linked-worktree"
  touch "$BATS_TEST_TMPDIR/linked-worktree/.git"
  run bash -c '
    cd "$1"
    gsd_run() { printf "false\\n"; }
    if [ -f .git ] && [ "$(gsd_run query config-get workflow.use_worktrees --raw 2>/dev/null || echo true)" != "false" ]; then
      exit 99
    fi
  ' -- "$BATS_TEST_TMPDIR/linked-worktree"
  [ "$status" -eq 0 ]

  run bash -c '
    cd "$1"
    gsd_run() { printf "true\\n"; }
    if [ -f .git ] && [ "$(gsd_run query config-get workflow.use_worktrees --raw 2>/dev/null || echo true)" != "false" ]; then
      exit 99
    fi
  ' -- "$BATS_TEST_TMPDIR/linked-worktree"
  [ "$status" -eq 99 ]
}

@test "safe-resume overlay ignores same-scope history unless its paths intersect the active plan" {
  REPO="$BATS_TEST_TMPDIR/resume-repo"
  mkdir -p "$REPO/src" "$REPO/legacy" "$REPO/docs/guide" "$REPO/assets/images"
  git -C "$REPO" init -q
  git -C "$REPO" config user.email "ffs-test@example.invalid"
  git -C "$REPO" config user.name "FFS test"
  cat > "$REPO/03-04-PLAN.md" <<'PLAN'
---
files_modified: [./src/current.py, docs/guide/**, "assets\\images\\"]
files_deleted:
  - legacy/obsolete.py
---
PLAN
  printf 'unrelated\n' > "$REPO/src/unrelated.py"
  printf 'obsolete\n' > "$REPO/legacy/obsolete.py"
  git -C "$REPO" add src/unrelated.py legacy/obsolete.py
  git -C "$REPO" commit -qm 'chore: seed paths'

  printf 'still unrelated\n' >> "$REPO/src/unrelated.py"
  git -C "$REPO" add src/unrelated.py
  git -C "$REPO" commit -qm 'feat(03-04): unrelated prior milestone work'
  UNRELATED=$(git -C "$REPO" rev-parse HEAD)

  printf 'current\n' > "$REPO/src/current.py"
  git -C "$REPO" add src/current.py
  git -C "$REPO" commit -qm 'feat(03-04): current plan partial implementation'
  CURRENT=$(git -C "$REPO" rev-parse HEAD)

  git -C "$REPO" rm -q legacy/obsolete.py
  git -C "$REPO" commit -qm 'feat(03-04): remove obsolete current-plan file'
  DELETED=$(git -C "$REPO" rev-parse HEAD)

  run python3 - "$OVERLAY" "$REPO/03-04-PLAN.md" "$UNRELATED" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("overlay", sys.argv[1])
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)
scope = {"__name__": "matcher"}
exec(overlay.SAFE_RESUME_PATH_MATCHER, scope)
os.chdir(os.path.dirname(sys.argv[2]))
declared = scope["parse_declared_paths"](sys.argv[2])
raise SystemExit(0 if any(scope["path_matches"](path, declared) for path in scope["changed_paths_for_commit"](sys.argv[3])) else 1)
PY
  [ "$status" -eq 1 ]

  for COMMIT in "$CURRENT" "$DELETED"; do
    run python3 - "$OVERLAY" "$REPO/03-04-PLAN.md" "$COMMIT" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("overlay", sys.argv[1])
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)
scope = {"__name__": "matcher"}
exec(overlay.SAFE_RESUME_PATH_MATCHER, scope)
os.chdir(os.path.dirname(sys.argv[2]))
declared = scope["parse_declared_paths"](sys.argv[2])
raise SystemExit(0 if any(scope["path_matches"](path, declared) for path in scope["changed_paths_for_commit"](sys.argv[3])) else 1)
PY
    [ "$status" -eq 0 ]
  done

  run python3 - "$OVERLAY" "$REPO/03-04-PLAN.md" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("overlay", sys.argv[1])
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)
scope = {"__name__": "matcher"}
exec(overlay.SAFE_RESUME_PATH_MATCHER, scope)
declared = scope["parse_declared_paths"](sys.argv[2])
assert scope["path_matches"]("docs/guide/nested/page.md", declared)
assert scope["path_matches"]("assets/images/hero.jpg", declared)
assert not scope["path_matches"]("assets/images-old/hero.jpg", declared)
PY
  [ "$status" -eq 0 ]
}

@test "safe-resume matcher exits 2 (fail closed) on an unparseable plan or unknown commit" {
  REPO="$BATS_TEST_TMPDIR/resume-bad-repo"
  mkdir -p "$REPO"
  git -C "$REPO" init -q
  git -C "$REPO" config user.email "ffs-test@example.invalid"
  git -C "$REPO" config user.name "FFS test"
  printf 'seed\n' > "$REPO/seed.txt"
  git -C "$REPO" add seed.txt
  git -C "$REPO" commit -qm 'feat(03-04): seed'
  SHA=$(git -C "$REPO" rev-parse HEAD)
  printf 'no frontmatter here\n' > "$REPO/03-04-PLAN.md"
  printf -- '---\nfiles_modified: [seed.txt]\n---\n' > "$REPO/03-04-GOOD-PLAN.md"

  for CASE in "$REPO/03-04-PLAN.md $SHA" "$REPO/03-04-GOOD-PLAN.md 0000000000000000000000000000000000000000" "$REPO/missing-PLAN.md $SHA"; do
    set -- $CASE
    run python3 - "$OVERLAY" "$1" "$2" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("overlay", sys.argv[1])
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)
scope = {"__name__": "matcher"}
exec(overlay.SAFE_RESUME_PATH_MATCHER, scope)
os.chdir(os.path.dirname(sys.argv[2]))
sys.argv = ["safe-resume-path-matcher", sys.argv[2], sys.argv[3]]
scope["main"]()
PY
    [ "$status" -eq 2 ]
  done

  run python3 - "$OVERLAY" "$REPO/03-04-GOOD-PLAN.md" "$SHA" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("overlay", sys.argv[1])
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)
scope = {"__name__": "matcher"}
exec(overlay.SAFE_RESUME_PATH_MATCHER, scope)
os.chdir(os.path.dirname(sys.argv[2]))
sys.argv = ["safe-resume-path-matcher", sys.argv[2], sys.argv[3]]
scope["main"]()
PY
  [ "$status" -eq 0 ]
}

@test "overlay rolls back earlier replacements if a later target replacement fails" {
  run python3 - "$OVERLAY" "$BATS_TEST_TMPDIR" <<'PY'
import importlib.util, os, stat, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("overlay", sys.argv[1])
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)
root = Path(sys.argv[2]) / "rollback"
root.mkdir()
first, second = root / "first", root / "second"
first.write_bytes(b"first-before")
second.write_bytes(b"second-before")
first.chmod(0o640)
second.chmod(0o600)
third = root / "third"
third.write_bytes(b"third-before")
third.chmod(0o644)
before = [(path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in (first, second, third)]
error = overlay.replace_pending([
    (first, before[0][0], b"first-after", before[0][1], "first"),
    (second, before[1][0], b"second-after", before[1][1], "second"),
    (third, before[2][0], b"third-after", before[2][1], "third"),
], "third")
assert error and "prior targets restored" in error
assert [(path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in (first, second, third)] == before
PY
  [ "$status" -eq 0 ]
}

@test "overlay verifier rejects package-version, base, and patched-digest mismatch" {
  FIX="$BATS_TEST_TMPDIR/fixture"
  TARGET="$FIX/node_modules/@opengsd/gsd-core/gsd-core/bin/lib/tdd-red-evidence.cjs"
  EXECUTOR="$FIX/node_modules/@opengsd/gsd-core/agents/gsd-executor.md"
  RESUME="$FIX/node_modules/@opengsd/gsd-core/gsd-core/workflows/execute-phase.md"
  mkdir -p "$(dirname "$TARGET")" "$(dirname "$EXECUTOR")" "$(dirname "$RESUME")" "$FIX/patches"
  cp "$ROOT/patches/gsd-core-overlay.json" "$FIX/patches/"
  cp "$ROOT/node_modules/@opengsd/gsd-core/gsd-core/bin/lib/tdd-red-evidence.cjs" "$TARGET"
  cp "$ROOT/node_modules/@opengsd/gsd-core/agents/gsd-executor.md" "$EXECUTOR"
  cp "$ROOT/node_modules/@opengsd/gsd-core/gsd-core/workflows/execute-phase.md" "$RESUME"
  printf '{"name":"@opengsd/gsd-core","version":"1.13.1"}\n' > "$FIX/node_modules/@opengsd/gsd-core/package.json"
  run python3 "$OVERLAY" verify --repo "$FIX"
  [ "$status" -eq 78 ]
  [[ "$output" == *"exact @opengsd/gsd-core@1.13.0"* ]]

  printf '{"name":"@opengsd/gsd-core","version":"1.13.0"}\n' > "$FIX/node_modules/@opengsd/gsd-core/package.json"
  printf 'untrusted drift\n' > "$TARGET"
  run python3 "$OVERLAY" apply --repo "$FIX"
  [ "$status" -eq 78 ]
  [[ "$output" == *"base digest"* ]]

  cp "$ROOT/node_modules/@opengsd/gsd-core/gsd-core/bin/lib/tdd-red-evidence.cjs" "$TARGET"
  BOGUS_PATCHED_SHA="$(printf '0%.0s' {1..64})"  # runtime-built: AC-011 hex-run gate stays quiet
  python3 - "$FIX/patches/gsd-core-overlay.json" "$BOGUS_PATCHED_SHA" <<'PY'
import json, sys
path, bogus = sys.argv[1:]
manifest = json.load(open(path))
manifest["targets"][0]["patched_sha256"] = bogus
open(path, "w").write(json.dumps(manifest))
PY
  run python3 "$OVERLAY" verify --repo "$FIX"
  [ "$status" -eq 78 ]
  [[ "$output" == *"digest mismatch"* ]]

  cp "$ROOT/patches/gsd-core-overlay.json" "$FIX/patches/"
  printf 'untrusted executor drift\n' > "$EXECUTOR"
  run python3 "$OVERLAY" verify --repo "$FIX"
  [ "$status" -eq 78 ]
  [[ "$output" == *"agents/gsd-executor.md"* ]]

  cp "$ROOT/patches/gsd-core-overlay.json" "$FIX/patches/"
  cp "$ROOT/node_modules/@opengsd/gsd-core/agents/gsd-executor.md" "$EXECUTOR"
  printf 'untrusted safe resume drift\n' > "$RESUME"
  run python3 "$OVERLAY" verify --repo "$FIX"
  [ "$status" -eq 78 ]
  [[ "$output" == *"gsd-core/workflows/execute-phase.md"* ]]
}
