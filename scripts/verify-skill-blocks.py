#!/usr/bin/env python3
"""Extract and EXECUTE every ```bash verify block in skills/*/SKILL.md.

Anti-rot gate for skill prose (borrowed pattern: ai-cost-cutter-skills):
a SKILL.md claim about the code — a constant's value, a lever's existence —
is only trustworthy while CI re-proves it. Each block runs in a fresh temp
dir with REPO_ROOT exported; a nonzero exit fails the build and names the
skill. Plain ```bash fences are prose and never executed. Blocks must be
hermetic: no network, no OAuth, no writes outside their temp cwd.

Usage: verify-skill-blocks.py [--root PATH]   (default: repo root)
"""
import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

# Fences may sit indented inside list items; bash tolerates the body's
# leading whitespace, so only the fence markers need the indent allowance.
BLOCK_RE = re.compile(r"^[ \t]*```bash verify\n(.*?)^[ \t]*```", re.M | re.S)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parents[1]))
    root = pathlib.Path(ap.parse_args().root).resolve()

    blocks: list[tuple[str, str]] = []  # (skill name, script body)
    for skill_md in sorted(root.glob("skills/*/SKILL.md")):
        for body in BLOCK_RE.findall(skill_md.read_text()):
            blocks.append((skill_md.parent.name, body))

    print(f"{len(blocks)} verify block(s) found under {root}/skills")
    failures = 0
    for name, body in blocks:
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", body],
                cwd=tmp,
                env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                     "HOME": tmp, "REPO_ROOT": str(root)},
                capture_output=True, text=True, timeout=120,
            )
        if r.returncode == 0:
            print(f"  ok   {name}")
        else:
            failures += 1
            print(f"  FAIL {name} (exit {r.returncode})")
            out = (r.stdout + r.stderr).strip()
            if out:
                print("       " + out.replace("\n", "\n       "))
    if failures:
        print(f"FAIL: {failures} verify block(s) no longer hold — the code moved out from under the doc")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
