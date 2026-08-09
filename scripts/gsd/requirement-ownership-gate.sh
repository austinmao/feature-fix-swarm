#!/usr/bin/env bash
# Fail before GSD execution when PLAN requirement ownership would let
# execute-plan mark a ROADMAP requirement complete prematurely.
#
# Usage: requirement-ownership-gate.sh <phase-number>
# Exit 0: every ROADMAP phase requirement is owned by exactly one PLAN.
# Exit 2: malformed/missing/duplicate/extra ownership; no network is touched.
set -uo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: requirement-ownership-gate.sh <phase-number>" >&2
  exit 2
fi

case "$1" in
  ''|*[!0-9]*)
    echo "requirement-ownership-gate: ERROR: phase must be numeric: $1" >&2
    exit 2
    ;;
esac

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

python3 - "$REPO_ROOT" "$1" <<'PY'
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path


root = Path(sys.argv[1])
phase_number = int(sys.argv[2], 10)
phase_id = f"{phase_number:02d}"
planning = root / ".planning"
roadmap_path = planning / "ROADMAP.md"
# Lowercase allowed in the id TAIL: spec-007 mints REQ-202a-style
# sub-requirements; the uppercase-only tail truncated it to REQ-202 and
# reported a phantom duplicate.
id_re = re.compile(r"[A-Z][A-Z0-9]*-[A-Z0-9][A-Za-z0-9-]*")


def stop(message: str) -> None:
    print(f"requirement-ownership-gate: ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def extract_roadmap_requirements() -> list[str]:
    if not roadmap_path.is_file():
        stop(f"missing {roadmap_path.relative_to(root)}")
    lines = roadmap_path.read_text(encoding="utf-8").splitlines()
    heading_re = re.compile(rf"^(#{{2,6}})\s+Phase\s+0*{phase_number}(?:\b|\s*:)", re.I)
    headings: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = heading_re.match(line)
        if match:
            headings.append((index, len(match.group(1))))
    if not headings:
        stop(f"ROADMAP has no Phase {phase_number} heading")

    declarations: list[list[str]] = []
    any_declaration = False
    for start, level in headings:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            match = re.match(r"^(#{2,6})\s+", lines[index])
            if match and len(match.group(1)) <= level:
                end = index
                break
        for index in range(start + 1, end):
            normalized = lines[index].replace("**", "")
            match = re.match(r"^\s*Requirements\s*:\s*(.*)$", normalized, re.I)
            if not match:
                continue
            any_declaration = True
            value_lines = [match.group(1)]
            cursor = index + 1
            while cursor < end and lines[cursor].strip():
                candidate = lines[cursor]
                if candidate.lstrip().startswith("#") or "**" in candidate:
                    break
                if not candidate[:1].isspace():
                    break
                value_lines.append(candidate)
                cursor += 1
            ids = id_re.findall(" ".join(value_lines))
            if len(ids) != len(set(ids)):
                duplicates = sorted(
                    item for item, count in collections.Counter(ids).items() if count > 1
                )
                stop(
                    f"ROADMAP Phase {phase_number} repeats requirement(s): "
                    f"{', '.join(duplicates)}"
                )
            declarations.append(ids)

    if not any_declaration:
        stop(f"ROADMAP Phase {phase_number} has no Requirements declaration")
    if len(declarations) > 1:
        rendered = {tuple(items) for items in declarations}
        if len(rendered) > 1:
            stop(f"ROADMAP Phase {phase_number} has conflicting Requirements declarations")
    return declarations[-1]


def parse_scalar(value: str, plan_name: str) -> str:
    value = value.split("#", 1)[0].strip().strip("\"'")
    if not id_re.fullmatch(value):
        stop(f"{plan_name} has malformed requirement value: {value or '<empty>'}")
    return value


def extract_plan_requirements(plan_path: Path) -> list[str]:
    lines = plan_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        stop(f"{plan_path.name} has no YAML frontmatter")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        stop(f"{plan_path.name} has unterminated YAML frontmatter")
    frontmatter = lines[1:end]
    field_index = None
    field_value = ""
    for index, line in enumerate(frontmatter):
        match = re.match(r"^requirements\s*:\s*(.*)$", line)
        if match:
            field_index = index
            field_value = match.group(1).strip()
            break
    if field_index is None:
        stop(
            f"{plan_path.name} has no explicit requirements field; "
            "use requirements: [] for preparatory plans"
        )

    uncommented = field_value.split("#", 1)[0].strip()
    if uncommented:
        if not (uncommented.startswith("[") and uncommented.endswith("]")):
            stop(f"{plan_path.name} requirements must be a YAML list")
        inner = uncommented[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(item, plan_path.name) for item in inner.split(",")]

    items: list[str] = []
    for line in frontmatter[field_index + 1 :]:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:", line):
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^\s+-\s+(.+?)\s*$", line)
        if not match:
            stop(f"{plan_path.name} has malformed requirements list")
        items.append(parse_scalar(match.group(1), plan_path.name))
    return items


roadmap_requirements = extract_roadmap_requirements()
phases_root = planning / "phases"
phase_dirs = sorted(
    path
    for path in phases_root.glob("*-*")
    if path.is_dir() and re.match(rf"^0*{phase_number}-", path.name)
)
if not phase_dirs:
    stop(f"no .planning/phases/{phase_id}-* directory")
if len(phase_dirs) > 1:
    stop(
        f"multiple Phase {phase_number} directories: "
        f"{', '.join(path.name for path in phase_dirs)}"
    )

plans = sorted(phase_dirs[0].glob(f"{phase_id}-*-PLAN.md"))
if not plans:
    stop(f"no {phase_id}-*-PLAN.md files in {phase_dirs[0].relative_to(root)}")

owners: dict[str, list[str]] = collections.defaultdict(list)
for plan in plans:
    for requirement in extract_plan_requirements(plan):
        owners[requirement].append(plan.name)

errors: list[str] = []
for requirement, plan_names in sorted(owners.items()):
    if len(plan_names) > 1:
        errors.append(f"{requirement} is owned by multiple plans: {', '.join(plan_names)}")

roadmap_set = set(roadmap_requirements)
owned_set = set(owners)
missing = sorted(roadmap_set - owned_set)
extra = sorted(owned_set - roadmap_set)
if missing:
    errors.append(f"missing from plans: {', '.join(missing)}")
if extra:
    errors.append(f"not owned by ROADMAP phase: {', '.join(extra)}")

if errors:
    for error in errors:
        print(f"requirement-ownership-gate: ERROR: {error}", file=sys.stderr)
    print(
        "requirement-ownership-gate: each ID belongs only on the last plan "
        "that genuinely completes it; use requirements: [] on preparatory plans",
        file=sys.stderr,
    )
    raise SystemExit(2)

print(
    f"requirement-ownership-gate: PASS phase={phase_id} "
    f"roadmap={len(roadmap_requirements)} plans={len(plans)}"
)
PY
