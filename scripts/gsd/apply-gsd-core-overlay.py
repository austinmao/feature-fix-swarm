#!/usr/bin/env python3
"""Apply or verify the exact-pin FFS overlay for @opengsd/gsd-core.

The package lives in ignored node_modules and npm ci replaces it.  This
small, deterministic overlay is therefore the durable patch seam: it admits
one package version and one upstream file digest, atomically writes one
reviewed transform, then verifies the resulting digest on every check.
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
TARGET = Path("gsd-core/bin/lib/tdd-red-evidence.cjs")
MANIFEST = Path("patches/gsd-core-overlay.json")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fail(message: str) -> int:
    print(f"gsd-core-overlay: {message}", file=os.sys.stderr)
    return 78


def render(source: bytes) -> bytes:
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
        const collection = [...output.matchAll(/^collected (\d+) items?(?:\s*\/\s*(\d+) errors?)?\s*$/gm)];
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
        const targetBase = baseOf(input?.targetFile ?? '');
        const matchesTarget = (item) => targetBase !== '' && baseOf(item.file) === targetBase
            && (item.name === targetName || item.name.replace(/\[[^\]]*\]$/, '') === targetName);
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
    if manifest != {
        "schema": "ffs.gsd-core-overlay/v1",
        "package": PACKAGE,
        "version": VERSION,
        "target": TARGET.as_posix(),
        "base_sha256": manifest.get("base_sha256"),
        "patched_sha256": manifest.get("patched_sha256"),
    } or not all(isinstance(manifest.get(key), str) and len(manifest[key]) == 64 for key in ("base_sha256", "patched_sha256")):
        return fail("overlay manifest is invalid")
    package = repo / "node_modules" / "@opengsd" / "gsd-core"
    try:
        package_meta = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(f"pinned package metadata is unreadable: {exc}")
    if package_meta.get("name") != PACKAGE or package_meta.get("version") != VERSION:
        return fail(f"overlay requires exact {PACKAGE}@{VERSION}")
    target = package / TARGET
    try:
        source = target.read_bytes()
    except OSError as exc:
        return fail(f"overlay target is unreadable: {exc}")
    current = digest(source)
    if args.mode == "verify":
        return 0 if current == manifest["patched_sha256"] else fail("overlay digest mismatch; run scripts/gsd/deps.sh install --yes")
    if current == manifest["patched_sha256"]:
        return 0
    if current != manifest["base_sha256"]:
        return fail("overlay target does not match the exact upstream base digest")
    try:
        patched = render(source)
    except ValueError as exc:
        return fail(str(exc))
    if digest(patched) != manifest["patched_sha256"]:
        return fail("rendered overlay digest does not match the manifest")
    fd, temporary = tempfile.mkstemp(prefix=".ffs-gsd-overlay.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(patched)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, target.stat().st_mode & 0o777)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
