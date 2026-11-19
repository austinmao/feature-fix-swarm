"""Tests for lib/cloud_scan.py. Canaries are assembled at runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import cloud_scan as cs  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(repo: Path, *args: str, check: bool = True) -> str:
    env = dict(os.environ)
    env.update(GIT_ENV)
    proc = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )
    return proc.stdout.strip()


def commit(repo: Path, files: dict, message: str = "c") -> str:
    for name, data in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if data is None:
            git(repo, "rm", "-q", "-f", name)
            continue
        target.write_bytes(data if isinstance(data, bytes) else data.encode())
        git(repo, "add", "--", name)
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "r"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


# ------------------------------------------------------------------ patterns


@pytest.mark.parametrize("cls", [c for c in cs.pattern_classes() if c != cs.CLS_FILENAME])
def test_each_class_detected_without_value(cls):
    sample = cs.canary(cls)
    found = cs.scan_bytes(b"line1\nvalue " + sample + b" end\n", label="f")
    assert cls in {f.cls for f in found}
    for finding in found:
        assert sample.decode() not in finding.location
        assert finding.location.startswith("f:L2")


def test_self_test_passes_and_covers_every_class():
    cs.self_test()
    assert set(cs.pattern_classes()) >= {"pem-block", "jwt", "doppler-token", "secret-filename"}


def test_self_test_failure_refuses(monkeypatch):
    monkeypatch.setattr(cs, "_PATTERNS", tuple(p for p in cs._PATTERNS if p[0] != "jwt"))
    with pytest.raises(cs.ScanRefused):
        cs.scan_bytes(b"hello", label="x")
    with pytest.raises(cs.ScanRefused):
        cs.redact(b"hello")


def test_internal_exception_refuses(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cs, "_scan_raw", boom)
    with pytest.raises(cs.ScanRefused):
        cs.scan_bytes(b"hello", label="x")


@pytest.mark.parametrize(
    "text",
    [
        "password = os.environ['X']",
        'api_key = "${API_KEY}"',
        "token = https://example.invalid/path/xyz",
        'secret = "aaaaaaaaaaaaaaaa"',
        "TOKEN=short",
        "uses: actions/checkout@v4",
        "just prose about a secret handshake",
    ],
)
def test_benign_text_is_clean(text):
    assert cs.scan_bytes(text.encode(), label="f") == []


def test_assignment_with_literal_detected():
    line = ("pass" + "word") + ' = "' + "Zx9Qw8Er7Ty6Ui5" + '"\n'
    assert [f.cls for f in cs.scan_bytes(line.encode(), label="f")] == ["secret-assignment"]


def test_text_fields_nested_and_keys():
    secret = cs.canary("aws-access-key").decode()
    found = cs.scan_text_fields({"a": ["ok", {"b": secret}], secret: "x"})
    assert len(found) == 2
    assert all(secret not in f.location for f in found)


def test_text_fields_depth_limit_refuses():
    deep: object = "x"
    for _ in range(cs.MAX_TEXT_DEPTH + 5):
        deep = [deep]
    with pytest.raises(cs.ScanRefused):
        cs.scan_text_fields(deep)


def test_redact_replaces_with_class_marker():
    token = cs.canary("github-token")
    out = cs.redact(b"a " + token + b" b " + cs.canary("jwt") + b" c")
    assert out == b"a [REDACTED:github-token] b [REDACTED:jwt] c"
    assert cs.redact(b"nothing") == b"nothing"


def test_redact_overlap_one_class_per_span():
    out = cs.redact(b"x " + cs.canary("sk-ant") + b" y")
    assert out == b"x [REDACTED:sk-ant] y"


# ---------------------------------------------------------------- git scans


def test_git_objects_clean_range(repo):
    base = commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {"b.txt": "two\n"}, "add b")
    assert cs.scan_git_objects(str(repo), base, tip) == []


def test_git_objects_finds_secret_in_early_commit_later_removed(repo):
    base = commit(repo, {"a.txt": "one\n"})
    commit(repo, {"leak.txt": b"k=" + cs.canary("github-token")}, "leak")
    tip = commit(repo, {"leak.txt": None}, "remove")
    found = cs.scan_git_objects(str(repo), base, tip)
    assert [f.cls for f in found] == ["github-token"]
    assert "leak.txt" in found[0].location


def test_git_objects_secret_in_commit_message(repo):
    base = commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {"b.txt": "two\n"}, "msg " + cs.canary("aws-access-key").decode())
    assert [f.cls for f in cs.scan_git_objects(str(repo), base, tip)] == ["aws-access-key"]


def test_git_objects_secret_in_pathname_not_echoed(repo):
    base = commit(repo, {"a.txt": "one\n"})
    name = "dir/" + cs.canary("github-token").decode() + ".txt"
    tip = commit(repo, {name: "x\n"})
    found = cs.scan_git_objects(str(repo), base, tip)
    assert found and all(cs.canary("github-token").decode() not in f.location for f in found)


def test_git_objects_secret_filename(repo):
    base = commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {"sub/.env.local": "A=1\n", "k/id_rsa": "x\n", "c.pem": "x\n"})
    found = cs.scan_git_objects(str(repo), base, tip)
    assert {f.cls for f in found} == {cs.CLS_FILENAME}
    assert len(found) >= 3


def test_git_objects_secret_only_in_merge_commit(repo):
    commit(repo, {"a.txt": "one\n"})
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"s.txt": "side\n"}, "side")
    git(repo, "checkout", "-q", "main")
    base = commit(repo, {"m.txt": "main\n"}, "main")
    git(repo, "merge", "--no-commit", "--no-ff", "side", check=False)
    (repo / "merged-only.txt").write_bytes(b"t=" + cs.canary("slack-token"))
    git(repo, "add", "merged-only.txt")
    git(repo, "commit", "-q", "-m", "merge")
    tip = git(repo, "rev-parse", "HEAD")
    found = cs.scan_git_objects(str(repo), base, tip)
    assert [f.cls for f in found] == ["slack-token"]


def test_binary_blob_flagged_only_when_outbound(repo):
    base = commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {"img.bin": b"\x00\x01\x02binary"})
    assert cs.scan_git_objects(str(repo), base, tip) == []
    out = cs.scan_git_objects(str(repo), base, tip, outbound=True)
    assert [f.cls for f in out] == [cs.CLS_BINARY]


def test_secret_in_binary_blob_still_found(repo):
    base = commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {"img.bin": b"\x00\x01" + cs.canary("github-token")})
    assert "github-token" in {f.cls for f in cs.scan_git_objects(str(repo), base, tip)}


def test_unrelated_base_scans_everything(repo):
    tip = commit(repo, {"a.txt": b"k=" + cs.canary("jwt")})
    assert [f.cls for f in cs.scan_git_objects(str(repo), None, tip)] == ["jwt"]


def test_bad_oid_and_git_failure_refuse(repo):
    commit(repo, {"a.txt": "x\n"})
    with pytest.raises(cs.ScanRefused):
        cs.scan_git_objects(str(repo), None, "not-an-oid")
    with pytest.raises(cs.ScanRefused):
        cs.scan_git_objects(str(repo), None, "0" * 40)
    with pytest.raises(cs.ScanRefused):
        cs.scan_git_objects(str(repo / "nope"), None, "1" * 40)


def test_missing_object_refuses_with_partial_findings(tmp_path, repo):
    base = commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {"b.txt": "two\n"})
    blob = git(repo, "rev-parse", tip + ":b.txt")
    loose = repo / ".git" / "objects" / blob[:2] / blob[2:]
    loose.chmod(0o644)
    loose.unlink()
    with pytest.raises(cs.ScanRefused) as info:
        cs.scan_git_objects(str(repo), base, tip)
    assert info.value.reason == "missing-object"


# ----------------------------------------------------------------- ancestry


def test_ancestry_clean_then_cached(repo, tmp_path):
    commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {"b.txt": "two\n"})
    cache = tmp_path / "state" / "scan-cache.json"
    assert cs.scan_git_ancestry(str(repo), tip, str(cache)) == []
    data = json.loads(cache.read_text())
    assert len(data) == 2 and all(v["clean"] and v["findings"] == 0 for v in data.values())
    assert cs.scan_git_ancestry(str(repo), tip, str(cache)) == []


def test_ancestry_version_1_cache_entries_are_ignored(repo, tmp_path):
    tip = commit(repo, {"a.txt": b"k=" + cs.canary("github-token")})
    cache = tmp_path / "c.json"
    assert cs.scan_git_ancestry(str(repo), tip, str(cache))
    forged = {oid: {"clean": True, "findings": 0, "v": 1, "allow": cs._allow_signature([])} for oid in
              [tip] + list(json.loads(cache.read_text()))}
    cache.write_text(json.dumps(forged))  # a signed v1 entry, as the previous code could have written
    assert cs.scan_git_ancestry(str(repo), tip, str(cache))
    assert cs.SCANNER_VERSION > 1


def test_ancestry_cache_hit_skips_rescanning(repo, tmp_path, monkeypatch):
    tip = commit(repo, {"a.txt": "one\n"})
    cache = tmp_path / "c.json"
    cs.scan_git_ancestry(str(repo), tip, str(cache))
    monkeypatch.setattr(cs, "_scan_objects", lambda *a, **k: pytest.fail("rescanned"))
    assert cs.scan_git_ancestry(str(repo), tip, str(cache)) == []


def test_ancestry_incremental_scans_only_new_commits(repo, tmp_path):
    first = commit(repo, {"a.txt": "one\n"})
    cache = tmp_path / "c.json"
    cs.scan_git_ancestry(str(repo), first, str(cache))
    tip = commit(repo, {"b.txt": b"k=" + cs.canary("github-token")})
    found = cs.scan_git_ancestry(str(repo), tip, str(cache))
    assert [f.cls for f in found] == ["github-token"]
    data = json.loads(cache.read_text())
    assert data[tip]["clean"] is False and data[first]["clean"] is True


def test_ancestry_dirty_cached_stays_dirty(repo, tmp_path):
    tip = commit(repo, {"a.txt": b"k=" + cs.canary("jwt")})
    cache = tmp_path / "c.json"
    assert cs.scan_git_ancestry(str(repo), tip, str(cache))
    assert cs.scan_git_ancestry(str(repo), tip, str(cache))


def test_ancestry_allow_entry_suppresses_finding(repo, tmp_path):
    tip = commit(repo, {"a.txt": b"k=" + cs.canary("github-token")})
    blob = git(repo, "rev-parse", tip + ":a.txt")
    cache = tmp_path / "c.json"
    allow = [{"blob": blob, "class": "github-token"}]
    assert cs.scan_git_ancestry(str(repo), tip, str(cache), allow) == []
    # a different allow list must not reuse the allowed-clean cache entry
    assert cs.scan_git_ancestry(str(repo), tip, str(cache), [])


def test_ancestry_clean_descendant_does_not_hide_a_removed_exception(repo, tmp_path):
    """A suppressed hit in A, then a clean child B scanned under the same exception."""
    first = commit(repo, {"fixtures/k.txt": b"t=" + cs.canary("github-token")})
    allow = [{"path": "fixtures/k.txt", "class": "github-token"}]
    cache = tmp_path / "c.json"
    assert cs.scan_git_ancestry(str(repo), first, str(cache), allow) == []
    tip = commit(repo, {"b.txt": "clean\n"})
    assert cs.scan_git_ancestry(str(repo), tip, str(cache), allow) == []
    assert cs.scan_git_ancestry(str(repo), tip, str(cache), [])  # exception removed: A must be rescanned
    assert cs.scan_git_ancestry(str(repo), tip, str(cache), allow) == []


def test_ancestry_cache_entry_without_signature_is_ignored(repo, tmp_path):
    tip = commit(repo, {"a.txt": "one\n"})
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({tip: {"clean": True, "findings": 0, "v": cs.SCANNER_VERSION}}))
    called = []
    real = cs._scan_objects
    cs._scan_objects = lambda *a, **k: called.append(1) or real(*a, **k)
    try:
        assert cs.scan_git_ancestry(str(repo), tip, str(cache)) == []
    finally:
        cs._scan_objects = real
    assert called


def test_ancestry_allow_wrong_class_does_not_suppress(repo, tmp_path):
    tip = commit(repo, {"a.txt": b"k=" + cs.canary("github-token")})
    blob = git(repo, "rev-parse", tip + ":a.txt")
    allow = [{"blob": blob, "class": "jwt"}]
    assert cs.scan_git_ancestry(str(repo), tip, str(tmp_path / "c.json"), allow)


def test_ancestry_flags_binary_blob(repo, tmp_path):
    tip = commit(repo, {"x.bin": b"\x00\x01\x02"})
    found = cs.scan_git_ancestry(str(repo), tip, str(tmp_path / "c.json"))
    assert [f.cls for f in found] == [cs.CLS_BINARY]


def test_ancestry_corrupt_cache_is_ignored(repo, tmp_path):
    tip = commit(repo, {"a.txt": "one\n"})
    cache = tmp_path / "c.json"
    cache.write_text("{not json")
    assert cs.scan_git_ancestry(str(repo), tip, str(cache)) == []
    assert json.loads(cache.read_text())[tip]["clean"] is True


# ------------------------------------------------------------ C1: performance


def adversarial_inputs(size: int):
    token = b"ghp_" + b"A" * 20 + b" "
    return {
        "pem-block": b"-----BEGIN A-----\n" * (size // 18),
        "pem-unterminated": b"-----BEGIN RSA PRIVATE KEY-----\n" + b"A" * size,
        "sk-ant": b"sk-ant-AAAAAAAA " * (size // 16),
        "sk-proj": b"sk-proj-AAAAAAAA " * (size // 17),
        "sk-key": b"sk-" + b"A1" * (size // 2),
        "aws-access-key": (b"AKIA" + b"ABCDEFGHIJKLMNOP ") * (size // 21),
        "github-pat": b"github_pat_AAAAAAAAAAAAAAAAAAAA " * (size // 32),
        "github-token": token * (size // len(token)),
        "slack-token": b"xoxb-12345678 " * (size // 14),
        "doppler-token": b"dp.st.AAAAAAAAAAAAAAAAAAAA " * (size // 28),
        "jwt": b"eyJaaaa." * (size // 8),
        "jwt-dash-run": b"eyJ-" * (size // 4),  # no dots: a start inside every base64url run backtracked quadratically
        "credential-url": b"ab://c:d " * (size // 9),
        "credential-url-long": b"a://a:" + b"b" * size,
        "credential-url-bare": b"https://t@h " * (size // 12),
        "secret-assignment": (b"token" + b"a" * 40 + b"=") * (size // 46),
        "secret-assignment-value": b"token=" + b"a1" * (size // 2),
        "one-long-line": b"x" * size,
        "only-newlines": b"\n" * size,
    }


@pytest.mark.parametrize("name", sorted(adversarial_inputs(100)))
def test_two_megabyte_adversarial_input_scans_and_redacts_fast(name):
    data = adversarial_inputs(2 * 1024 * 1024)[name]
    started = time.time()
    findings = cs.scan_bytes(data, label="x")
    cs.redact(data)
    assert time.time() - started < 5.0, name
    assert isinstance(findings, list)


def test_many_findings_stay_linear_on_the_pathological_jwt_run():
    data = b"eyJaaaa." * 200_000  # 1.6 MB: took 59 s with the pairwise overlap check
    started = time.time()
    assert len(cs.scan_bytes(data, label="x")) > 1000
    assert time.time() - started < 5.0


def test_budget_exhaustion_refuses(monkeypatch):
    monkeypatch.setattr(cs, "SCAN_BUDGET_S", -1.0)
    for call in (lambda: cs.scan_bytes(b"x" * 10, label="f"), lambda: cs.redact(b"x"), lambda: cs.scan_text_fields({"a": "b"})):
        with pytest.raises(cs.ScanRefused) as info:
            call()
        assert info.value.reason == "budget"


def test_total_budget_covers_a_whole_public_scan(monkeypatch):
    monkeypatch.setattr(cs, "TOTAL_BUDGET_S", -1.0)
    with pytest.raises(cs.ScanRefused) as info:
        cs.scan_bytes(b"hello", label="f")
    assert info.value.reason == "budget"
    monkeypatch.setattr(cs, "TOTAL_BUDGET_S", 900.0)
    assert cs.scan_bytes(b"hello", label="f") == []  # the deadline does not leak between scans


def test_very_long_lines_are_scanned_in_overlapping_windows(monkeypatch):
    monkeypatch.setattr(cs, "WINDOW", 1024)
    monkeypatch.setattr(cs, "WINDOW_OVERLAP", 128)
    token = cs.canary("github-token")
    for offset in (0, 1000, 1024 - 128, 1024 - 3, 1900, 2048 - 128 - 5, 4000):
        line = b"." * offset + b" " + token + b" " + b"." * 3000  # no newline anywhere
        found = cs.scan_bytes(line, label="f")
        assert [f.cls for f in found] == ["github-token"], offset
        assert cs.redact(line).count(b"[REDACTED:github-token]") == 1


def test_windows_do_not_split_findings_across_lines(monkeypatch):
    monkeypatch.setattr(cs, "WINDOW", 256)
    token = cs.canary("aws-access-key")
    text = b"".join(b"line %d %s\n" % (i, token if i % 7 == 0 else b"ok") for i in range(300))
    found = cs.scan_bytes(text, label="f")
    assert len(found) == len([i for i in range(300) if i % 7 == 0])
    assert found[1].location == "f:L8"


# ------------------------------------------------------------ C2: PEM redaction


def pem(body: str = "MIIEBODYLINE1\nMIIEBODYLINE2\n", label: str = "RSA PRIVATE KEY") -> bytes:
    return ("-----BEGIN " + label + "-----\n" + body + "-----END " + label + "-----").encode()


def test_redact_removes_the_whole_pem_block():
    out = cs.redact(b"before\n" + pem() + b"\nafter\n")
    assert out == b"before\n[REDACTED:pem-block]\nafter\n"


def test_redact_pem_without_end_goes_to_end_of_input():
    data = b"keep\n-----BEGIN " + b"PRIVATE KEY-----\nMIIEBODYLINE1\nghp_" + b"A" * 20 + b"\nmore\n"
    out = cs.redact(data)
    assert out == b"keep\n[REDACTED:pem-block]"
    assert b"MIIE" not in out and b"more" not in out


def test_redact_two_pem_blocks_and_inner_secret_not_double_marked():
    inner = cs.canary("github-token").decode()
    data = pem(body=inner + "\nMIIE\n") + b" mid " + pem(label="EC PRIVATE KEY")
    assert cs.redact(data) == b"[REDACTED:pem-block] mid [REDACTED:pem-block]"
    assert len(cs.scan_bytes(data, label="k")) == 2


def test_pem_scan_reports_one_finding_per_block_at_its_begin_line():
    found = cs.scan_bytes(b"a\nb\n" + pem(), label="k")
    assert [(f.cls, f.location) for f in found] == [("pem-block", "k:L3")]


# ------------------------------------------------------ C3: decoded JSON, prompts


def escaped_secret(cls: str = "sk-key") -> str:
    secret = cs.canary(cls).decode()
    return "\\u%04x" % ord(secret[0]) + secret[1:]


def test_scan_json_decoded_finds_json_escaped_secrets():
    secret = cs.canary("sk-key").decode()
    raw = ('{"summary": "token is %s ok", "n": 1}' % escaped_secret()).encode()
    assert cs.scan_bytes(raw, label="m") == []  # the raw scan is blind to the escape
    found = cs.scan_json_decoded(raw)
    assert [f.cls for f in found] == ["sk-key"]
    assert all(secret not in f.location for f in found)


def test_scan_json_decoded_covers_keys_nested_json_and_duplicate_keys():
    secret = cs.canary("aws-access-key").decode()
    esc = "\\u0041" + secret[1:]
    nested = '{\\"deep\\": \\"%s\\"}' % esc.replace("\\", "\\\\")
    raw = ('{"%s": 1, "a": "x", "a": "%s", "n": "%s"}' % (esc, esc, nested)).encode()
    found = cs.scan_json_decoded(raw)
    assert len(found) >= 3 and {f.cls for f in found} == {"aws-access-key"}


def test_scan_json_decoded_refuses_invalid_json_without_echoing_it():
    secret = cs.canary("github-token").decode()
    with pytest.raises(cs.ScanRefused) as info:
        cs.scan_json_decoded(("{broken " + secret).encode())
    assert info.value.reason == "bad-json" and secret not in str(info.value) + repr(info.value.findings)
    assert [f.cls for f in info.value.findings] == ["github-token"]


def test_scan_text_fields_scans_decoded_strings_and_accepts_json_text():
    raw = '{"summary": "%s"}' % escaped_secret()
    assert [f.cls for f in cs.scan_text_fields(raw)] == ["sk-key"]
    assert [f.cls for f in cs.scan_text_fields(raw.encode())] == ["sk-key"]
    assert [f.cls for f in cs.scan_text_fields(json.loads(raw))] == ["sk-key"]


def test_scan_prompt_finds_secrets_and_escaped_secrets():
    secret = cs.canary("github-token").decode()
    assert cs.scan_prompt("review this\n" + secret + "\n") != []
    esc = "\\u0067hp_" + secret[4:]
    assert [f.cls for f in cs.scan_prompt("note: " + esc)] == ["github-token"]
    assert cs.scan_prompt("a clean prompt about \\u00e9 and \\x41") == []
    assert cs.scan_prompt(b"bytes prompt " + secret.encode())[0].location.startswith("prompt:L1")


# ------------------------------------------------------ C4: filename rule and allow


@pytest.mark.parametrize("name", [".env.example", ".env.sample", ".env.template", ".env.dist", "sub/.ENV.EXAMPLE"])
def test_template_env_names_are_allowed(repo, name):
    base = commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {name: "API_URL=http://localhost\n"})
    assert cs.scan_git_objects(str(repo), base, tip) == []


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production", ".envrc", "id_rsa", "x/server.pem"])
def test_real_secret_filenames_still_flagged(repo, name):
    base = commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {name: "A=1\n"})
    assert {f.cls for f in cs.scan_git_objects(str(repo), base, tip)} == {cs.CLS_FILENAME}


def test_content_of_template_env_file_is_still_scanned(repo):
    base = commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {".env.example": b"KEY=" + cs.canary("github-token")})
    found = cs.scan_git_objects(str(repo), base, tip)
    assert [f.cls for f in found] == ["github-token"] and ".env.example" in found[0].location


def test_allow_by_path_and_class_suppresses_a_filename_finding(repo, tmp_path):
    tip = commit(repo, {"sub/.env.local": "A=1\n", "a.txt": "x\n"})
    cache = tmp_path / "c.json"
    assert cs.scan_git_ancestry(str(repo), tip, str(cache))
    allow = [{"path": "sub/.env.local", "class": "secret-filename"}]
    assert cs.scan_git_ancestry(str(repo), tip, str(tmp_path / "c2.json"), allow) == []
    # the allow list is part of the cache identity: a cache built with it must not hide the finding without it
    assert cs.scan_git_ancestry(str(repo), tip, str(tmp_path / "c2.json"), []) != []


def test_allow_by_path_is_exact_and_class_specific(repo, tmp_path):
    tip = commit(repo, {"sub/.env.local": "A=1\n", "other/.env.local": "B=2\n"})
    only_sub = [{"path": "sub/.env.local", "class": "secret-filename"}]
    left = cs.scan_git_ancestry(str(repo), tip, str(tmp_path / "a.json"), only_sub)
    assert left and all("sub/.env.local" not in f.location for f in left)
    assert any("other/.env.local" in f.location for f in left)
    wrong_class = [{"path": "sub/.env.local", "class": "jwt"}, {"path": "sub/other", "class": "secret-filename"}]
    assert len(cs.scan_git_ancestry(str(repo), tip, str(tmp_path / "b.json"), wrong_class)) == len(
        cs.scan_git_ancestry(str(repo), tip, str(tmp_path / "c.json")))


def test_allow_by_path_also_covers_content_findings_of_that_class(repo, tmp_path):
    tip = commit(repo, {"fixtures/k.txt": b"t=" + cs.canary("github-token")})
    assert cs.scan_git_ancestry(str(repo), tip, str(tmp_path / "a.json"))
    allow = [{"path": "fixtures/k.txt", "class": "github-token"}]
    assert cs.scan_git_ancestry(str(repo), tip, str(tmp_path / "b.json"), allow) == []


def test_allow_entry_without_blob_or_path_matches_nothing(repo, tmp_path):
    tip = commit(repo, {".env": "A=1\n"})
    assert cs.scan_git_ancestry(str(repo), tip, str(tmp_path / "a.json"), [{"class": "secret-filename"}])


# ------------------------------------------------------------ C5: credential URLs


@pytest.mark.parametrize("url", [
    "https://user" + ":pass@host.invalid/repo.git",
    "https://abcDEF123xyz@github.com/o/r.git",
    "http://token123@host.invalid/x",
    "git+https://token123@host.invalid/x.git",
    "HTTPS://Token@Host.invalid/x",
    "postgres://app" + ":s3cret@db.invalid/app",
])
def test_credential_urls_detected_and_redacted(url):
    found = cs.scan_bytes(("remote = %s\n" % url).encode(), label="f")
    assert [f.cls for f in found] == ["credential-url"]
    assert url.split("@")[0].rsplit("//", 1)[1] not in cs.redact(url.encode()).decode()


@pytest.mark.parametrize("text", [
    "https://github.com/o/r.git", "ssh://git@github.com/o/r.git", "git@github.com:o/r.git",
    "https://registry.npmjs.org/@scope/pkg", "see https://example.invalid/a@b", "mail me at a@b.invalid",
])
def test_urls_without_userinfo_are_clean(text):
    assert cs.scan_bytes(text.encode(), label="f") == []


@pytest.mark.parametrize("url,expected", [
    ("https://user" + ":pass@host/x", True), ("https://token@host/x", True), ("ssh://git@host/o/r", True),
    ("https://host/a@b", False), ("https://host?x=a@b", False), ("https://host#a@b", False),
    ("https://good.invalid\\@evil.invalid/", True), ("git@github.com:o/r.git", False), ("", False),
    ("https://host:8080/x", False),
])
def test_url_has_userinfo(url, expected):
    assert cs.url_has_userinfo(url) is expected
    assert cs.url_has_userinfo(None) is False  # type: ignore[arg-type]


# ------------------------------------------------------------ C6: no values anywhere


def nest(value, depth):
    for _ in range(depth):
        value = [value]
    return value


def test_no_matched_value_reaches_findings_exceptions_or_redacted_output(repo, monkeypatch):
    classes = [c for c in cs.pattern_classes() if c != cs.CLS_FILENAME]
    canaries = {cls: cs.canary(cls) for cls in classes}
    blob = b"\n".join(b"k%d = %s" % (i, c) for i, c in enumerate(canaries.values())) + b"\n"
    surfaces = []
    found = cs.scan_bytes(blob, label="data")
    surfaces += [repr(f) + f.location for f in found]
    surfaces.append(cs.redact(blob).decode())
    surfaces += [repr(f) for f in cs.scan_text_fields({c.decode(): c.decode() for c in canaries.values()})]
    surfaces += [repr(f) for f in cs.scan_prompt(blob)]
    base = commit(repo, {"a.txt": "one\n"})
    tip = commit(repo, {"dir/" + cs.canary("github-token").decode() + "/.env": blob})
    surfaces += [repr(f) for f in cs.scan_git_objects(str(repo), base, tip)]
    for call in (
        lambda: cs.scan_json_decoded(b"{bad " + blob),
        lambda: cs.scan_text_fields(nest(blob.decode(), cs.MAX_TEXT_DEPTH + 5)),
        lambda: cs.scan_git_objects(str(repo), None, "0" * 40),
    ):
        with pytest.raises(cs.ScanRefused) as info:
            call()
        surfaces += [str(info.value), repr(info.value), repr(info.value.findings)]
    monkeypatch.setattr(cs, "SCAN_BUDGET_S", -1.0)
    with pytest.raises(cs.ScanRefused) as info:
        cs.scan_bytes(blob, label="x")
    surfaces += [str(info.value), repr(info.value)]
    joined = "\n".join(surfaces)
    for cls, raw in canaries.items():
        needle = raw.decode()
        assert needle not in joined, cls
        if cls == "secret-assignment":  # the value, not the key name, is the secret
            assert "A1b2C3d4E5f6G7" not in joined
