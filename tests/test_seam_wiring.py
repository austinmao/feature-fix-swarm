"""Seam-wiring integration tests (spec-007 phase 4, plan 04-01).

INT-002 — promote-emit EMITS (never runs) the staging→prod promote command:
subprocess-driven against scripts/gsd/promote-emit.sh with an isolated
$GATES_STORE fixture store. The emitted string must carry the VERBATIM
gates.py promote flag set (lib/gates.py:2885-2897) — drift = unrunnable emit.

Wall-residual pins carried into this suite (WALL-RESIDUALS.md):
- 1ea31e0b: evidence selection reuses the EXISTING store schema — top-level
  gate records carry exit_code/executed_by/artifact (_evidence_resolves
  semantics, gates.py:1003-1014); `_promotions[run_id]` records carry the only
  run+surface binding the store has (surface/artifact/evidence_ids/recorded_at).
- 156cf77a: emission binds to run AND surface — a passing artifact claimed by
  another run's promotion records must never appear in this run's emit.
- 7cec17e8: multiple passing artifacts for one run+surface → LATEST by ts wins
  (promotion recorded_at, else store insertion order) and the skipped
  candidates are NAMED on stderr — deterministic, auditable.
- 31a608dc: every interpolated value is shape-gated BEFORE emission and
  single-quoted in the emitted command; non-conforming store values → typed
  value-free refusal, nothing emitted.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EMIT = ROOT / "scripts" / "gsd" / "promote-emit.sh"
GATES = ROOT / "lib" / "gates.py"

DIG_A = "app@sha256:" + "a" * 64
DIG_B = "app@sha256:" + "b" * 64


def _gate(artifact=None, exit_code=0, executed_by="run_gate"):
    g = {"exit_code": exit_code, "cmd": "python3 -m pytest -q",
         "tests_before": "", "tests_after": "ok", "failure_sig": "",
         "executed_by": executed_by}
    if artifact is not None:
        g["artifact"] = artifact
    return {"gate": g}


def _grants(actions, run="run-42"):
    now = time.time()
    return {run: {"grants": {a: {"granted_at": now,
                                 "expires_at": now + 3600,
                                 "granted_by": "operator"} for a in actions}}}


def _promo(surface, artifact, evidence_ids, recorded_at):
    return {"from_env": "dev", "to_env": "staging", "surface": surface,
            "artifact": artifact, "evidence_ids": evidence_ids,
            "recorded_at": recorded_at, "expires_at": recorded_at + 3600}


def _write_store(tmp_path, data):
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(data))
    return p


def _emit(store, run="run-42"):
    env = dict(os.environ, GATES_STORE=str(store))
    env.pop("FFS_ENV_REGISTRY_REQUIRED", None)
    return subprocess.run(["bash", str(EMIT), run], capture_output=True,
                          text=True, cwd=ROOT, env=env, timeout=60)


# ── INT-002 ─────────────────────────────────────────────────────────────────

def test_emit_single_staging_grant(tmp_path):
    data = {"gate-1": _gate(DIG_A)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    data["_promotions"] = {"run-42": [_promo("web", DIG_A, ["gate-1"],
                                             time.time())]}
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    line = lines[0]
    assert "promote" in line
    assert "run-42" in line
    # verbatim gates.py:2885-2897 flag set, run_id positional
    assert "--from staging --to prod" in line
    assert "--surface 'web'" in line
    assert f"--artifact '{DIG_A}'" in line
    assert "--evidence 'gate-1'" in line
    assert "--ttl-hours" not in line  # default TTL is the promote arm's own


def test_emit_never_runs_store_byte_identical(tmp_path):
    data = {"gate-1": _gate(DIG_A)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    store = _write_store(tmp_path, data)
    before = store.read_bytes()
    r = _emit(store)
    assert r.returncode == 0
    assert store.read_bytes() == before  # cmp: EMIT-not-run proof
    after = json.loads(store.read_text())
    assert "_promotions" not in after
    assert "pending" not in after.get("_autonomy", {}).get("run-42", {})
    # nothing was promoted: the prod check-grant against the SAME store
    # still refuses
    env = dict(os.environ, GATES_STORE=str(store))
    env.pop("FFS_ENV_REGISTRY_REQUIRED", None)
    chk = subprocess.run(
        ["python3", str(GATES), "check-grant", "run-42",
         "--action", "deploy:prod-web"],
        capture_output=True, text=True, cwd=ROOT, env=env, timeout=60)
    assert chk.returncode != 0


def test_no_staging_grant_silent_exit_zero(tmp_path):
    data = {"gate-1": _gate(DIG_A)}
    data["_autonomy"] = _grants(["push:origin/main"])
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0
    assert r.stdout == ""
    assert r.stderr == ""
    # a run with no grants at all is silent too
    r2 = _emit(store, run="run-43")
    assert r2.returncode == 0 and r2.stdout == "" and r2.stderr == ""


def test_grant_without_evidence_one_stderr_advisory(tmp_path):
    data = {"gate-1": _gate(DIG_A, exit_code=1),      # failing gate
            "gate-2": _gate(None),                    # no artifact bound
            "gate-3": _gate(DIG_A, executed_by="caller")}  # fabricable
    data["_autonomy"] = _grants(["deploy:staging-web"])
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0
    assert r.stdout == ""
    advisory = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(advisory) == 1
    # value-free: no artifact digest bytes in the advisory
    assert "a" * 64 not in r.stderr


def test_two_staging_grants_one_line_each(tmp_path):
    data = {"gate-1": _gate(DIG_A)}
    data["_autonomy"] = _grants(["deploy:staging-web", "deploy:staging-api"])
    data["_promotions"] = {"run-42": [
        _promo("web", DIG_A, ["gate-1"], time.time()),
        _promo("api", DIG_A, ["gate-1"], time.time())]}
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert any("--surface 'web'" in ln for ln in lines)
    assert any("--surface 'api'" in ln for ln in lines)


def test_emission_binds_to_run_and_surface(tmp_path):
    # sig 156cf77a: each run's emission uses ONLY its own promotion-bound
    # rows — run-42 binds DIG_A, run-77 binds DIG_B; neither sees the other.
    data = {"gate-a": _gate(DIG_A), "gate-b": _gate(DIG_B)}
    auto = _grants(["deploy:staging-web"], run="run-42")
    auto.update(_grants(["deploy:staging-web"], run="run-77"))
    data["_autonomy"] = auto
    data["_promotions"] = {
        "run-42": [_promo("web", DIG_A, ["gate-a"], time.time())],
        "run-77": [_promo("web", DIG_B, ["gate-b"], time.time())]}
    store = _write_store(tmp_path, data)
    r = _emit(store, run="run-42")
    assert r.returncode == 0, r.stderr
    assert DIG_A in r.stdout
    assert DIG_B not in r.stdout
    # and run-77 emits its OWN promotion-bound artifact
    r77 = _emit(store, run="run-77")
    assert r77.returncode == 0, r77.stderr
    assert DIG_B in r77.stdout
    assert DIG_A not in r77.stdout


def test_multiple_artifacts_latest_by_ts_wins_and_skipped_named(tmp_path):
    # sig 7cec17e8 (promotion path): two records for the SAME run+surface —
    # the later recorded_at wins; the stale artifact is NAMED on stderr.
    t = time.time()
    data = {"gate-a": _gate(DIG_A), "gate-b": _gate(DIG_B)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    data["_promotions"] = {"run-42": [
        _promo("web", DIG_A, ["gate-a"], t - 100),
        _promo("web", DIG_B, ["gate-b"], t),
    ]}
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert DIG_B in lines[0]
    assert DIG_A not in r.stdout
    assert DIG_A in r.stderr  # skipped candidate named — auditable


def test_unbound_evidence_never_emits_advisory_instead(tmp_path):
    # sigs 156cf77a + 1ea31e0b (diff review 2026-08-10): top-level gate
    # records carry no run/surface ownership — with no _promotions binding
    # they must NOT be emitted (the removed fallback pool could promote an
    # unrelated run's artifact); the advisory fires and stdout stays empty.
    data = {"gate-a": _gate(DIG_A), "gate-b": _gate(DIG_B)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""
    assert "PROMOTE-EMIT-ADVISORY" in r.stderr
    assert DIG_A not in r.stdout and DIG_B not in r.stdout


def test_injection_proof_metachar_artifact_refused(tmp_path):
    # sig 31a608dc: a shell-metachar artifact value in the store → typed
    # refusal, NOTHING emitted, no metachar bytes on stdout.
    evil = "x; rm -rf /tmp/pwn @sha256:" + "c" * 64
    data = {"gate-1": _gate(evil)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    data["_promotions"] = {"run-42": [_promo("web", evil, ["gate-1"],
                                             time.time())]}
    store = _write_store(tmp_path, data)
    r = _emit(store)
    assert r.returncode == 1
    assert r.stdout == ""
    assert "rm -rf" not in r.stdout + r.stderr  # value-free refusal
    assert "PROMOTE-EMIT-REFUSED" in r.stderr


def test_bad_run_id_refused():
    r = subprocess.run(["bash", str(EMIT), "run-42; echo pwn"],
                       capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert r.returncode == 1
    assert r.stdout == ""
    assert "pwn" not in r.stdout


# ── INT-004 + seam-presence pins (Task 3) ───────────────────────────────────

import re  # noqa: E402

REVIEW_GATE = ROOT / "scripts" / "gsd" / "review-gate-command.sh"
FI_SKILL = ROOT / "skills" / "feature-implement" / "SKILL.md"
PF_SKILL = ROOT / "skills" / "preflight" / "SKILL.md"
# Content pin for INT-004(b) — see that test for the re-pin protocol.
# The "sha256:" prefix is the credential scanner's own sanctioned form for a
# legitimate digest literal (env-registry.sh _WHITELIST) — keep it.
REVIEW_GATE_SHA256 = (
    "sha256:4b5835e28ab47860d790b158e5282ae8a30f04fc91c863a671962ac62be77cf4")


def test_int004a_review_gate_zero_seam_tokens():
    """INT-004(a): the whole-file scan is deliberate and cannot
    self-invalidate — this phase never edits review-gate-command.sh, so ANY
    mention of these tokens (comments included) is a seam violation. Hard
    mode reaches it through gates.py env-var resolution (lib/gates.py:
    2941-2945), never a call-site edit."""
    text = REVIEW_GATE.read_text()
    for token in ("env-registry", "FFS_ENV_REGISTRY", "require-environments"):
        assert token not in text, token


def test_int004b_review_gate_content_pin():
    """INT-004(b): review-gate-command.sh is tripwired — it may only change
    deliberately, never as collateral of an unrelated edit.

    Re-based 2026-08-27 (was a `git diff` against phase base 2e77ed7): the
    commit-diff form skipped itself whenever the base commit became
    unreachable (post-squash-merge main), so the tripwire silently stopped
    guarding. A content hash always runs and survives squash merges.

    Editing this file deliberately? Update REVIEW_GATE_SHA256 to the new
    `shasum -a 256 scripts/gsd/review-gate-command.sh` in the SAME commit —
    that re-pin is the conscious acknowledgement this gate exists to force."""
    actual = "sha256:" + hashlib.sha256(REVIEW_GATE.read_bytes()).hexdigest()
    assert actual == REVIEW_GATE_SHA256, (
        f"review-gate-command.sh changed ({actual}); if intended, "
        f"re-pin REVIEW_GATE_SHA256 in this test in the same commit")


def test_seam_presence_pins():
    """test_promotion_protocol_doc idiom: guards silent seam removal by
    later SKILL edits."""
    fi = FI_SKILL.read_text()
    assert "FFS_ENV_REGISTRY_REQUIRED" in fi          # Step-2 hard-mode seam
    assert fi.count("promote-emit.sh") >= 2           # Step 6 + verify block
    assert "env-registry.sh seed" in PF_SKILL.read_text()  # preflight seam


def test_step2_export_guarded_by_autonomous_and_prod_detection():
    """The export appears exactly once, INSIDE a conditional guarded by
    AUTONOMOUS=1 AND prod-prefix detection (ledger-first, plan-grep
    fallback) — never unconditionally."""
    fi = FI_SKILL.read_text()
    assert fi.count("export FFS_ENV_REGISTRY_REQUIRED=1") == 1
    guarded = re.search(
        r'if \[ "\$AUTONOMOUS" = "1" \]'            # autonomous guard
        r'[\s\S]{0,1200}?PROD_ACTION_PREFIXES'      # ledger-first detection
        r'[\s\S]{0,1200}?'
        + re.escape("deploy:prod-|flip:prod-|migrate:prod-")  # plan-grep
        + r'[\s\S]{0,1200}?export FFS_ENV_REGISTRY_REQUIRED=1',
        fi)
    assert guarded, "export must sit inside the guarded detection block"


def test_require_environments_flag_only_at_skill_prod_callsites():
    """sig 16ac087d resolution: the 'zero call-site edits' pin covers
    gates.py INTERNALS and pre-existing consumers (review-gate-command.sh —
    INT-004 above); this skill's OWN new/edited prod invocation lines may
    and DO carry the flag explicitly (sig ba1efa84: deterministic per-call
    hard mode; the Step-2 export is belt-and-braces for subprocesses, and
    no assertion depends on it surviving block boundaries)."""
    fi = FI_SKILL.read_text()
    flag_lines = [ln for ln in fi.splitlines()
                  if "--require-environments" in ln]
    assert flag_lines, "the skill's prod call site must pass the flag"
    assert all("check-grant" in ln for ln in flag_lines), flag_lines


# ── AC-010 (REQ-402) + AC-011 (REQ-403) + Seam-5 pin — plan 04-02 ───────────

import hashlib  # noqa: E402
import shutil   # noqa: E402

CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
LEAK_SCAN_DIR = ROOT / "tests" / "fixtures" / "leak-scan"
NONPROD_ACTION = "push:origin/test"


def _fixture_repo(tmp_path, name, with_registry):
    """A tmp `git init` repo whose own git-common-dir keeps gates.py registry
    resolution away from FFS's committed config/environments.yaml."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)],
                   check=True, capture_output=True)

    def g(*a):
        subprocess.run(["git", "-C", str(repo), *a],
                       check=True, capture_output=True)

    g("config", "user.email", "t@example.invalid")
    g("config", "user.name", "t")
    (repo / "README").write_text("fixture\n")
    g("add", "README")
    if with_registry:
        cfg = repo / "config"
        cfg.mkdir()
        shutil.copy(ROOT / "config" / "environments.yaml",
                    cfg / "environments.yaml")
        g("add", "config/environments.yaml")
    g("commit", "-q", "-m", "init")
    return repo


