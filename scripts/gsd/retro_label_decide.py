#!/usr/bin/env python3
"""Pure decision boundary for the retro-label workflow.

The workflow supplies one bounded GitHub snapshot, either on stdin (useful for
tests) or as the sole argv path.  This module never performs network I/O and
never treats the producer's local occurrence total as authoritative.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any


MAX_COUNT = 2_147_483_647
BODY_META = re.compile(
    r"^<!-- ffs-retro fingerprint:([0-9a-f]{16}) priority:(P[0-3]) "
    r"occurrences:([1-9][0-9]{0,9}) -->$",
    re.MULTILINE,
)
COMMENT_META = re.compile(
    r"\AAdditional occurrence recorded by ffs-retro\.\n\n"
    r"<!-- ffs-retro fingerprint:([0-9a-f]{16}) priority:(P[0-3]) "
    r"occurrences:([1-9][0-9]{0,9}) -->\n\Z"
)
PRIORITIES = ("P0", "P1", "P2", "P3")
MANAGED_PRIORITIES = {f"priority/{priority}" for priority in PRIORITIES}


def empty() -> dict[str, Any]:
    return {
        "labels_to_add": [],
        "priority_labels_to_remove": [],
        "body": None,
        "expected_body": None,
    }


def canonical_count(text: str) -> int | None:
    value = int(text)
    return value if value <= MAX_COUNT else None


def is_bot(author: object) -> bool:
    if not isinstance(author, dict):
        return True
    login, author_type = author.get("login"), author.get("type")
    return author_type == "Bot" or (isinstance(login, str) and login.endswith("[bot]"))


def label_names(labels: object) -> list[str]:
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.append(name)
    return sorted(set(names))


def parse_issue(body: object) -> tuple[re.Match[str], str, str, int] | None:
    if not isinstance(body, str):
        return None
    matches = list(BODY_META.finditer(body))
    if len(matches) != 1:
        return None
    match = matches[0]
    count = canonical_count(match.group(3))
    return (match, match.group(1), match.group(2), count) if count is not None else None


def replacement(body: str, match: re.Match[str], count: int) -> str:
    # Only the captured count changes; all surrounding bytes stay untouched.
    start, end = match.span(3)
    return body[:start] + str(count) + body[end:]


def recount(comments: object, fingerprint: str) -> int:
    if not isinstance(comments, list):
        return 1
    authors: set[str] = set()
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author")
        if is_bot(author) or not isinstance(author, dict):
            continue
        login = author.get("login")
        marker = COMMENT_META.fullmatch(comment.get("body", ""))
        if not isinstance(login, str) or not login or marker is None:
            continue
        embedded_count = canonical_count(marker.group(3))
        if embedded_count is not None and marker.group(1) == fingerprint:
            authors.add(login)
    return min(MAX_COUNT, 1 + len(authors))


def decide(snapshot: object) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("skip") or snapshot.get("truncated") is True:
        return empty()
    if snapshot.get("pull_request") is True or snapshot.get("actor") == "github-actions[bot]":
        return empty()

    issue = snapshot.get("issue")
    if not isinstance(issue, dict):
        return empty()
    parsed = parse_issue(issue.get("body"))
    if parsed is None:
        return empty()
    match, fingerprint, priority, current_count = parsed
    issue_body = issue["body"]
    labels = label_names(issue.get("labels"))
    desired = [f"priority/{priority}", "source/ffs-retro", "triage"]
    remove = sorted(label for label in labels if label in MANAGED_PRIORITIES and label != desired[0])
    add = [label for label in desired if label not in labels]
    new_body: str | None = None

    # The workflow trigger is already restricted to comment creation.  Keeping
    # this pure boundary keyed to the event lets callers supply minimal
    # snapshots without weakening the workflow's delivery guard.
    if snapshot.get("event") == "issue_comment":
        target = recount(snapshot.get("comments"), fingerprint)
        if target != current_count:
            new_body = replacement(issue_body, match, target)

    if not add and not remove and new_body is None:
        return empty()
    return {
        "labels_to_add": add,
        "priority_labels_to_remove": remove,
        "body": new_body,
        "expected_body": issue_body,
        # The workflow compare-and-skip gate also rejects a stale label snapshot.
        "expected_labels": labels,
    }


def load_snapshot() -> object:
    source = open(sys.argv[1], encoding="utf-8") if len(sys.argv) == 2 else sys.stdin
    try:
        return json.load(source)
    except (OSError, json.JSONDecodeError):
        return {"skip": "invalid-snapshot"}
    finally:
        if source is not sys.stdin:
            source.close()


if __name__ == "__main__":
    print(json.dumps(decide(load_snapshot()), separators=(",", ":")))
