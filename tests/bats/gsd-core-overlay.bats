#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

# node_modules stays pristine under 1.14 (the installer overlays a staged
# copy), so every behavioural test runs against one overlaid staged copy.
setup_file() {
  local root
  root="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  cp -R "$root/node_modules/@opengsd/gsd-core" "$BATS_FILE_TMPDIR/gsd-core"
  python3 "$root/scripts/gsd/apply-gsd-core-overlay.py" apply --repo "$root" \
    --package-root "$BATS_FILE_TMPDIR/gsd-core"
}

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  OVERLAY="$ROOT/scripts/gsd/apply-gsd-core-overlay.py"
  PKG="$BATS_FILE_TMPDIR/gsd-core"
  TOOLS="$PKG/gsd-core/bin/gsd-tools.cjs"
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
  run python3 "$OVERLAY" verify --repo "$ROOT" --package-root "$PKG"
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
  EXECUTOR="$PKG/agents/gsd-executor.md"
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
raise SystemExit(0 if any(scope["path_matches"](path, declared) for path in scope["changed_paths_for_commit"](sys.argv[3])) else 3)
PY
  [ "$status" -eq 3 ]

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
raise SystemExit(0 if any(scope["path_matches"](path, declared) for path in scope["changed_paths_for_commit"](sys.argv[3])) else 3)
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
assert scope["path_matches"]("docs/guide/page.md", declared)
assert scope["path_matches"]("assets/images/hero.jpg", declared)
assert not scope["path_matches"]("assets/images-old/hero.jpg", declared)
assert scope["normalize_path"]("src\\one\\two.py") == "src/one/two.py"
assert scope["normalize_path"]("\\\\server\\share\\x") is None
assert scope["path_matches"]("src/a.py", [("src/**/*.py", False)])
assert not scope["path_matches"]("src/pkg/a.py", [("src/*.py", False)])
assert scope["normalize_path"]("C:\\repo\\x.py") is None
import subprocess
scope["subprocess"].check_output = lambda *a, **k: b"C100\x00src/pkg/a.py\x00src/copy.py\x00M\x00other.txt\x00"
assert scope["changed_paths_for_commit"]("deadbeef") == ["src/pkg/a.py", "src/copy.py", "other.txt"]
import tempfile, os
with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
    fh.write("---\nfiles_modified :\n  - a.py\n\n  # later entries survive blank and comment lines\n  - later/b.py\n---\n")
try:
    assert scope["parse_declared_paths"](fh.name) == [("a.py", False), ("later/b.py", False)]
finally:
    os.unlink(fh.name)
with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
    fh.write('---\n"files_modified": [a.py]\nfiles_deleted: [b.py]\n---\n')
try:
    assert scope["parse_declared_paths"](fh.name) == [("a.py", False), ("b.py", False)]
finally:
    os.unlink(fh.name)
for bad in ('files_modified: [a.py, "b.py]', 'files_modified: ["a.py"junk]', "files_modified:\n  - a.py\n  b.py", "files_modified: [../escape.py]", "files_modified: src/a.py # c", "files_modified:\n  - \"src/a.py\"junk", " files_modified: [a.py]\nfiles_deleted: [b.py]", "'files_modified\": [a.py]\nfiles_deleted: [b.py]", "files_modified_extra: [a.py]"):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write("---\n" + bad + "\n---\n")
    try:
        scope["parse_declared_paths"](fh.name)
        raise AssertionError("accepted malformed declaration: " + bad)
    except ValueError:
        pass
    finally:
        os.unlink(fh.name)
PY
  [ "$status" -eq 0 ]
}

@test "safe-resume gate runs the RENDERED heredoc verbatim: 0 intersect, 1 unrelated, 2 malformed plan" {
  # R-1/R-5 (PR #171 review): execute the exact Python the rendered workflow
  # embeds, not the source constant, so quoting drift in the renderer is caught.
  REPO="$BATS_TEST_TMPDIR/rendered-repo"
  mkdir -p "$REPO/src/pkg"
  git -C "$REPO" init -q
  git -C "$REPO" config user.email "ffs-test@example.invalid"
  git -C "$REPO" config user.name "FFS test"
  printf 'a\n' > "$REPO/src/pkg/a.py"
  printf 'b\n' > "$REPO/src/b.py"
  printf 'c\n' > "$REPO/other.txt"
  git -C "$REPO" add src other.txt
  git -C "$REPO" commit -qm 'feat(03-04): seed'
  SEED=$(git -C "$REPO" rev-parse HEAD)
  git -C "$REPO" mv src/b.py src/renamed.py
  git -C "$REPO" commit -qm 'feat(03-04): rename'
  RENAMED=$(git -C "$REPO" rev-parse HEAD)
  printf 'more\n' >> "$REPO/other.txt"
  git -C "$REPO" add other.txt
  git -C "$REPO" commit -qm 'feat(03-04): unrelated'
  UNRELATED=$(git -C "$REPO" rev-parse HEAD)
  git -C "$REPO" checkout -q -b side
  printf 'side\n' > "$REPO/src/pkg/side.py"
  git -C "$REPO" add src/pkg/side.py
  git -C "$REPO" commit -qm 'chore: side work'
  git -C "$REPO" checkout -q -
  git -C "$REPO" merge -q --no-ff -m 'feat(03-04): merge side' side
  MERGED=$(git -C "$REPO" rev-parse HEAD)
  cat > "$REPO/03-04-PLAN.md" <<'PLAN'
---
files_modified:
  - src/**/*.py
  - "src\renamed.py"
---
PLAN
  printf -- '---\nfiles_modified: [src/a.py, src/b.py\n---\n' > "$REPO/03-04-BAD-PLAN.md"
  printf -- '---\nfiles_modified: [*.txt]\n---\n' > "$REPO/03-04-TOP-PLAN.md"

  python3 - "$OVERLAY" "$PKG/gsd-core/workflows/execute-phase.md" "$BATS_TEST_TMPDIR/rendered-matcher.py" <<'PY'
import importlib.util, re, sys
spec = importlib.util.spec_from_file_location("overlay", sys.argv[1])
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)
installed = open(sys.argv[2], "rb").read()
try:
    rendered = overlay.render_execute_phase_safe_resume(installed).decode("utf-8")