def _check_grant_nonprod(tmp_path, repo, granted, hard=False):
    """wall b75484dc: gates.py is resolved ABSOLUTELY from the checkout under
    test (ROOT-derived, computed before any chdir); the fixture repo never
    contains lib/gates.py and must not need to."""
    assert GATES.is_file(), GATES
    data = {}
    if granted:
        data["_autonomy"] = _grants([NONPROD_ACTION])
    store = tmp_path / f"store-{repo.name}-{granted}-{hard}.json"
    store.write_text(json.dumps(data))
    env = {k: v for k, v in os.environ.items()
           if k not in ("FFS_ENV_REGISTRY", "FFS_ENV_REGISTRY_REQUIRED")}
    env["GATES_STORE"] = str(store)
    if hard:
        env["FFS_ENV_REGISTRY_REQUIRED"] = "1"
    return subprocess.run(
        ["python3", str(GATES), "check-grant", "run-42",
         "--action", NONPROD_ACTION],
        capture_output=True, text=True, cwd=str(repo), env=env, timeout=60)


def test_ac010a_nonprod_granted_absent_registry_exit0_no_advisory(tmp_path):
    """AC-010(a): non-prod + grant + NO registry -> exit 0 and zero
    ENV-REGISTRY bytes on either stream (the advisory is prod-path-only,
    lib/gates.py _registry_absent_advisory sits inside the prod branch)."""
    repo = _fixture_repo(tmp_path, "noreg", with_registry=False)
    r = _check_grant_nonprod(tmp_path, repo, granted=True)
    assert r.returncode == 0, r.stderr
    assert "ENV-REGISTRY" not in r.stdout + r.stderr


