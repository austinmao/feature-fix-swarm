#!/usr/bin/env python3
"""collect-queue.py — deterministic land-queue intake and item prechecks.

Three-source intake (REQ-201): takeover records, landable collect-estate
dispositions, and explicit arguments are unioned after a bounded
fetch-first; identity is ``(branch, head SHA)`` and any disagreement on
head, run id, or spec id becomes a ``BLOCKED:identity-conflict`` record —
never a richest-record merge.

Deterministic ordering (REQ-202): transitive file-overlap components run
oldest-first; disjoint singletons sort by residual file count.

Authority-only precheck (REQ-203 / EDGE-005): every branch is rechecked at
item start from Git and PR facts alone — no model or reviewer call exists
anywhere in this module.

Shell consumers read this module's output ONLY through the closed
``get-scalar`` (one validated UTF-8 line) and ``emit-array0``
(NUL-delimited) accessors.
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import subprocess
import sys
from pathlib import Path

MAX_ITEMS = 10
LANDABLE_DISPOSITIONS = {"merge-ready", "review-then-land", "docs-only"}
NON_PRODUCTION_PREFIXES = ("tests/", "lib/tests/", "docs/", ".planning/", "specs/")
SCALAR_FIELDS = frozenset({
    "schema", "base", "count", "fetched", "truncated",
    "branch", "head", "run_id", "spec_id", "status", "reason", "unblock",
    "merge_sha", "residual_count", "production_touch", "committed_epoch",
})
ARRAY_FIELDS = frozenset({"changed_files", "production_files", "sources"})

ESTATE_SCRIPT = (Path(__file__).resolve().parents[2]
                 / "git-branch-consolidate" / "scripts" / "collect-estate.py")


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


# ── Sources (REQ-201) ─────────────────────────────────────────────────────


def load_takeover_records(pattern):
    records = []
    for path in sorted(globmod.glob(pattern)):
        try:
            if os.path.getsize(path) > 1024 * 1024:
                continue
            with open(path, encoding="utf-8") as handle:
                row = json.load(handle)
        except (OSError, ValueError):
            continue  # malformed/hostile takeover records never become items
        if not isinstance(row, dict):
            continue
        # H5 (ship round 5): the CANONICAL versioned writer schema
        # (scripts/gsd/takeover-record.py) nests branch/head under
        # git_state and both ids under ids — that shape is authoritative.
        # The legacy flat top-level fields survive only as a trivially
        # cheap fallback for pre-schema records.
        ids = row.get("ids") if isinstance(row.get("ids"), dict) else {}
        git_state = (row.get("git_state")
                     if isinstance(row.get("git_state"), dict) else {})
        branch = git_state.get("branch") or row.get("branch")
        if not branch:
            continue
        records.append({"source": "takeover", "branch": str(branch),
                        "head": git_state.get("head") or row.get("head")
                        or row.get("head_sha"),
                        "run_id": ids.get("run_id") or row.get("run_id"),
                        "spec_id": ids.get("spec_id") or row.get("spec_id")
                        or row.get("spec")})
    return records


def estate_candidates(rows):
    """Eligibility is the landable disposition set — never the landed boolean."""
    records = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("branch"):
            continue
        if row.get("disposition") not in LANDABLE_DISPOSITIONS:
            continue
        records.append({"source": "estate", "branch": str(row["branch"]),
                        "head": row.get("head"), "run_id": row.get("run_id"),
                        "spec_id": row.get("spec_id")})
    return records


def run_estate(repo, base):
    """Wholesale reuse of the existing estate authority (never reimplemented)."""
    proc = subprocess.run(
        [sys.executable, str(ESTATE_SCRIPT), "--repo", str(repo),
         "--base", base, "--no-gh"],
        capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise CollectError(f"collect-estate failed rc={proc.returncode}")
    return json.loads(proc.stdout).get("branches", [])


# ── Collection ────────────────────────────────────────────────────────────


def _identity_conflict(branch, records):
    return {
        "branch": branch,
        "status": "BLOCKED:identity-conflict",
        "reason": "sources disagree on head/run/spec identity",
        "unblock": (f"inspect the disagreeing intake records for {branch}, "
                    "remove or correct the stale source, then re-run intake"),
        "sources": sorted({record["source"] for record in records}),
        # Preserved verbatim — disagreements are never coalesced.
        "candidates": records,
    }


def collect(repo, base, explicit=(), takeover_glob=None, estate=None,
            use_estate=False, fetch=True):
    repo = str(repo)
    fetched = False
    if fetch:
        fetch_first(repo)
        fetched = True

    records = []
    if takeover_glob:
        records.extend(load_takeover_records(takeover_glob))
    estate_rows = estate if estate is not None else (
        run_estate(repo, base) if use_estate else [])
    records.extend(estate_candidates(estate_rows))
    for branch in explicit:
        records.append({"source": "explicit", "branch": str(branch),
                        "head": None, "run_id": None, "spec_id": None})

    by_branch = {}
    for record in records:
        by_branch.setdefault(record["branch"], []).append(record)

    items, conflicts = [], []
    bref = base_ref(repo, base)
    for branch in sorted(by_branch):
        branch_records = by_branch[branch]
        git_head = resolve_head(repo, branch)
        heads = {r.get("head") for r in branch_records if r.get("head")}
        if git_head:
            heads.add(git_head)
        runs = {r.get("run_id") for r in branch_records if r.get("run_id")}
        specs = {r.get("spec_id") for r in branch_records if r.get("spec_id")}
        if len(heads) > 1 or len(runs) > 1 or len(specs) > 1:
            conflicts.append(_identity_conflict(branch, branch_records))
            continue
        head = git_head or (next(iter(heads)) if heads else None)
        if head is None:
            conflicts.append({
                "branch": branch, "status": "BLOCKED:source-missing",
                "reason": "no git ref and no recorded head for this branch",
                "unblock": f"push {branch} to origin or remove it from intake",
                "sources": sorted({r["source"] for r in branch_records}),
                "candidates": branch_records,
            })
            continue
        try:
            files = changed_files(repo, bref, head)
        except CollectError:
            files = []  # gone branch whose objects are absent: precheck decides
        prod = production_files(files)
        items.append({
            "branch": branch,
            "head": head,
            "run_id": next(iter(runs)) if runs else None,
            "spec_id": next(iter(specs)) if specs else None,
            "sources": sorted({r["source"] for r in branch_records}),
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


def precheck(repo, base, branch, head=None, pr_state=None, fetch=True):
    """Authority-only recheck: Git facts plus optional PR facts, zero model calls."""
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
        # Valid for merge/rebase landings only; squash landings never make the
        # source head an ancestor of base and fall through to PR facts.
        result.update(status="LANDED",
                      reason="recorded head is reachable from base",
                      merge_sha=head)
        return result
    if isinstance(pr_state, dict) and pr_state.get("state") == "MERGED":
        merge_commit = pr_state.get("merge_commit")
        if not merge_commit and isinstance(pr_state.get("mergeCommit"), dict):
            merge_commit = pr_state["mergeCommit"].get("oid")
        # 221c8690: squash landed-ness is PR state MERGED plus a merge commit
        # reachable from base — never ancestor-of-source-head.
        if (merge_commit and object_exists(repo, merge_commit)
                and is_ancestor(repo, merge_commit, bref)):
            result.update(status="LANDED",
                          reason="PR merged with merge commit reachable from base",
                          merge_sha=merge_commit)
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
    p.add_argument("--takeover-glob")
    p.add_argument("--estate-json")
    p.add_argument("--use-estate", action="store_true")
    p.add_argument("--no-fetch", action="store_true")

    p = sub.add_parser("precheck")
    p.add_argument("--repo", required=True)
    p.add_argument("--base", default="main")
    p.add_argument("--branch", required=True)
    p.add_argument("--head")
    p.add_argument("--pr-json")
    p.add_argument("--no-fetch", action="store_true")

    for name in ("get-scalar", "emit-array0"):
        p = sub.add_parser(name)
        p.add_argument("--doc", required=True)
        p.add_argument("--item")
        p.add_argument("--field", required=True)

    ns = parser.parse_args(argv)
    try:
        if ns.cmd == "collect":
            estate = None
            if ns.estate_json:
                estate = json.loads(Path(ns.estate_json).read_text()).get("branches", [])
            doc = collect(repo=ns.repo, base=ns.base, explicit=ns.explicit,
                          takeover_glob=ns.takeover_glob, estate=estate,
                          use_estate=ns.use_estate, fetch=not ns.no_fetch)
            print(json.dumps(doc, sort_keys=True))
            return 0
        if ns.cmd == "precheck":
            pr_state = json.loads(Path(ns.pr_json).read_text()) if ns.pr_json else None
            result = precheck(repo=ns.repo, base=ns.base, branch=ns.branch,
                              head=ns.head, pr_state=pr_state,
                              fetch=not ns.no_fetch)
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
