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
    assert dispatch.route_agent("Write architecture docs and OpenAPI specs") == "docs-architect"
    assert dispatch.route_agent("Improve accessibility and WCAG compliance on the landing page") == "accessibility-expert"


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
