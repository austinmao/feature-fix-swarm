#!/usr/bin/env python3
"""Fail when a junit report holds a skipped test whose reason names the sandbox.

The macOS leg exists to run the sandbox-gated cloud tests.  Every one of them is gated on the sandbox being usable
when pytest imports the test module, which is a different process from the CI preflight: a transient failure there
would skip dozens of tests and leave the step green.  This check turns any such skip into a failure.

    fail-on-sandbox-skips.py REPORT.xml
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET

SANDBOX = re.compile(r"sandbox", re.IGNORECASE)


def sandbox_skips(path: str) -> list:
    skipped = []
    for case in ET.parse(path).getroot().iter("testcase"):
        for node in case.findall("skipped"):
            message = (node.get("message") or "").strip()  # the reason only: the body carries a file path
            if SANDBOX.search(message):
                skipped.append("%s::%s: %s" % (case.get("classname"), case.get("name"), message))
    return skipped


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: fail-on-sandbox-skips.py REPORT.xml", file=sys.stderr)
        return 2
    try:
        skipped = sandbox_skips(argv[1])
    except (OSError, ET.ParseError) as exc:
        print("cannot read the junit report %s: %s" % (argv[1], exc), file=sys.stderr)
        return 2
    if skipped:
        print("%d sandbox-gated test(s) were skipped on the sandbox leg:" % len(skipped), file=sys.stderr)
        for line in skipped[:20]:
            print("  " + line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
