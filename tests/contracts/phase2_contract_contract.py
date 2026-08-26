"""Fail-closed schema + live-identifier validator for the Phase 2 contract.

This filename intentionally avoids pytest's default test_*.py rules (same
convention as land_queue_gates_contract.py): its evidence is always the
explicitly named direct path, never a vacuous broad collection run.

The contract (.planning/.../03-PHASE2-CONTRACT.json) is the ONLY upstream
authority Plans 03-01/03-02 may consume.  Every consumable leaf must carry
immutable evidence pinned to phase2_commit — `git show COMMIT:PATH` must
contain the exact identifier at the recorded positive line with a matching
line_sha256 — AND the exact identifier must still occur in the current live
file, so later line movement cannot silently retarget the contract.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = (ROOT / ".planning" / "phases"
                 / "03-consolidate-4-a-posture-docs" / "03-PHASE2-CONTRACT.json")

TOP_KEYS = {"schema_version", "phase2_commit", "queue_contract",
            "posture_consumers", "fixture_helpers"}
QUEUE_KEYS = {"document_version", "validated_accessors", "terminal_landed",
              "tuple_fields", "clock_fields", "timeout", "post_terminal_hook",
              "controller", "config_seam"}
TUPLE_KEYS = {"branch_ref", "expected_tip_oid", "pr_number",
              "observed_merge_commit"}
CLOCK_KEYS = {"started_at", "deadline"}
CONSUMER_KEYS = {"reviewer_reachability", "no_cross_vendor_block",
                 "degradation_recording", "quarantine_requeue",
                 "production_promotion"}
# consolidate:estate grants are capped at 8h; a queue wall beyond that would
# make the timeout evidence TTL-incompatible.
MAX_TTL_COMPATIBLE_SECONDS = 8 * 3600


class ContractError(AssertionError):
    pass


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True)


def _require(cond: bool, why: str) -> None:
    if not cond:
        raise ContractError(why)


def _walk_sources(node, out, crumb="$"):
    if isinstance(node, dict):
        if "source" in node:
            out.append((crumb, node["source"]))
        for k, v in node.items():
            if k != "source":
                _walk_sources(v, out, f"{crumb}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_sources(v, out, f"{crumb}[{i}]")


def _validate_source(commit: str, crumb: str, source) -> None:
    _require(isinstance(source, dict), f"{crumb}: source is not an object")
    _require(set(source) == {"path", "identifier", "line", "line_sha256"},
             f"{crumb}: source keys must be exactly path/identifier/line/line_sha256")
    path, ident = source["path"], source["identifier"]
    line_no, line_sha = source["line"], source["line_sha256"]
    _require(isinstance(path, str) and bool(path), f"{crumb}: empty source.path")
    _require(not path.startswith("/") and not path.startswith("\\"),
             f"{crumb}: absolute source.path {path!r}")
    parts = Path(path).parts
    _require(".." not in parts, f"{crumb}: traversing source.path {path!r}")
    # no symlinked component between ROOT and the leaf
    probe = ROOT
    for part in parts:
        probe = probe / part
        _require(not probe.is_symlink(), f"{crumb}: symlinked component {probe}")
    live = ROOT / path
    _require(live.is_file(), f"{crumb}: missing live file {path}")
    _require(isinstance(ident, str) and bool(ident), f"{crumb}: empty identifier")
    _require(isinstance(line_no, int) and not isinstance(line_no, bool)
             and line_no > 0, f"{crumb}: line must be a positive int")
    blob = _git("show", f"{commit}:{path}")
    _require(blob.returncode == 0, f"{crumb}: {path} absent at {commit}")
    lines = blob.stdout.split("\n")
    _require(line_no <= len(lines), f"{crumb}: line {line_no} beyond blob")
    pinned = lines[line_no - 1]
    _require(ident in pinned,
             f"{crumb}: identifier {ident!r} not on pinned line {line_no}")
    _require(hashlib.sha256(pinned.encode()).hexdigest() == line_sha,
             f"{crumb}: line_sha256 mismatch at {path}:{line_no}")
    # live retargeting guard: the exact identifier must still exist in the
    # CURRENT file — line movement is allowed, identifier loss is not.
    _require(ident in live.read_text(),
             f"{crumb}: identifier {ident!r} no longer in live {path}")


def _nonempty(node, crumb="$"):
    if isinstance(node, str):
        _require(node.strip() != "", f"{crumb}: empty string")
    elif isinstance(node, list):
        _require(len(node) > 0, f"{crumb}: empty array")
        for i, v in enumerate(node):
            _nonempty(v, f"{crumb}[{i}]")
    elif isinstance(node, dict):
        _require(len(node) > 0, f"{crumb}: empty object")
        for k, v in node.items():
            _nonempty(v, f"{crumb}.{k}")


def validate(doc: dict, *, check_git: bool = True) -> None:
    _require(isinstance(doc, dict), "contract is not an object")
    _require(doc.get("schema_version") == 1,
             f"unknown schema_version {doc.get('schema_version')!r}")
    _require(set(doc) == TOP_KEYS,
             f"top-level keys must be exactly {sorted(TOP_KEYS)}, got {sorted(doc)}")
    qc = doc["queue_contract"]
    _require(isinstance(qc, dict) and set(qc) == QUEUE_KEYS,
             f"queue_contract keys must be exactly {sorted(QUEUE_KEYS)}")
    _require(set(qc["tuple_fields"]) == TUPLE_KEYS,
             "tuple_fields must be exactly branch_ref/expected_tip_oid/"
             "pr_number/observed_merge_commit")
    _require(set(qc["clock_fields"]) == CLOCK_KEYS,
             "clock_fields must be exactly started_at/deadline")
    pc = doc["posture_consumers"]
    _require(isinstance(pc, dict) and set(pc) == CONSUMER_KEYS,
             f"posture_consumers keys must be exactly {sorted(CONSUMER_KEYS)}")
    for key in CONSUMER_KEYS:
        _require(isinstance(pc[key], list) and bool(pc[key]),
                 f"posture_consumers.{key} must be a non-empty array")
    _require(isinstance(doc["fixture_helpers"], list) and bool(doc["fixture_helpers"]),
             "fixture_helpers must be a non-empty array")
    for helper in doc["fixture_helpers"]:
        _require(isinstance(helper.get("name"), str) and bool(helper["name"]),
                 "fixture helper without a callable/helper name")
    for acc in qc["validated_accessors"]:
        _require(isinstance(acc.get("argv"), list) and bool(acc["argv"]),
                 "accessor without a JSON argv array")
        _require(all(isinstance(a, str) and a for a in acc["argv"]),
                 "accessor argv must be non-empty strings")
    for name, entry in qc["tuple_fields"].items():
        _require(isinstance(entry.get("field"), str) and bool(entry["field"]),
                 f"tuple_fields.{name} must name its exact serialized field")
    timeout = qc["timeout"]
    seconds = timeout.get("seconds")
    _require(isinstance(seconds, (int, float)) and not isinstance(seconds, bool),
             "timeout.seconds must be numeric")
    _require(seconds > 0, "timeout.seconds must be positive")
    _require(seconds <= MAX_TTL_COMPATIBLE_SECONDS,
             f"timeout.seconds {seconds} is TTL-incompatible (> 8h grant cap)")
    _nonempty(doc)
    commit = doc["phase2_commit"]
    _require(isinstance(commit, str) and len(commit) == 40
             and all(c in "0123456789abcdef" for c in commit),
             "phase2_commit must be a full 40-hex sha")
    if check_git:
        _require(_git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0,
                 f"phase2_commit {commit} is not a commit in this repository")
        _require(_git("merge-base", "--is-ancestor", commit, "HEAD").returncode == 0,
                 f"phase2_commit {commit} is not an ancestor of HEAD")
        sources: list = []
        _walk_sources(doc, sources)
        _require(bool(sources), "contract carries no source evidence at all")
        for crumb, source in sources:
            _validate_source(commit, crumb, source)


def _load() -> dict:
    if not CONTRACT_PATH.is_file():
        pytest.fail(f"03-PHASE2-CONTRACT.json is absent at {CONTRACT_PATH} "
                    "(Task 1 must pin the landed Phase 2 interfaces first)")
    try:
        return json.loads(CONTRACT_PATH.read_text())
    except json.JSONDecodeError as exc:
        pytest.fail(f"03-PHASE2-CONTRACT.json is not strict JSON: {exc}")


def test_contract_validates_end_to_end() -> None:
    validate(_load())


def test_markdown_audit_companion_is_present_and_nonempty() -> None:
    md = CONTRACT_PATH.with_suffix(".md")
    assert md.is_file() and md.stat().st_size > 0, f"missing audit {md}"
    text = md.read_text()
    assert "03-PHASE2-CONTRACT.json" in text, "audit must name the JSON contract"


# ── non-vacuity: every rejection class must actually reject ───────────────

def _mutations(doc: dict):
    m1 = copy.deepcopy(doc); m1["schema_version"] = 2
    yield "unknown schema_version", m1
    m2 = copy.deepcopy(doc); del m2["queue_contract"]["timeout"]
    yield "missing required category", m2
    m3 = copy.deepcopy(doc); m3["queue_contract"]["extra"] = {"x": "y"}
    yield "extra category", m3
    m4 = copy.deepcopy(doc)
    m4["posture_consumers"]["reviewer_reachability"] = []
    yield "empty consumer array", m4
    m5 = copy.deepcopy(doc)
    m5["queue_contract"]["timeout"]["seconds"] = "28800"
    yield "nonnumeric timeout", m5
    m6 = copy.deepcopy(doc)
    m6["queue_contract"]["timeout"]["seconds"] = -1
    yield "nonpositive timeout", m6
    m7 = copy.deepcopy(doc)
    m7["queue_contract"]["timeout"]["seconds"] = 9 * 3600
    yield "TTL-incompatible timeout", m7
    m8 = copy.deepcopy(doc)
    m8["queue_contract"]["controller"]["source"]["path"] = "/etc/passwd"
    yield "absolute source path", m8
    m9 = copy.deepcopy(doc)
    m9["queue_contract"]["controller"]["source"]["path"] = "../outside.sh"
    yield "traversing source path", m9
    m10 = copy.deepcopy(doc)
    m10["queue_contract"]["controller"]["source"]["path"] = "scripts/gsd/no-such-file.sh"
    yield "missing source path", m10
    m11 = copy.deepcopy(doc)
    m11["queue_contract"]["controller"]["source"]["line_sha256"] = "0" * 64
    yield "line hash mismatch", m11
    m12 = copy.deepcopy(doc)
    m12["queue_contract"]["controller"]["source"]["line"] = 1
    yield "identifier missing from pinned line", m12
    m13 = copy.deepcopy(doc)
    m13["queue_contract"]["controller"]["source"]["line"] = 0
    yield "nonpositive line", m13
    m14 = copy.deepcopy(doc)
    m14["queue_contract"]["document_version"]["value"] = ""
    yield "empty string leaf", m14
    m15 = copy.deepcopy(doc)
    m15["phase2_commit"] = "f" * 40
    yield "unknown phase2_commit", m15


def test_validator_rejects_every_named_defect_class() -> None:
    doc = _load()
    for label, mutated in _mutations(doc):
        with pytest.raises(ContractError):
            validate(mutated)
        del label


def test_validator_is_non_vacuous_on_the_real_contract() -> None:
    # the real contract must carry at least one source per consumer category
    doc = _load()
    sources: list = []
    _walk_sources(doc["posture_consumers"], sources)
    assert len(sources) >= len(CONSUMER_KEYS), sources
