import copy
import json

import pytest

import delegation_router as dr
from delegation_router import RouteError

CONFIG = {
    "schema": 1,
    "limits": {"claude": 3, "codex": 3, "total": 6, "max_local_verifiers": 1},
    "verify": {"cmd": "pytest -q"},
    "secrets": {"provider": "none", "exposure": {"claude": [], "codex": []}},
}
BOTH = {"claude", "codex"}


def mk(*lines, sequential=False):
    return dr.normalize_tasks(list(lines), sequential=sequential)


def cloud_task(task_id, owns, est=45, **extra):
    base = {"id": task_id, "prompt": "do " + task_id, "owns": owns, "est_minutes": est}
    base.update(extra)
    return base


def build(raw, **overrides):
    return dr.normalize_tasks(raw, sequential=overrides.pop("sequential", False))


def plan(tasks, **overrides):
    kwargs = dict(
        policy="auto",
        config=CONFIG,
        providers_available=BOTH,
        slots_free={"claude": 3, "codex": 3},
        host_pressure_high=False,
        grants=set(),
        run_refusals=[],
    )
    kwargs.update(overrides)
    return dr.plan_routes(tasks, **kwargs)


def one(task_dict, **overrides):
    result = plan(build([task_dict]), **overrides)
    return result["tasks"][task_dict["id"]]


# ── annotations ──────────────────────────────────────────────────────────────


def test_parse_all_annotations():
    parsed = dr.parse_annotations(
        "- [ ] Add endpoint [owns: src/a/**, b.py] [est: 45m] [needs-secret: API_KEY] "
        "[local] [after: t0] [verify: npm test -- a] [id: api]"
    )
    assert parsed["owns"] == ["src/a/**", "b.py"]
    assert parsed["est_minutes"] == 45
    assert parsed["needs_secrets"] == ["API_KEY"]
    assert parsed["local_only"] is True
    assert parsed["depends_on"] == ["t0"]
    assert parsed["verify_cmd"] == "npm test -- a"
    assert parsed["id"] == "api"
    assert parsed["title"] == "- [ ] Add endpoint"


@pytest.mark.parametrize(
    "text,minutes",
    [("45m", 45), ("2h", 120), ("1h30m", 90), ("90", 90)],
)
def test_parse_est_forms(text, minutes):
    assert dr.parse_annotations("x [est: %s]" % text)["est_minutes"] == minutes


@pytest.mark.parametrize("text", ["", "soon", "m", "1x"])
def test_parse_est_invalid(text):
    with pytest.raises(RouteError) as info:
        dr.parse_annotations("x [est: %s]" % text)
    assert info.value.code == dr.ANNOTATION_INVALID


def test_parse_accumulates_lists_and_keeps_unknown_brackets():
    parsed = dr.parse_annotations("t [owns: a/**] [model: sonnet] [owns: b/**] [needs-secret: A B]")
    assert parsed["owns"] == ["a/**", "b/**"]
    assert parsed["needs_secrets"] == ["A", "B"]
    assert "[model: sonnet]" in parsed["title"]


def test_parse_local_with_reason_and_nested_verify_brackets():
    parsed = dr.parse_annotations("t [local: needs live db] [verify: [ -f x ] && make test]")
    assert parsed["local_reason"] == "needs live db"
    assert parsed["verify_cmd"] == "[ -f x ] && make test"


def test_parse_no_annotations():
    assert dr.parse_annotations("plain text") == {"title": "plain text"}


# ── normalization ────────────────────────────────────────────────────────────


def test_normalize_default_ids_and_shape():
    tasks = mk("one [owns: a/**]", "two [owns: b/**] [id: second]", "three")
    assert [t["id"] for t in tasks] == ["t1", "second", "t3"]
    first = tasks[0]
    assert first["owns_source"] == "annotated"
    assert first["tier"] == "execution"
    assert first["model_request"] == {"kind": "tier", "name": "execution"}
    assert tasks[2]["owns_source"] == "missing"
    assert set(first) == {
        "id", "title", "prompt", "tier", "model_request", "owns", "owns_source",
        "est_minutes", "needs_secrets", "local_only", "local_reason", "depends_on",
        "kind", "verify_cmd",
    }


def test_normalize_dict_fields_win_over_line():
    tasks = dr.normalize_tasks(
        [{"line": "x [owns: a/**] [est: 10m]", "owns": ["z/**"], "tier": "volume", "kind": "search"}],
        sequential=False,
    )
    assert tasks[0]["owns"] == ["z/**"]
    assert tasks[0]["est_minutes"] == 10
    assert tasks[0]["tier"] == "volume"
    assert tasks[0]["kind"] == "search"


