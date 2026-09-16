#!/usr/bin/env python3
"""Fail-closed validator for a repository-contained native plan-wall report.

It validates provenance and emits only the per-plan findings object plus the
actual reviewer/producer identities; plan-wall.sh remains the sole writer of
queue rows and wall records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

MAX_BYTES = 1_048_576
SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
VENDORS = {None, "openai", "anthropic"}


def fail(message: str) -> None:
    print(f"native-plan-wall-import: {message}", file=sys.stderr)
    raise SystemExit(78)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inside_regular(repo: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo)
    except (FileNotFoundError, ValueError):
        fail("report must be a regular file contained by the repository")
    if path.is_symlink() or not resolved.is_file():
        fail("report must be a non-symlink regular file")
    if resolved.stat().st_size > MAX_BYTES:
        fail("report exceeds bounded import size")
    return resolved


def finding_ok(item: object) -> bool:
    if not isinstance(item, dict) or set(item) != {"severity", "file", "claim", "line", "repro", "vendor", "confidence"}:
        return False
    return (
        item["severity"] in SEVERITIES
        and isinstance(item["file"], str) and bool(item["file"])
        and isinstance(item["claim"], str) and bool(item["claim"])
        and (item["line"] is None or isinstance(item["line"], int) and item["line"] >= 1)
        and (item["repro"] is None or isinstance(item["repro"], str))
        and item["vendor"] in VENDORS
        and (item["confidence"] is None or isinstance(item["confidence"], (int, float)) and not isinstance(item["confidence"], bool) and 0 <= item["confidence"] <= 1)
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--phase", required=True)
    p.add_argument("--plans", nargs="+", required=True, help="repo-relative plan paths")
    p.add_argument("--plan", required=True, help="the plan currently being recorded")
    p.add_argument("--socratic", required=True, help="repo-relative Socratic document")
    p.add_argument("--configured-planner", required=True)
    args = p.parse_args()
    repo = Path(args.repo).resolve()
    report_path = inside_regular(repo, args.report)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("report is not valid UTF-8 JSON")
    if not isinstance(report, dict) or report.get("schema") != "ffs.native-plan-wall-import/v1":
        fail("missing exact native import schema")
    if report.get("run_id") != args.run_id or report.get("phase") != args.phase:
        fail("run_id or phase provenance does not match this wall invocation")
    reviewer = report.get("reviewer")
    if not isinstance(reviewer, dict) or set(reviewer) != {"model", "transport"} or not all(isinstance(reviewer[k], str) and reviewer[k] for k in reviewer):
        fail("reviewer must carry non-empty actual model and transport")
    expected = {path: sha(repo / path) for path in args.plans}
    plans = report.get("plans")
    if not isinstance(plans, list) or len(plans) != len(expected):
        fail("report plan set is incomplete or has extras")
    seen: set[str] = set()
    producers: dict[str, str] = {}
    for item in plans:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "producer_model", "findings"}:
            fail("each plan provenance entry must have path, sha256, producer_model, findings")
        path, digest, producer = item["path"], item["sha256"], item["producer_model"]
        if not isinstance(path, str) or path in seen or path not in expected:
            fail("report has a missing, duplicate, or extra plan")
        # Existing wall records identify the planner from current configuration,
        # not an unverifiable historical process. Preserve that contract: the
        # report may annotate historical provenance, but its per-plan producer
        # must equal the planner identity the wall itself resolves now.
        if digest != expected[path] or producer != args.configured_planner or producer == reviewer["model"]:
            fail("plan digest is stale or configured producer/reviewer provenance is invalid")
        if not isinstance(item["findings"], list) or not all(finding_ok(finding) for finding in item["findings"]):
            fail("plan findings do not match the canonical finding schema")
        seen.add(path); producers[path] = producer
    socratic = report.get("socratic")
    if not isinstance(socratic, dict) or set(socratic) != {"path", "sha256"} or socratic.get("path") != args.socratic or socratic.get("sha256") != sha(repo / args.socratic):
        fail("Socratic provenance is missing or stale")
    selected = next((item for item in plans if item["path"] == args.plan), None)
    if selected is None:
        fail("requested plan has no provenance entry")
    print(json.dumps({"findings": selected["findings"], "reviewer_model": reviewer["model"], "reviewer_transport": reviewer["transport"], "producer_models": producers}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
