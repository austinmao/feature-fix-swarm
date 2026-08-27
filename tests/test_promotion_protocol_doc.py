import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "promotion-protocol.md"
SKILL_FILES = [
    REPO_ROOT / "skills" / "review-gate" / "SKILL.md",
    REPO_ROOT / "skills" / "preflight" / "SKILL.md",
    REPO_ROOT / "skills" / "feature-implement" / "SKILL.md",
]
# Separate from SKILL_FILES above (which asserts a promotion-protocol doc
# reference — false for a read-only assessment skill). One entry only:
# widening this to every FFS skill is a follow-up, not this guard's scope —
# skills/preflight/SKILL.md carries vendor tokens today and would fail it.
VENDOR_NEUTRAL_SKILL_FILES = [
    REPO_ROOT / "skills" / "area-status" / "SKILL.md",
]
FORBIDDEN = re.compile(
    r"openclaw|doppler|railway|vercel|hetzner|neon|paperclip|glance", re.IGNORECASE
)
RULE_LINE = re.compile(r"^\d+\.\s")
ANNOTATION = re.compile(r"\(Enforcement:.*?\)", re.IGNORECASE)


def test_doc_has_exactly_thirteen_recognized_rules() -> None:
    # Phase 3 (spec-006) split Rule 12 into 12a/12b: eleven plain-numbered
    # rules plus the two lettered split lines total thirteen recognized
    # rules, matching the doc's "Thirteen rules" count line.
    lines = DOC.read_text().splitlines()
    numbered = [l for l in lines if RULE_LINE.match(l)]
    lettered = [l for l in lines if RULE_12A.match(l) or RULE_12B.match(l)]
    assert len(numbered) == 11, f"expected 11 numbered rules, found {len(numbered)}"
    total = len(numbered) + len(lettered)
    assert total == 13, f"expected 13 recognized rules, found {total}"


def test_doc_has_zero_vendor_names() -> None:
    text = DOC.read_text()
    matches = FORBIDDEN.findall(text)
    assert not matches, f"vendor/product names found: {matches}"


def test_every_rule_carries_an_enforcement_annotation() -> None:
    lines = DOC.read_text().splitlines()
    rule_lines = [l for l in lines if RULE_LINE.match(l)]
    missing = [l for l in rule_lines if not ANNOTATION.search(l)]
    assert not missing, f"rules missing enforcement annotation: {missing}"


def test_all_three_skills_reference_the_doc() -> None:
    for path in SKILL_FILES:
        text = path.read_text()
        assert "docs/promotion-protocol.md" in text, f"{path} does not reference the doc"


def test_rules_3_4_5_name_registry_path_and_check_command() -> None:
    # Seam 4 (REQ-401): rules 3/4/5 pin the concrete enforcement path + command
    # so the doc can never promise an enforcement lever that drifted away.
    lines = DOC.read_text().splitlines()
    rule_lines = [l for l in lines if RULE_LINE.match(l)]
    for rule_number in (3, 4, 5):
        line = rule_lines[rule_number - 1]
        assert "config/environments.yaml" in line, (
            f"rule {rule_number} missing registry path literal: {line}"
        )
        assert "env-registry.sh check" in line, (
            f"rule {rule_number} missing check-command literal: {line}"
        )


def test_vendor_neutral_skill_files_have_zero_vendor_tokens() -> None:
    # REQ-10 (spec-380): area-status is bound for an upstream vendor-neutral
    # package. This is a SEPARATE list from SKILL_FILES above — that list's
    # three entries are asserted to reference docs/promotion-protocol.md,
    # which is not true of area-status (a read-only assessment skill with no
    # promotion semantics), and widening it to every FFS skill would fail on
    # skills/preflight/SKILL.md today for a reason unrelated to this guard.
    for path in VENDOR_NEUTRAL_SKILL_FILES:
        text = path.read_text()
        matches = FORBIDDEN.findall(text)
        assert not matches, f"{path}: vendor/product tokens found: {matches}"


def test_forbidden_regex_is_non_vacuous() -> None:
    # Proves the guard above can actually fail: derive a probe token from the
    # regex's own source (never a transcribed literal, which would be a
    # second copy that drifts and would also self-match this test file).
    first_alternative = FORBIDDEN.pattern.split("|")[0]
    assert FORBIDDEN.search(f"a sentence mentioning {first_alternative} in passing")