@pytest.mark.parametrize("bad", ["T1", "-a", "a" * 25, "a_b", "A"])
def test_normalize_rejects_bad_ids(bad):
    with pytest.raises(RouteError) as info:
        mk("x [id: %s]" % bad)
    assert info.value.code == dr.TASK_ID_INVALID


def test_normalize_accepts_max_length_id():
    assert mk("x [id: %s]" % ("a" * 24))[0]["id"] == "a" * 24


def test_normalize_duplicate_ids():
    with pytest.raises(RouteError) as info:
        mk("a [id: x]", "b [id: x]")
    assert info.value.code == dr.TASK_ID_DUPLICATE


def test_normalize_sequential_chains():
    tasks = mk("a", "b", "c", sequential=True)
    assert [t["depends_on"] for t in tasks] == [[], ["t1"], ["t2"]]


def test_normalize_sequential_keeps_existing_dep_once():
    tasks = mk("a", "b [after: t1]", sequential=True)
    assert tasks[1]["depends_on"] == ["t1"]


def test_normalize_exact_model_request_kept():
    tasks = dr.normalize_tasks(
        [{"prompt": "p", "model_request": {"kind": "exact", "id": "claude-opus-5-5"}}],
        sequential=False,
    )
    assert tasks[0]["model_request"]["kind"] == "exact"


@pytest.mark.parametrize(
    "raw",
    [
        {"prompt": "p", "tier": "bogus"},
        {"prompt": "p", "model_request": {"kind": "tier", "name": "bogus"}},
        {"prompt": "p", "tier": "volume", "model_request": {"kind": "tier", "name": "execution"}},
        {"prompt": "p", "kind": "dance"},
        {"prompt": "p", "owns_source": "guess"},
        {"prompt": "p", "est_minutes": -1},
        {"prompt": "p", "needs_secrets": ["bad name"]},
        {"prompt": "p", "owns": "src/**"},
        {"prompt": "p", "owns": ["a"], "owns_source": "missing"},
        42,
    ],
)
def test_normalize_rejects_invalid(raw):
    with pytest.raises(RouteError) as info:
        dr.normalize_tasks([raw], sequential=False)
    assert info.value.code == dr.TASK_INVALID

PASSPHRASE = "Zq7! synthetic pass phrase"  # not a name, not scanner-shaped: it must still never be echoed


@pytest.mark.parametrize("line", [
    {"prompt": "p", "needs_secrets": [PASSPHRASE]},
    "do it [needs-secret: %s]" % PASSPHRASE,
])
def test_an_invalid_needs_secret_entry_is_never_echoed(line):
    with pytest.raises(RouteError) as info:
        dr.normalize_tasks([{"id": "build-1", "prompt": "p"}, line], sequential=False)
    assert info.value.code == dr.TASK_INVALID
    assert PASSPHRASE not in str(info.value) and "pass phrase" not in str(info.value)
    assert "t2" in str(info.value) and "entry 1" in str(info.value)


@pytest.mark.parametrize("raw, code", [
    ("do it [est: %s]" % PASSPHRASE, dr.ANNOTATION_INVALID),
    ({"prompt": "p", "tier": PASSPHRASE}, dr.TASK_INVALID),
    ({"prompt": "p", "tier": PASSPHRASE, "model_request": {"kind": "tier", "name": "judgment"}}, dr.TASK_INVALID),
    ({"prompt": "p", "tier": "volume", "model_request": {"kind": "tier", "name": "judgment"}}, dr.TASK_INVALID),
    ({"prompt": "p", "model_request": {"kind": "tier", "name": PASSPHRASE}}, dr.TASK_INVALID),
    ({"prompt": "p", "model_request": {"kind": PASSPHRASE}}, dr.TASK_INVALID),
])
def test_the_other_free_text_validation_errors_never_echo_the_value_either(raw, code):
    with pytest.raises(RouteError) as info:
        dr.normalize_tasks([raw], sequential=False)
    assert info.value.code == code
    assert PASSPHRASE not in str(info.value) and "pass phrase" not in str(info.value)


def test_an_invalid_needs_secret_entry_names_the_task_id_when_it_has_one():
    with pytest.raises(RouteError) as info:
        dr.normalize_tasks([{"id": "build-1", "prompt": "p", "needs_secrets": ["OK_NAME", PASSPHRASE]}], sequential=False)
    assert "build-1" in str(info.value) and "entry 2" in str(info.value) and PASSPHRASE not in str(info.value)