def test_ac010b_nonprod_ungranted_byte_identical_to_registry_control(tmp_path):
    """AC-010(b): non-prod WITHOUT a grant -> exit 1 NOT-GRANTED, streams
    byte-identical to a registry-present control run (comparison, never
    assumption)."""
    absent = _fixture_repo(tmp_path, "noreg", with_registry=False)
    present = _fixture_repo(tmp_path, "reg", with_registry=True)
    ra = _check_grant_nonprod(tmp_path, absent, granted=False)
    rp = _check_grant_nonprod(tmp_path, present, granted=False)
    assert ra.returncode == 1
    assert "NOT-GRANTED" in ra.stdout + ra.stderr
    assert (ra.returncode, ra.stdout, ra.stderr) == \
        (rp.returncode, rp.stdout, rp.stderr)


def test_ac010_wall_77b8cc68_granted_registry_present_control(tmp_path):
    """wall 77b8cc68: the GRANTED non-prod case runs twice (registry absent
    vs present) and must be byte-identical on stdout+stderr+rc — absence-
    behavior equivalence proven by comparison."""
    absent = _fixture_repo(tmp_path, "noreg", with_registry=False)
    present = _fixture_repo(tmp_path, "reg", with_registry=True)
    ra = _check_grant_nonprod(tmp_path, absent, granted=True)
    rp = _check_grant_nonprod(tmp_path, present, granted=True)
    assert ra.returncode == 0, ra.stderr
    assert (ra.returncode, ra.stdout, ra.stderr) == \
        (rp.returncode, rp.stdout, rp.stderr)


