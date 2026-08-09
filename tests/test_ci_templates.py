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
