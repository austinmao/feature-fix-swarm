"""Structural contract suite for templates/ci/ (REQ-301, phase 03-01).

STDLIB-ONLY by plan decision: assertions ride on the pinned Template Style
Contract (2-space indent, block style, named steps, six-token placeholder
inventory) via a small indentation-block helper — never a YAML library.
This helper is an assertion aid over repo-owned files, not a parser product;
the no-NEW-parser non-negotiable governs registry parsing.

Grep hygiene (#429): every NEGATIVE assertion runs on comment-stripped bytes
so a template comment can never satisfy or self-invalidate a gate. The one
positive-shape hex sweep (Pitfall 5) is deliberately comment-INCLUSIVE.
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "templates" / "ci"

WORKFLOWS = [
    "pr-fast.yml",
    "main-full.yml",
    "nightly-deep.yml",
    "deploy-staging.yml",
    "deploy-prod.yml",
]
# wall d3b57631: dependabot.yml is excluded from workflow-shaped sweeps by
# explicit filename, never by discovery accident.
DEPENDABOT = "dependabot.yml"

PLACEHOLDER_INVENTORY = {
    "TIER_FAST",
    "TIER_FULL",
    "TIER_NIGHTLY",
    "STAGING_ENV",
    "PROD_ENV",
    "LOCKFILE_HASH_PATH",
}

# Placeholder shape: {{UPPER_SNAKE}} NOT preceded by `$` — GitHub `${{ ... }}`
# expressions are not placeholders and must survive substitution untouched.
PLACEHOLDER_RE = re.compile(r"(?<!\$)\{\{([A-Z_]+)\}\}")
EXPRESSION_RE = re.compile(r"\$\{\{.*?\}\}")

PINNED_TIMEOUTS = {"pr-fast.yml": 10, "main-full.yml": 30, "nightly-deep.yml": 60}

TIER_STEP = (
    'bash scripts/gsd/test-tier.sh {token} > "$RUNNER_TEMP/tier-cmds"'
    ' && bash -e "$RUNNER_TEMP/tier-cmds"'
)

# wall ebadd63a: the smoke step run block is EXACTLY this — run-gate wraps the
# sole tier->command source; commands come from the committed registry, never
# from callers.
SMOKE_RUN = (
    'python3 lib/gates.py run-gate smoke --artifact "$ARTIFACT_DIGEST"'
    ' -- bash scripts/gsd/test-tier.sh "$SMOKE_TIER"'
)

PINNED_INPUTS_STAGING = {
    "surface": {"type": "string", "required": "true"},
    "artifact_digest": {"type": "string", "required": "true"},
    "previous_digest": {"type": "string", "required": "false"},
    "smoke_tier": {"type": "string", "required": "true"},
}
PINNED_INPUTS_PROD = {
    "surface": {"type": "string", "required": "true"},
    "artifact_digest": {"type": "string", "required": "true"},
    "previous_digest": {"type": "string", "required": "true"},
    "smoke_tier": {"type": "string", "required": "true"},
}


# ---------------------------------------------------------------------------
# indentation-block helpers (pinned Style Contract makes these deterministic)
# ---------------------------------------------------------------------------

def read(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


def strip_comments(text):
    """Drop `#`-to-EOL outside quoted strings (YAML: `#` at line start or
    after whitespace opens a comment)."""
    out = []
    for line in text.splitlines():
        buf = []
        quote = None
        for i, ch in enumerate(line):
            if quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
                buf.append(ch)
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                break
            else:
                buf.append(ch)
        out.append("".join(buf).rstrip())
    return "\n".join(out)


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def section(text, key, indent=0):
    """Lines strictly inside the `key:` block found at exactly `indent`."""
    pat = re.compile(r"^" + " " * indent + re.escape(key) + r":")
    out, cap = [], False
    for ln in text.splitlines():
        if not cap:
            if pat.match(ln):
                cap = True
        else:
            if ln.strip() and indent_of(ln) <= indent:
                break
            out.append(ln)
    return "\n".join(out)


def scalar(text, key, indent):
    m = re.search(
        r"^" + " " * indent + re.escape(key) + r":\s*(.*)$", text, re.M
    )
    return m.group(1).strip() if m else None


def jobs(text):
    """job_id -> job body (fields at indent 4)."""
    body = section(text, "jobs", 0)
    result, cur = {}, None
    for ln in body.splitlines():
        if ln.strip() and indent_of(ln) == 2:
            cur = ln.strip().rstrip(":")
            result[cur] = []
        elif cur is not None:
            result[cur].append(ln)
    return {k: "\n".join(v) for k, v in result.items()}


def steps(job_body):
    """Step item blocks (each starts `      - ` per the Style Contract)."""
    body = section(job_body, "steps", 4)
    items, cur = [], None
    for ln in body.splitlines():
        if ln.startswith("      - "):
            cur = [ln]
            items.append(cur)
        elif cur is not None:
            cur.append(ln)
    return ["\n".join(i) for i in items]


def step_name(step):
    m = re.search(r"^\s*- name:\s*(.*)$", step, re.M)
    return m.group(1).strip() if m else None


def run_of(step):
    """The run block value: single-line remainder, or dedented `|` body."""
    m = re.search(r"^\s{8}run:\s*(.*)$", step, re.M)
    if not m:
        return None
    rest = m.group(1).strip()
    if rest and not rest.startswith("|"):
        return rest
    lines = step.splitlines()
    idx = next(
        i for i, ln in enumerate(lines) if re.match(r"^\s{8}run:", ln)
    )
    body = []
    for ln in lines[idx + 1:]:
        if not ln.strip():
            body.append("")
        elif indent_of(ln) >= 10:
            body.append(ln[10:])
        else:
            break
    return "\n".join(body)


def all_run_blocks(text):
    """(job_id, step_name, run_text) over every run step, in file order."""
    out = []
    for job_id, body in jobs(text).items():
        for st in steps(body):
            r = run_of(st)
            if r is not None:
                out.append((job_id, step_name(st), r))
    return out


def env_map(env_text):
    result = {}
    for ln in env_text.splitlines():
        if not ln.strip():
            continue
        k, _, v = ln.strip().partition(":")
        result[k.strip()] = v.strip()
    return result


def inputs_of(text, trigger):
    on = section(text, "on", 0)
    trig = section(on, trigger, 2)
    inp = section(trig, "inputs", 4)
    result, cur = {}, None
    for ln in inp.splitlines():
        if not ln.strip():
            continue
        if indent_of(ln) == 6:
            cur = ln.strip().rstrip(":")
            result[cur] = {}
        elif cur is not None and indent_of(ln) == 8:
            k, _, v = ln.strip().partition(":")
            result[cur][k.strip()] = v.strip()
    return result


def structure_lines(text):
    """Lines that are YAML structure — block-scalar bodies excluded."""
    out, scalar_indent = [], None
    for ln in text.splitlines():
        if scalar_indent is not None:
            if not ln.strip() or indent_of(ln) > scalar_indent:
                continue
            scalar_indent = None
        m = re.match(r"^(\s*)(?:- )?[\w.-]+:\s*[|>][+-]?\s*$", ln)
        out.append(ln)
        if m:
            scalar_indent = len(m.group(1))
    return out


def existing_workflows():
    return [w for w in WORKFLOWS if (TEMPLATES / w).exists()]


def discovered_templates():
    if not TEMPLATES.is_dir():
        return []
    return sorted(TEMPLATES.glob("*.yml"))


# ---------------------------------------------------------------------------
# helper self-checks (keep the assertion aid honest)
# ---------------------------------------------------------------------------

_FIXTURE = textwrap.dedent(
    """\
    name: fx

    on:
      pull_request:

    permissions:
      contents: read

    jobs:
      one:
        runs-on: ubuntu-latest
        timeout-minutes: 10
        steps:
          - name: Alpha
            uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
            with:
              fetch-depth: 1
          - name: Beta
            run: |
              echo "hi # not a comment"
              exit 0
          - name: Gamma
            run: echo done
    """
)


def test_helper_strip_comments_preserves_quoted_hash():
    assert strip_comments('key: "a # b" # real') == 'key: "a # b"'


def test_helper_strip_comments_drops_full_line_comment():
    assert strip_comments("# gone\nkey: v") == "\nkey: v"


def test_helper_strip_comments_keeps_midword_hash():
    assert strip_comments("run: echo a#b") == "run: echo a#b"


def test_helper_placeholder_scan_ignores_expressions():
    text = (
        "a: ${{ github.sha }}\n"
        "b: {{TIER_FAST}}\n"
        "c: ${{ hashFiles('{{LOCKFILE_HASH_PATH}}') }}"
    )
    assert set(PLACEHOLDER_RE.findall(text)) == {
        "TIER_FAST",
        "LOCKFILE_HASH_PATH",
    }


def test_helper_section_and_jobs_and_steps():
    assert scalar(_FIXTURE, "name", 0) == "fx"
    jb = jobs(_FIXTURE)
    assert list(jb) == ["one"]
    st = steps(jb["one"])
    assert [step_name(s) for s in st] == ["Alpha", "Beta", "Gamma"]
    assert run_of(st[0]) is None
    assert run_of(st[1]) == 'echo "hi # not a comment"\nexit 0'
    assert run_of(st[2]) == "echo done"


def test_helper_structure_lines_skip_block_scalar_bodies():
    lines = structure_lines(_FIXTURE)
    joined = "\n".join(lines)
    assert "not a comment" not in joined
    assert "run: echo done" in joined


# ---------------------------------------------------------------------------
# style guard — every templates/ci/*.yml present (pinned Style Contract)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path", discovered_templates(), ids=lambda p: p.name
)
def test_style_contract(path):
    raw = path.read_text(encoding="utf-8")
    assert "\t" not in raw, "tabs are banned"
    for ln in raw.splitlines():
        if ln.strip():
            assert indent_of(ln) % 2 == 0, f"odd indent: {ln!r}"
    stripped = strip_comments(raw)
    # block style only: after removing expressions and placeholders, YAML
    # structure lines carry no flow braces/brackets.
    for ln in structure_lines(stripped):
        # placeholders first: a {{TOKEN}} may nest inside a ${{ ... }}
        # expression (e.g. hashFiles('{{LOCKFILE_HASH_PATH}}')) and the
        # non-greedy expression regex would stop at the inner `}}`.
        cleaned = PLACEHOLDER_RE.sub("", ln)
        cleaned = EXPRESSION_RE.sub("", cleaned)
        assert not re.search(r"[{}\[\]]", cleaned), f"flow style: {ln!r}"
    # placeholder inventory is closed (wall 4b37c841)
    assert set(PLACEHOLDER_RE.findall(raw)) <= PLACEHOLDER_INVENTORY
    # named, unique steps per job (workflows only; dependabot has no jobs —
    # excluded by explicit filename per wall d3b57631)
    if path.name != DEPENDABOT:
        for job_id, body in jobs(stripped).items():
            names = [step_name(s) for s in steps(body)]
            assert names, f"{path.name}:{job_id} has no steps"
            assert None not in names, f"unnamed step in {job_id}"
            assert len(names) == len(set(names)), f"dup step name in {job_id}"


# ---------------------------------------------------------------------------
# shared hardening sweep — the FIVE workflow templates only (wall d3b57631)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("wf", existing_workflows())
def test_workflow_level_permissions_contents_read_only(wf):
    text = strip_comments(read(wf))
    entries = [
        ln.strip()
        for ln in section(text, "permissions", 0).splitlines()
        if ln.strip()
    ]
    assert entries == ["contents: read"]


@pytest.mark.parametrize("wf", existing_workflows())
def test_every_job_has_pinned_timeout(wf):
    text = strip_comments(read(wf))
    for job_id, body in jobs(text).items():
        t = scalar(body, "timeout-minutes", 4)
        assert t is not None, f"{wf}:{job_id} missing timeout-minutes"
        assert t.isdigit()
        if wf in PINNED_TIMEOUTS:
            assert int(t) == PINNED_TIMEOUTS[wf]


@pytest.mark.parametrize("wf", existing_workflows())
def test_every_checkout_is_hardened(wf):
    text = strip_comments(read(wf))
    _assert_checkouts_hardened(text, wf)


@pytest.mark.parametrize("wf", existing_workflows())
def test_uses_pin_shape(wf):
    # positive shape check — deliberately comment-INCLUSIVE (the ` # v` tail
    # is the point).
    raw = read(wf)
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(.+)$", raw, re.M)
    assert uses, f"{wf} declares no actions"
    for u in uses:
        assert re.fullmatch(
            r"actions/[\w.-]+@[0-9a-f]{40} # v\S+", u.strip()
        ), f"unpinned or non-official uses: {u!r}"


@pytest.mark.parametrize("wf", existing_workflows())
def test_hex_only_in_uses_pins(wf):
    # Pitfall 5: comment-INCLUSIVE sweep — hex runs >=32 chars only in the
    # whitelisted uses-pin shape.
    for ln in read(wf).splitlines():
        if re.search(r"[0-9a-fA-F]{32,}", ln):
            assert re.search(
                r"uses:\s*actions/[\w.-]+@[0-9a-f]{40}", ln
            ), f"stray long hex outside uses pin: {ln!r}"


@pytest.mark.parametrize("wf", existing_workflows())
def test_no_expression_inside_run_blocks(wf):
    # wall af9f57b1: caller values cross into shell only via env: mappings.
    text = strip_comments(read(wf))
    for job_id, name, run in all_run_blocks(text):
        assert "${{" not in run, f"expression in run block {job_id}/{name}"


@pytest.mark.parametrize("wf", existing_workflows())
def test_no_direct_test_runner_invocation(wf):
    stripped = strip_comments(read(wf))
    assert not re.search(r"\bpytest\b|\bbats\b", stripped)


@pytest.mark.parametrize("wf", existing_workflows())
def test_job_level_permissions_restate_contents_read(wf):
    # wall 7f08ccda: job-level permissions blocks REPLACE the workflow block,
    # so every escalation must restate contents: read.
    text = strip_comments(read(wf))
    for job_id, body in jobs(text).items():
        perm = section(body, "permissions", 4)
        entries = [ln.strip() for ln in perm.splitlines() if ln.strip()]
        if entries:
            assert "contents: read" in entries, (
                f"{wf}:{job_id} job permissions omit contents: read"
            )


@pytest.mark.parametrize("wf", existing_workflows())
def test_concurrency_declared(wf):
    text = strip_comments(read(wf))
    assert section(text, "concurrency", 0).strip()


@pytest.mark.parametrize("wf", existing_workflows())
def test_no_command_shaped_inputs(wf):
    # wall ebadd63a: NO command-shaped workflow input on ANY template.
    text = strip_comments(read(wf))
    for trigger in ("workflow_call", "workflow_dispatch"):
        for key in inputs_of(text, trigger):
            assert not re.search(r"command|cmd|script|run", key), (
                f"command-shaped input {key!r} in {wf}"
            )


# ---------------------------------------------------------------------------
# pr-fast.yml contract (REQ-301 row 1)
# ---------------------------------------------------------------------------

def test_pr_fast_trigger_and_concurrency():
    text = strip_comments(read("pr-fast.yml"))
    assert re.search(r"^  pull_request:", section(text, "on", 0), re.M)
    conc = section(text, "concurrency", 0)
    assert scalar(conc, "cancel-in-progress", 2) == "true"


def test_pr_fast_checkout_hardening():
    text = strip_comments(read("pr-fast.yml"))
    _assert_checkouts_hardened(text, "pr-fast.yml")


def test_pr_fast_cache_keyed_on_lockfile_placeholder():
    raw = read("pr-fast.yml")
    assert "hashFiles('{{LOCKFILE_HASH_PATH}}')" in raw


def test_pr_fast_two_step_fast_tier():
    text = strip_comments(read("pr-fast.yml"))
    runs = [r for _, _, r in all_run_blocks(text)]
    assert TIER_STEP.format(token="{{TIER_FAST}}") in runs


def _assert_checkouts_hardened(text, wf):
    found = 0
    for job_id, body in jobs(text).items():
        for st in steps(body):
            uses = scalar(st, "uses", 8)
            if uses and uses.split("#")[0].strip().startswith(
                "actions/checkout@"
            ):
                found += 1
                with_block = section(st, "with", 8)
                assert scalar(with_block, "fetch-depth", 10) == "1", (
                    f"{wf}:{job_id} checkout without fetch-depth 1"
                )
                assert scalar(
                    with_block, "persist-credentials", 10
                ) == "false", (
                    f"{wf}:{job_id} checkout without persist-credentials false"
                )
    assert found >= 1, f"{wf} has no checkout step"


# ---------------------------------------------------------------------------
# main-full.yml contract (REQ-301 row 2)
# ---------------------------------------------------------------------------

def test_main_full_trigger_push_main():
    text = strip_comments(read("main-full.yml"))
    push = section(section(text, "on", 0), "push", 2)
    branches = section(push, "branches", 4)
    assert "- main" in [ln.strip() for ln in branches.splitlines()]


def test_main_full_needs_chaining():
    text = strip_comments(read("main-full.yml"))
    needs = [
        scalar(body, "needs", 4) for body in jobs(text).values()
    ]
    assert any(n for n in needs if n), "no job declares needs:"


def test_main_full_matrix_fail_fast_false():
    text = strip_comments(read("main-full.yml"))
    hits = [
        scalar(section(body, "strategy", 4), "fail-fast", 6)
        for body in jobs(text).values()
    ]
    assert "false" in hits


def test_main_full_artifact_retention_seven():
    text = strip_comments(read("main-full.yml"))
    for _, body in jobs(text).items():
        for st in steps(body):
            uses = scalar(st, "uses", 8)
            if uses and uses.split("#")[0].strip().startswith(
                "actions/upload-artifact@"
            ):
                with_block = section(st, "with", 8)
                assert scalar(with_block, "retention-days", 10) == "7"
                return
    pytest.fail("no upload-artifact step")


def test_main_full_two_step_full_tier():
    text = strip_comments(read("main-full.yml"))
    runs = [r for _, _, r in all_run_blocks(text)]
    assert TIER_STEP.format(token="{{TIER_FULL}}") in runs


def test_main_full_cache_keyed_on_lockfile_placeholder():
    assert "hashFiles('{{LOCKFILE_HASH_PATH}}')" in read("main-full.yml")


# ---------------------------------------------------------------------------
# nightly-deep.yml contract (REQ-301 row 3)
# ---------------------------------------------------------------------------

def test_nightly_triggers_schedule_and_dispatch():
    text = strip_comments(read("nightly-deep.yml"))
    on = section(text, "on", 0)
    sched = section(on, "schedule", 2)
    assert re.search(r"-\s*cron:", sched)
    assert re.search(r"^  workflow_dispatch:", on, re.M)


def test_nightly_two_step_nightly_tier():
    text = strip_comments(read("nightly-deep.yml"))
    runs = [r for _, _, r in all_run_blocks(text)]
    assert TIER_STEP.format(token="{{TIER_NIGHTLY}}") in runs


def test_nightly_failure_job_opens_issue():
    """Failure path opens an issue instead of reddening main (RESEARCH A3,
    Pitfall 8): if failure() + needs on the test job + job-scoped
    issues: write + gh guarded by command -v."""
    text = strip_comments(read("nightly-deep.yml"))
    jb = jobs(text)
    fail_jobs = [
        (job_id, body)
        for job_id, body in jb.items()
        if scalar(body, "if", 4) == "failure()"
    ]
    assert len(fail_jobs) == 1
    job_id, body = fail_jobs[0]
    needs = scalar(body, "needs", 4)
    assert needs in jb and needs != job_id
    perm = [
        ln.strip()
        for ln in section(body, "permissions", 4).splitlines()
        if ln.strip()
    ]
    assert "issues: write" in perm
    assert "contents: read" in perm  # wall 7f08ccda
    runs = "\n".join(run_of(st) or "" for st in steps(body))
    assert "command -v gh" in runs
    assert "gh issue create" in runs


# ---------------------------------------------------------------------------
# dependabot.yml — SOURCE CONTENT only, asserted structurally (wall d3b57631,
# wall 731b58ce: emission/advisory BEHAVIOR is 03-02's render concern; no
# render-behavior assertion may live here)
# ---------------------------------------------------------------------------

def test_dependabot_source_structural():
    text = strip_comments(read(DEPENDABOT))
    assert scalar(text, "version", 0) == "2"
    upd = section(text, "updates", 0)
    entries = [ln.strip() for ln in upd.splitlines() if ln.strip()]
    assert "- package-ecosystem: github-actions" in entries
    sched = section(upd, "schedule", 4)
    assert scalar(sched, "interval", 6) == "weekly"


def test_dependabot_excluded_from_workflow_sweep_by_name():
    # wall d3b57631: exclusion is by explicit filename, not discovery accident
    assert DEPENDABOT not in WORKFLOWS
    assert set(PINNED_TIMEOUTS) <= set(WORKFLOWS)


# ---------------------------------------------------------------------------
# completeness + inventory drift tripwires
# ---------------------------------------------------------------------------

def test_exactly_six_templates():
    names = sorted(p.name for p in discovered_templates())
    assert names == sorted(WORKFLOWS + [DEPENDABOT])


def test_placeholder_inventory_union_is_exactly_six():
    # key link: this inventory IS 03-02 render's substitution set — a token
    # missing here ships a dead {{TOKEN}} in a rendered workflow.
    union = set()
    for p in discovered_templates():
        union |= set(PLACEHOLDER_RE.findall(p.read_text(encoding="utf-8")))
    assert union == PLACEHOLDER_INVENTORY


# ---------------------------------------------------------------------------
# deploy pair — structural (REQ-301 rows 4-5, audit rows 22/24 locked)
# ---------------------------------------------------------------------------

DEPLOYS = {
    "deploy-staging.yml": PINNED_INPUTS_STAGING,
    "deploy-prod.yml": PINNED_INPUTS_PROD,
}
DEPLOY_ENVIRONMENT = {
    "deploy-staging.yml": "{{STAGING_ENV}}",
    "deploy-prod.yml": "{{PROD_ENV}}",
}


def run_steps(job_body):
    out = []
    for st in steps(job_body):
        r = run_of(st)
        if r is not None:
            out.append((step_name(st), r, st))
    return out


def _deploy_job(wf):
    text = strip_comments(read(wf))
    jb = jobs(text)
    assert len(jb) == 1
    return next(iter(jb.values()))


@pytest.mark.parametrize("wf", sorted(DEPLOYS))
def test_deploy_inputs_pinned_and_dispatch_parity(wf):
    # wall 9d404c88: workflow_dispatch mirrors workflow_call inputs exactly
    # (same keys, types, required flags) so manual dispatch can supply
    # everything the steps consume.
    text = strip_comments(read(wf))
    call_inputs = inputs_of(text, "workflow_call")
    dispatch_inputs = inputs_of(text, "workflow_dispatch")
    assert call_inputs == DEPLOYS[wf]
    assert dispatch_inputs == DEPLOYS[wf]


@pytest.mark.parametrize("wf", sorted(DEPLOYS))
def test_deploy_environment_and_oidc(wf):
    body = _deploy_job(wf)
    assert scalar(body, "environment", 4) == DEPLOY_ENVIRONMENT[wf]
    perm = [
        ln.strip()
        for ln in section(body, "permissions", 4).splitlines()
        if ln.strip()
    ]
    assert "id-token: write" in perm
    assert "contents: read" in perm  # wall 7f08ccda


@pytest.mark.parametrize("wf", sorted(DEPLOYS))
def test_deploy_env_binds_inputs_once(wf):
    # walls af9f57b1 + eb28c568: inputs cross into shell only via the job
    # env mapping, and ARTIFACT_DIGEST is bound exactly once — the digest
    # that is gated IS the digest deployed.
    text = strip_comments(read(wf))
    body = _deploy_job(wf)
    env = env_map(section(body, "env", 4))
    assert env["ARTIFACT_DIGEST"] == "${{ inputs.artifact_digest }}"
    assert env["SURFACE"] == "${{ inputs.surface }}"
    assert env["SMOKE_TIER"] == "${{ inputs.smoke_tier }}"
    # OQ9: positional run-id for gates.py calls
    assert env["RUN_ID"] == "ffs-${{ github.run_id }}"
    # the job env binding is the ONLY dotted inputs.artifact_digest reference
    assert text.count("inputs.artifact_digest") == 1


@pytest.mark.parametrize("wf", sorted(DEPLOYS))
def test_deploy_smoke_step_is_exactly_the_gate_wrapped_form(wf):
    # walls ebadd63a + d688023e: run-gate wraps the sole tier->command source.
    runs = [r for _, r, _ in run_steps(_deploy_job(wf))]
    assert SMOKE_RUN in runs


@pytest.mark.parametrize("wf", sorted(DEPLOYS))
def test_deploy_smoke_tier_shape_validated_in_job(wf):
    runs = "\n".join(r for _, r, _ in run_steps(_deploy_job(wf)))
    assert "^[a-z][a-z0-9_-]{0,31}$" in runs


@pytest.mark.parametrize("wf", sorted(DEPLOYS))
def test_deploy_consumer_marker_block(wf):
    # wall 4b37c841: deploy mechanism is ONLY the marked no-op consumer
    # block — no input and no token carries a command.
    raw = read(wf)
    assert "CONSUMER DEPLOY STEP — replace this block" in raw
    marker = [
        (n, r, st)
        for n, r, st in run_steps(_deploy_job(wf))
        if "CONSUMER DEPLOY STEP" in r
    ]
    assert len(marker) == 1
    _, run, _ = marker[0]
    # wall 760b2ad9: the marker guidance binds BOTH surface and digest.
    assert "$SURFACE" in run
    assert "$ARTIFACT_DIGEST" in run
    # wall 431cd39a: template form is echo-only and exits 0 so the smoke
    # gate is what fails the job — the placeholder must not mask it.
    assert "exit 1" not in run
    for line in run.splitlines():
        if line.strip():
            assert line.lstrip().startswith("echo"), (
                f"non-echo line in consumer block: {line!r}"
            )


@pytest.mark.parametrize("wf", sorted(DEPLOYS))
def test_deploy_gate_calls_share_the_digest_env_var(wf):
    # wall eb28c568: every gates.py call binds the SAME "$ARTIFACT_DIGEST".
    for _, run, _ in run_steps(_deploy_job(wf)):
        if "lib/gates.py" in run:
            assert '"$ARTIFACT_DIGEST"' in run


@pytest.mark.parametrize("wf", sorted(DEPLOYS))
def test_deploy_no_build_invocation(wf):
    stripped = strip_comments(read(wf))
    assert not re.search(
        r"docker\s+build|docker\s+push|npm\s+publish|pip\s+wheel", stripped
    )


def test_staging_order_is_the_contract():
    # deploy -> run-gate smoke -> promote (REQ-301; audit row 24 locked)
    rs = run_steps(_deploy_job("deploy-staging.yml"))
    runs = [r for _, r, _ in rs]
    i_deploy = next(i for i, r in enumerate(runs) if "CONSUMER DEPLOY" in r)
    i_smoke = runs.index(SMOKE_RUN)
    i_promote = next(
        i for i, r in enumerate(runs)
        if r.startswith("python3 lib/gates.py promote")
    )
    assert i_deploy < i_smoke < i_promote


def test_staging_promote_shape_and_no_escape():
    rs = run_steps(_deploy_job("deploy-staging.yml"))
    promote = [
        (r, st) for _, r, st in rs
        if r.startswith("python3 lib/gates.py promote")
    ]
    assert len(promote) == 1
    run, st = promote[0]
    assert '"$RUN_ID" --from staging --to prod --surface "$SURFACE"' in run
    assert "--evidence" in run
    # no-escape: a smoke failure must skip this step (comment-stripped)
    assert scalar(st, "if", 8) is None
    assert scalar(st, "continue-on-error", 8) is None


def test_prod_checkout_pins_run_sha_and_trust_chain_comment():
    # walls 792e5b5b + c6a42b43: ref pin + authoritative-control-chain comment
    raw = read("deploy-prod.yml")
    assert "branch protection" in raw
    assert "defense-in-depth" in raw
    text = strip_comments(raw)
    body = _deploy_job("deploy-prod.yml")
    checkout = [
        st for st in steps(body)
        if (scalar(st, "uses", 8) or "").startswith("actions/checkout@")
    ]
    assert len(checkout) == 1
    with_block = section(checkout[0], "with", 8)
    assert scalar(with_block, "ref", 10) == "${{ github.sha }}"


def test_prod_ref_guard_then_check_grant_before_any_mutation():
    # wall c6a42b43: the ref guard refuses non-default-branch runs BEFORE
    # check-grant; check-grant runs strictly before any mutation step.
    rs = run_steps(_deploy_job("deploy-prod.yml"))
    runs = [r for _, r, _ in rs]
    assert "default branch" in runs[0] and "exit 1" in runs[0]
    assert runs[1].startswith("python3 lib/gates.py check-grant")
    assert "--require-environments" in runs[1]
    assert '"$RUN_ID"' in runs[1]
    assert 'deploy:prod-$SURFACE' in runs[1]
    i_deploy = next(i for i, r in enumerate(runs) if "CONSUMER DEPLOY" in r)
    assert i_deploy > 1
    i_smoke = runs.index(SMOKE_RUN)
    assert i_deploy < i_smoke


def test_prod_rollback_emitted_never_executed():
    # previous_digest is referenced ONLY by the if: failure() emission step,
    # whose run block consists solely of writes to GITHUB_OUTPUT /
    # GITHUB_STEP_SUMMARY (PROJECT.md non-negotiable).
    text = strip_comments(read("deploy-prod.yml"))
    refs = [
        ln for ln in text.splitlines() if "inputs.previous_digest" in ln
    ]
    body = _deploy_job("deploy-prod.yml")
    emission = [
        st for st in steps(body) if scalar(st, "if", 8) == "failure()"
    ]
    assert len(emission) == 1
    st = emission[0]
    env = env_map(section(st, "env", 8))
    assert env.get("PREVIOUS_DIGEST") == "${{ inputs.previous_digest }}"
    # the emission step env binding is the ONLY dotted reference (input
    # declarations use the bare key, not the dotted consumption form)
    assert len(refs) == 1
    assert "PREVIOUS_DIGEST:" in refs[0]
    for line in run_of(st).splitlines():
        if line.strip():
            assert re.search(
                r'>>\s*"\$GITHUB_(OUTPUT|STEP_SUMMARY)"\s*$', line
            ), f"emission line does not write to outputs: {line!r}"


def test_prod_promote_absent():
    # promotion is staging's job; prod records no promote
    runs = [r for _, r, _ in run_steps(_deploy_job("deploy-prod.yml"))]
    assert not any("lib/gates.py promote" in r for r in runs)


# ---------------------------------------------------------------------------
# gates.py CLI parity (zero network, subprocess over the repo checkout).
# DEVIATION from plan letter: the hand-rolled gates.py parser has no
# per-subcommand --help (`check-grant --help` prints NOT-GRANTED, rc 1);
# the bare usage text carries the full CLI surface, so parity pins ride on
# it — same flag-drift tripwire, zero network.
# ---------------------------------------------------------------------------

def _gates_usage():
    proc = subprocess.run(
        [sys.executable, str(REPO / "lib" / "gates.py")],
        capture_output=True,
        text=True,
    )
    return proc.stdout + proc.stderr


def test_cli_parity_check_grant_require_environments():
    assert "--require-environments" in _gates_usage()


def test_cli_parity_promote_evidence():
    assert "--evidence" in _gates_usage()


# ---------------------------------------------------------------------------
# behavioral smoke-fail (plan.md row 84): smoke failure records NO promotion
# ---------------------------------------------------------------------------

EXPR_FIXTURES = {
    "inputs.artifact_digest": "app@sha256:" + "ab" * 32,
    "inputs.surface": "web",
    "inputs.smoke_tier": "fast",
    "inputs.previous_digest": "app@sha256:" + "cd" * 32,
    "github.run_id": "12345",
}
PLACEHOLDER_FIXTURES = {
    "TIER_FAST": "fast",
    "TIER_FULL": "full",
    "TIER_NIGHTLY": "nightly",
    "STAGING_ENV": "staging",
    "PROD_ENV": "production",
    "LOCKFILE_HASH_PATH": "requirements.txt",
}

_STUB_GATES = textwrap.dedent(
    """\
    import sys
    from pathlib import Path

    log = Path(__file__).resolve().parent.parent / "calls.log"
    with log.open("a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\\n")
    sys.exit(1 if sys.argv[1:] and sys.argv[1] == "run-gate" else 0)
    """
)


def _subst_fixtures(value):
    def erepl(m):
        return EXPR_FIXTURES.get(m.group(1).strip(), "fixture")

    v = PLACEHOLDER_RE.sub(
        lambda m: PLACEHOLDER_FIXTURES[m.group(1)], value
    )
    return re.sub(r"\$\{\{(.*?)\}\}", erepl, v)


def test_staging_smoke_failure_records_no_promotion(tmp_path):
    """Execute deploy-staging's run blocks in step order against a recorder
    stub gates.py (run-gate exits 1), honoring Actions semantics: stop at
    the first failure, then only if: failure() steps run. wall 431cd39a:
    the consumer no-op block exits 0 so the smoke step IS exercised."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "gates.py").write_text(_STUB_GATES)

    text = strip_comments(read("deploy-staging.yml"))
    body = _deploy_job("deploy-staging.yml")
    env = dict(os.environ)
    for k, v in env_map(section(body, "env", 4)).items():
        env[k] = _subst_fixtures(v)

    failed = False
    executed = []
    for name, run, st in run_steps(body):
        cond = scalar(st, "if", 8)
        if failed and cond != "failure()":
            continue
        if not failed and cond == "failure()":
            continue
        step_env = dict(env)
        for k, v in env_map(section(st, "env", 8)).items():
            step_env[k] = _subst_fixtures(v)
        proc = subprocess.run(
            ["bash", "-e", "-c", _subst_fixtures(run)],
            cwd=tmp_path,
            env=step_env,
            capture_output=True,
            text=True,
        )
        executed.append(name)
        if proc.returncode != 0:
            failed = True

    log_path = tmp_path / "calls.log"
    log = log_path.read_text() if log_path.exists() else ""
    calls = [ln for ln in log.splitlines() if ln.strip()]
    assert failed, "smoke failure must fail the job"
    # wall 431cd39a: the smoke step was actually exercised
    assert any(c.startswith("run-gate smoke") for c in calls)
    assert "Smoke gate" in executed
    # zero promote invocations recorded
    assert not any(c.startswith("promote") for c in calls)