# ── denylist and overlap ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        ".ffs/cloud/config.json", ".ffs-result/t1.json", ".planning/x.md", ".claude/settings.json",
        ".codex/config", ".github/workflows/ci.yml", ".feature-fix-swarm/x", ".gitignore",
        ".gitmodules", ".git/config", "scripts/hooks/pre-commit", "hooks/a", ".husky/pre-commit",
        ".circleci/config.yml", ".gitlab-ci.yml", ".mcp.json", "CLAUDE.md", "AGENTS.md",
        ".envrc", ".npmrc", ".env", ".env.local", "certs/server.pem", "id_rsa", "id_rsa.pub",
        "setup.sh", "sub/dir/setup.sh", "pkg/CLAUDE.md", ".github/**", "scripts/**", "**", "*",
        "/abs/path", "a/../b", "", ".ffs/", "./.ffs/x",
    ],
)
def test_denylist_hits(path):
    assert dr.matches_denylist(path)


@pytest.mark.parametrize(
    "path",
    [
        # nesting bypasses (dot-directories match at any depth)
        "pkg/.claude/settings.json", "pkg/.claude/settings.local.json", "pkg/.claude/skills/x/SKILL.md",
        "pkg/.codex/config.toml", "pkg/.husky/pre-commit", "pkg/.agents/skills/x", "pkg/.vscode/tasks.json",
        "a/b/.cursor/rules", "pkg/.gemini/settings.json", "pkg/.ffs/cloud/setup.sh", "pkg/.devcontainer/devcontainer.json",
        # case bypasses
        "pkg/CLAUDE.MD", "pkg/AGENTS.MD", "pkg/.NPMRC", "pkg/.Envrc", "pkg/.ENV.local", "pkg/.Mcp.json",
        ".Claude/x", ".CLAUDE/settings.json", "pkg/.GitHub/workflows/ci.yml", "SCRIPTS/HOOKS/pre-push",
        # new entries
        ".yarnrc.yml", "pkg/.yarnrc", "pkg/.pnpmfile.cjs", ".pre-commit-config.yaml", "pkg/.lfsconfig",
        "pkg/.gitattributes", "pkg/.gitconfig", "pkg/.git/hooks/pre-commit", "pkg/.GIT/config", "GEMINI.md",
        "pkg/.mise.toml", ".tool-versions", ".env.production", "pkg/.env.example.local",
        # unicode case folding
        "pkg/.\u0043laude/x",
    ],
)
def test_denylist_bypasses_closed(path):
    assert dr.matches_denylist(path), path


@pytest.mark.parametrize(
    "path",
    [
        "pkg/.c\u200claude/settings.json", "pkg/CLAU\u200cDE.md", "pkg/.np\ufeffmrc", "pkg/a\u202eb", "pkg/a\u2060b",
        "pkg/a\u00adb", "pkg/a\x07b", "pkg/a\tb", "pkg/.c\u200claude/**", "pkg/\u200b*",
    ],
)
def test_invisible_and_control_characters_are_denied(path):
    """HFS+ ignores Cf code points when it compares names, so they must never reach a checkout."""
    assert dr.matches_denylist(path)


@pytest.mark.parametrize("path", ["docs/caf\u00e9.md", "docs/cafe\u0301.md", "docs/\u65e5\u672c.md"])
def test_ordinary_unicode_names_are_not_denied(path):
    assert not dr.matches_denylist(path)


@pytest.mark.parametrize("path", [".env.example", "pkg/.env.sample", ".env.template", "a/.env.dist", ".ENV.EXAMPLE"])
def test_env_example_names_are_not_denied(path):
    assert not dr.matches_denylist(path)


@pytest.mark.parametrize(
    "paths,base,expected",
    [
        (["src/a.py", "src/A.py"], [], ["src/A.py", "src/a.py"]),  # collide with each other
        (["README.MD"], ["README.md"], ["README.MD"]),  # collide with a base path
        (["SRC/new.py"], ["src/old.py"], ["SRC/new.py"]),  # directory aliasing
        (["src/new.py"], ["src/old.py", "docs/x"], []),
        (["src/a.py"], ["src/a.py"], []),  # same spelling is not a collision
        (["caf\u00e9.txt"], ["cafe\u0301.txt"], ["caf\u00e9.txt"]),  # NFC vs NFD
    ],
)
def test_case_collisions(paths, base, expected):
    assert dr.case_collisions(paths, base) == expected


@pytest.mark.parametrize(
    "path",
    [
        "src/a/**", "src/app.py", "docs/guide.md", "src/hooks/use.ts", "scripts/build.sh",
        "tests/test_x.py", "lib/ffs.py", "README.md", "src/*.py", "src/envelope.py",
    ],
)
def test_denylist_misses(path):
    assert not dr.matches_denylist(path)


def test_denylist_contains_contract_entries():
    assert ".ffs/**" in dr.DENYLIST and "setup.sh" in dr.DENYLIST and "*.pem" in dr.DENYLIST
    assert len(dr.DENYLIST) == len(set(dr.DENYLIST)) == 39


