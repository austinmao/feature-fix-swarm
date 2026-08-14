"""Independent Wave-0 contract for the maintainer retro-label workflow.

These assertions deliberately execute the future decision boundary as a child
process; they never duplicate its parsing or recount algorithm in test code.
"""

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/retro-label.yml"
DECIDE = ROOT / "scripts/gsd/retro_label_decide.py"
FP = "0123456789abcdef"
MAX_COUNT = 2147483647
BODY_META = "<!-- ffs-retro fingerprint:{fp} priority:{priority} occurrences:{count} -->"
COMMENT = "Additional occurrence recorded by ffs-retro.\n\n" + BODY_META + "\n"


def strip_comments(text: str) -> str:
    """Drop YAML comments without treating quoted # values as comments."""
    result = []
    for line in text.splitlines():
        quote = None
        kept = []
        for index, char in enumerate(line):
            if quote:
                kept.append(char)
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
                kept.append(char)
            elif char == "#" and (index == 0 or line[index - 1] in " \t"):
                break
            else:
                kept.append(char)
        result.append("".join(kept).rstrip())
    return "\n".join(result)


def section(text: str, key: str, indent: int = 0) -> str:
    out, active = [], False
    prefix = " " * indent + key + ":"
    for line in text.splitlines():
        if not active:
            active = line.startswith(prefix)
        else:
            current = len(line) - len(line.lstrip(" "))
            if line.strip() and current <= indent:
                break
            out.append(line)
    return "\n".join(out)


def body(count: int = 1) -> str:
    return "facts remain byte-preserved\n" + BODY_META.format(fp=FP, priority="P1", count=count) + "\n"


def snapshot(*, event="issues", action="opened", issue_body=None, comments=None, triggering_comment=None, actor="human", pr=False, truncated=False):
    return {
        "event": event,
        "action": action,
        "actor": actor,
        "pull_request": pr,
        "issue": {"number": 31, "body": issue_body if issue_body is not None else body()},
        "comments": comments or [],
        "triggering_comment": triggering_comment,
        "truncated": truncated,
    }


def run_decision(data: dict) -> dict:
    assert DECIDE.is_file(), f"missing decision module: {DECIDE}"
    child = subprocess.run(
        [sys.executable, str(DECIDE)], input=json.dumps(data), text=True,
        capture_output=True, check=False, cwd=ROOT,
    )
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout)


def assert_empty(data: dict) -> None:
    assert run_decision(data) == {"labels_to_add": [], "priority_labels_to_remove": [], "body": None, "expected_body": None}


def test_helper_comment_stripping_and_indentation_contract():
    source = textwrap.dedent("""\
        permissions: # comment cannot satisfy a guard
          contents: read
          issues: write # keep
        quoted: "# value"
    """)
    clean = strip_comments(source)
    assert "comment cannot" not in clean
    assert section(clean, "permissions") == "  contents: read\n  issues: write"


def test_grammar_canonical_metadata_and_comment_bytes():
    marker = BODY_META.format(fp=FP, priority="P2", count=MAX_COUNT)
    assert re.fullmatch(r"<!-- ffs-retro fingerprint:[0-9a-f]{16} priority:P[0-3] occurrences:([1-9][0-9]{0,9}) -->", marker)
    assert COMMENT.format(fp=FP, priority="P1", count=1).endswith(" -->\n")
    assert not re.fullmatch(r"[1-9][0-9]{0,9}", "01")


def test_workflow_trigger_permissions_actor_and_comment_guards():
    assert WORKFLOW.is_file(), f"missing workflow source: {WORKFLOW}"
    text = strip_comments(WORKFLOW.read_text(encoding="utf-8"))
    assert re.search(r"^\s*issues:\s*\n\s*types:\s*\[opened\]", text, re.M)
    assert re.search(r"^\s*issue_comment:\s*\n\s*types:\s*\[created\]", text, re.M)
    permissions = section(text, "permissions")
    assert {line.strip() for line in permissions.splitlines() if line.strip()} == {"contents: read", "issues: write"}
    assert "github-actions[bot]" in text
    assert "pull_request" in text


def test_workflow_label_redelivery_concurrency_and_expected_body_cas():
    assert WORKFLOW.is_file(), f"missing workflow source: {WORKFLOW}"
    text = strip_comments(WORKFLOW.read_text(encoding="utf-8"))
    for label in ("source/ffs-retro", "triage", "priority/P0", "priority/P1", "priority/P2", "priority/P3"):
        assert label in text
    assert "retro-label-" in text and "cancel-in-progress: false" in text
    assert "expected_body" in text and text.count("refetch") >= 1
    assert "actions/checkout@" in text and re.search(r"actions/checkout@[0-9a-f]{40}", text)
    assert "actions/github-script@" in text and re.search(r"actions/github-script@[0-9a-f]{40}", text)
    run_blocks = re.findall(r"^\s*run:\s*\|?\n((?:\s{10,}.*\n?)*)", text, re.M)
    assert any("retro_label_decide.py" in block and "${{" not in block for block in run_blocks)


