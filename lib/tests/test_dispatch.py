from __future__ import annotations

import importlib.util
from pathlib import Path


DISPATCH_PATH = Path(__file__).resolve().parents[1] / "dispatch.py"
SPEC = importlib.util.spec_from_file_location("dispatch", DISPATCH_PATH)
dispatch = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dispatch)


def test_parse_canonical_model_tiers() -> None:
    content = """# Tasks

## Phase 1: Setup

- [ ] T001 [US1] [model:haiku] [thinking:low] [agent:engineering/backend-engineer] Add helper.
- [ ] T002 [US1] [model:sonnet] [thinking:med] [agent:engineering/backend-engineer] Add route.
      Depends-on: T001
- [ ] T003 [US1] [model:opus] [thinking:max] [agent:quality/reviewer] Review route.
      Depends-on: T002
"""

    tasks = dispatch.parse_tasks_md(content)

    assert [task["model"] for task in tasks] == [
        "haiku",
        "sonnet",
        "opus",
    ]
    assert tasks[1]["depends_on"] == ["T001"]
    assert tasks[2]["depends_on"] == ["T002"]
    assert tasks[0]["description"] == "Add helper."


def test_parse_legacy_codex_model_ids_still_work() -> None:
    content = """# Tasks

## Phase 1: Setup

- [ ] T001 [US1] [model:gpt-5.4-mini] [thinking:low] [agent:engineering/backend-engineer] Add helper.
- [ ] T002 [US1] [model:gpt-5.4] [thinking:med] [agent:engineering/backend-engineer] Add route.
      Depends-on: T001
- [ ] T003 [US1] [model:gpt-5.5] [thinking:max] [agent:quality/reviewer] Review route.
      Depends-on: T002
"""

    tasks = dispatch.parse_tasks_md(content)

    assert [task["model"] for task in tasks] == [
        "gpt-5.4-mini",
        "gpt-5.4",
        "gpt-5.5",
    ]
    assert tasks[1]["depends_on"] == ["T001"]
    assert tasks[2]["depends_on"] == ["T002"]


def test_route_agent_maps_common_tasks_to_ecc_agents() -> None:
    assert dispatch.route_agent("Write failing unit tests for auth service") == "ecc:tdd-guide"
    assert dispatch.route_agent("Review PR for quality and lint rules") == "ecc:code-reviewer"
    assert dispatch.route_agent("Fix FastAPI endpoint in service.py") == "fastapi-pro"
    assert dispatch.route_agent("Update Next.js landing page UI") == "frontend-developer"
    assert dispatch.route_agent("Patch PostgreSQL schema migration") == "database-architect"


def test_route_agent_maps_specialist_tasks_to_wshobson_agents() -> None:
    assert dispatch.route_agent("Build a backend API architecture for a microservice") == "backend-architect"
    assert dispatch.route_agent("Tune slow PostgreSQL queries and indexes") == "database-optimizer"
    assert dispatch.route_agent("Investigate an intermittent stack trace in production") == "error-detective"
    assert dispatch.route_agent("Create Playwright browser automation for the checkout flow") == "test-automator"
    assert dispatch.route_agent("Write architecture docs and system design guides") == "docs-architect"
    assert dispatch.route_agent("Improve accessibility and WCAG compliance on the landing page") == "accessibility-expert"
    # codex-gate (PR #11): api-documenter was unreachable dead code — docs-architect's
    # bare "docs" keyword shadowed it via substring match on "api docs"/"developer docs"/
    # "openapi docs". Regression coverage for the reorder fix.
    assert dispatch.route_agent("Write OpenAPI docs for the auth endpoint") == "api-documenter"
    assert dispatch.route_agent("developer docs for the API") == "api-documenter"
    assert dispatch.route_agent("Review the Swagger schema") == "api-documenter"


def test_parse_without_explicit_agent_routes_to_ecc_agent() -> None:
    content = """# Tasks

## Phase 1: Setup

- [ ] T001 [model:sonnet] Write failing unit tests for auth service.
- [ ] T002 [model:sonnet] Review PR for quality and lint rules.
- [ ] T003 [model:sonnet] Fix FastAPI endpoint in `src/api.py`.
- [ ] T004 [model:sonnet] Implement responsive React landing page.
"""

    tasks = dispatch.parse_tasks_md(content)

    assert [task["agent"] for task in tasks] == [
        "ecc:tdd-guide",
        "ecc:code-reviewer",
        "fastapi-pro",
        "frontend-developer",
    ]


def test_resolve_thinking_handles_codex_tiers() -> None:
    assert dispatch.resolve_thinking("gpt-5.5", "med") == "high"
    assert dispatch.resolve_thinking("gpt-5.4-mini", "high") == "med"
    assert dispatch.resolve_thinking("gpt-5.4", "high") == "high"


def test_qa_dims_with_digits_and_hyphens_round_trip() -> None:
    """[qa:] values containing digits (e2e) or hyphens (review-gate) must parse.

    Regression: the char class [a-z,]+ silently dropped [qa:e2e] and the
    reserved [qa:review-gate] phase-gate tag, falling back to defaults.
    """
    assert dispatch.parse_annotations("T001 [qa:e2e] browser flow")["qa_dims"] == ["e2e"]
    assert dispatch.parse_annotations("T002 [qa:review-gate] gate task")["qa_dims"] == ["review-gate"]
    assert dispatch.parse_annotations("T003 [qa:unit,integration,e2e] full stack")["qa_dims"] == [
        "unit", "integration", "e2e",
    ]
    # scoped list without digits/hyphens must keep working
    assert dispatch.parse_annotations("T004 [qa:review,security] auth")["qa_dims"] == ["review", "security"]


def test_depends_on_round_trip() -> None:
    """Depends-on: on the indented next line must populate depends_on."""
    md = (
        "## Phase 3\n"
        "- [ ] T042 [model:opus thinking:high] [agent:ecc:security-reviewer] [US3] [P] [qa:review,security] harden auth\n"
        "  Depends-on: T010, T011\n"
        "- [ ] T043 [US1] [model:sonnet thinking:med] [agent:ecc:code-reviewer] /review-gate gate [qa:review-gate]\n"
        "  Depends-on: T042\n"
    )
    tasks = dispatch.parse_tasks_md(md)
    assert tasks[0]["depends_on"] == ["T010", "T011"]
    assert tasks[1]["depends_on"] == ["T042"]
    assert tasks[1]["qa_dims"] == ["review-gate"]
