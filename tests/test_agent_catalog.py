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
# exact-agent catalog. The catalog itself is still live in the decompose prompt
# and pipeline doc, but third-party catalogs are optional integrations rather
# than FFS-installed dependencies. GSD's upstream installer owns its own agent
# packs; the scoped FFS installer must not recreate external artifacts.
def test_hybrid_catalog_is_defined_in_the_decompose_prompt() -> None:
    prompt = _read("prompts/decompose-spec.md")

    assert "[agent:exact-agent]" in prompt
    assert "hybrid ECC + wshobson catalog" in prompt

    for label in CATALOG_LABELS:
        assert label in prompt, label


def test_decompose_prompt_emits_shell_free_preflight_probes() -> None:
    prompt = _read("prompts/decompose-spec.md")

    assert '`{"kind": "probe", "name": …, "argv": […]}`' in prompt
    assert '`{"kind": "probe", "name": …, "cmd": …}`' not in prompt
    assert "shell command strings, environment placeholders" in prompt
    assert "must read secrets from it rather than from process arguments" in prompt


def test_supporting_docs_and_scoped_installer_match_the_same_catalog() -> None:
    pipeline = _read("docs/pipeline.md")
    readme = _read("README.md")
    dependencies = _read("docs/dependencies.md")
    installer = _read("lib/ffs_installer.py")

    assert "[agent:exact-agent]" in pipeline
    assert "External agent catalogs" in readme
    assert "not installed or refreshed by FFS" in dependencies
    assert '"owner": "upstream-installer"' in installer
    assert '"--profile=full"' in installer
    assert "source_skills(source)" in installer
    assert '(".agents", ".claude")' in installer
    assert "install_ecc_pack" not in installer
    assert "install_wshobson_pack" not in installer
