#!/usr/bin/env bash
# forbidden-paths-check.sh -- REQ-10's mechanical enforcement (AC-010,
# spec.md:194-197): catches a touch of any parallel-program-owned file
# (lib/gates.py, scripts/gsd/plan-wall.sh, scripts/gsd/run-finalizer.sh) in
# THREE diff scopes, independently -- committed since a base ref, staged but
# never committed, and unstaged in the working tree -- and unions them.
#
# Usage: forbidden-paths-check.sh [--base <ref>]
# Exit 0: no forbidden path touched in any checked scope (PASS line on stdout).
# Exit 2: a forbidden path was touched, an explicit --base could not be
#         resolved to a merge base, or a usage error.
#
# House shape (mirrors requirement-ownership-gate.sh): bash wrapper does
# argument parsing and repo-root resolution; a python3 heredoc body does the
# diff-scope work, because a pure-bash version has three real traps this
# avoids: (1) `git diff --name-status` piped to awk word-splits any path
# containing a space; (2) `"${#hits[@]}"` on an empty array is an unbound-
# variable error under `set -u` on bash 3.2 (macos-latest); (3) a rename
# record needs status-aware parsing (R100 consumes TWO paths) to catch the
# OLD name, which --name-only loses entirely.
set -uo pipefail

BASE_REF=""
BASE_EXPLICIT=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --base)
      if [ "$#" -lt 2 ]; then
        echo "forbidden-paths-check: ERROR: --base requires a value" >&2
        exit 2
      fi
      BASE_REF="$2"
      BASE_EXPLICIT=1
      shift 2
      ;;
    *)
      echo "forbidden-paths-check: ERROR: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "forbidden-paths-check: ERROR: not inside a git repository" >&2
  exit 2
}
cd "$REPO_ROOT" || exit 2

if [ "$BASE_EXPLICIT" -eq 0 ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # Never a hardcoded origin/main -- the repo's ONE default-branch resolver.
  # base-branch.sh always exits 0 and can hand back a bare branch name (e.g.
  # "main") with no origin/ counterpart in a fresh repo; that is the local
  # quick-mode case below, distinguished from an EXPLICIT unresolvable ref
  # by BASE_EXPLICIT, never by whether resolution happened to succeed.
  DERIVED_BRANCH="$(bash "$SCRIPT_DIR/../gsd/base-branch.sh" 2>/dev/null)" || DERIVED_BRANCH=""
  if [ -n "$DERIVED_BRANCH" ]; then
    BASE_REF="origin/$DERIVED_BRANCH"
  fi
fi

python3 - "$REPO_ROOT" "$BASE_REF" "$BASE_EXPLICIT" <<'PY'
import subprocess
import sys

repo_root, base_ref, base_explicit_flag = sys.argv[1], sys.argv[2], sys.argv[3]
base_explicit = base_explicit_flag == "1"

# This script checks diff PATH NAMES only and never reads file content, so
# these literals appearing in this script's own source are not self-matching
# -- a later reader must not "fix" that.
FORBIDDEN = {
    "lib/gates.py",
    "scripts/gsd/plan-wall.sh",
    "scripts/gsd/run-finalizer.sh",
}


def stop(message):
    print(f"forbidden-paths-check: ERROR: {message}", file=sys.stderr)
    sys.exit(2)


def git_diff(args):
    proc = subprocess.run(
        ["git", "diff", "--name-status", "-z", *args],
        cwd=repo_root,
        capture_output=True,
    )
    if proc.returncode not in (0, 1):
        stop(
            "git diff failed: "
            + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    return proc.stdout


def touched_paths(raw):
    # NUL-delimited, never word-split by a shell filter -- a path containing
    # a space stays one field. A status starting with R (rename) or C (copy)
    # consumes TWO following path fields (old, new); every other status
    # consumes one. The OLD name is what a renamed-away forbidden file must
    # still be caught on.
    fields = [
        f for f in raw.decode("utf-8", errors="surrogateescape").split("\0") if f
    ]
    touched = set()
    i = 0
    while i < len(fields):
        status = fields[i]
        if status[:1] in ("R", "C"):
            touched.add(fields[i + 1])
            touched.add(fields[i + 2])
            i += 3
        else:
            touched.add(fields[i + 1])
            i += 2
    return touched


touched = set()
scopes_checked = []

if base_ref:
    merge_base_proc = subprocess.run(
        ["git", "merge-base", base_ref, "HEAD"],
        cwd=repo_root,
        capture_output=True,
    )
    if merge_base_proc.returncode == 0:
        merge_base = merge_base_proc.stdout.decode().strip()
        touched |= touched_paths(git_diff([merge_base, "HEAD"]))
        scopes_checked.append("committed")
    elif base_explicit:
        # The caller ASKED for a scope; silently delivering a smaller one is
        # a lie that reads as a pass. This is the CI shape: a shallow clone
        # or a misconfigured base surfaces as a red step, never a quiet
        # downgrade to worktree-only scope with a PASS printed over a
        # committed forbidden-path change.
        stop(
            f"explicit --base does not resolve to a merge base: {base_ref!r} "
            "-- the committed scope could not be established, nothing checked"
        )
    # else: DERIVED and unresolvable -- the documented local quick mode.
    # Nothing was asked for explicitly, so nothing failed; fall through to
    # the index and working-tree scopes below with the committed one skipped.
elif base_explicit:
    # Defensive: the wrapper's argument parser above never leaves BASE_REF
    # empty when BASE_EXPLICIT=1 (--base requires a value), so this should
    # be unreachable. Fail loud rather than silently degrading if it is.
    stop("--base requires a non-empty value")

touched |= touched_paths(git_diff(["--cached"]))
scopes_checked.append("staged")
touched |= touched_paths(git_diff([]))
scopes_checked.append("working tree")

# Exact repo-relative path equality, never substring/grep -- a real file at
# vendor/lib/gates.py must pass.
hits = sorted(touched & FORBIDDEN)
if hits:
    for hit in hits:
        print(f"forbidden-paths-check: ERROR: forbidden path touched: {hit}", file=sys.stderr)
    sys.exit(2)

print(
    f"forbidden-paths-check: PASS base={base_ref or '(none)'} "
    f"forbidden={len(FORBIDDEN)} scopes={','.join(scopes_checked)}"
)
sys.exit(0)
PY