except ValueError:
    # node_modules already carries the applied overlay (local dev after
    # `apply`); the rendered gate IS the installed file then.
    rendered = installed.decode("utf-8")
match = re.search(r"<<'PY'\n(.*?)\nPY\n", rendered, re.S)
assert match, "rendered gate has no quoted PY heredoc"
assert match.group(1) == overlay.SAFE_RESUME_PATH_MATCHER.rstrip(), "heredoc is not the verbatim matcher"
compile(match.group(1), "rendered", "exec")
open(sys.argv[3], "w").write(match.group(1))
PY
  MATCHER="$BATS_TEST_TMPDIR/rendered-matcher.py"
  cd "$REPO"
  run python3 "$MATCHER" 03-04-PLAN.md "$SEED"
  [ "$status" -eq 0 ]
  run python3 "$MATCHER" 03-04-PLAN.md "$RENAMED"
  [ "$status" -eq 0 ]
  # a scoped MERGE commit that brings in a declared path intersects (first-parent diff)
  run python3 "$MATCHER" 03-04-PLAN.md "$MERGED"
  [ "$status" -eq 0 ]
  run python3 "$MATCHER" 03-04-PLAN.md "$UNRELATED"
  [ "$status" -eq 3 ]
  # a traversal declaration is refused (2), never silently unrelated
  printf -- '---\nfiles_modified: [../escape.py]\n---\n' > 03-04-TRAV-PLAN.md
  run python3 "$MATCHER" 03-04-TRAV-PLAN.md "$SEED"
  [ "$status" -eq 2 ]
  run python3 "$MATCHER" 03-04-TRAV-PLAN.md --validate
  [ "$status" -eq 2 ]
  run python3 "$MATCHER" 03-04-PLAN.md --validate
  [ "$status" -eq 0 ]
  # trailing junk on a block entry is malformed, not a path that never matches
  printf -- '---\nfiles_modified:\n  - src/pkg/a.py # touched\n---\n' > 03-04-JUNK-PLAN.md
  run python3 "$MATCHER" 03-04-JUNK-PLAN.md "$SEED"
  [ "$status" -eq 2 ]
  run python3 "$MATCHER" 03-04-BAD-PLAN.md "$SEED"
  [ "$status" -eq 2 ]
  # * never crosses a directory: top-level *.txt matches other.txt, not src/pkg/a.py
  run python3 "$MATCHER" 03-04-TOP-PLAN.md "$UNRELATED"
  [ "$status" -eq 0 ]
  run python3 "$MATCHER" 03-04-TOP-PLAN.md "$RENAMED"
  [ "$status" -eq 3 ]
}

@test "rendered safe-resume gate bash: ignores only matcher exit 3, refuses 1/2 and a failing git log" {
  REPO="$BATS_TEST_TMPDIR/gate-repo"
  mkdir -p "$REPO/.planning/phases/03-x" "$BATS_TEST_TMPDIR/stub"
  git -C "$REPO" init -q
  git -C "$REPO" config user.email "ffs-test@example.invalid"
  git -C "$REPO" config user.name "FFS test"
  printf -- '---\nfiles_modified: [seed.txt]\n---\n' > "$REPO/.planning/phases/03-x/04-PLAN.md"
  printf 'seed\n' > "$REPO/seed.txt"
  git -C "$REPO" add seed.txt .planning
  git -C "$REPO" commit -qm 'feat(03-04): seed'
  # the rendered bash block, with gsd's template placeholders filled
  awk '/<step name="safe_resume_gate">/{f=1} f&&/^```bash/{g=1;next} f&&g&&/^```/{exit} f&&g' \
    "$PKG/gsd-core/workflows/execute-phase.md" \
    | sed 's|{phase_dir}|.planning/phases/03-x|g; s|{plan_padded}|04|g; s|{phase_number}|03|g' \
    > "$BATS_TEST_TMPDIR/gate.sh"
  grep -q 'PLAN_COMMIT_LIST=' "$BATS_TEST_TMPDIR/gate.sh"
  REAL_GIT="$(command -v git)"
  cat > "$BATS_TEST_TMPDIR/stub/python3" <<'STUB'
#!/usr/bin/env bash
case " $* " in *" --validate "*) exit "${STUB_VALIDATE_STATUS:-0}" ;; esac
exit "${STUB_MATCHER_STATUS:-3}"
STUB
  cat > "$BATS_TEST_TMPDIR/stub/git" <<STUB