@pytest.mark.parametrize(
    "left,right,expected",
    [
        (["src/a/**"], ["src/a/**"], True),
        (["src/a/**"], ["src/a/b.py"], True),
        (["src/a"], ["src/a/b.py"], True),
        (["src/a/**"], ["src/b/**"], False),
        (["src/**"], ["src/a/b/c.py"], True),
        (["a/*.py"], ["a/b.py"], True),
        (["a/*.py"], ["a/*.ts"], True),
        (["a/b.py"], ["a/c.py"], False),
        (["a/b.py"], ["a/b.py"], True),
        (["**"], ["x/y"], True),
        (["src/**/x.py"], ["src/a/b/x.py"], True),
        (["src/**/x.py"], ["lib/a/x.py"], False),
        (["src/**/x.py"], ["src/a/y.py"], True),  # conservative: y.py could be a directory
        (["a/b/**"], ["a/**/b/c"], True),
        (["docs/**", "src/a/**"], ["lib/**", "src/a/x"], True),
        (["docs/**"], ["lib/**"], False),
        ([], ["a/**"], False),
        (["a/**"], [], False),
        (["../x"], ["y"], True),
        (["/abs"], ["y"], True),
        (["./src/a"], ["src/a/b"], True),
    ],
)
def test_owns_overlap(left, right, expected):
    assert dr.owns_overlap(left, right) is expected
    assert dr.owns_overlap(right, left) is expected


# ── hard filters and reason codes ────────────────────────────────────────────

ELIGIBLE = cloud_task("t1", ["src/a/**"])


def test_eligible_baseline_goes_cloud():
    entry = one(ELIGIBLE)
    assert entry["route"] == "cloud"
    assert entry["reasons"] == [dr.CLOUD_ELIGIBLE]
    assert entry["provider"] in ("claude", "codex")


@pytest.mark.parametrize(
    "extra,code",
    [
        ({"local_only": True}, dr.LOCAL_ONLY),
        ({"kind": "review"}, dr.KIND_NOT_CLOUDABLE),
        ({"kind": "verify"}, dr.KIND_NOT_CLOUDABLE),
        ({"model_request": {"kind": "exact", "id": "claude-opus-5-5"}}, dr.MODEL_EXACT),
        ({"tier": "frontier"}, dr.TIER_FRONTIER),
        ({"owns": []}, dr.OWNS_MISSING),
        ({"owns": [".github/**"]}, dr.OWNS_DENYLIST),
        ({"owns": ["../x"]}, dr.OWNS_INVALID),
        ({"needs_secrets": ["TOKEN"]}, dr.SECRET_NOT_EXPOSED),
    ],
)
def test_hard_filter_reason_codes(extra, code):
    entry = one(dict(ELIGIBLE, **extra))
    assert entry["route"] == "local"
    assert code in entry["reasons"]
    assert entry["provider"] is None


def test_missing_verify_cmd_without_config_fallback():
    config = copy.deepcopy(CONFIG)
    config["verify"]["cmd"] = ""
    entry = one(ELIGIBLE, config=config)
    assert dr.VERIFY_CMD_MISSING in entry["reasons"]
    assert one(dict(ELIGIBLE, verify_cmd="make"), config=config)["route"] == "cloud"


def test_no_provider_available():
    entry = one(ELIGIBLE, providers_available=set())
    assert entry["reasons"] == [dr.NO_PROVIDER_AVAILABLE]


def test_provider_with_zero_limit_is_unavailable():
    config = copy.deepcopy(CONFIG)
    config["limits"]["claude"] = 0
    entry = one(ELIGIBLE, config=config, providers_available={"claude"})
    assert dr.NO_PROVIDER_AVAILABLE in entry["reasons"]


def test_judgment_needs_claude():
    judgment = dict(ELIGIBLE, tier="judgment")
    assert one(judgment, providers_available={"codex"})["reasons"] == [dr.NO_PROVIDER_FOR_TIER]
    entry = one(judgment)
    assert entry["provider"] == "claude" and entry["model"] == "claude-opus-5-5"


def test_codex_takes_execution_and_volume():
    for tier in ("execution", "volume"):
        entry = one(dict(ELIGIBLE, tier=tier), providers_available={"codex"})
        assert entry["provider"] == "codex"


def test_secret_exposure_intersection():
    config = copy.deepcopy(CONFIG)
    config["secrets"]["exposure"] = {"claude": ["A"], "codex": ["A", "B"]}
    only_a = dict(ELIGIBLE, needs_secrets=["A"])
    assert one(only_a, config=config, slots_free={"claude": 3, "codex": 0})["provider"] == "claude"
    needs_b = dict(ELIGIBLE, needs_secrets=["B"])
    assert one(needs_b, config=config)["provider"] == "codex"
    both = dict(ELIGIBLE, needs_secrets=["A", "B"])
    assert one(both, config=config, providers_available={"claude"})["reasons"] == [dr.SECRET_NOT_EXPOSED]


