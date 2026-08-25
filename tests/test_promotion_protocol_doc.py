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


def test_doc_has_exactly_12_numbered_rules() -> None:
    lines = DOC.read_text().splitlines()
    rule_lines = [l for l in lines if RULE_LINE.match(l)]
    assert len(rule_lines) == 12, f"expected 12 rules, found {len(rule_lines)}"


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
