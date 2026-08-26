#!/usr/bin/env python3
"""collect-queue.py — deterministic land-queue intake and item prechecks.

Plan 02-01 tracer slice: explicit-item intake after a bounded fetch-first,
deterministic REQ-202 ordering, and the authority-only item precheck the
serial runner invokes at item start and again immediately before merge.
The full three-source union (takeover records + landable estate
dispositions) and identity-conflict blocking land with the collector task.

Shell consumers read this module's output ONLY through the closed
``get-scalar`` (one validated UTF-8 line) and ``emit-array0``
(NUL-delimited) accessors.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

MAX_ITEMS = 10
NON_PRODUCTION_PREFIXES = ("tests/", "lib/tests/", "docs/", ".planning/", "specs/")
SCALAR_FIELDS = frozenset({
    "schema", "base", "count", "fetched", "truncated",
    "branch", "head", "run_id", "spec_id", "status", "reason", "unblock",
    "merge_sha", "residual_count", "production_touch", "committed_epoch",
})
ARRAY_FIELDS = frozenset({"changed_files", "production_files", "sources"})


class CollectError(RuntimeError):
    pass


# ── Git authority ─────────────────────────────────────────────────────────


def run_git(repo, *args, timeout=120, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise CollectError(
            f"git {' '.join(args[:2])} failed rc={proc.returncode}: "
            f"{proc.stderr.strip()[:200]}")
    return proc


def fetch_first(repo, timeout=120):
    """REQ-201: remote truth precedes intake; --prune drops dead branches."""
    run_git(repo, "fetch", "--prune", "origin", timeout=timeout)


def base_ref(repo, base):
    probe = run_git(repo, "rev-parse", "--verify", "--quiet",
                    f"refs/remotes/origin/{base}", check=False)
    return f"refs/remotes/origin/{base}" if probe.returncode == 0 else base


def resolve_head(repo, branch):
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        probe = run_git(repo, "rev-parse", "--verify", "--quiet", ref, check=False)
        if probe.returncode == 0:
            return probe.stdout.strip()
    return None


def is_ancestor(repo, commit, ref):
    return run_git(repo, "merge-base", "--is-ancestor", commit, ref,
                   check=False).returncode == 0


def object_exists(repo, sha):
    return run_git(repo, "cat-file", "-e", f"{sha}^{{commit}}",
                   check=False).returncode == 0


def merge_tree_conflicts(repo, bref, head):
    probe = run_git(repo, "merge-tree", "--write-tree", "--no-messages",
                    bref, head, check=False)
    if probe.returncode == 0:
        return False
    if probe.returncode == 1:
        return True
    raise CollectError(f"merge-tree failed rc={probe.returncode}")


def changed_files(repo, bref, head):
    out = run_git(repo, "diff", "--no-renames", "--name-only", "-z",
                  f"{bref}...{head}").stdout
    return [entry for entry in out.split("\0") if entry]


def committed_epoch(repo, head):
    probe = run_git(repo, "log", "-1", "--format=%ct", head, check=False)
    text = probe.stdout.strip()
    return int(text) if probe.returncode == 0 and text.isdigit() else 0


# ── Pure helpers ──────────────────────────────────────────────────────────


def is_production_path(path):
    if path.startswith(NON_PRODUCTION_PREFIXES):
        return False
    if path.endswith(".md") and "/" not in path:
        return False  # root-level Markdown is non-production
    return True


def production_files(paths):
    """Closed non-production allowlist; everything else is production-touching."""
    return [path for path in paths if is_production_path(path)]


def order_queue(items):
    """REQ-202: transitive overlap components oldest-first, then singletons
    by ascending residual file count; branch name breaks every tie."""
    count = len(items)
    file_sets = [set(item.get("changed_files") or []) for item in items]
    parent = list(range(count))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(count):
        for j in range(i + 1, count):
            if file_sets[i] & file_sets[j]:
                parent[find(i)] = find(j)

    groups = {}
    for i in range(count):
        groups.setdefault(find(i), []).append(items[i])

    multi, singles = [], []
    for members in groups.values():
        rows = sorted(members,
                      key=lambda it: (it.get("committed_epoch") or 0, it["branch"]))
        if len(rows) > 1:
            multi.append(rows)
        else:
            singles.append(rows[0])
    # ponytail: components run before singletons — a deterministic v1 choice;
    # interleave by age only if a real queue proves it matters.
    multi.sort(key=lambda rows: (rows[0].get("committed_epoch") or 0,
                                 rows[0]["branch"]))
    singles.sort(key=lambda it: (it.get("residual_count",
                                        len(it.get("changed_files") or [])),
                                 it["branch"]))
    return [item for rows in multi for item in rows] + singles


# ── Collection (tracer slice: explicit source) ────────────────────────────


def collect(repo, base, explicit=(), fetch=True):
    repo = str(repo)
    fetched = False
    if fetch:
        fetch_first(repo)
        fetched = True

    items, conflicts = [], []
    bref = base_ref(repo, base)
    for branch in sorted({str(branch) for branch in explicit}):
        head = resolve_head(repo, branch)
        if head is None:
            conflicts.append({
                "branch": branch, "status": "BLOCKED:source-missing",
                "reason": "no git ref for this branch",
                "unblock": f"push {branch} to origin or remove it from intake",
                "sources": ["explicit"],
            })
            continue
        try:
            files = changed_files(repo, bref, head)
        except CollectError:
            files = []
        prod = production_files(files)
        items.append({
            "branch": branch,
            "head": head,
            "run_id": None,
            "spec_id": None,
            "sources": ["explicit"],
            "changed_files": files,
            "production_files": prod,
            "production_touch": bool(prod),
            "residual_count": len(files),
            "committed_epoch": committed_epoch(repo, head),
        })

    items = order_queue(items)
    truncated = len(items) > MAX_ITEMS
    if truncated:
        items = items[:MAX_ITEMS]
    return {"schema": 1, "base": base, "fetched": fetched, "items": items,
            "conflicts": conflicts, "count": len(items), "truncated": truncated}


# ── Item-start / pre-merge precheck (REQ-203, EDGE-005) ───────────────────


def precheck(repo, base, branch, head=None, fetch=True):
    """Authority-only recheck from Git facts alone — zero model calls."""
    repo = str(repo)
    if fetch:
        fetch_first(repo)
    bref = base_ref(repo, base)
    result = {"branch": branch, "base": base,
              "status": "OK", "reason": "", "unblock": ""}

    current = resolve_head(repo, branch)
    if current:
        result["head"] = current
        if is_ancestor(repo, current, bref):
            result.update(status="SKIPPED:already-landed",
                          reason=f"{current} is already reachable from {bref}")
            return result
        if merge_tree_conflicts(repo, bref, current):
            result.update(
                status="BLOCKED:conflict",
                reason=f"trial merge of {branch} onto {bref} conflicts",
                unblock=f"git -C {repo} rebase origin/{base} {branch}")
            return result
        result["reason"] = f"{branch} rebases cleanly onto {bref}"
        return result

    result["head"] = head or ""
    if head and object_exists(repo, head) and is_ancestor(repo, head, bref):
        result.update(status="LANDED",
                      reason="recorded head is reachable from base",
                      merge_sha=head)
        return result
    result.update(
        status="BLOCKED:source-missing",
        reason=f"branch {branch} is gone and no landing proof exists",
        unblock=(f"git -C {repo} branch {branch} {head}" if head
                 else f"restore branch {branch} from its owner, then re-run intake"))
    return result


# ── Closed shell accessors ────────────────────────────────────────────────


def _node(doc, item_index):
    if item_index is None:
        return doc
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list) or not 0 <= item_index < len(items):
        raise CollectError("item index out of range")
    return items[item_index]


def get_scalar(doc, item_index, field):
    if field not in SCALAR_FIELDS:
        raise CollectError(f"field not allowlisted: {field!r}")
    node = _node(doc, item_index)
    if not isinstance(node, dict) or field not in node:
        raise CollectError(f"missing field: {field}")
    value = node[field]
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        raise CollectError(f"non-scalar value for field: {field}")
    if any(ch in value for ch in "\0\r\n"):
        raise CollectError(f"scalar contains forbidden control bytes: {field}")
    return value


def emit_array0(doc, item_index, field):
    if field not in ARRAY_FIELDS:
        raise CollectError(f"array field not allowlisted: {field!r}")
    node = _node(doc, item_index)
    if not isinstance(node, dict) or not isinstance(node.get(field), list):
        raise CollectError(f"missing array field: {field}")
    values = node[field]
    for value in values:
        if not isinstance(value, str) or "\0" in value:
            raise CollectError(f"non-string or NUL array element in: {field}")
    return values


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv=None):
    parser = argparse.ArgumentParser(prog="collect-queue.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect")
    p.add_argument("--repo", required=True)
    p.add_argument("--base", default="main")
    p.add_argument("--explicit", action="append", default=[])
    p.add_argument("--no-fetch", action="store_true")

    p = sub.add_parser("precheck")
    p.add_argument("--repo", required=True)
    p.add_argument("--base", default="main")
    p.add_argument("--branch", required=True)
    p.add_argument("--head")
    p.add_argument("--no-fetch", action="store_true")

    for name in ("get-scalar", "emit-array0"):
        p = sub.add_parser(name)
        p.add_argument("--doc", required=True)
        p.add_argument("--item")
        p.add_argument("--field", required=True)

    ns = parser.parse_args(argv)
    try:
        if ns.cmd == "collect":
            doc = collect(repo=ns.repo, base=ns.base, explicit=ns.explicit,
                          fetch=not ns.no_fetch)
            print(json.dumps(doc, sort_keys=True))
            return 0
        if ns.cmd == "precheck":
            result = precheck(repo=ns.repo, base=ns.base, branch=ns.branch,
                              head=ns.head, fetch=not ns.no_fetch)
            print(json.dumps(result, sort_keys=True))
            return 0
        doc = json.loads(Path(ns.doc).read_text())
        index = int(ns.item) if ns.item is not None else None
        if ns.cmd == "get-scalar":
            sys.stdout.write(get_scalar(doc, index, ns.field) + "\n")
            return 0
        for value in emit_array0(doc, index, ns.field):
            sys.stdout.write(value + "\0")
        return 0
    except (CollectError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"COLLECT-QUEUE-ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
