#!/usr/bin/env python3
"""behavioral_red.py — no-shell argv runner/classifier for RED gates.

Wraps a candidate RED suite for `gates.py run-red`.  gates.py records a RED
proof iff THIS process exits nonzero, so the exit contract is inverted and
fail-closed:

  exit 1  ONLY when the child suite itself exited nonzero, its combined
          output carries the suite-specific EXACT marker line, and no
          infrastructure signature is present  (a real behavioral RED)
  exit 0  for everything else — unexpected green, missing marker, and every
          infrastructure class (command discovery, syntax/parse, import/
          module, pytest fixture/collection/setup, unstubbed boundary,
          connection/DNS/HTTP/network, missing test commands/files,
          unexpected exceptions) — which makes the enclosing run-red FAIL.

Usage:
  behavioral_red.py --expect-marker MARKER -- CMD [ARG...]
  behavioral_red.py --self-test
"""
from __future__ import annotations

import re
import subprocess
import sys

MARKERS = (
    "EXPECTED-RED:CONSOLIDATE:missing-production-seam",
    "EXPECTED-RED:GRANT:missing-consolidate-grant",
    "EXPECTED-RED:POSTURE:missing-posture-resolution",
    "EXPECTED-RED:DOCS:missing-phase3-doc-contract",
)

# Any of these in the combined output means the failure is infrastructure,
# never a behavioral assertion — classify to exit 0 so run-red fails.
INFRA_SIGNATURES = [
    ("command-not-found", re.compile(r"command not found", re.I)),
    ("missing-file", re.compile(r"No such file or directory", re.I)),
    ("shell-syntax", re.compile(r"syntax error", re.I)),
    ("python-syntax", re.compile(r"SyntaxError")),
    ("parse-error", re.compile(r"parse error", re.I)),
    ("import-error", re.compile(r"ImportError")),
    ("module-error", re.compile(r"ModuleNotFoundError")),
    ("pytest-fixture", re.compile(r"fixture '[^']+' not found")),
    ("pytest-setup", re.compile(r"ERROR at (setup|teardown) of")),
    ("pytest-collection", re.compile(r"errors? during collection", re.I)),
    ("pytest-internal", re.compile(r"INTERNALERROR")),
    ("pytest-missing-target", re.compile(r"file or directory not found", re.I)),
    ("bats-missing-target", re.compile(r"bats: .*does not exist", re.I)),
    ("unstubbed-boundary", re.compile(r"UNSTUBBED-BOUNDARY")),
    ("connection", re.compile(r"Connection (refused|reset|timed out)", re.I)),
    ("dns", re.compile(r"Could not resolve|Name or service not known|"
                       r"Temporary failure in name resolution|getaddrinfo", re.I)),
    ("network", re.compile(r"Network is unreachable", re.I)),
    ("http", re.compile(r"HTTPError|HTTP Error|HTTPSConnection|"
                        r"urlopen error|curl: \(", re.I)),
    ("unexpected-exception", re.compile(r"Traceback \(most recent call last\)")),
]


def _marker_present(output: str, marker: str) -> bool:
    """True iff some line, after stripping whitespace and TAP '#' comment
    prefixes, is EXACTLY the marker (substring hits never count)."""
    for raw in output.splitlines():
        line = raw.strip()
        while line.startswith("#"):
            line = line[1:].lstrip()
        if line == marker:
            return True
    return False


def classify(returncode: int, output: str, marker: str) -> tuple[str, int]:
    """(verdict, exit_code) per the fail-closed contract above."""
    if returncode == 0:
        return "unexpected-green", 0
    if returncode == 127:
        return "infrastructure:exit-127", 0
    for name, pat in INFRA_SIGNATURES:
        if pat.search(output):
            return f"infrastructure:{name}", 0
    if not _marker_present(output, marker):
        return "missing-marker", 0
    return "behavioral-red", 1


def run(marker: str, argv: list[str]) -> int:
    # WR-02 (round 2): capture BYTES and decode with an explicit fail-closed
    # policy — any decode failure or spawn/read OSError is an infrastructure
    # verdict (exit 0, run-red FAILS), never a crash whose nonzero exit
    # would satisfy the RED gate without a behavioral-red verdict.
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=1500)
    except FileNotFoundError:
        print(f"BEHAVIORAL-RED-CLASSIFIER: verdict=infrastructure:spawn-failed "
              f"argv0={argv[0]!r}", file=sys.stderr)
        return 0
    except subprocess.TimeoutExpired:
        print("BEHAVIORAL-RED-CLASSIFIER: verdict=infrastructure:timeout",
              file=sys.stderr)
        return 0
    except OSError as exc:
        print(f"BEHAVIORAL-RED-CLASSIFIER: verdict=infrastructure:spawn-failed "
              f"argv0={argv[0]!r} ({exc.__class__.__name__})", file=sys.stderr)
        return 0
    # WR-01 (round 3): classify BEFORE replaying anything.  The enclosing
    # `gates.py run-red` consumer reads this process's streams as text, so
    # replaying undecodable raw child bytes crashed the parent with
    # UnicodeDecodeError and no RED record.  Valid UTF-8 replays untouched;
    # undecodable output is NEVER replayed raw — a replacement-decoded
    # diagnostic is emitted after the infrastructure classification instead.
    try:
        output = (proc.stdout + b"\n" + proc.stderr).decode("utf-8")
    except UnicodeError:
        sys.stdout.write(proc.stdout.decode("utf-8", errors="replace"))
        sys.stderr.write(proc.stderr.decode("utf-8", errors="replace"))
        print("BEHAVIORAL-RED-CLASSIFIER: "
              "verdict=infrastructure:undecodable-output", file=sys.stderr)
        return 0
    sys.stdout.buffer.write(proc.stdout)
    sys.stderr.buffer.write(proc.stderr)
    verdict, code = classify(proc.returncode, output, marker)
    print(f"BEHAVIORAL-RED-CLASSIFIER: verdict={verdict} "
          f"child-rc={proc.returncode} marker={marker}", file=sys.stderr)
    return code