#!/usr/bin/env bash
if [ "\${STUB_GIT_LOG_FAILS:-0}" = 1 ] && [ "\$1" = log ]; then exit 128; fi
exec "$REAL_GIT" "\$@"
STUB
  chmod +x "$BATS_TEST_TMPDIR/stub/python3" "$BATS_TEST_TMPDIR/stub/git"
  cd "$REPO"
  run env PATH="$BATS_TEST_TMPDIR/stub:$PATH" STUB_MATCHER_STATUS=3 bash "$BATS_TEST_TMPDIR/gate.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ignoring unrelated scoped commit"* ]]
  run env PATH="$BATS_TEST_TMPDIR/stub:$PATH" STUB_MATCHER_STATUS=0 bash -c ". $BATS_TEST_TMPDIR/gate.sh; printf '%s' \"\$PLAN_COMMITS\""
  [ "$status" -eq 0 ]
  [[ "$output" == *"feat(03-04): seed"* ]]
  for BAD in 1 2; do
    run env PATH="$BATS_TEST_TMPDIR/stub:$PATH" STUB_MATCHER_STATUS="$BAD" bash "$BATS_TEST_TMPDIR/gate.sh"
    [ "$status" -eq 1 ]
    [[ "$output" == *"refusing dispatch"* ]]
  done
  run env PATH="$BATS_TEST_TMPDIR/stub:$PATH" STUB_GIT_LOG_FAILS=1 bash "$BATS_TEST_TMPDIR/gate.sh"
  [ "$status" -eq 1 ]
  [[ "$output" == *"git log failed"* ]]
  # a malformed declaration refuses BEFORE any candidate is considered
  run env PATH="$BATS_TEST_TMPDIR/stub:$PATH" STUB_VALIDATE_STATUS=2 bash "$BATS_TEST_TMPDIR/gate.sh"
  [ "$status" -eq 1 ]
  [[ "$output" == *"cannot parse declared paths"* ]]
  # ...and with the REAL matcher, a malformed plan and zero scoped commits still refuse
  printf -- '---\nfiles_modified: [seed.txt\n---\n' > "$REPO/.planning/phases/03-x/04-PLAN.md"
  git -C "$REPO" commit -qam 'chore: unscoped'
  run bash "$BATS_TEST_TMPDIR/gate.sh"
  [ "$status" -eq 1 ]
  [[ "$output" == *"cannot parse declared paths"* ]]
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

  for CASE in "$REPO/03-04-PLAN.md $SHA" "$REPO/03-04-GOOD-PLAN.md no-such-commit" "$REPO/missing-PLAN.md $SHA"; do
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
before = [(path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in (first, second)]
third_before = (third.read_bytes(), stat.S_IMODE(third.stat().st_mode))
error = overlay.replace_pending([
    (first, before[0][0], b"first-after", before[0][1], "first"),
    (second, before[1][0], b"second-after", before[1][1], "second"),
    (third, third_before[0], b"third-after", third_before[1], "third"),
], "third")
assert error and "prior targets restored" in error
assert [(path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in (first, second)] == before
assert (third.read_bytes(), stat.S_IMODE(third.stat().st_mode)) == third_before
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
  printf '{"name":"@opengsd/gsd-core","version":"1.14.1"}\n' > "$FIX/node_modules/@opengsd/gsd-core/package.json"
  run python3 "$OVERLAY" verify --repo "$FIX"
  [ "$status" -eq 78 ]
  [[ "$output" == *"exact @opengsd/gsd-core@1.14.0"* ]]

  printf '{"name":"@opengsd/gsd-core","version":"1.14.0"}\n' > "$FIX/node_modules/@opengsd/gsd-core/package.json"
  printf 'untrusted drift\n' > "$TARGET"
  run python3 "$OVERLAY" apply --repo "$FIX"
  [ "$status" -eq 78 ]
  [[ "$output" == *"base digest"* ]]

  cp "$PKG/gsd-core/bin/lib/tdd-red-evidence.cjs" "$TARGET"
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
  cp "$PKG/agents/gsd-executor.md" "$EXECUTOR"
  printf 'untrusted safe resume drift\n' > "$RESUME"
  run python3 "$OVERLAY" verify --repo "$FIX"
  [ "$status" -eq 78 ]
  [[ "$output" == *"gsd-core/workflows/execute-phase.md"* ]]
}