def test_secret_tier_and_exposure_combine():
    config = copy.deepcopy(CONFIG)
    config["secrets"]["exposure"] = {"claude": [], "codex": ["B"]}
    judgment = dict(ELIGIBLE, tier="judgment", needs_secrets=["B"])
    assert one(judgment, config=config)["reasons"] == [dr.SECRET_NOT_EXPOSED]


def test_exposure_validation():
    config = copy.deepcopy(CONFIG)
    config["secrets"]["exposure"] = {"claude": "A"}
    with pytest.raises(RouteError) as info:
        plan(build([ELIGIBLE]), config=config)
    assert info.value.code == dr.CONFIG_INVALID


def test_exact_and_frontier_skip_provider_reasons():
    entry = one(dict(ELIGIBLE, tier="frontier"), providers_available=set())
    assert entry["reasons"] == [dr.TIER_FRONTIER]


def test_model_fields_and_provenance():
    claude = one(ELIGIBLE, providers_available={"claude"})
    assert (claude["model"], claude["effort"]) == ("claude-sonnet-5-5", None)
    assert claude["provenance"] == "requested-unverified"
    codex = one(ELIGIBLE, providers_available={"codex"})
    assert (codex["model"], codex["effort"]) == ("gpt-6.1-terra", "medium")
    assert codex["provenance"] == "provider-default-unverified"


# ── thresholds ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "policy,pressure,est,route",
    [
        ("auto", False, 19, "local"),
        ("auto", False, 20, "cloud"),
        ("prefer", False, 4, "local"),
        ("prefer", False, 5, "cloud"),
        ("auto", True, 4, "local"),
        ("auto", True, 5, "cloud"),
        ("prefer", True, 5, "cloud"),
        ("required", False, 0, "cloud"),
    ],
)
def test_thresholds(policy, pressure, est, route):
    entry = one(dict(ELIGIBLE, est_minutes=est), policy=policy, host_pressure_high=pressure)
    assert entry["route"] == route
    if route == "local":
        assert entry["reasons"] == [dr.BELOW_THRESHOLD]


def test_group_needs_only_one_task_at_threshold():
    tasks = build(
        [
            cloud_task("a", ["x/**"], est=5),
            cloud_task("b", ["y/**"], est=25, depends_on=["a"]),
        ]
    )
    result = plan(tasks)
    assert [g["route"] for g in result["groups"]] == ["cloud"]


# ── groups ───────────────────────────────────────────────────────────────────


def test_groups_by_dependency_components_in_input_order():
    tasks = build(
        [
            cloud_task("a", ["a/**"]),
            cloud_task("b", ["b/**"]),
            cloud_task("c", ["c/**"], depends_on=["a"]),
        ]
    )
    groups = plan(tasks)["groups"]
    assert [(g["id"], g["tasks"]) for g in groups] == [("g1", ["a", "c"]), ("g2", ["b"])]


def test_overlap_edges_across_all_tasks_join_groups_and_serialize():
    tasks = build(
        [
            cloud_task("a", ["src/**"]),
            cloud_task("b", ["docs/**"]),
            cloud_task("c", ["src/x/**"]),
            cloud_task("d", ["docs/y.md"]),
        ]
    )
    result = plan(tasks)
    assert [g["tasks"] for g in result["groups"]] == [["a", "c"], ["b", "d"]]
    assert result["tasks"]["c"]["implicit_deps"] == ["a"]
    assert result["tasks"]["d"]["implicit_deps"] == ["b"]
    assert [result["tasks"][t]["wave"] for t in "abcd"] == [0, 0, 1, 1]


def test_overlap_edge_is_transitive_bridge_between_groups():
    tasks = build(
        [
            cloud_task("a", ["a/**"]),
            cloud_task("b", ["b/**"]),
            cloud_task("c", ["a/x", "b/y"]),
        ]
    )
    assert [g["tasks"] for g in plan(tasks)["groups"]] == [["a", "b", "c"]]


def test_overlap_skips_edge_when_already_ordered():
    tasks = build(
        [
            cloud_task("a", ["src/**"]),
            cloud_task("b", ["src/**"], depends_on=["a"]),
        ]
    )
    assert plan(tasks)["tasks"]["b"]["implicit_deps"] == []


def test_overlap_with_reverse_explicit_dependency_adds_no_cycle():
    tasks = build(
        [
            cloud_task("a", ["src/**"], depends_on=["b"]),
            cloud_task("b", ["src/**"]),
        ]
    )
    result = plan(tasks)
    assert result["tasks"]["a"]["wave"] == 1 and result["tasks"]["b"]["wave"] == 0