# ── self-test ─────────────────────────────────────────────────────────────

def self_test() -> int:
    marker = "EXPECTED-RED:CONSOLIDATE:missing-production-seam"
    cases = [
        # (label, child_rc, output, expected_exit)
        ("expected-marker", 1, f"{marker}\nassert failed", 1),
        ("tap-prefixed-marker", 1, f"# {marker}\n", 1),
        ("unexpected-green", 0, marker, 0),
        ("missing-marker", 1, "some assertion failed", 0),
        ("substring-never-counts", 1, f"prefix {marker} suffix", 0),
        ("exit-127", 127, marker, 0),
        ("command-not-found", 1, f"{marker}\nbash: nope: command not found", 0),
        ("missing-file", 1, f"{marker}\nbash: x: No such file or directory", 0),
        ("shell-syntax", 1, f"{marker}\nline 3: syntax error near token", 0),
        ("python-syntax", 1, f"{marker}\nSyntaxError: invalid syntax", 0),
        ("parse-error", 1, f"{marker}\nparse error at line 9", 0),
        ("import-error", 1, f"{marker}\nImportError: cannot import name", 0),
        ("module-error", 1, f"{marker}\nModuleNotFoundError: No module named x", 0),
        ("pytest-fixture", 1, f"{marker}\nfixture 'queue_repo' not found", 0),
        ("pytest-setup", 1, f"{marker}\nERROR at setup of test_x", 0),
        ("pytest-collection", 1, f"{marker}\n2 errors during collection", 0),
        ("pytest-internal", 1, f"{marker}\nINTERNALERROR> boom", 0),
        ("pytest-missing-target", 1,
         f"{marker}\nERROR: file or directory not found: tests/x.py", 0),
        ("bats-missing-target", 1,
         f"{marker}\nbats: tests/bats/x.bats does not exist", 0),
        ("unstubbed-boundary", 1, f"{marker}\nUNSTUBBED-BOUNDARY:gh pr merge", 0),
        ("connection", 1, f"{marker}\nConnection refused", 0),
        ("dns", 1, f"{marker}\ncurl: Could not resolve host: example.com", 0),
        ("network", 1, f"{marker}\nNetwork is unreachable", 0),
        ("http", 1, f"{marker}\nurllib.error.HTTPError: 503", 0),
        ("unexpected-exception", 1,
         f"{marker}\nTraceback (most recent call last):\n  boom", 0),
    ]
    failures = []
    for label, rc, output, expected in cases:
        verdict, code = classify(rc, output, marker)
        if code != expected:
            failures.append(f"{label}: got exit {code} ({verdict}), "
                            f"expected {expected}")
    # end-to-end through a real subprocess for the two pivotal outcomes
    e2e = [
        ("e2e-behavioral-red",
         [sys.executable, "-c", f"print({marker!r}); raise SystemExit(1)"], 1),
        ("e2e-unexpected-green",
         [sys.executable, "-c", f"print({marker!r})"], 0),
        ("e2e-infrastructure-traceback",
         [sys.executable, "-c", f"print({marker!r}); raise RuntimeError('x')"], 0),
    ]
    # WR-02 (round 2): decoding and spawn infrastructure failures are never
    # RED proof — the classifier must exit 0 with an infrastructure verdict
    # instead of crashing into a nonzero exit that run-red records as RED.
    import os as _os
    import tempfile as _tempfile
    fd, noexec = _tempfile.mkstemp(prefix="behavioral-red-noexec-")
    _os.close(fd)
    _os.chmod(noexec, 0o600)  # exists but is not executable: PermissionError
    e2e += [
        ("e2e-invalid-utf8-output",
         [sys.executable, "-c",
          "import sys; sys.stdout.buffer.write(b'\\xff\\xfe garbage\\n'); "
          f"print({marker!r}); raise SystemExit(1)"], 0),
        ("e2e-spawn-permission-denied", [noexec], 0),
    ]
    try:
        for label, argv, expected in e2e:
            code = run(marker, argv)
            if code != expected:
                failures.append(f"{label}: got exit {code}, expected {expected}")
    finally:
        _os.unlink(noexec)
    if failures:
        for f in failures:
            print(f"SELF-TEST FAIL: {f}", file=sys.stderr)
        return 1
    print(f"SELF-TEST PASS: {len(cases) + len(e2e)} classifications")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    if len(argv) >= 3 and argv[0] == "--expect-marker" and argv[2] == "--":
        marker, child = argv[1], argv[3:]
        if marker not in MARKERS:
            print(f"BEHAVIORAL-RED-CLASSIFIER: unknown marker {marker!r} "
                  f"(known: {', '.join(MARKERS)})", file=sys.stderr)
            return 0  # fail-closed: the enclosing run-red fails
        if not child:
            print("BEHAVIORAL-RED-CLASSIFIER: no child argv", file=sys.stderr)
            return 0
        return run(marker, child)
    print(__doc__, file=sys.stderr)
    return 0  # fail-closed for the enclosing run-red


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