def test_ac010c_hard_mode_env_var_inert_off_prod_path(tmp_path):
    """AC-010(c): the SAME non-prod pairs with FFS_ENV_REGISTRY_REQUIRED=1 in
    env exit AND stream byte-identical to soft mode — hard mode scopes to
    PROD_ACTION_PREFIXES only (lib/gates.py:1209), proving 04-01's run-wide
    Step-2 export is inert off the prod path (REQ-402's core promise)."""
    repo = _fixture_repo(tmp_path, "noreg", with_registry=False)
    for granted in (True, False):
        soft = _check_grant_nonprod(tmp_path, repo, granted=granted)
        hard = _check_grant_nonprod(tmp_path, repo, granted=granted, hard=True)
        assert (soft.returncode, soft.stdout, soft.stderr) == \
            (hard.returncode, hard.stdout, hard.stderr), granted


# ── AC-011 (REQ-403): network + credential repo-scan gates ──────────────────
#
# HONEST SCOPE (wall 9abbd7eb; residual 1a7ab992 documented, not resolved):
# these gates prove (1) absence of the ENUMERATED live-network mechanisms
# (gh/curl/wget/requests/urllib/socket/httpx/aiohttp/dev-tcp) at call-site
# positions in the five spec-007 test files, and (2) zero egress through
# PATH-resolved network binaries when the spec-007 executables run under
# `env -i` with a fail-loud stub PATH. They do NOT prove universal
# hermeticity: in-process Python network calls made by imported production
# code or helpers outside the enumerated token set are not intercepted —
# syscall-level proof is out of scope.