def test_tasks_without_owns_do_not_create_edges():
    tasks = build([cloud_task("a", []), cloud_task("b", [])])
    assert [g["tasks"] for g in plan(tasks)["groups"]] == [["a"], ["b"]]


def test_group_with_one_ineligible_task_is_local():
    tasks = build(
        [
            cloud_task("a", ["a/**"]),
            cloud_task("b", ["b/**"], depends_on=["a"], local_only=True),
            cloud_task("c", ["c/**"]),
        ]
    )
    result = plan(tasks)
    assert [g["route"] for g in result["groups"]] == ["local", "cloud"]
    assert result["groups"][0]["reasons"] == [dr.GROUP_HAS_INELIGIBLE_TASK]
    assert result["tasks"]["a"]["reasons"] == [dr.GROUP_LOCAL]
    assert result["tasks"]["b"]["reasons"] == [dr.LOCAL_ONLY]
    assert result["tasks"]["c"]["route"] == "cloud"
    assert result["local_slots_saved"] == 1


def test_sequential_makes_one_group():
    tasks = build(
        [cloud_task("a", ["a/**"]), cloud_task("b", ["b/**"]), cloud_task("c", ["c/**"])],
        sequential=True,
    )
    result = plan(tasks)
    assert len(result["groups"]) == 1
    assert [result["tasks"][t]["wave"] for t in "abc"] == [0, 1, 2]


# ── dependency errors ────────────────────────────────────────────────────────


def test_dep_unknown():
    tasks = build([cloud_task("a", ["a/**"], depends_on=["ghost"])])
    with pytest.raises(RouteError) as info:
        plan(tasks)
    assert info.value.code == dr.DEP_UNKNOWN
    assert "ghost" in info.value.detail


def test_dep_cycle_and_self_cycle():
    cyc = build(
        [
            cloud_task("a", ["a/**"], depends_on=["c"]),
            cloud_task("b", ["b/**"], depends_on=["a"]),
            cloud_task("c", ["c/**"], depends_on=["b"]),
            cloud_task("d", ["d/**"]),
        ]
    )
    with pytest.raises(RouteError) as info:
        plan(cyc)
    assert info.value.code == dr.DEP_CYCLE
    assert "d" not in info.value.detail.split(": ")[1].split(", ")
    with pytest.raises(RouteError) as info:
        plan(build([cloud_task("a", ["a/**"], depends_on=["a"])]))
    assert info.value.code == dr.DEP_CYCLE


def test_dep_errors_precede_policy_off():
    tasks = build([cloud_task("a", ["a/**"], depends_on=["ghost"])])
    with pytest.raises(RouteError):
        plan(tasks, policy=None)


# ── provider choice and ties ─────────────────────────────────────────────────


def test_more_free_slots_wins():
    assert one(ELIGIBLE, slots_free={"claude": 1, "codex": 3})["provider"] == "codex"
    assert one(ELIGIBLE, slots_free={"claude": 3, "codex": 1})["provider"] == "claude"


def test_ties_alternate_starting_with_claude():
    tasks = build([cloud_task("t%d" % i, ["d%d/**" % i]) for i in range(1, 6)])
    result = plan(tasks)
    assert [result["tasks"][t.get("id")]["provider"] for t in tasks] == [
        "claude", "codex", "claude", "codex", "claude",
    ]


def test_each_assignment_decrements_free_slots():
    tasks = build([cloud_task("t%d" % i, ["d%d/**" % i]) for i in range(1, 6)])
    result = plan(tasks, slots_free={"claude": 3, "codex": 2})
    providers = [result["tasks"][t["id"]]["provider"] for t in tasks]
    assert providers == ["claude", "claude", "codex", "claude", "codex"]
    assert providers.count("claude") == 3 and providers.count("codex") == 2


def test_assignments_never_pile_onto_one_provider():
    tasks = build([cloud_task("t%d" % i, ["d%d/**" % i]) for i in range(1, 5)])
    result = plan(tasks, slots_free={"claude": 3, "codex": 2})
    providers = {result["tasks"][t["id"]]["provider"] for t in tasks}
    assert providers == {"claude", "codex"}


def test_free_slots_default_to_limits():
    result = plan(build([ELIGIBLE]), slots_free=None)
    assert result["tasks"]["t1"]["provider"] == "claude"


def test_slots_free_must_be_int():
    with pytest.raises(RouteError):
        plan(build([ELIGIBLE]), slots_free={"claude": "3"})


# ── policy and run-level refusals ────────────────────────────────────────────


