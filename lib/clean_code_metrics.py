"""Advisory complexity/duplication report for a review diff (never blocks).

Usage: git diff -U0 <target> | python3 lib/clean_code_metrics.py [--base <rev>]
Optional tools: crapper (CC per function), dryer (duplicate blocks). A missing
tool prints nothing; every failure is swallowed and the exit code is 0.
"""
from __future__ import annotations

import argparse, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

import gates

MAX_FILES, MAX_LINES, MIN_CC, BUDGET = 40, 20, 10, 90  # BUDGET: seconds for the whole report
HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")
SPAN = re.compile(r"^(.+):(\d+)-(\d+)$")


def parse_diff(text: str) -> dict[str, list[tuple[int, int]]]:
    """Map each added/changed file to its added (start, end) line ranges."""
    files: dict[str, list[tuple[int, int]]] = {}
    current: list[tuple[int, int]] | None = None
    for line in text.splitlines():
        if line.startswith("+++ "):
            path = line[4:]
            current = files.setdefault(path[2:], []) if path.startswith("b/") else None
        elif current is not None and (m := HUNK.match(line)):
            start, count = int(m[1]), int(m[2] or 1)
            if count:
                current.append((start, start + count - 1))
    return files


def production_files(files: dict[str, list[tuple[int, int]]], cap: int = MAX_FILES) -> list[str]:
    return [p for p in files if gates._is_production(p)][:cap]


def find_tool(name: str) -> str | None:
    home = Path(os.environ.get("FFS_UB_TOOLS_DIR") or Path.home() / ".local/share/ub-mode")
    for cand in (shutil.which(name), str(home / name / name)):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def parse_crapper(text: str) -> dict[tuple[str, str], int]:
    """Rows after the dashed line: function, namespace, CC, Cov%, CRAP."""
    rows, seen_rule = {}, False
    for line in text.splitlines():
        if line.startswith("---"):
            seen_rule = True
        elif seen_rule and len(parts := line.split()) >= 5 and parts[-3].isdigit():
            rows[(parts[0], parts[1])] = int(parts[-3])
    return rows


def parse_dryer(text: str, root: str) -> list[tuple[str, tuple[str, int, int], tuple[str, int, int]]]:
    pairs, score, spans = [], "", []
    for line in [*text.splitlines(), "DUPLICATE score="]:
        if line.startswith("DUPLICATE score="):
            if score and len(spans) == 2:
                pairs.append((score, spans[0], spans[1]))
            score, spans = line.split("=", 1)[1].strip(), []
        elif m := SPAN.match(line.strip()):
            spans.append((os.path.relpath(m[1], root) if os.path.isabs(m[1]) else m[1], int(m[2]), int(m[3])))
    return pairs


def overlaps(span: tuple[str, int, int], ranges: dict[str, list[tuple[int, int]]]) -> bool:
    return any(span[1] <= end and start <= span[2] for start, end in ranges.get(span[0], []))


def run(cmd: list[str], deadline: float) -> str:
    if (left := deadline - time.monotonic()) <= 0:
        return ""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=left)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def analyse(crapper: str, root: Path, rel: str, src: str, deadline: float) -> dict[tuple[str, str], int]:
    """Run crapper on one copy of `rel` placed under `root`, so namespaces match across versions."""
    copy = root / rel
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(src)
    return parse_crapper(run([crapper, "--no-coverage", "--root", str(root), str(copy)], deadline))


def complexity(crapper: str, paths: list[str], base: str, tmp: str, deadline: float) -> list[str]:
    out = []
    for path in paths:
        new = analyse(crapper, Path(tmp, "head"), path, Path(path).read_text(errors="replace"), deadline)
        old_src = run(["git", "show", f"{base}:{path}"], deadline)
        old = analyse(crapper, Path(tmp, "base"), path, old_src, deadline) if old_src else {}
        out += [f"COMPLEXITY: {path} {key[0]} CC {old.get(key, 'new')}->{cc}"
                for key, cc in new.items() if cc >= MIN_CC and cc > old.get(key, 0)]
    return out


def duplication(dryer: str, ranges: dict[str, list[tuple[int, int]]], tmp: str, root: str, deadline: float) -> list[str]:
    text = run([dryer, "--root", tmp, "--source-root", root, "--threshold", "0.8"], deadline)
    return [f"DUPLICATION: {s} {a[0]}:{a[1]}-{a[2]} {b[0]}:{b[1]}-{b[2]}"
            for s, a, b in parse_dryer(text, root) if overlaps(a, ranges) or overlaps(b, ranges)]


def report(diff: str, base: str) -> str:
    ranges = parse_diff(diff)
    paths = [p for p in production_files(ranges, MAX_FILES) if Path(p).is_file()]
    root = os.getcwd()
    ranges = {p: ranges[p] for p in paths}
    tools = {n: find_tool(n) for n in ("crapper", "dryer")}
    lines: list[str] = []
    deadline = time.monotonic() + BUDGET
    with tempfile.TemporaryDirectory() as tmp:
        if tools["crapper"] and paths:
            lines += complexity(tools["crapper"], paths, base, tmp, deadline)
        if tools["dryer"] and paths:
            lines += duplication(tools["dryer"], ranges, tmp, root, deadline)
    if not lines:
        return ""
    names = ", ".join(n for n, t in tools.items() if t)
    shown = lines[:MAX_LINES] + ([f"... {len(lines) - MAX_LINES} more"] if len(lines) > MAX_LINES else [])
    return "\n".join([f"CLEAN-CODE METRICS (advisory; tools: {names})", *shown])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="HEAD")
    try:
        text = report(sys.stdin.read(), parser.parse_args(argv).base)
        if text:
            print(text)
    except Exception:  # advisory only: never raise to the caller
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