NETWORK_SCOPE = [
    ROOT / "lib" / "tests" / "test_gates.py",
    ROOT / "tests" / "test_ci_templates.py",
    ROOT / "tests" / "test_seam_wiring.py",
    ROOT / "tests" / "bats" / "env-registry.bats",
    ROOT / "tests" / "bats" / "test-tier.bats",
]

# Patterns are built by concatenation so this file's own source never
# contains a literal call site (the gate must not trip on itself).
_LP = chr(40)  # "("
_PY_NET_PATTERNS = [
    re.compile("urlopen" + r"\s*" + re.escape(_LP)),
    re.compile(r"requests\.(?:get|post|put|delete|patch|head|options"
               r"|request|Session)\s*" + re.escape(_LP)),
    re.compile(r"socket\.create_connection\s*" + re.escape(_LP)),
    re.compile(r"http\.client\.HTTP"),
    re.compile(r"httpx\.(?:get|post|put|delete|patch|head|options|request"
               r"|stream|Client|AsyncClient)\s*" + re.escape(_LP)),
    re.compile(r"aiohttp\.(?:ClientSession|request)\s*" + re.escape(_LP)),
]
_BATS_NET_CMD = re.compile(
    r"^\s*(?:run\s+(?:-\S+\s+)*)?(?:curl|wget|nc|ncat|socat|gh|ssh)\b")
_DEV_TCP = re.compile(r"/dev/(?:tcp|udp)/")
_URL_LITERAL = re.compile(r"https?://([A-Za-z0-9.-]+)")
_ALLOWED_URL_HOSTS = {"127.0.0.1", "localhost"}


def _scan_network(paths):
    findings = []
    for p in paths:
        is_bats = p.suffix == ".bats"
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if is_bats:
                if _BATS_NET_CMD.search(line):
                    findings.append(f"{p.name}:{i}:net-cmd")
            else:
                for rx in _PY_NET_PATTERNS:
                    if rx.search(line):
                        findings.append(f"{p.name}:{i}:py-call-site")
                        break
            if _DEV_TCP.search(line):
                findings.append(f"{p.name}:{i}:dev-tcp")
            for m in _URL_LITERAL.finditer(line):
                host = m.group(1)
                if host not in _ALLOWED_URL_HOSTS:
                    findings.append(f"{p.name}:{i}:url:{host}")
    return findings


def test_ac011_network_gate_fires_on_seeded_violations(tmp_path):
    """RED proof: both prongs FIRE on seeded violations in a tmpdir copy —
    a gate that cannot fire proves nothing about a clean tree. Seeded
    content is concat-built so this source file stays gate-clean."""
    py = tmp_path / "seeded_test.py"
    py.write_text("import urllib.request\n"
                  "urllib.request." + "urlopen" + _LP
                  + '"http:' + '//203.0.113.9/x")' + "\n")
    bats = tmp_path / "seeded.bats"
    bats.write_text('@test "x" {\n  '
                    + "curl" + " -sf http:" + "//203.0.113.9/x\n}\n")
    f = _scan_network([py, bats])
    assert any(v.startswith("seeded_test.py:2:py-call-site") for v in f), f
    assert any(v.startswith("seeded_test.py:2:url:203.0.113.9") for v in f), f
    assert any(v.startswith("seeded.bats:2:net-cmd") for v in f), f
    assert any(v.startswith("seeded.bats:2:url:203.0.113.9") for v in f), f
    # localhost-only URLs never trip (04-01's INT-003 fixtures are
    # 127.0.0.1-only for exactly this gate)
    ok = tmp_path / "ok.bats"
    ok.write_text('base_url: http:' + '//127.0.0.1:18789\n')
    assert _scan_network([ok]) == []


