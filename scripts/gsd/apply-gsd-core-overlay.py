#!/usr/bin/env python3
"""Apply or verify exact-pin FFS overlays for @opengsd/gsd-core.

The package lives in ignored node_modules and npm ci replaces it.  This
small, deterministic overlay set is therefore the durable patch seam: it
admits one package version and byte-pinned upstream targets, atomically writes
reviewed transforms, then verifies every resulting digest on each check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


PACKAGE = "@opengsd/gsd-core"
VERSION = "1.13.0"
MANIFEST = Path("patches/gsd-core-overlay.json")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fail(message: str) -> int:
    print(f"gsd-core-overlay: {message}", file=os.sys.stderr)
    return 78


def render_tdd_red_evidence(source: bytes) -> bytes:
    old = br"""    const summary = (0, prohibition_enforcement_cjs_1.parseNodeTestSummary)(output);
    const failing = (0, prohibition_enforcement_cjs_1.tapFailedTestNames)(output);
    const evidence = {
        command,
        exit_code: exitCode,
        target_test: targetTest,
        tests: summary.tests,
        pass: summary.pass,
        fail: summary.fail,
        failing_tests: failing,
    };
"""
    new = br"""    const summary = (0, prohibition_enforcement_cjs_1.parseNodeTestSummary)(output);
    const topLevelFailing = (0, prohibition_enforcement_cjs_1.tapFailedTestNames)(output);
    // Vitest 4 can emit valid nested TAP without Node's '# tests/# fail'
    // trailer. Parse its brace/indent hierarchy rather than assuming one
    // suite level: every encountered plan must own exactly its numbered
    // assertions, every opened suite must close, and a failed child requires
    // a real failed enclosing result. This remains fail-closed for truncated
    // TAP, loader/import/config failures, and skipped/TODO tests.
    const nestedTap = (() => {
        if (!/^TAP version 13\s*$/m.test(output))
            return null;
        const root = { openerIndent: -1, group: null, closed: true };
        const stack = [root];
        const groups = [];
        let invalid = false;
        for (const line of output.split(/\r?\n/)) {
            let match = line.match(/^([\t ]*)}\s*$/);
            if (match) {
                const scope = stack.at(-1);
                if (scope === root || scope.openerIndent !== match[1].length)
                    invalid = true;
                else {
                    scope.closed = true;
                    stack.pop();
                }
                continue;
            }
            match = line.match(/^([\t ]*)1\.\.(\d+)\s*$/);
            if (match) {
                const scope = stack.at(-1);
                const planned = Number(match[2]);
                if (scope.group || !Number.isSafeInteger(planned) || planned < 1
                    || (scope !== root && match[1].length <= scope.openerIndent)
                    || (scope === root && match[1].length !== 0)) {
                    invalid = true;
                    continue;
                }
                scope.group = { indent: match[1].length, planned, assertions: [] };
                groups.push(scope.group);
                continue;
            }
            match = line.match(/^([\t ]*)(not )?ok (\d+) - (.+)$/);
            if (!match)
                continue;
            const scope = stack.at(-1);
            const group = scope.group;
            const number = Number(match[3]);
            if (!group || group.indent !== match[1].length || !Number.isSafeInteger(number)) {
                invalid = true;
                continue;
            }
            const assertion = { failed: Boolean(match[2]), number, rest: match[4], child: null };
            group.assertions.push(assertion);
            if (/\{\s*$/.test(assertion.rest)) {
                assertion.child = { openerIndent: match[1].length, group: null, closed: false };
                stack.push(assertion.child);
            }
        }
        if (invalid || stack.length !== 1 || !root.group)
            return null;
        for (const group of groups) {
            if (group.assertions.length !== group.planned
                || group.assertions.some((item) => item.number < 1 || item.number > group.planned))
                return null;
            const numbers = new Set(group.assertions.map((item) => item.number));
            if (numbers.size !== group.planned)
                return null;
            for (let number = 1; number <= group.planned; number++) {
                if (!numbers.has(number))
                    return null;
            }
            if (group.assertions.some((item) => item.child && (!item.child.closed || !item.child.group)))
                return null;
        }
        const isRealFailure = (item) => item.failed && !/\s#\s*(?:SKIP|TODO)\b/i.test(item.rest);
        const groupFailed = (group) => group.assertions.some((item) => {
            const childFailed = item.child ? groupFailed(item.child.group) : false;
            if (childFailed && !isRealFailure(item))
                invalid = true;
            return isRealFailure(item) || childFailed;
        });
        if (!groupFailed(root.group) || invalid)
            return null;
        const targetBase = baseOf(input?.targetFile ?? '');
        if (targetBase === '' || !root.group.assertions.some((item) => isRealFailure(item)
            && baseOf(item.rest.replace(/\s+#\s.*$/, '').replace(/\s*\{\s*$/, '').trim()) === targetBase))
            return null;
        // Filter directives before stripping diagnostics. A TODO/SKIP line is
        // not a failed test even if its remaining text happens to match the
        // target name.
        const failing = groups.flatMap((group) => group.assertions)
            .filter(isRealFailure)
            .map((item) => item.rest.replace(/\s+#\s.*$/, '').replace(/\s*\{\s*$/, '').trim())
            .filter(Boolean);
        if (failing.length === 0)
            return null;
        return { tests: root.group.planned, fail: failing.length, failing };
    })();
    // Pytest's normal terminal output is not TAP. Accept it only when its
    // collection line, per-item named outcomes, and complete short summary
    // agree exactly. In particular, collection/config errors, zero items,
    // interrupted output, and progress-only `-q` output cannot authorize
    // GREEN. A ModuleNotFoundError inside a collected, named FAILED test is a
    // real RED result; an import error during collection is not.
    const pytest = (() => {
        if (!/^=+ test session starts =+$/m.test(output))
            return null;
        const collection = [...output.matchAll(/^(?:collecting\b[^\r\n]*?\s+)?collected (\d+) items?(?:\s*\/\s*(\d+) errors?)?\s*$/gm)];
        if (collection.length !== 1)
            return null;
        const tests = Number(collection[0][1]);
        const collectionErrors = Number(collection[0][2] ?? 0);
        if (!Number.isSafeInteger(tests) || tests < 1 || collectionErrors !== 0
            || /(?:^|\n)(?:ERROR collecting|ERROR:|!+ Interrupted:|=+ ERRORS =+)/m.test(output))
            return null;
        const summary = [...output.matchAll(/^=+\s*(.*?)\s*=+\s*$/gm)];
        const terminal = summary.at(-1)?.[1] ?? '';
        const failed = terminal.match(/(?:^|,\s*)(\d+) failed\b/);
        if (!failed || /\b(?:error|errors|interrupted|no tests ran)\b/i.test(terminal)
            || !/\bin\s+\d+(?:\.\d+)?s\b/.test(terminal))
            return null;
        const expectedFailures = Number(failed[1]);
        if (!Number.isSafeInteger(expectedFailures) || expectedFailures < 1)
            return null;
        const normalizePath = (value) => {
            if (typeof value !== 'string' || value.length === 0)
                return '';
            const parts = [];
            for (const part of value.replace(/\\/g, '/').split('/')) {
                if (part === '' || part === '.')
                    continue;
                if (part === '..') {
                    if (parts.length === 0)
                        return '';
                    parts.pop();
                    continue;
                }
                parts.push(part);
            }
            return parts.join('/');
        };
        const results = [];
        for (const line of output.split(/\r?\n/)) {
            const match = line.match(/^(\S+)::(.+?)\s+(PASSED|FAILED|SKIPPED|XFAIL|XPASS)\b/);
            if (!match)
                continue;
            const file = match[1];
            const name = match[2].trim();
            if (!file || !name)
                return null;
            results.push({ file, name, failed: match[3] === 'FAILED' });
        }
        if (results.length !== tests || new Set(results.map((item) => `${item.file}::${item.name}`)).size !== tests)
            return null;
        const failing = results.filter((item) => item.failed);
        if (failing.length !== expectedFailures)
            return null;
        const targetName = targetTest.includes('::') ? targetTest.split('::').at(-1) : targetTest;
        const suppliedTargetFile = typeof input?.targetFile === 'string' ? input.targetFile : '';
        const targetPath = normalizePath(suppliedTargetFile);
        const targetHasDirectory = /[\\/]/.test(suppliedTargetFile);
        const matchesTarget = (item) => {
            const filePath = normalizePath(item.file);
            const matchesFile = targetHasDirectory
                ? filePath === targetPath || (targetPath.includes('/') && filePath.endsWith(`/${targetPath}`))
                : targetPath !== '' && baseOf(filePath) === targetPath;
            return matchesFile
            && (item.name === targetName || item.name.replace(/\[[^\]]*\]$/, '') === targetName);
        };
        if (!failing.some(matchesTarget))
            return null;
        return { tests, fail: failing.length, failing: failing.map((item) => matchesTarget(item) ? targetTest : item.name) };
    })();
    const failing = [...new Set([...topLevelFailing, ...(nestedTap?.failing ?? []), ...(pytest?.failing ?? [])])];
    const hasNodeSummary = /^# tests \d+\s*$/m.test(output);
    const evidenceSummary = hasNodeSummary ? summary : nestedTap ? {
        tests: nestedTap.tests,
        pass: 0,
        fail: nestedTap.fail,
    } : pytest ? {
        tests: pytest.tests,
        pass: 0,
        fail: pytest.fail,
    } : { tests: 0, pass: 0, fail: 0 };
    const evidence = {
        command,
        exit_code: exitCode,
        target_test: targetTest,
        tests: evidenceSummary.tests,
        pass: evidenceSummary.pass,
        fail: evidenceSummary.fail,
        failing_tests: failing,
    };
"""
    if source.count(old) != 1:
        raise ValueError("upstream tdd-red-evidence transform anchor is missing or ambiguous")
    rendered = source.replace(old, new)
    old_checks = b"""if (summary.tests === 0) {
        return { verdict: 'INVALID_RED', reason: 'zero_tests_discovered', evidence };
    }
    // Nonzero exit but TAP reports no failing test: harness/setup/parser crash
    // whose failure never reached a test assertion (or unparseable output).
    if (summary.fail === 0 || failing.length === 0) {
"""
    new_checks = b"""if (evidence.tests === 0) {
        return { verdict: 'INVALID_RED', reason: 'zero_tests_discovered', evidence };
    }
    // Nonzero exit but TAP reports no failing test: harness/setup/parser crash
    // whose failure never reached a test assertion (or unparseable output).
    if (evidence.fail === 0 || failing.length === 0) {
"""
    if rendered.count(old_checks) != 1:
        raise ValueError("upstream RED-evidence guard anchor is missing or ambiguous")
    return rendered.replace(old_checks, new_checks)


def render_executor_sequential_guard(source: bytes) -> bytes:
    old = """if [ -f .git ]; then  # worktree
  # Positive allow-list: HEAD must be on a per-agent branch (`agent-<id>` or
  # legacy `worktree-agent-<id>`). This catches feature/* and any other
  # arbitrary branch that the deny-list would silently allow (#2924, #1995).
  if ! echo "$ACTUAL_BRANCH" | grep -Eq '^((worktree-)?agent-|worktree-wf_)[A-Za-z0-9._/-]+$'; then
    echo "FATAL: refusing to commit — worktree HEAD '$ACTUAL_BRANCH' is not in the agent-* / worktree-agent-* / worktree-wf_* namespace." >&2
    echo "Agent commits must live on per-agent branches; surface as blocker (#2924)." >&2
    exit 1
  fi
fi
""".encode("utf-8")
    new = """# This positive namespace check applies only to an executor dispatched with
# isolation="worktree". A sequential executor can run from the user's existing
# linked worktree, where .git is also a file but the legitimate branch is the
# feature branch. In sequential mode, preserve the protected-branch check above
# and skip this isolated-agent namespace check.
if [ -f .git ] && [ "$(gsd_run query config-get workflow.use_worktrees --raw 2>/dev/null || echo true)" != "false" ]; then
  # Positive allow-list: HEAD must be on a per-agent branch (`agent-<id>` or
  # legacy `worktree-agent-<id>`). This catches feature/* and any other
  # arbitrary branch that the deny-list would silently allow (#2924, #1995).
  if ! echo "$ACTUAL_BRANCH" | grep -Eq '^((worktree-)?agent-|worktree-wf_)[A-Za-z0-9._/-]+$'; then
    echo "FATAL: refusing to commit — worktree HEAD '$ACTUAL_BRANCH' is not in the agent-* / worktree-agent-* / worktree-wf_* namespace." >&2
    echo "Agent commits must live on per-agent branches; surface as blocker (#2924)." >&2
    exit 1
  fi
fi
""".encode("utf-8")
    if source.count(old) != 1:
        raise ValueError("upstream executor namespace-guard anchor is missing or ambiguous")
    return source.replace(old, new)


# This program is rendered verbatim into the execute-phase safe-resume gate.
# Keeping it as one constant gives the Bats suite a direct way to exercise the
# exact parser/matcher which the pinned workflow instructs an executor to run.
SAFE_RESUME_PATH_MATCHER = r'''import csv
import fnmatch
import re
import subprocess
import sys


def normalize_path(raw):
    """Return a normalized repository-relative path, or None when unsafe."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().strip("\\\"'").replace("\\\\", "/")
    if not value or value.startswith("/"):
        return None
    parts = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) or None


def parse_declared_paths(plan_path):
    """Read files_modified/files_deleted from constrained PLAN frontmatter."""
    text = open(plan_path, encoding="utf-8").read()
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("PLAN frontmatter is missing")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("PLAN frontmatter is unterminated") from exc
    frontmatter = lines[1:end]
    declared = []
    saw_field = False
    index = 0
    while index < len(frontmatter):
        match = re.match(r"^(files_(?:modified|deleted)):\s*(.*?)\s*$", frontmatter[index])
        if not match:
            index += 1
            continue
        saw_field = True
        value = match.group(2)
        if value.startswith("[") and value.endswith("]"):
            try:
                declared.extend(next(csv.reader([value[1:-1]], skipinitialspace=True), []))
            except csv.Error as exc:
                raise ValueError("PLAN declared-path list is malformed") from exc
            index += 1
            continue
        if value:
            declared.append(value)
            index += 1
            continue
        index += 1
        while index < len(frontmatter):
            item = re.match(r"^\s+-\s+(.*?)\s*$", frontmatter[index])
            if not item:
                break
            declared.append(item.group(1))
            index += 1
    if not saw_field:
        raise ValueError("PLAN declares neither files_modified nor files_deleted")
    normalized = []
    for item in declared:
        raw = item.strip()
        is_directory = raw.rstrip().replace("\\", "/").endswith("/")
        path = normalize_path(raw)
        if path is None:
            raise ValueError("PLAN contains an unsafe declared path")
        normalized.append((path, is_directory))
    return normalized


def path_matches(changed, declared):
    changed_path = normalize_path(changed)
    if changed_path is None:
        return False
    for pattern, is_directory in declared:
        if is_directory and changed_path.startswith(pattern + "/"):
            return True
        if pattern.endswith("/**") and changed_path.startswith(pattern[:-3].rstrip("/") + "/"):
            return True
        if any(token in pattern for token in "*?["):
            if fnmatch.fnmatchcase(changed_path, pattern):
                return True
        elif changed_path == pattern:
            return True
    return False


def changed_paths_for_commit(commit):
    output = subprocess.check_output(
        ["git", "diff-tree", "--root", "--no-commit-id", "--name-status", "-z", "-r", "-M", commit]
    )
    fields = output.decode("utf-8", "surrogateescape").split("\0")
    paths = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            continue
        if index >= len(fields):
            raise ValueError("git diff-tree name-status output is malformed")
        paths.append(fields[index])
        index += 1
        if status.startswith(("R", "C")):
            if index >= len(fields):
                raise ValueError("git rename/copy output is malformed")
            paths.append(fields[index])
            index += 1
    return paths


def main():
    # Exit 0 = intersects the plan, 1 = unrelated, 2 = cannot decide. The
    # rendered gate treats only >1 as fail-closed, and an uncaught exception
    # would exit 1 and read as "unrelated", so every failure maps to 2 here.
    if len(sys.argv) != 3:
        print("usage: safe-resume-path-matcher PLAN_PATH COMMIT", file=sys.stderr)
        raise SystemExit(2)
    try:
        declared = parse_declared_paths(sys.argv[1])
        changed = changed_paths_for_commit(sys.argv[2])
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"safe-resume-path-matcher: {exc}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0 if any(path_matches(path, declared) for path in changed) else 1)


if __name__ == "__main__":
    main()
'''


def render_execute_phase_safe_resume(source: bytes) -> bytes:
    old = b'''SUMMARY_PATH="{phase_dir}/{plan_padded}-SUMMARY.md"
# #4003: no padding rule in the commit protocol, so zero-strip both components and
# match ANCHORED at the commit scope; bound to the latest reachable tag (milestone marker).
PHASE_N=$((10#{phase_number}))
PLAN_N=$((10#{plan_padded}))
PLAN_SCOPE_RE="^[a-z]+\\((0*${PHASE_N})-(0*${PLAN_N})\\):"
MILESTONE_BASE=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
PLAN_COMMITS=$(git log --oneline -E ${MILESTONE_BASE:+"$MILESTONE_BASE..HEAD"} --grep="${PLAN_SCOPE_RE}" -30)
'''
    matcher = SAFE_RESUME_PATH_MATCHER.rstrip().replace("'", "'\\\"'\\\"'")
    new = f'''PLAN_PATH="{{phase_dir}}/{{plan_padded}}-PLAN.md"
SUMMARY_PATH="{{phase_dir}}/{{plan_padded}}-SUMMARY.md"
# A commit message scope is necessary but not sufficient: phase/plan numbers recur
# across milestones. A candidate is production work for this plan only when one of
# its changed (including deleted, renamed, or copied) repository paths intersects
# the current PLAN's files_modified/files_deleted declaration. No declared-path
# parse failure may silently authorize dispatch; fail closed and inspect the plan.
PHASE_N=$((10#{{phase_number}}))
PLAN_N=$((10#{{plan_padded}}))
PLAN_SCOPE_RE="^[a-z]+\\((0*${{PHASE_N}})-(0*${{PLAN_N}})\\):"
MILESTONE_BASE=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
PLAN_COMMITS=""
while IFS= read -r PLAN_COMMIT_SHA; do
  [ -n "$PLAN_COMMIT_SHA" ] || continue
  if python3 - "$PLAN_PATH" "$PLAN_COMMIT_SHA" <<'PY'
{matcher}
PY
  then
    PLAN_COMMITS="${{PLAN_COMMITS}}${{PLAN_COMMITS:+$'\\n'}}$(git show -s --format='%h %s' "$PLAN_COMMIT_SHA")"
  else
    MATCH_STATUS=$?
    if [ "$MATCH_STATUS" -gt 1 ]; then
      echo "SAFE RESUME GATE: cannot safely parse declared paths for $PLAN_PATH; refusing dispatch." >&2
      exit 1
    fi
    # Same numeric scope but no declared-file intersection is unrelated history.
    # It is advisory only and must not block this plan's first execution.
    echo "SAFE RESUME: ignoring unrelated scoped commit $PLAN_COMMIT_SHA" >&2
  fi
done < <(git log --format=%H -E ${{MILESTONE_BASE:+"$MILESTONE_BASE..HEAD"}} --grep="${{PLAN_SCOPE_RE}}" -30)
'''.encode("utf-8")
    if source.count(old) != 1:
        raise ValueError("upstream execute-phase safe-resume anchor is missing or ambiguous")
    return source.replace(old, new)


RENDERERS = {
    "gsd-core/bin/lib/tdd-red-evidence.cjs": render_tdd_red_evidence,
    "agents/gsd-executor.md": render_executor_sequential_guard,
    "gsd-core/workflows/execute-phase.md": render_execute_phase_safe_resume,
}


def validate_manifest(manifest: object) -> list[dict[str, str]] | None:
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "package", "version", "targets"}:
        return None
    if manifest.get("schema") != "ffs.gsd-core-overlay/v3" or manifest.get("package") != PACKAGE or manifest.get("version") != VERSION:
        return None
    targets = manifest.get("targets")
    if not isinstance(targets, list) or len(targets) != len(RENDERERS):
        return None
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in targets:
        if not isinstance(item, dict) or set(item) != {"target", "base_sha256", "patched_sha256"}:
            return None
        target = item.get("target")
        base = item.get("base_sha256")
        patched = item.get("patched_sha256")
        if target not in RENDERERS or target in seen:
            return None
        if not all(isinstance(value, str) and len(value) == 64 for value in (base, patched)):
            return None
        seen.add(target)
        normalized.append({"target": target, "base_sha256": base, "patched_sha256": patched})
    return normalized if seen == set(RENDERERS) else None


def atomically_replace(target: Path, content: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".ffs-gsd-overlay.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def replace_pending(
    pending: list[tuple[Path, bytes, bytes, int, str]], fail_target: str | None
) -> str | None:
    """Replace all rendered targets, restoring earlier targets after a failure."""
    replaced: list[tuple[Path, bytes, int]] = []
    try:
        for target, original, patched, mode, relative_target in pending:
            if relative_target == fail_target:
                raise OSError(f"injected replacement failure for {relative_target}")
            atomically_replace(target, patched, mode)
            replaced.append((target, original, mode))
    except OSError as exc:
        rollback_errors: list[str] = []
        for target, original, mode in reversed(replaced):
            try:
                atomically_replace(target, original, mode)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        if rollback_errors:
            return f"overlay replacement failed: {exc}; rollback failed: {'; '.join(rollback_errors)}"
        return f"overlay replacement failed: {exc}; prior targets restored"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("apply", "verify"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    repo = args.repo.resolve()
    try:
        manifest = json.loads((repo / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(f"overlay manifest is unreadable: {exc}")
    targets = validate_manifest(manifest)
    if targets is None:
        return fail("overlay manifest is invalid")
    package = repo / "node_modules" / "@opengsd" / "gsd-core"
    try:
        package_meta = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(f"pinned package metadata is unreadable: {exc}")
    if package_meta.get("name") != PACKAGE or package_meta.get("version") != VERSION:
        return fail(f"overlay requires exact {PACKAGE}@{VERSION}")
    pending: list[tuple[Path, bytes, bytes, int, str]] = []
    for spec in targets:
        target = package / spec["target"]
        try:
            source = target.read_bytes()
            mode = target.stat().st_mode & 0o777
        except OSError as exc:
            return fail(f"overlay target {spec['target']} is unreadable: {exc}")
        current = digest(source)
        if args.mode == "verify":
            if current != spec["patched_sha256"]:
                return fail(f"overlay digest mismatch for {spec['target']}; run scripts/gsd/deps.sh install --yes")
            continue
        if current == spec["patched_sha256"]:
            continue
        if current != spec["base_sha256"]:
            return fail(f"overlay target {spec['target']} does not match the exact upstream base digest")
        try:
            patched = RENDERERS[spec["target"]](source)
        except ValueError as exc:
            return fail(str(exc))
        if digest(patched) != spec["patched_sha256"]:
            return fail(f"rendered overlay digest does not match the manifest for {spec['target']}")
        pending.append((target, source, patched, mode, spec["target"]))

    fail_target = os.environ.get("FFS_GSD_OVERLAY_TEST_FAIL_TARGET")
    if fail_target is not None and fail_target not in RENDERERS:
        return fail("invalid FFS_GSD_OVERLAY_TEST_FAIL_TARGET")
    replacement_error = replace_pending(pending, fail_target)
    if replacement_error:
        return fail(replacement_error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
