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


# The gsd migration (spec-002, gsd replaces ruflo) moved the execution SKILLs
# (feature-implement, spec-decompose) and docs/commands.md to gsd-native model
# routing (`model_profiles` in config.json), so they no longer restate the
# exact-agent catalog. The catalog itself is still live: it is defined in the
# decompose prompt and pipeline doc, advertised in the README, and installed by
# setup.sh (ECC + wshobson packs). These tests guard the surfaces that still own it.
def test_hybrid_catalog_is_defined_in_the_decompose_prompt() -> None:
    prompt = _read("prompts/decompose-spec.md")

    assert "[agent:exact-agent]" in prompt
    assert "hybrid ECC + wshobson catalog" in prompt

    for label in CATALOG_LABELS:
        assert label in prompt, label


def test_supporting_docs_and_bootstrap_match_the_same_catalog() -> None:
    pipeline = _read("docs/pipeline.md")
    readme = _read("README.md")
    setup = _read("setup.sh")

    assert "[agent:exact-agent]" in pipeline
    assert "exact hybrid catalog" in readme
    assert "ECC_REPO" in setup
    assert "WSH_REPO" in setup
    assert "install_ecc_pack" in setup
    assert "install_wshobson_pack" in setup