def test_ac011_network_gate_real_files_clean():
    """GREEN half: the five pinned spec-007 test files carry zero enumerated
    live-network call sites and only localhost URL literals. Scope-honest
    per the module note above (residual 1a7ab992)."""
    for p in NETWORK_SCOPE:
        assert p.is_file(), p
    assert _scan_network(NETWORK_SCOPE) == []


def test_ac011_env_i_path_stub_fence_no_egress(tmp_path):
    """wall 9abbd7eb executable fence: the spec-007 executables run under
    `env -i` with every network binary stubbed to fail loud; normal
    behavior plus zero stub trips proves stubbed-PATH egress absence
    (NOT syscall-level hermeticity — documented residual)."""
    stub = tmp_path / "netstub"
    stub.mkdir()
    marker = tmp_path / "egress.log"
    for name in ("curl", "wget", "nc", "ncat", "socat", "gh", "ssh",
                 "scp", "ftp"):
        b = stub / name
        b.write_text("#!/bin/sh\n"
                     f"echo \"NETWORK-EGRESS-ATTEMPT: $0 $*\" >> '{marker}'\n"
                     "echo NETWORK-EGRESS-ATTEMPT >&2\n"
                     "exit 97\n")
        b.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    fenced_path = f"{stub}:{os.environ['PATH']}"
    base = ["env", "-i", f"PATH={fenced_path}", f"HOME={home}",
            f"TMPDIR={tmp_path}"]
    r = subprocess.run(
        [*base, "bash", str(ROOT / "scripts" / "gsd" / "env-registry.sh"),
         "check"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    data = {"gate-1": _gate(DIG_A)}
    data["_autonomy"] = _grants(["deploy:staging-web"])
    data["_promotions"] = {"run-42": [_promo("web", DIG_A, ["gate-1"],
                                             time.time())]}
    store = _write_store(tmp_path, data)
    r2 = subprocess.run(
        [*base, f"GATES_STORE={store}", "bash", str(EMIT), "run-42"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "promote" in r2.stdout
    assert not marker.exists(), marker.read_text() if marker.exists() else ""
    assert "NETWORK-EGRESS-ATTEMPT" not in r.stderr + r2.stderr


# REQ-202a families — REIMPLEMENTED here because the originals live inside
# env-registry.sh's bash-embedded python (not importable). Drift tripwire —
# cross-reference the leak-scan fixture filenames:
#   hex-run          <-> tests/fixtures/leak-scan/hex.txt
#   provider-prefix  <-> tests/fixtures/leak-scan/prefix.txt + aws.txt
#   credential-url   <-> tests/fixtures/leak-scan/url.txt
_CRED_HEX_RUN = re.compile(r"[0-9a-fA-F]{32,}")
_CRED_PROVIDER_PREFIX = re.compile(
    r"(?:AKIA[0-9A-Z]{16}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|gho_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|xox[bp]-[A-Za-z0-9-]{10,})")
_CRED_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:[^/\s:@'\"]+@")
# REQ-202a whitelist shapes — the ONLY non-path exemptions
_WHITELIST_SHAPES = [
    re.compile(r"uses:\s*[\w./-]+@[0-9a-f]{40}\b"),
    re.compile(r"sha256:[0-9a-fA-F]{64}"),
]
# residual 802972c3: pytest-generated bytecode can embed seeded credential
# literals — generated artifacts are excluded EXPLICITLY, pinned by test.
_GENERATED_DIRS = {"__pycache__", ".pytest_cache"}
_GENERATED_SUFFIXES = {".pyc", ".pyo"}


def _scan_credentials(roots, leak_scan_dir=None):
    findings = []
    for root in roots:
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in _GENERATED_DIRS for part in p.parts):
                continue
            if p.suffix in _GENERATED_SUFFIXES:
                continue
            if leak_scan_dir is not None and leak_scan_dir in p.parents:
                continue
            text = p.read_text(errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                residue = line
                for wl in _WHITELIST_SHAPES:
                    residue = wl.sub("", residue)
                for fam, rx in (("hex-run", _CRED_HEX_RUN),
                                ("provider-prefix", _CRED_PROVIDER_PREFIX),
                                ("credential-url", _CRED_URL)):
                    if rx.search(residue):
                        findings.append(
                            f"{p.relative_to(root)}:{i}:{fam}")
    return findings


def test_ac011_reimplemented_families_fire_on_their_fixtures():
    """The greppable drift tripwire: each reimplemented family must fire on
    its cross-referenced leak-scan fixture, or the reimplementation has
    drifted from env-registry.sh's embedded originals."""
    assert _CRED_HEX_RUN.search((LEAK_SCAN_DIR / "hex.txt").read_text())
    assert _CRED_PROVIDER_PREFIX.search(
        (LEAK_SCAN_DIR / "prefix.txt").read_text())
    assert _CRED_PROVIDER_PREFIX.search(
        (LEAK_SCAN_DIR / "aws.txt").read_text())
    assert _CRED_URL.search((LEAK_SCAN_DIR / "url.txt").read_text())


def test_ac011_credential_gate_fires_and_excludes_generated(tmp_path):
    """RED proof + residual 802972c3 pin: the gate fires on a seeded source
    violation while bytecode-like generated artifacts carrying the SAME
    literal are explicitly excluded (dir name AND suffix)."""
    cred = "AKIA" + "SYNTHETICNOTREAL"  # concat: keeps THIS file gate-clean
    tree = tmp_path / "tree"
    (tree / "__pycache__").mkdir(parents=True)
    (tree / "__pycache__" / "seeded.cpython-311.pyc").write_bytes(
        b"\x00\x01" + cred.encode() + b"\x02")
    (tree / "seeded.pyc").write_text(cred + "\n")
    (tree / "seeded_test.py").write_text('token = "' + cred + '"\n')
    f = _scan_credentials([tree])
    assert f == ["seeded_test.py:1:provider-prefix"], f


def test_ac011_credential_gate_whitelist_and_leak_scan_exempt(tmp_path):
    """REQ-202a whitelist shapes (uses: 40-hex pins, sha256: digests) are
    NOT findings; leak-scan fixture paths are exempt by path."""
    tree = tmp_path / "tree"
    (tree / "fixtures" / "leak-scan").mkdir(parents=True)
    (tree / "wf.yml").write_text(
        "      - uses: actions/checkout@" + "3d" * 20 + "\n")
    (tree / "digest_test.py").write_text(
        'd = "sha256:' + "ab" * 32 + '"\n')
    (tree / "fixtures" / "leak-scan" / "x.txt").write_text(
        "AKIA" + "SYNTHETICNOTREAL" + "\n")
    f = _scan_credentials([tree],
                          leak_scan_dir=tree / "fixtures" / "leak-scan")
    assert f == [], f


def test_ac011_credential_gate_real_tree_clean():
    """GREEN half: every file under tests/ and lib/tests/ recursively is
    free of the three families outside the audited exemptions. A hit here
    means FIX THE OFFENDING FILE — never widen the gate."""
    f = _scan_credentials([ROOT / "tests", ROOT / "lib" / "tests"],
                          leak_scan_dir=LEAK_SCAN_DIR)
    assert f == [], f


# wall 62482155: the leak-scan path exemption is an AUDITED allowlist —
# name + sha256 pinned per fixture. A new or changed fixture fails until
# this inventory is consciously updated in the same diff. Every fixture is
# an obviously-synthetic constant (header-pinned below), never a
# provider-issued shape with real entropy. Values carry the sha256: prefix
# (a REQ-202a whitelist shape) so this inventory is itself gate-clean.
_LEAK_SCAN_INVENTORY = {
    "assignment.secret": "sha256:21a02d00d9d81b084b6179ae8b5bdd56d2c0b86618132adbfe03436f289f1128",
    "assignment.txt": "sha256:ee37fe4c446b16ecd0e8e6e42e7ca1c1a153d93d0fa200d7f7b175651022d3de",
    "aws.secret": "sha256:57c376f01ca731355c3e031e1aa27a169fa28cbf1d5f24672ab926c92884c466",
    "aws.txt": "sha256:4b2b6435fd59f056c447b1eede97886d70692d5b5b57134274c241be2adff0f1",
    "base64.secret": "sha256:7e12e1c1571c9d70f68fb8df201bbc5350fa16473fef192766250f5048947b61",
    "base64.txt": "sha256:091d604bb820de1e006d3ed75ae18fe43f847578f697bb416f7f41d7f6005526",
    "boundary-b64-39.txt": "sha256:af255e77f01088d44f1f676162d5be3e258d846622ef29052ff62572232d0a02",
    "boundary-hex31.txt": "sha256:77fa0153567193da7ff5c69ff2f5718ff87e9ebe29fe14825a503592df42ccd2",
    "credential-kind.secret": "sha256:369c39fc95f7da04fb4e71da71313ac777ecae3ee0fa09571aeafe190e206d92",
    "credential-kind.txt": "sha256:9e8f6c7cad22b3e2dc6bd6f7892b0d25ae6d71b09b729d92b1631f85cd8829af",
    "hex.secret": "sha256:dcc2483d4edfed7d79c0b1d9d4c8a40b6ec481e1ef15c951c0a044a0b0c875e6",
    "hex.txt": "sha256:8a6826636404c63b19dfbe0d9b8c4cd706fa5b818bc1ecfc67171ec509b61eb5",
    "jwt.secret": "sha256:c7b2844f9de456c5c3febe84614f71980c9c1091a80a7585a3db443a5b6ffbff",
    "jwt.txt": "sha256:6480d1fa22484e5c54bfd8ccec9cc18f2a09ce20f055652421df988c9bbaba80",
    "leak-answers.secret": "sha256:0691e533c2b129f893f5fd0bddf33ab42dd941b5b213d546899649ed81e85f33",
    "leak-answers.yaml": "sha256:bc9265931c83936b87bb895a2e7589ab4df70198e7608622713e2d24928ee66c",
    "mixed-span.secret": "sha256:4a38888ba896cd518937c603670df3a82bda3a74dcce3ee63b92c52624b5cbdf",
    "mixed-span.txt": "sha256:af046aec5b0a5b6ce7251219e60f63887a300af60fc392d0c5e59b7f548fee52",
    "pem.secret": "sha256:051576cab47e4d780bd8162105ec658946dc952caad413af91a049107ec6cceb",
    "pem.txt": "sha256:2db8313133948722a536e79192aea03ba3a7b6ed178a93f7ec3184399d8c7025",
    "prefix.secret": "sha256:82b8f7329c00c6e114693528b9a7d88d79c7bff9e3107fb903369e6ef168f03e",
    "prefix.txt": "sha256:aabb61d0c83085867eb029abebe4f2cf1938e4cf2cf4752fcaec385c777704da",
    "secret-key.secret": "sha256:a52e3f20642f755cbf55b0390db384408da6376d45df472bdcb40e16c040552a",
    "secret-key.txt": "sha256:24f4faf885e2dd6013d1bd1a3f00e58d5dc4baf6f36495c75fdd9bd0eee1d838",
    "url.secret": "sha256:cc2cccb71aae4ece504b1484aa414f356caa8f8d87f0ef275b79d3dfe10e104b",
    "url.txt": "sha256:08d4271e4be95d05f2cdf6711cfeee74ed2007125923ec9f5817b3c01d46ff7f",
    "wf-envname.secret": "sha256:2e12356a48f10c415fa39d02c34276770dbd1bca35828645109906d71d449768",
    "whitelist-clean.txt": "sha256:5c156b50323d76d4b034cc5690119682be4dfbb6fbb467dcbb49d5fe02688e6e",
}


def test_ac011_leak_scan_fixture_inventory_audited():
    """wall 62482155: a real credential can no longer ride into the exempt
    path silently — any new/changed fixture reddens this pin until the
    inventory above is consciously updated in the same diff."""
    actual = {
        p.name: "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
        for p in LEAK_SCAN_DIR.iterdir() if p.is_file()
    }
    assert actual == _LEAK_SCAN_INVENTORY
    for p in sorted(LEAK_SCAN_DIR.iterdir()):
        if not p.is_file() or p.suffix == ".secret":
            # .secret files are raw expected-answer values (headerless by
            # design); their bytes are still sha-pinned above.
            continue
        head = p.read_text().splitlines()[0]
        assert head.startswith("#"), p.name
        assert ("SYNTHETIC-NOT-A-REAL-KEY" in head
                or "config/environments.yaml" in head), p.name


def test_seam5_ci_pytest_job_runs_env_registry_check():
    """Seam 5 (REQ-401): the ALWAYS-RUN pytest job (push AND PR) lints the
    registry via `env-registry.sh check` — never the PR-only tamper job,
    never with --probe-gh (the repo lint stays hermetic, T-04-08).
    Comment-filtered per the GATE literal — never a bare count over
    unfiltered YAML."""
    lines = [ln for ln in CI_YML.read_text().splitlines()
             if not ln.lstrip().startswith("#")]
    text = "\n".join(lines)
    pytest_i = text.index("\n  pytest:")
    tamper_i = text.index("\n  tamper:")
    assert pytest_i < tamper_i
    pytest_job = text[pytest_i:tamper_i]
    assert "env-registry.sh check" in pytest_job
    assert "--probe-gh" not in pytest_job