@pytest.mark.parametrize("policy", [None, "off"])
def test_policy_off_yields_zero_cloud(policy):
    tasks = build([cloud_task("a", ["a/**"]), cloud_task("b", ["b/**"], est=500)])
    result = plan(tasks, policy=policy)
    assert result["policy"] == "off"
    assert all(t["route"] == "local" for t in result["tasks"].values())
    assert all(t["reasons"] == [dr.POLICY_OFF] for t in result["tasks"].values())
    assert result["local_slots_saved"] == 0
    assert result["preconditions"]["grants_missing"] == []


def test_policy_invalid():
    with pytest.raises(RouteError) as info:
        plan(build([ELIGIBLE]), policy="always")
    assert info.value.code == dr.POLICY_INVALID


def test_run_refusals_keep_everything_local():
    result = plan(build([ELIGIBLE]), run_refusals=["RUN_DIRTY_BASE", "RUN_TOKEN_BUDGET", "RUN_DIRTY_BASE"])
    entry = result["tasks"]["t1"]
    assert entry["route"] == "local"
    assert entry["reasons"] == ["RUN_DIRTY_BASE", "RUN_TOKEN_BUDGET"]


def test_run_refusal_code_must_be_upper_snake():
    with pytest.raises(RouteError):
        plan(build([ELIGIBLE]), run_refusals=["dirty base"])


def test_required_refuses_when_any_group_local():
    tasks = build([cloud_task("a", ["a/**"]), cloud_task("b", ["b/**"], local_only=True)])
    with pytest.raises(RouteError) as info:
        plan(tasks, policy="required")
    err = info.value
    assert err.code == dr.CLOUD_REQUIRED_NOT_ELIGIBLE
    assert [r["task"] for r in err.refused] == ["b"]
    assert err.plan is not None and err.plan["tasks"]["a"]["route"] == "cloud"


def test_required_refuses_on_run_level_refusal():
    with pytest.raises(RouteError) as info:
        plan(build([ELIGIBLE]), policy="required", run_refusals=["RUN_DOCTOR_NOT_GREEN"])
    assert info.value.code == dr.CLOUD_REQUIRED_NOT_ELIGIBLE
    assert info.value.refused[0]["reasons"] == ["RUN_DOCTOR_NOT_GREEN"]


def test_required_all_eligible_ok_and_refused_empty():
    result = plan(build([ELIGIBLE]), policy="required")
    assert result["refused"] == []


def test_auto_with_local_group_does_not_raise():
    result = plan(build([dict(ELIGIBLE, local_only=True)]))
    assert result["tasks"]["t1"]["route"] == "local" and result["refused"] == []


# ── preconditions ────────────────────────────────────────────────────────────


def test_preconditions_grants_and_consent():
    config = copy.deepcopy(CONFIG)
    config["secrets"]["exposure"] = {"claude": ["B", "A"], "codex": []}
    tasks = build([dict(ELIGIBLE, needs_secrets=["A"])])
    result = plan(tasks, config=config, run="r1", providers_available={"claude"}, grants={"cloud-dispatch:claude"})
    pre = result["preconditions"]
    assert pre["doctor"] == "unknown"
    assert pre["grants_missing"] == ["push:origin/ffs/cloud/r1", "secret-use:A", "secret-use:B"]
    assert pre["consent_required"] == ["A", "B"]
    assert result["secret_needs"] == {"t1": ["A"]}


def test_preconditions_all_granted():
    grants = {"cloud-dispatch:claude", "push:origin/ffs/cloud/r1"}
    result = plan(build([ELIGIBLE]), run="r1", providers_available={"claude"}, grants=grants)
    assert result["preconditions"]["grants_missing"] == []


def test_preconditions_unknown_grants_reports_all_missing():
    result = plan(build([ELIGIBLE]), run="r1", providers_available={"codex"}, grants=None)
    assert result["preconditions"]["grants_missing"] == [
        "cloud-dispatch:codex", "push:origin/ffs/cloud/r1",
    ]


def test_preconditions_doctor_state():
    assert plan(build([ELIGIBLE]), doctor="green")["preconditions"]["doctor"] == "green"
    refusal = plan(build([ELIGIBLE]), run_refusals=[dr.RUN_DOCTOR_NOT_GREEN])
    assert refusal["preconditions"]["doctor"] == "not-green"
    with pytest.raises(RouteError):
        plan(build([ELIGIBLE]), doctor="maybe")


def test_run_id_validated():
    with pytest.raises(RouteError) as info:
        plan(build([ELIGIBLE]), run="bad run")
    assert info.value.code == dr.RUN_ID_INVALID


# ── shape and determinism ────────────────────────────────────────────────────


def test_plan_shape_matches_contract():
    result = plan(build([ELIGIBLE]), run="r1")
    assert {"run", "policy", "groups", "tasks", "budgets", "refused", "secret_needs",
            "local_slots_saved", "preconditions"} <= set(result)
    assert result["budgets"] == {"claude": 3, "codex": 3, "total": 6, "max_local_verifiers": 1}
    assert set(result["tasks"]["t1"]) >= {"route", "provider", "reasons", "model", "effort", "provenance", "wave"}


