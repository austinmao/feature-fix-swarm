#!/usr/bin/env python3
"""Collect deterministic facts about every worktree + branch in a repo estate.

Emits JSON on stdout. No LLM, no network except an optional `gh` PR lookup.
Read-only: never checks out, never fetches, never deletes.

Usage:
  python3 collect-estate.py [--repo PATH] [--base main] [--no-gh]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import socket
from collections import defaultdict

TEST_PAT = re.compile(
    r"(^|/)(tests?|__tests__|e2e|bats|fixtures)/|"
    r"\.(test|spec)\.[jt]sx?$|"
    r"(^|/)test_[^/]+\.py$|[^/]+_test\.(py|go|sh)$|"
    r"\.bats$|(^|/)conftest\.py$",
    re.I,
)
SRC_PAT = re.compile(r"\.(py|[jt]sx?|go|rs|sh|sql)$", re.I)
DOC_PAT = re.compile(r"\.(md|ya?ml|json|txt)$|(^|/)(docs|specs|openwiki|\.planning)/", re.I)
# Bookkeeping paths: per-run planning churn that must never make a branch look
# like it still owes main real work.
BOOKKEEPING_PAT = re.compile(r"^(\.planning/|spike-results/|specs/)")


def sh(args, cwd, timeout=60):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        return (r.stdout if "-z" in args else r.stdout.strip()), r.returncode
    except Exception as e:  # noqa: BLE001 - collector must never crash the audit
        return f"ERROR:{e}", 1


def worktrees(repo):
    out, rc = sh(["git", "worktree", "list", "--porcelain", "-z"], repo)
    if rc:
        raise RuntimeError("Cannot read worktree registry")
    wts, cur = [], {}
    for field in out.split("\0"):
        if not field:
            if cur:
                wts.append(cur)
            cur = {}
            continue
        k, _, v = field.partition(" ")
        if k == "worktree":
            cur["path"] = os.path.realpath(v)
            cur["registered_path"] = v
        elif k == "HEAD":
            cur["head"] = v
        elif k == "branch":
            cur["branch"] = v.removeprefix("refs/heads/")
        elif k == "detached":
            cur["branch"] = None
        elif k in ("locked", "prunable"):
            cur[k] = True
            cur[k + "_reason"] = v
    if cur:
        wts.append(cur)
    # A legacy alias does not create another estate; Git common-dir identity
    # is attached to this audit below and independent clones run separately.
    expected, rc = sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], repo)
    if rc:
        raise RuntimeError("Cannot determine worktree registry owner")
    expected = os.path.realpath(expected)
    unique = {}
    for wt in wts:
        owner, rc = sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], wt["path"])
        wt["common_dir"] = os.path.realpath(owner) if rc == 0 else None
        wt["ownership_error"] = rc != 0 or wt["common_dir"] != expected
        admin, admin_rc = sh(["git", "rev-parse", "--absolute-git-dir"], wt["path"])
        wt["ownership_error"] = wt["ownership_error"] or admin_rc != 0
        wt["primary"] = not wt["ownership_error"] and os.path.realpath(admin) == expected
        key = (wt["path"], wt["common_dir"])
        if key not in unique:
            unique[key] = wt
        else:
            unique[key].setdefault("aliases", []).append(wt["registered_path"])
            for flag in ("locked", "prunable"):
                if wt.get(flag):
                    unique[key][flag] = True
    return list(unique.values())


def dirty(path):
    out, rc = sh(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], path)
    if rc != 0:
        return -1
    entries, count, i = out.split("\0"), 0, 0
    while i < len(entries):
        entry = entries[i]
        if entry:
            count += 1
            if "R" in entry[:2] or "C" in entry[:2]:
                i += 1  # -z rename/copy has one extra pathname field.
        i += 1
    return count


def ignored(path):
    out, rc = sh(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"], path)
    return -1 if rc else sum(bool(x) for x in out.split("\0"))


def exact_merged_pr(pr, head, base):
    return bool(pr and pr.get("mergedAt") and pr.get("headRefOid") == head
                and pr.get("baseRefName") == base)


def diff_sets(repo, base, ref):
    """Return (added_files, residual_files, residual_code_files).

    added    = three-dot: what this ref contributed since its merge-base.
    residual = added files whose blob STILL differs from base. A squash-merged
               branch has added>0 but residual==0 — `merge-base --is-ancestor`
               alone reports it unmerged, which is the false-positive this
               function exists to kill.
    """
    a_raw, a_rc = sh(["git", "diff", "--no-renames", "--name-only", "-z", f"{base}...{ref}"], repo, timeout=120)
    b_raw, b_rc = sh(["git", "diff", "--no-renames", "--name-only", "-z", f"{base}..{ref}"], repo, timeout=120)
    if a_rc or b_rc:
        raise RuntimeError("Cannot compare branch content; retain branch")
    added = [f for f in a_raw.split("\0") if f]
    differing = {f for f in b_raw.split("\0") if f}
    residual = [f for f in added if f in differing]
    residual_code = [f for f in residual if not BOOKKEEPING_PAT.match(f)]
    return added, residual, residual_code


def spec_id_of(branch, files):
    """Derive spec number from branch name, else from touched specs/NNN- paths."""
    m = re.match(r"^(?:spec)?(\d{3})[-a-z]", branch or "")
    if m:
        return m.group(1)
    m = re.search(r"(\d{3})", branch or "")
    if m and m.group(1) != "000":
        return m.group(1)
    counts = defaultdict(int)
    for f in files:
        mm = re.match(r"^specs/(\d{3})-", f)
        if mm:
            counts[mm.group(1)] += 1
    return max(counts, key=counts.get) if counts else None


def spec_artifacts(repo, spec_id):
    """Which spec artifacts exist on disk, and tasks.md checkbox progress."""
    if not spec_id:
        return None
    out, _ = sh(["bash", "-c", f"ls -d specs/{spec_id}-*/ 2>/dev/null | head -1"], repo)
    if not out or out.startswith("ERROR:"):
        return {"dir": None}
    d = out.rstrip("/")
    info = {"dir": d}
    for name in ("spec.md", "plan.md", "tasks.md"):
        info[name] = os.path.isfile(os.path.join(repo, d, name))
    tpath = os.path.join(repo, d, "tasks.md")
    if os.path.isfile(tpath):
        try:
            txt = open(tpath, encoding="utf-8", errors="replace").read()
            info["tasks_done"] = len(re.findall(r"^\s*- \[[xX]\]", txt, re.M))
            info["tasks_open"] = len(re.findall(r"^\s*- \[ \]", txt, re.M))
        except OSError:
            pass
    ev = os.path.join(repo, d, "evidence")
    info["evidence_files"] = len(os.listdir(ev)) if os.path.isdir(ev) else 0
    return info


def planning_state(wt_path):
    """gsd .planning state for a worktree: ROADMAP phase flips + STATE.md status."""
    p = os.path.join(wt_path, ".planning")
    if not os.path.isdir(p):
        return None
    st = {}
    rm = os.path.join(p, "ROADMAP.md")
    if os.path.isfile(rm):
        try:
            txt = open(rm, encoding="utf-8", errors="replace").read()
            st["phases_done"] = len(re.findall(r"^\s*- \[[xX]\]\s*(?:###\s*)?Phase", txt, re.M | re.I))
            st["phases_open"] = len(re.findall(r"^\s*- \[ \]\s*(?:###\s*)?Phase", txt, re.M | re.I))
            if not st["phases_done"] and not st["phases_open"]:
                st["phases_done"] = len(re.findall(r"^\s*- \[[xX]\]", txt, re.M))
                st["phases_open"] = len(re.findall(r"^\s*- \[ \]", txt, re.M))
        except OSError:
            pass
    sm = os.path.join(p, "STATE.md")
    if os.path.isfile(sm):
        try:
            txt = open(sm, encoding="utf-8", errors="replace").read()
            for key in ("status", "current_phase", "current_phase_name", "last_updated"):
                m = re.search(rf"^{key}:\s*(.+)$", txt, re.M)
                if m:
                    st[key] = m.group(1).strip().strip('"')
        except OSError:
            pass
    pid = os.path.join(p, "run-state", "gsd-run.pid")
    if os.path.isfile(pid):
        st["runner_pid_alive"] = None
        try:
            n = int(open(pid).read().split()[0])
            os.kill(n, 0)
            st["runner_pid_alive"] = n
        except (OSError, ValueError, IndexError):
            pass
    return st


FINISHED_STATUS = re.compile(r"\b(complete|completed|done|shipped|archived)\b", re.I)


def session_hold(planning):
    """True when gsd session state reserves the worktree.

    A run PID file (live or stale) or an unfinished STATE.md status means a
    stopped or resumable session may still own the work; a dead PID is not
    deletion proof.
    """
    if not planning:
        return False
    if "runner_pid_alive" in planning:
        return True
    status = planning.get("status")
    return bool(status) and not FINISHED_STATUS.search(status)


def classify(rec):
    """One disposition per branch — the consolidation plan's unit of work.

    delete-safe        content is in base; branch is pure residue
    merge-ready        open PR, mergeable, base already carries its parents
    pr-needs-attention open PR that is dirty/blocked/draft
    review-then-land   real residual code, no PR yet
    docs-only          residual is documentation/bookkeeping only
    stale-abandoned    PR closed unmerged, or untouched >30d with residual
    """
    pr = rec.get("pr") or {}
    if rec.get("worktree_current"):
        return "retain-current-worktree"
    if rec.get("worktree_primary"):
        return "retain-primary-worktree"
    if rec.get("worktree") and (
        rec.get("worktree_dirty", -1) != 0
        or rec.get("worktree_ignored", -1) != 0
        or rec.get("worktree_locked") or rec.get("worktree_prunable")
        or rec.get("worktree_ownership_error")
        or session_hold(rec.get("planning"))
    ):
        return "retain-worktree-payload"
    if rec.get("landed"):
        return "delete-safe"
    if pr.get("state") == "OPEN":
        if pr.get("draft"):
            return "pr-needs-attention"
        return "merge-ready" if pr.get("mergeState") == "CLEAN" else "pr-needs-attention"
    if pr.get("state") == "CLOSED" and not pr.get("merged"):
        return "stale-abandoned"
    if rec.get("residual_code", 0) == 0:
        return "docs-only"
    return "review-then-land"


def gh_prs(repo):
    """branch -> PR record. Includes closed/merged so we can spot landed work."""
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--limit", "400", "--json",
             "number,title,state,headRefName,headRefOid,baseRefName,mergeStateStatus,mergedAt,isDraft,url"],
            cwd=repo, capture_output=True, text=True, timeout=180, env=env,
        )
        if r.returncode != 0:
            return {}, f"gh failed rc={r.returncode}"
        out = {}
        for p in json.loads(r.stdout):
            b = p["headRefName"]
            if b not in out or p["number"] > out[b]["number"]:
                out[b] = p
        return out, None
    except Exception as e:  # noqa: BLE001
        return {}, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default="main")
    ap.add_argument("--no-gh", action="store_true")
    args = ap.parse_args()
    requested_repo = os.path.abspath(args.repo)
    top, root_rc = sh(["git", "rev-parse", "--show-toplevel"], requested_repo)
    if root_rc:
        print(json.dumps({"error": "Repository unavailable; check the requested checkout and mounted storage", "requested_repo": requested_repo}))
        return 1
    repo = os.path.realpath(top)
    common, common_rc = sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], repo)
    if common_rc:
        raise RuntimeError("Cannot determine physical repository ownership")
    common = os.path.realpath(common)
    common_stat = os.stat(common)
    base = args.base

    base_sha, rc = sh(["git", "rev-parse", base], repo)
    if rc != 0:
        print(json.dumps({"error": f"base {base} not found: {base_sha}"}))
        return 1

    wts = worktrees(repo)
    wt_by_branch = {w["branch"]: w for w in wts if w.get("branch")}

    prs, gh_err = ({}, "skipped") if args.no_gh else gh_prs(repo)

    out, refs_rc = sh(["git", "for-each-ref", "refs/heads/",
                 "--format=%(refname:short)\t%(committerdate:short)\t%(upstream:short)"], repo)
    if refs_rc:
        raise RuntimeError("Cannot enumerate local branch refs")
    branches = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, date = parts[0], parts[1]
        upstream = parts[2] if len(parts) > 2 else ""
        if name == base:
            continue

        # Pin both sides before any content/ancestry metrics. Later cleanup
        # must re-read live refs against these expected OIDs, never their names.
        tip, tip_rc = sh(["git", "rev-parse", "--verify", "refs/heads/" + name], repo)
        if tip_rc:
            raise RuntimeError("Cannot pin local branch tip")
        is_merged = sh(["git", "merge-base", "--is-ancestor", tip, base_sha], repo)[1] == 0

        ab, _ = sh(["git", "rev-list", "--left-right", "--count", f"{base_sha}...{tip}"], repo)
        try:
            behind, ahead = (int(x) for x in ab.split())
        except ValueError:
            behind = ahead = -1

        files, residual, residual_code = diff_sets(repo, base_sha, tip)

        tests = [f for f in files if TEST_PAT.search(f)]
        srcs = [f for f in files if SRC_PAT.search(f) and not TEST_PAT.search(f)]
        docs = [f for f in files if DOC_PAT.search(f) and f not in tests and f not in srcs]

        sid = spec_id_of(name, files)
        wt = wt_by_branch.get(name)

        rec = {
            "branch": name,
            "last_commit": date,
            "upstream": upstream or None,
            "merged_into_base": is_merged,
            "ahead": ahead,
            "behind": behind,
            "files_changed": len(files),
            "residual_files": len(residual),
            "residual_code": len(residual_code),
            "residual_code_sample": sorted(residual_code)[:12],
            "src_files": len(srcs),
            "test_files": len(tests),
            "doc_files": len(docs),
            "test_gap": bool(srcs) and not tests,
            "spec_id": sid,
            "spec": spec_artifacts(repo, sid),
            "worktree": wt["path"] if wt else None,
            "worktree_dirty": dirty(wt["path"]) if wt else None,
            "worktree_current": bool(wt and wt["path"] == repo),
            "worktree_primary": bool(wt and wt.get("primary")),
            "worktree_ownership_error": bool(wt and wt.get("ownership_error")),
            "worktree_ignored": ignored(wt["path"]) if wt else None,
            "worktree_locked": bool(wt and wt.get("locked")),
            "worktree_prunable": bool(wt and wt.get("prunable")),
            "planning": planning_state(wt["path"]) if wt else None,
        }
        pr = prs.get(name)
        if pr:
            rec["pr"] = {
                "number": pr["number"], "state": pr["state"],
                "merged": bool(pr.get("mergedAt")), "draft": pr.get("isDraft"),
                "mergeState": pr.get("mergeStateStatus"), "url": pr.get("url"),
            }
        # Keep unique docs/plans too; old merged PRs do not prove a reused tip.
        # Errors above never become an empty-diff proof.
        rec["head"] = tip
        rec["landed"] = bool(is_merged or exact_merged_pr(pr, tip, base) or not residual)
        rec["disposition"] = classify(rec)
        branches.append(rec)

    # Remote branches carrying an OPEN PR but no local ref would be invisible
    # above — an open PR is the single most consolidation-relevant object there is.
    local_names = {b["branch"] for b in branches}
    for bname, pr in prs.items():
        if bname in local_names or pr["state"] != "OPEN":
            continue
        ref = f"origin/{bname}"
        remote_tip, remote_rc = sh(["git", "rev-parse", "--verify", "-q", ref], repo)
        if remote_rc != 0:
            branches.append({
                "branch": bname, "remote_only": True, "ref_missing": True,
                "landed": False, "disposition": "pr-open-ref-unfetched",
                "pr": {"number": pr["number"], "state": pr["state"], "merged": False,
                       "draft": pr.get("isDraft"), "mergeState": pr.get("mergeStateStatus"),
                       "url": pr.get("url")},
            })
            continue
        files, residual, residual_code = diff_sets(repo, base_sha, remote_tip)
        tests = [f for f in files if TEST_PAT.search(f)]
        srcs = [f for f in files if SRC_PAT.search(f) and not TEST_PAT.search(f)]
        sid = spec_id_of(bname, files)
        # L2 (ship round 5): sh() returns "ERROR:..." text on failure —
        # int() over that crashed the whole audit.  Mirror the
        # committed_epoch guard: digits-or-sentinel, never a raw int().
        ahead_out, ahead_rc = sh(["git", "rev-list", "--count", f"{base_sha}..{remote_tip}"], repo)
        rec = {
            "branch": bname, "head": remote_tip, "remote_only": True, "last_commit": None,
            "upstream": ref, "merged_into_base": False,
            "ahead": int(ahead_out) if ahead_rc == 0 and ahead_out.isdigit() else -1,
            "behind": None, "files_changed": len(files),
            "residual_files": len(residual), "residual_code": len(residual_code),
            "residual_code_sample": sorted(residual_code)[:12],
            "src_files": len(srcs), "test_files": len(tests),
            "doc_files": len(files) - len(srcs) - len(tests),
            "test_gap": bool(srcs) and not tests,
            "spec_id": sid, "spec": spec_artifacts(repo, sid),
            "worktree": None, "worktree_dirty": None, "planning": None,
            "pr": {"number": pr["number"], "state": pr["state"], "merged": False,
                   "draft": pr.get("isDraft"), "mergeState": pr.get("mergeStateStatus"),
                   "url": pr.get("url")},
            "landed": False,
        }
        rec["disposition"] = classify(rec)
        branches.append(rec)

    detached = [w for w in wts if not w.get("branch")]
    for w in detached:
        w["dirty"] = dirty(w["path"])
        w["ignored"] = ignored(w["path"])
        w["cleanup_candidate"] = False  # Ancestor HEAD alone never authorizes removal.
        w["on_base"] = sh(["git", "merge-base", "--is-ancestor", w["head"], base_sha], repo)[1] == 0

    result = {
        "repo": repo,
        "cleanup_authorized": False,
        "activity_verified": False,
        "requested_repo": requested_repo,
        "executing_host": socket.gethostname(),
        "repository_identity": {"common_dir": common, "device": common_stat.st_dev,
                                "inode": common_stat.st_ino},
        "worktrees": wts,
        "base": base,
        "base_sha": base_sha[:10],
        "base_oid": base_sha,
        "counts": {
            "worktrees": len(wts),
            "detached_worktrees": len(detached),
            "branches": len(branches),
            "landed": sum(1 for b in branches if b.get("landed")),
            "not_landed": sum(1 for b in branches if not b.get("landed")),
            "with_open_pr": sum(1 for b in branches if (b.get("pr") or {}).get("state") == "OPEN"),
            "test_gaps": sum(1 for b in branches if b.get("test_gap") and not b.get("landed")),
            "dirty_worktrees": sum(1 for b in branches if (b.get("worktree_dirty") or 0) > 0),
            "by_disposition": {
                k: sum(1 for b in branches if b.get("disposition") == k)
                for k in sorted({b.get("disposition") for b in branches if b.get("disposition")})
            },
        },
        "gh_error": gh_err,
        "branches": sorted(branches, key=lambda b: (b.get("landed", False), -(b.get("residual_code") or 0))),
        "detached_worktrees": detached,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError) as exc:
        print(json.dumps({"error": str(exc), "cleanup_authorized": False}))
        sys.exit(1)
