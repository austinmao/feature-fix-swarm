#!/usr/bin/env python3
"""Fail when a junit report holds any skipped test whose reason is not on the short allowlist below.

The macOS leg exists to run the sandbox-gated cloud tests.  Every one of them is gated on the sandbox being usable
when pytest imports the test module, which is a different process from the CI preflight: a transient failure there
would skip dozens of tests and leave the step green.  This check turns every skip into a failure, except the reasons
below: runtime conditions of the host (no pasteboard, no node, no terminal device, running as root) that can really
occur on the macOS job and say nothing about the sandbox.  tests/test_ci_sandbox_skip_guard.py derives the set of
skips in the cloud test files from their code and fails when it differs from this list.

    fail-on-sandbox-skips.py REPORT.xml
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

ALLOWED_REASONS = (
    "no pasteboard in this session",
    "node is not installed",
    "no /dev/ttys* device on this host",
    "root reads any file",
)


def unexpected_skips(path: str) -> list:
    skipped = []
    for case in ET.parse(path).getroot().iter("testcase"):
        for node in case.findall("skipped"):
            message = (node.get("message") or "").strip()  # the reason only: the body carries a file path
            if message not in ALLOWED_REASONS:
                skipped.append("%s::%s: %s" % (case.get("classname"), case.get("name"), message))
    return skipped


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: fail-on-sandbox-skips.py REPORT.xml", file=sys.stderr)
        return 2
    try:
        skipped = unexpected_skips(argv[1])
    except (OSError, ET.ParseError) as exc:
        print("cannot read the junit report %s: %s" % (argv[1], exc), file=sys.stderr)
        return 2
    if skipped:
        print("%d test(s) were skipped on the sandbox leg (only %d host-condition reasons are allowed):" % (len(skipped), len(ALLOWED_REASONS)), file=sys.stderr)
        for line in skipped[:20]:
            print("  " + line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
