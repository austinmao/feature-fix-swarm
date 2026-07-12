import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "promotion-protocol.md"
SKILL_FILES = [
    REPO_ROOT / "skills" / "review-gate" / "SKILL.md",
    REPO_ROOT / "skills" / "preflight" / "SKILL.md",
    REPO_ROOT / "skills" / "feature-implement" / "SKILL.md",
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