# ── Phase 3 Wave-0 RED contract (plan 03-04): bounded autonomy docs ───────
# These selectors fail until Phase 3 lands docs/autonomy-posture.md, splits
# Rule 12 into annotated 12a/12b with the count line updated, and exposes
# the consolidate report-only default plus explicit --execute in the
# commands documentation.  Each missing assertion emits the typed behavioral
# marker below; the tests read files only — no subprocess, no vendor call.

import pytest

PHASE3_MARKER = "EXPECTED-RED:DOCS:missing-phase3-doc-contract"
POSTURE_DOC = REPO_ROOT / "docs" / "autonomy-posture.md"
COMMANDS_DOC = REPO_ROOT / "docs" / "commands.md"
RULE_12A = re.compile(r"^12a\.\s")
RULE_12B = re.compile(r"^12b\.\s")


def _phase3_red(cond: bool, why: str) -> None:
    """Behavioral RED: emit the exact typed marker line, then fail."""
    if not cond:
        print(PHASE3_MARKER)
        pytest.fail(why)


def test_autonomy_posture_doc_exists_and_is_bounded() -> None:
    _phase3_red(POSTURE_DOC.is_file(),
                "docs/autonomy-posture.md does not exist yet")
    lines = POSTURE_DOC.read_text().splitlines()
    _phase3_red(len(lines) <= 60,
                f"docs/autonomy-posture.md has {len(lines)} lines; the "
                "bounded-autonomy contract caps it at 60")


def test_autonomy_posture_doc_names_every_required_policy_subject() -> None:
    _phase3_red(POSTURE_DOC.is_file(),
                "docs/autonomy-posture.md does not exist yet")
    text = POSTURE_DOC.read_text().lower()
    subjects = {
        "zero default": ("zero", "default"),
        "floor strengthening": ("floor",),
        "current-run degraded-review threshold": ("degraded", "50%"),
        "production-touch refusal": ("production", "touch"),
        "quarantine auto-requeue-once": ("requeue", "once"),
        "accepted credential risk": ("credential", "risk"),
    }
    for name, needles in subjects.items():
        missing = [n for n in needles if n not in text]
        _phase3_red(not missing,
                    f"docs/autonomy-posture.md does not cover {name!r} "
                    f"(missing tokens: {missing})")


def test_promotion_protocol_declares_thirteen_rules_with_split_rule_12() -> None:
    text = DOC.read_text()
    lines = text.splitlines()
    _phase3_red("Thirteen rules" in text,
                "the promotion-protocol count line still does not declare "
                "thirteen rules")
    twelve_a = [l for l in lines if RULE_12A.match(l)]
    twelve_b = [l for l in lines if RULE_12B.match(l)]
    _phase3_red(len(twelve_a) == 1,
                "Rule 12a is not present as a separate numbered rule line")
    _phase3_red(len(twelve_b) == 1,
                "Rule 12b is not present as a separate numbered rule line")
    _phase3_red(bool(ANNOTATION.search(twelve_a[0])),
                "Rule 12a carries no enforcement annotation")
    _phase3_red(bool(ANNOTATION.search(twelve_b[0])),
                "Rule 12b carries no enforcement annotation")
    low_a = twelve_a[0].lower()
    _phase3_red("posture" in low_a and "bypass" in low_a,
                "Rule 12a does not cover the posture-dependent emergency bypass")
    low_b = twelve_b[0].lower()
    _phase3_red("grant" in low_b and "consolidate:estate" in low_b,
                "Rule 12b does not cover grant-registry maintenance and "
                "consolidate:estate")


def test_rules_1_through_11_remain_intact_and_annotated() -> None:
    # Not weakened by the split: the first eleven numbered rules stay, each
    # with its enforcement annotation.  (Green before AND after Phase 3.)
    lines = DOC.read_text().splitlines()
    rule_lines = [l for l in lines if RULE_LINE.match(l)]
    assert len(rule_lines) >= 11, f"only {len(rule_lines)} numbered rules remain"
    missing = [l for l in rule_lines[:11] if not ANNOTATION.search(l)]
    assert not missing, f"rules 1-11 missing enforcement annotation: {missing}"


def test_commands_doc_exposes_report_only_default_and_explicit_execute() -> None:
    text = COMMANDS_DOC.read_text()
    consolidate_rows = [l for l in text.splitlines()
                        if "git-branch-consolidate" in l]
    assert consolidate_rows, "commands doc no longer mentions consolidation"
    joined = " ".join(consolidate_rows).lower()
    _phase3_red("--execute" in joined,
                "the consolidate command documentation does not expose an "
                "explicit --execute lever")
    _phase3_red("report" in joined and "default" in joined,
                "the consolidate command documentation does not state the "
                "report-only default")
