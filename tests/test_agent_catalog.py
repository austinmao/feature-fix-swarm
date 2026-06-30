from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

CATALOG_LABELS = [
    "ecc:tdd-guide",
    "ecc:code-reviewer",
    "ecc:architect",
    "ecc:spec-miner",
    "ecc:doc-updater",
    "ecc:build-error-resolver",
    "ecc:silent-failure-hunter",
    "ecc:refactor-cleaner",
    "backend-architect",
    "graphql-architect",
    "frontend-developer",
    "ui-ux-designer",
    "accessibility-expert",
    "ui-visual-validator",
    "test-automator",
    "security-auditor",
    "backend-security-coder",
    "frontend-security-coder",
    "typescript-pro",
    "python-pro",
    "fastapi-pro",
    "django-pro",
    "database-architect",
    "database-optimizer",
    "database-admin",
    "performance-engineer",
    "debugger",
    "error-detective",
    "incident-responder",
    "deployment-engineer",
    "cloud-architect",
    "kubernetes-architect",
    "terraform-specialist",
    "observability-engineer",
    "network-engineer",
    "docs-architect",
    "api-documenter",
    "reference-builder",
    "reverse-engineer",
    "context-manager",
    "prompt-engineer",
    "business-analyst",
    "sales-automator",
    "customer-support",
    "seo-meta-optimizer",
]


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_hybrid_catalog_is_defined_in_prompt_and_execution_skill() -> None:
    prompt = _read("prompts/decompose-spec.md")
    skill = _read("skills/feature-implement/SKILL.md")

    assert "[agent:exact-agent]" in prompt
    assert "[agent:exact-agent]" in skill
    assert "hybrid ECC + wshobson catalog" in prompt
    assert "Exact agent delegation uses the hybrid ECC + wshobson catalog" in skill

    for label in CATALOG_LABELS:
        assert label in prompt, label
        assert label in skill, label


def test_supporting_docs_and_bootstrap_match_the_same_catalog() -> None:
    spec_skill = _read("skills/spec-decompose/SKILL.md")
    commands = _read("docs/commands.md")
    pipeline = _read("docs/pipeline.md")
    readme = _read("README.md")
    setup = _read("setup.sh")

    assert "exact-agent hybrid catalog" in spec_skill
    assert "hybrid exact-agent catalog" in commands
    assert "[agent:exact-agent]" in pipeline
    assert "exact hybrid catalog" in readme
    assert "ECC_REPO" in setup
    assert "WSH_REPO" in setup
    assert "install_ecc_pack" in setup
    assert "install_wshobson_pack" in setup