def test_determinism_and_json_serializable():
    raw = [cloud_task("t%d" % i, ["d%d/**" % (i % 3)], est=10 * i) for i in range(1, 8)]
    first = json.dumps(plan(build(raw), policy="prefer"), sort_keys=False)
    second = json.dumps(plan(build(copy.deepcopy(raw)), policy="prefer"), sort_keys=False)
    assert first == second


def test_set_inputs_do_not_change_order():
    config = copy.deepcopy(CONFIG)
    config["secrets"]["exposure"] = {"claude": ["Z", "A", "M"], "codex": []}
    tasks = build([dict(ELIGIBLE, needs_secrets=["M"])])
    pre_a = plan(tasks, config=config, run="r", grants={"x"}, providers_available={"claude"})["preconditions"]
    pre_b = plan(tasks, config=config, run="r", grants={"x"}, providers_available=["claude"])["preconditions"]
    assert pre_a == pre_b
    assert pre_a["grants_missing"] == sorted(pre_a["grants_missing"])


def test_plan_does_not_mutate_inputs():
    tasks = build([ELIGIBLE, cloud_task("b", ["src/a/x"])])
    snapshot = copy.deepcopy(tasks)
    config = copy.deepcopy(CONFIG)
    plan(tasks, config=config)
    assert tasks == snapshot and config == CONFIG


def test_duplicate_ids_in_plan_input():
    with pytest.raises(RouteError) as info:
        plan([dict(ELIGIBLE), dict(ELIGIBLE)])
    assert info.value.code == dr.TASK_ID_DUPLICATE


def test_end_to_end_from_lines():
    tasks = mk(
        "- [ ] Build api [owns: src/api/**] [est: 45m] [verify: pytest api]",
        "- [ ] Docs [owns: docs/**] [est: 5m] [local]",
        "- [ ] Tests [owns: src/api/tests/**] [est: 10m]",
    )
    result = plan(tasks)
    assert [g["route"] for g in result["groups"]] == ["cloud", "local"]
    assert result["tasks"]["t3"]["implicit_deps"] == ["t1"]


# ── blank verify command ─────────────────────────────────────────────────────


def _blank_verify_config(cmd):
    config = copy.deepcopy(CONFIG)
    config["verify"]["cmd"] = cmd
    return config


@pytest.mark.parametrize("blank", ["", "   ", "\t\n ", " \n"])
@pytest.mark.parametrize("config_cmd", ["", "  ", "\n"])
def test_blank_verify_cmd_without_a_non_blank_fallback_stays_local(blank, config_cmd):
    config = _blank_verify_config(config_cmd)
    # routed without normalization too: plan_routes must not trust its caller to have stripped
    raw = dict(ELIGIBLE, verify_cmd=blank, kind="build", tier="execution", model_request={"kind": "tier", "name": "execution"},
               needs_secrets=[], local_only=False, local_reason="", depends_on=[], owns_source="annotated", title="t",
               prompt="p")
    for tasks in (build([dict(ELIGIBLE, verify_cmd=blank)]), [raw]):
        entry = plan(tasks, config=config)["tasks"]["t1"]
        assert entry["route"] == "local" and dr.VERIFY_CMD_MISSING in entry["reasons"]
        assert entry["provider"] is None


@pytest.mark.parametrize("blank", ["", "   ", "\t\n "])
def test_blank_verify_cmd_is_a_typed_refusal_under_required(blank):
    config = _blank_verify_config("   ")
    with pytest.raises(RouteError) as info:
        plan(build([dict(ELIGIBLE, verify_cmd=blank)]), policy="required", config=config)
    assert info.value.code == dr.CLOUD_REQUIRED_NOT_ELIGIBLE
    assert info.value.refused[0]["task"] == "t1" and dr.VERIFY_CMD_MISSING in info.value.refused[0]["reasons"]


def test_blank_task_verify_cmd_falls_back_to_a_non_blank_config_cmd():
    entry = one(dict(ELIGIBLE, verify_cmd="   "), config=_blank_verify_config("make test"))
    assert entry["route"] == "cloud"
    assert build([dict(ELIGIBLE, verify_cmd="  make  ")])[0]["verify_cmd"] == "make"
    assert build([dict(ELIGIBLE, verify_cmd="   ")])[0]["verify_cmd"] == ""


def test_whitespace_only_verify_annotation_is_blank():
    task = build(["- [ ] Do it [owns: src/a/**] [est: 10m] [verify:    ]"])[0]
    assert task["verify_cmd"] == ""