@pytest.mark.parametrize("bad", ["0", "01", str(MAX_COUNT + 1), "21474836470"])
def test_metadata_rejects_noncanonical_or_out_of_range_occurrence(bad):
    assert_empty(snapshot(issue_body=body().replace("occurrences:1", f"occurrences:{bad}")))


@pytest.mark.parametrize("mutated", [
    "<!-- ffs-retro fingerprint:0123456789abcdeg priority:P1 occurrences:1 -->",
    "<!-- ffs-retro fingerprint:0123456789abcdef priority:P4 occurrences:1 -->",
    "<!-- ffs-retro fingerprint:0123456789abcdef priority:P1 occurrences:1 --> extra",
    "<!-- ffs-retro fingerprint:0123456789abcdef priority:P1 occurrences:1 -->\n<!-- ffs-retro fingerprint:0123456789abcdef priority:P1 occurrences:2 -->",
])
def test_metadata_rejects_fingerprint_version_whitespace_or_ambiguous_forms(mutated):
    assert_empty(snapshot(issue_body="facts\n" + mutated + "\n"))


def test_comment_recount_distinct_human_authors_preserves_body_bytes():
    comments = [
        {"body": COMMENT.format(fp=FP, priority="P1", count=999), "author": {"login": "alice", "type": "User"}},
        {"body": COMMENT.format(fp=FP, priority="P1", count=1), "author": {"login": "bob", "type": "User"}},
        {"body": COMMENT.format(fp=FP, priority="P1", count=1), "author": {"login": "github-actions[bot]", "type": "Bot"}},
    ]
    data = snapshot(event="issue_comment", action="created", comments=comments, triggering_comment=comments[0])
    decision = run_decision(data)
    assert decision["expected_body"] == data["issue"]["body"]
    assert decision["body"] == body(3)
    assert all(key in decision for key in ("labels_to_add", "priority_labels_to_remove", "expected_body"))


def test_comment_occurrence_forgery_is_bounded_to_one_author():
    comment = {"body": COMMENT.format(fp=FP, priority="P1", count=MAX_COUNT), "author": {"login": "forger", "type": "User"}}
    data = snapshot(event="issue_comment", comments=[comment], triggering_comment=comment)
    assert run_decision(data)["body"] == body(2)


def test_comment_replay_is_deterministic_and_nonempty_has_expected_body():
    comment = {"body": COMMENT.format(fp=FP, priority="P1", count=1), "author": {"login": "alice", "type": "User"}}
    data = snapshot(event="issue_comment", comments=[comment], triggering_comment=comment)
    first, second = run_decision(data), run_decision(data)
    assert first == second
    if any(first[key] for key in ("labels_to_add", "priority_labels_to_remove", "body")):
        assert first["expected_body"] == data["issue"]["body"]


def test_comment_bot_pr_thread_skip_and_truncated_snapshots_are_empty():
    assert_empty({"skip": "bot"})
    assert_empty(snapshot(actor="github-actions[bot]"))
    assert_empty(snapshot(event="issue_comment", pr=True))
    comment = {"body": COMMENT.format(fp=FP, priority="P1", count=1), "author": {"login": "a", "type": "User"}}
    assert_empty(snapshot(event="issue_comment", comments=[comment] * 501, triggering_comment=comment, truncated=True))


@pytest.mark.parametrize("triggering_comment", [
    {"body": "arbitrary prose", "author": {"login": "alice", "type": "User"}},
    {"body": COMMENT.format(fp="fedcba9876543210", priority="P1", count=1), "author": {"login": "alice", "type": "User"}},
    {"body": COMMENT.format(fp=FP, priority="P1", count=1), "author": {"login": "ffs[bot]", "type": "Bot"}},
])
def test_issue_comment_requires_canonical_matching_human_trigger_before_any_mutation(triggering_comment):
    # Labels would otherwise be added, so the empty decision proves the gate
    # runs before every issue_comment mutation category.
    assert_empty(snapshot(event="issue_comment", triggering_comment=triggering_comment))


def test_opened_event_decides_exactly_the_three_required_labels_when_bare():
    data = snapshot()
    decision = run_decision(data)
    assert decision["labels_to_add"] == ["priority/P1", "source/ffs-retro", "triage"]
    assert decision["priority_labels_to_remove"] == []
    assert decision["body"] is None


def test_opened_event_reconciles_stale_managed_priority_and_preserves_unmanaged():
    data = snapshot()
    data["issue"]["labels"] = [
        {"name": "priority/P2"}, {"name": "source/ffs-retro"}, {"name": "wontfix"},
    ]
    decision = run_decision(data)
    assert decision["labels_to_add"] == ["priority/P1", "triage"]
    assert decision["priority_labels_to_remove"] == ["priority/P2"]
    assert "wontfix" not in decision["labels_to_add"]
    assert "wontfix" not in decision["priority_labels_to_remove"]


def test_opened_event_with_all_labels_present_is_idempotent():
    data = snapshot()
    data["issue"]["labels"] = [
        {"name": "priority/P1"}, {"name": "source/ffs-retro"}, {"name": "triage"},
    ]
    decision = run_decision(data)
    assert decision["labels_to_add"] == []
    assert decision["priority_labels_to_remove"] == []
