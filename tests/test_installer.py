from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from lib import ffs_installer

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"
INSTALLER = ROOT / "lib" / "ffs_installer.py"


def run_setup(
    tmp_path: Path,
    *args: str,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "FFS_SKIP_PROMPT_MASTER": "1",
            "FFS_GSD_INSTALLER": str(ROOT / "tests/fixtures/gsd-installer-stub.py"),
            "FFS_GSD_STUB_LOG": str(tmp_path / "gsd-installer.log"),
            # spec-004 AC-009: doctor's model-resolvability check shells out to
            # model-probe-lib.sh's cached probe, forcing past the TTL cache.
            # Stub both vendor commands so the baseline test suite never
            # depends on (or bills) a real claude/codex CLI that may happen to
            # be installed on the machine running these tests — deterministic
            # "always available" unless a test overrides these to prove the
            # warn path.
            "GSD_MODEL_PROBE_CMD": "true",
            "GSD_MODEL_PROBE_CMD_CODEX": "true",
        }
    )
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SETUP), *args],
        cwd=cwd or ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_project_install_uses_portable_relative_links_and_never_codex(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 0, result.stderr
    shipped = sorted(path.name for path in (ROOT / "skills").iterdir() if path.is_dir())
    assert "continue-compact" in shipped
    for name in shipped:
        for host in (".agents", ".claude"):
            link = project / host / "skills" / name
            assert link.is_symlink()
            target = os.readlink(link)
            assert not os.path.isabs(target)
            assert link.resolve() == project / f".feature-fix-swarm/vendor/skills/{name}"
    assert not (project / ".codex/skills").exists()
    manifest = json.loads((project / ".feature-fix-swarm/install-manifest.json").read_text())
    assert manifest["schema"] == "ffs.install/v1"
    assert manifest["scope"] == "project"


def test_same_release_project_reinstall_preserves_manifest_bytes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    first = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))
    manifest_path = project / ".feature-fix-swarm/install-manifest.json"
    before = manifest_path.read_bytes()

    second = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert manifest_path.read_bytes() == before


def test_project_manifest_source_is_relative_when_vendored(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "packages/feature-fix-swarm"
    assert ffs_installer.manifest_source(source, "project", project) == "packages/feature-fix-swarm"
    assert ffs_installer.manifest_source(source, "user", None) == str(source)


def test_backup_payloads_are_private_even_with_permissive_source_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "config"
    source.mkdir(mode=0o755)
    secret = source / "auth.json"
    secret.write_text('{"token":"redacted"}\n')
    nested = source / "private"
    nested.mkdir(mode=0o711)
    nested_file = nested / "token"
    nested_file.write_text("redacted\n")
    source.chmod(0o755)
    secret.chmod(0o644)
    nested.chmod(0o711)
    nested_file.chmod(0o640)

    backup = ffs_installer.Backup("test-private", "user")
    backup.before(source)
    backup.finish()

    assert stat.S_IMODE((home / ".cache/feature-fix-swarm").stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((backup.directory / "objects").stat().st_mode) == 0o700
    copied_root = backup.directory / "objects/0000"
    assert stat.S_IMODE(copied_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((copied_root / "auth.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((copied_root / "private").stat().st_mode) == 0o700
    assert stat.S_IMODE((copied_root / "private/token").stat().st_mode) == 0o600
    assert stat.S_IMODE((backup.directory / "manifest.json").stat().st_mode) == 0o600

    assert ffs_installer.rollback(backup.backup_id) == 0
    assert stat.S_IMODE(source.stat().st_mode) == 0o755
    assert stat.S_IMODE(secret.stat().st_mode) == 0o644
    assert stat.S_IMODE(nested.stat().st_mode) == 0o711
    assert stat.S_IMODE(nested_file.stat().st_mode) == 0o640


def test_user_install_copies_identical_hash_managed_skills(tmp_path: Path) -> None:
    result = run_setup(tmp_path, "--scope", "user")

    assert result.returncode == 0, result.stderr
    home = tmp_path / "home"
    shipped = sorted(path.name for path in (ROOT / "skills").iterdir() if path.is_dir())
    for name in shipped:
        agents = home / f".agents/skills/{name}/SKILL.md"
        claude = home / f".claude/skills/{name}/SKILL.md"
        assert agents.is_file() and not agents.is_symlink()
        assert claude.is_file() and not claude.is_symlink()
        assert hashlib.sha256(agents.read_bytes()).digest() == hashlib.sha256(claude.read_bytes()).digest()
    assert not (home / ".codex/skills/feature-spec").exists()
    manifest = json.loads((home / ".cache/feature-fix-swarm/install-manifest.json").read_text())
    assert manifest["scope"] == "user"
    assert manifest["paths"]


def test_install_invokes_upstream_full_claude_and_codex_profiles(tmp_path: Path) -> None:
    result = run_setup(tmp_path, "--scope", "user")

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "gsd-installer.log").read_text().splitlines()
    assert calls == ["--claude --global --profile=full", "--codex --global --profile=full"]
    manifest = json.loads((tmp_path / "home/.cache/feature-fix-swarm/install-manifest.json").read_text())
    assert manifest["gsd"]["version"] == "1.9.1"
    assert manifest["gsd"]["profiles"] == {"claude": "full", "codex": "full"}


def test_no_argument_install_is_deprecated_user_scope(tmp_path: Path) -> None:
    result = run_setup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "deprecated" in result.stderr.lower()
    assert (tmp_path / "home/.agents/skills/feature-spec/SKILL.md").is_file()


def test_doctor_json_schema_and_exit_codes(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0

    healthy = run_setup(tmp_path, "--doctor", "--scope", "user", "--json")
    report = json.loads(healthy.stdout)
    assert healthy.returncode == 0
    assert report["schema"] == "ffs.doctor/v1"
    assert report["status"] in {"healthy", "degraded"}
    assert report["exit_code"] == 0
    assert isinstance(report["checks"], list)
    assert any(check["id"] == "gsd-manifests" and check["status"] == "pass" for check in report["checks"])
    assert any(check["id"] == "codex-cli-version" and check["status"] == "pass" for check in report["checks"])

    invalid = run_setup(tmp_path, "--doctor", "--json")
    assert invalid.returncode == 2
    invalid_report = json.loads(invalid.stdout)
    assert invalid_report["schema"] == "ffs.doctor/v1"
    assert invalid_report["exit_code"] == 2


def test_doctor_rejects_wrong_gsd_manifest_version(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    codex_manifest = tmp_path / "home/.codex/gsd-file-manifest.json"
    data = json.loads(codex_manifest.read_text())
    data["version"] = "1.8.0"
    codex_manifest.write_text(json.dumps(data))

    result = run_setup(tmp_path, "--doctor", "--scope", "user", "--json")
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert any(check["id"] == "gsd-manifests" and check["status"] == "fail" for check in report["checks"])


def test_doctor_rejects_unsupported_codex_cli(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "codex"
    fake.write_text("#!/usr/bin/env bash\necho 'codex-cli 0.147.0'\n")
    fake.chmod(0o755)

    result = run_setup(
        tmp_path,
        "--doctor",
        "--scope",
        "user",
        "--json",
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert any(check["id"] == "codex-cli-version" and check["status"] == "fail" for check in report["checks"])


def test_doctor_reports_ac009_model_routing_advisory_checks(tmp_path: Path) -> None:
    """spec-004 AC-009: stale-bake surface + per-surface catalog/resolver
    warnings are advisory (never fail doctor) and the catalog check fires
    for claude-opus-5 today (gsd-core's own catalog only knows
    claude-opus-4-8)."""
    assert run_setup(tmp_path, "--scope", "user").returncode == 0

    result = run_setup(tmp_path, "--doctor", "--scope", "user", "--json")
    report = json.loads(result.stdout)
    assert result.returncode == 0
    checks_by_id = {check["id"]: check for check in report["checks"]}
    assert checks_by_id["stale-bake-guard"]["status"] == "pass"
    assert checks_by_id["model-resolvability"]["status"] == "pass"
    assert checks_by_id["model-routing-resolver"]["status"] == "pass"
    assert checks_by_id["model-routing-catalog"]["status"] == "warn"
    assert "claude-opus-5" in checks_by_id["model-routing-catalog"]["message"]


def test_doctor_warns_on_unreachable_canonical_tier_model(tmp_path: Path) -> None:
    """spec-004 AC-009(b): doctor forces a fresh probe (EDGE-006) and warns —
    never fails — when a canonical-tier model is unreachable."""
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_claude.chmod(0o755)
    fake_codex = fake_bin / "codex"
    fake_codex.write_text("#!/usr/bin/env bash\n[ \"${1:-}\" = --version ] && echo 'codex-cli 0.146.0'\nexit 0\n")
    fake_codex.chmod(0o755)
    fail_probe = tmp_path / "fail-on-opus.sh"
    fail_probe.write_text('#!/usr/bin/env bash\n[ "$1" = claude-opus-5 ] && exit 1\nexit 0\n')
    fail_probe.chmod(0o755)

    result = run_setup(
        tmp_path,
        "--doctor",
        "--scope",
        "user",
        "--json",
        extra_env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "GSD_MODEL_PROBE_CMD": str(fail_probe),
        },
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0
    check = next(item for item in report["checks"] if item["id"] == "model-resolvability")
    assert check["status"] == "warn"
    assert "claude-opus-5" in check["message"]


def test_doctor_degrades_to_warn_when_lint_model_routing_missing(tmp_path: Path) -> None:
    """spec-004 fix round finding 9a: a missing scripts/lint_model_routing.py
    must produce a single warn check row, never raise and abort the whole
    doctor run (it used to raise ActionableError/FileNotFoundError and hide
    every other doctor check behind it)."""
    fake_source = tmp_path / "fake-source"
    (fake_source / "lib").mkdir(parents=True)
    (fake_source / "lib" / "model_requests.py").write_bytes((ROOT / "lib" / "model_requests.py").read_bytes())
    # scripts/lint_model_routing.py deliberately absent.
    checks: list[dict[str, str]] = []
    ffs_installer.add_model_routing_doctor_checks(checks, fake_source)
    assert len(checks) == 1
    assert checks[0]["id"] == "model-resolvability"
    assert checks[0]["status"] == "warn"
    assert "lint_model_routing.py" in checks[0]["message"]


def _fake_host_cli_path(tmp_path: Path) -> str:
    """PATH with stub claude/codex binaries prepended.

    Doctor's model-resolvability check gates on `shutil.which(host)` before
    honoring the GSD_MODEL_PROBE_CMD* stubs, so a runner without the real
    CLIs (CI) silently takes the probe-skipped branch and the probe-path
    assertions never execute. The stubs are inert — the probe commands
    themselves are already overridden to `true` by run_setup."""
    bin_dir = tmp_path / "fake-host-bin"
    bin_dir.mkdir(exist_ok=True)
    for host in ("claude", "codex"):
        exe = bin_dir / host
        # Answer --version with an in-range pin: a codex on PATH also wakes
        # the pre-existing codex-cli-version doctor check, which fails hard
        # on an unparseable version.
        exe.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "--version" ]; then echo "0.146.0"; fi\n'
            'exit 0\n'
        )
        exe.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def test_doctor_model_resolvability_pass_message_notes_cache_refresh_and_timeout(tmp_path: Path) -> None:
    """spec-004 fix round finding 9b: the forced probe is bounded by a
    doctor-scoped, env-overridable timeout (FFS_DOCTOR_PROBE_TIMEOUT,
    default 20s — was unbounded at the shared 120s reviewer-dispatch
    default), and the pass message says the shared probe cache was
    refreshed rather than merely read from cache."""
    assert run_setup(tmp_path, "--scope", "user").returncode == 0

    result = run_setup(
        tmp_path,
        "--doctor",
        "--scope",
        "user",
        "--json",
        extra_env={
            "FFS_DOCTOR_PROBE_TIMEOUT": "7",
            "PATH": _fake_host_cli_path(tmp_path),
        },
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0
    check = next(item for item in report["checks"] if item["id"] == "model-resolvability")
    assert check["status"] == "pass"
    assert "cache refreshed" in check["message"]
    assert "7s/probe" in check["message"]


def test_different_version_duplicate_fails_doctor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    assert run_setup(tmp_path, "--scope", "project", "--project-dir", str(project)).returncode == 0
    user_manifest = tmp_path / "home/.cache/feature-fix-swarm/install-manifest.json"
    data = json.loads(user_manifest.read_text())
    data["version"] = "0.0.0-different"
    user_manifest.write_text(json.dumps(data))

    result = run_setup(
        tmp_path, "--doctor", "--scope", "project", "--project-dir", str(project), "--json"
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert any(check["id"] == "duplicate-version" and check["status"] == "fail" for check in report["checks"])


def test_same_version_duplicate_is_degraded_but_compatible(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    assert run_setup(tmp_path, "--scope", "project", "--project-dir", str(project)).returncode == 0

    result = run_setup(
        tmp_path, "--doctor", "--scope", "project", "--project-dir", str(project), "--json"
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["status"] == "degraded"
    assert any(check["id"] == "duplicate-version" and check["status"] == "warn" for check in report["checks"])


def test_known_legacy_codex_copy_is_removed_but_unknown_edit_is_preserved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    known = home / ".codex/skills/feature-spec/SKILL.md"
    unknown = home / ".codex/skills/fix/SKILL.md"
    known.parent.mkdir(parents=True)
    unknown.parent.mkdir(parents=True)
    known.write_bytes((ROOT / "skills/feature-spec/SKILL.md").read_bytes())
    unknown.write_text("locally edited\n")

    result = run_setup(tmp_path, "--scope", "user")

    assert result.returncode == 1
    assert not known.exists()
    assert unknown.read_text() == "locally edited\n"
    assert str(unknown) in result.stderr
    assert "preserved" in result.stderr.lower()


def test_unrelated_codex_skill_is_outside_ffs_migration_scope(tmp_path: Path) -> None:
    unrelated = tmp_path / "home/.codex/skills/my-private-skill/SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("private\n")

    installed = run_setup(tmp_path, "--scope", "user")
    checked = run_setup(tmp_path, "--doctor", "--scope", "user", "--json")

    assert installed.returncode == 0, installed.stderr
    assert checked.returncode == 0, checked.stdout
    assert unrelated.read_text() == "private\n"


def test_unmanifested_v413_legacy_copy_is_recognized_by_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    legacy = home / ".codex/skills/feature-implement/SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("hermetic stand-in for the v4.13.0 skill\n")
    historical_hash = "215e6f6355208ecac78280f92780bec64c25b60b78162fdbec8e42fa9731188e"
    known = ffs_installer.known_legacy_hashes(ROOT)
    assert historical_hash in known["skills/feature-implement/SKILL.md"]
    fixture_hash = ffs_installer.sha256_file(legacy)
    real_known_legacy_hashes = ffs_installer.known_legacy_hashes

    def catalog_with_fixture_hash(source: Path) -> dict[str, set[str]]:
        catalog = real_known_legacy_hashes(source)
        catalog["skills/feature-implement/SKILL.md"].add(fixture_hash)
        return catalog

    monkeypatch.setattr(
        ffs_installer,
        "known_legacy_hashes",
        catalog_with_fixture_hash,
    )

    backup = ffs_installer.Backup("test-migration", "user")
    preserved = ffs_installer.migrate_legacy(
        ROOT, [home / ".codex/skills"], backup
    )

    assert preserved == []
    assert not legacy.exists()


def test_catalog_covers_every_release_from_v413_through_v422() -> None:
    catalog = json.loads((ROOT / "data/installer/legacy-skill-hashes.json").read_text())
    required = {
        "4.13.0",
        "4.13.1",
        "4.14.0",
        "4.14.1",
        "4.14.2",
        "4.14.3",
        "4.14.4",
        "4.14.5",
        "4.15.0",
        "4.16.0",
        "4.17.0",
        "4.18.0",
        "4.19.0",
        "4.20.0",
        "4.21.0",
        "4.22.0",
    }
    assert required <= catalog["releases"].keys()
    assert all(len(commit) == 40 for commit in catalog["releases"].values())
    assert all(values for values in catalog["hashes_by_path"].values())


def test_upgrade_backup_and_rollback_restore_previous_layout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    legacy = home / ".codex/skills/feature-spec/SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes((ROOT / "skills/feature-spec/SKILL.md").read_bytes())

    installed = run_setup(tmp_path, "--scope", "user")
    assert installed.returncode == 0, installed.stderr
    backup_id = next(line.split("=", 1)[1] for line in installed.stdout.splitlines() if line.startswith("backup_id="))
    assert not legacy.exists()

    rolled_back = run_setup(tmp_path, "--rollback", backup_id)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert legacy.read_bytes() == (ROOT / "skills/feature-spec/SKILL.md").read_bytes()
    assert not (home / ".agents/skills/feature-spec").exists()


def test_upgrade_rollback_restores_prior_gsd_version_manifests(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for root in (home / ".claude", home / ".codex"):
        (root / "gsd-core").mkdir(parents=True)
        (root / "gsd-core/VERSION").write_text("1.8.0\n")
        (root / "gsd-core/old.txt").write_text("old-owned\n")
        (root / "gsd-file-manifest.json").write_text(
            json.dumps(
                {
                    "version": "1.8.0",
                    "mode": "full",
                    "files": {"gsd-core/VERSION": "old", "gsd-core/old.txt": "old"},
                }
            )
        )

    installed = run_setup(tmp_path, "--scope", "user")
    assert installed.returncode == 0, installed.stderr
    backup_id = next(line.split("=", 1)[1] for line in installed.stdout.splitlines() if line.startswith("backup_id="))
    assert json.loads((home / ".codex/gsd-file-manifest.json").read_text())["version"] == "1.9.1"

    rolled_back = run_setup(tmp_path, "--rollback", backup_id)

    assert rolled_back.returncode == 0, rolled_back.stderr
    for root in (home / ".claude", home / ".codex"):
        assert json.loads((root / "gsd-file-manifest.json").read_text())["version"] == "1.8.0"
        assert (root / "gsd-core/VERSION").read_text() == "1.8.0\n"
        assert (root / "gsd-core/old.txt").read_text() == "old-owned\n"


def test_uninstall_is_explicit_and_preserves_edited_managed_copy(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    managed = tmp_path / "home/.agents/skills/fix/SKILL.md"
    managed.write_text("edited after install\n")

    result = run_setup(tmp_path, "--uninstall", "--scope", "user")

    assert result.returncode == 1
    assert managed.read_text() == "edited after install\n"
    assert "preserved" in result.stderr.lower()
    assert run_setup(tmp_path, "--uninstall").returncode == 2


def test_clean_uninstall_then_reinstall_is_supported(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    removed = run_setup(tmp_path, "--uninstall", "--scope", "user")
    assert removed.returncode == 0, removed.stderr
    assert not (tmp_path / "home/.agents/skills/feature-spec").exists()
    reinstalled = run_setup(tmp_path, "--scope", "user")
    assert reinstalled.returncode == 0, reinstalled.stderr
    assert (tmp_path / "home/.agents/skills/feature-spec/SKILL.md").is_file()


def test_unmanaged_user_skill_collision_fails_before_replacement(tmp_path: Path) -> None:
    collision = tmp_path / "home/.agents/skills/fix/SKILL.md"
    collision.parent.mkdir(parents=True)
    collision.write_text("mine\n")

    result = run_setup(tmp_path, "--scope", "user")

    assert result.returncode == 1
    assert collision.read_text() == "mine\n"
    assert "preserved" in result.stderr.lower()
    assert not (tmp_path / "home/.claude/skills/feature-spec").exists()


def test_broken_managed_link_is_repaired(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    link = project / ".agents/skills/feature-spec"
    link.parent.mkdir(parents=True)
    link.symlink_to("../../missing/feature-spec")

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 0, result.stderr
    assert link.resolve() == project / ".feature-fix-swarm/vendor/skills/feature-spec"


def test_linked_worktree_resolves_same_git_common_lock(tmp_path: Path) -> None:
    main = tmp_path / "main"
    init_repo(main)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "Test"], check=True)
    (main / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(main), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "init"], check=True)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(linked), "-b", "linked"], check=True)

    script = (
        "from pathlib import Path; from lib.ffs_installer import project_lock_path; "
        "import sys; print(project_lock_path(Path(sys.argv[1])))"
    )
    first = subprocess.check_output([sys.executable, "-c", script, str(main)], cwd=ROOT, text=True).strip()
    second = subprocess.check_output([sys.executable, "-c", script, str(linked)], cwd=ROOT, text=True).strip()
    assert first == second
    assert first.endswith("feature-fix-swarm.setup.lock")


def test_project_manifest_cannot_escape_checkout_on_uninstall(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    manifest = project / ".feature-fix-swarm/install-manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "schema": "ffs.install/v1",
                "scope": "project",
                "paths": {"../victim": {"fingerprint": "file:forged"}},
            }
        )
    )

    result = run_setup(
        tmp_path, "--uninstall", "--scope", "project", "--project-dir", str(project)
    )

    assert result.returncode == 1
    assert "unsafe project manifest path" in result.stderr
    assert victim.read_text() == "keep\n"


def test_project_install_refuses_symlinked_discovery_ancestor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".agents").symlink_to(outside, target_is_directory=True)

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 1
    assert "symlinked ancestor" in result.stderr
    assert list(outside.iterdir()) == []


def test_project_install_refuses_ancestor_swap_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv(
        "FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py")
    )
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))
    real_replace_tree = ffs_installer.replace_tree
    swapped = False

    def swap_then_replace(*args: object, **kwargs: object) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            (project / ".feature-fix-swarm").symlink_to(
                outside, target_is_directory=True
            )
        real_replace_tree(*args, **kwargs)

    monkeypatch.setattr(ffs_installer, "replace_tree", swap_then_replace)

    with pytest.raises(
        ffs_installer.ActionableError,
        match="unsafe project destination|symlinked ancestor|changed during rollback",
    ):
        ffs_installer.install(ROOT, "project", project)

    assert list(outside.iterdir()) == []


def test_project_install_preserves_destination_created_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv(
        "FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py")
    )
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))
    real_replace_tree = ffs_installer.replace_tree
    injected: Path | None = None

    def inject_then_replace(
        source: Path, destination: Path, *args: object, **kwargs: object
    ) -> None:
        nonlocal injected
        if injected is None:
            destination.mkdir(parents=True)
            injected = destination / "operator.txt"
            injected.write_text("preserve me\n")
        real_replace_tree(source, destination, *args, **kwargs)

    monkeypatch.setattr(ffs_installer, "replace_tree", inject_then_replace)

    with pytest.raises(ffs_installer.ActionableError, match="changed during install"):
        ffs_installer.install(ROOT, "project", project)

    assert injected is not None
    assert injected.read_text() == "preserve me\n"


def test_project_entry_replacement_preserves_creation_during_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    destination = project / ".agents/skills/example"
    backup = ffs_installer.Backup("install", "project", project)

    def materialize(temporary: Path, _parent_fd: int) -> None:
        temporary.symlink_to("../../vendor/example")
        Path(destination.name).write_text("preserve me\n")

    with pytest.raises(ffs_installer.ActionableError, match="changed during install"):
        ffs_installer.replace_project_entry(
            destination,
            backup,
            project,
            "missing",
            materialize,
        )

    assert destination.read_text() == "preserve me\n"


def test_project_no_replace_rename_preserves_existing_destination(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    source = parent / "source"
    source.write_text("new\n")
    destination = parent / "destination"
    destination.write_text("preserve me\n")
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(FileExistsError):
            ffs_installer.rename_no_replace(
                source.name,
                destination.name,
                source_fd=directory_fd,
                destination_fd=directory_fd,
            )
    finally:
        os.close(directory_fd)

    assert source.read_text() == "new\n"
    assert destination.read_text() == "preserve me\n"


def test_project_manifest_write_refuses_post_write_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv(
        "FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py")
    )
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))
    real_replace_link = ffs_installer.replace_link
    last_skill = max(ffs_installer.source_skills(ROOT))
    swapped = False

    def swap_after_last_link(*args: object, **kwargs: object) -> None:
        nonlocal swapped
        real_replace_link(*args, **kwargs)
        destination = args[0]
        assert isinstance(destination, Path)
        if (
            not swapped
            and destination.name == last_skill
            and destination.parent == project / ".claude/skills"
        ):
            swapped = True
            anchor = project / ".feature-fix-swarm"
            anchor.rename(project / ".feature-fix-swarm-held")
            anchor.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(ffs_installer, "replace_link", swap_after_last_link)

    with pytest.raises(
        ffs_installer.ActionableError,
        match="unsafe project destination|symlinked ancestor|changed during rollback",
    ):
        ffs_installer.install(ROOT, "project", project)

    assert list(outside.iterdir()) == []


def test_partial_upstream_gsd_failure_restores_preexisting_namespace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old_agent = home / ".claude/agents/gsd-old.md"
    old_agent.parent.mkdir(parents=True)
    old_agent.write_text("old bytes\n")
    old_manifest = home / ".claude/gsd-file-manifest.json"
    old_manifest.write_text(
        json.dumps(
            {
                "version": "1.8.0",
                "mode": "full",
                "files": {"agents/gsd-old.md": "old"},
            }
        )
    )

    result = run_setup(
        tmp_path,
        "--scope",
        "user",
        extra_env={"FFS_GSD_STUB_FAIL_RUNTIME": "codex"},
    )

    assert result.returncode == 1
    assert old_agent.read_text() == "old bytes\n"
    assert json.loads(old_manifest.read_text())["version"] == "1.8.0"
    assert not (home / ".codex/gsd-file-manifest.json").exists()


def test_truncated_failure_manifest_cannot_bypass_upstream_restore(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old_manifest = home / ".codex/gsd-file-manifest.json"
    old_manifest.parent.mkdir(parents=True)
    old_manifest.write_text(
        json.dumps({"version": "1.8.0", "mode": "full", "files": {}})
    )

    result = run_setup(
        tmp_path,
        "--scope",
        "user",
        extra_env={
            "FFS_GSD_STUB_FAIL_RUNTIME": "codex",
            "FFS_GSD_STUB_CORRUPT_ON_FAILURE": "1",
        },
    )

    assert result.returncode == 1
    assert json.loads(old_manifest.read_text())["version"] == "1.8.0"


def test_project_legacy_migration_refuses_symlinked_codex_ancestor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    outside = tmp_path / "outside"
    legacy = outside / "skills/feature-spec"
    legacy.mkdir(parents=True)
    legacy_file = legacy / "SKILL.md"
    legacy_file.write_text((ROOT / "skills/feature-spec/SKILL.md").read_text())
    (project / ".codex").symlink_to(outside, target_is_directory=True)

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 1
    assert "symlinked ancestor" in result.stderr
    assert legacy_file.is_file()
    # The failure happens after the upstream installer; the outer transaction
    # must still put both global GSD profiles back to their prior absence.
    assert not (tmp_path / "home/.claude/gsd-file-manifest.json").exists()
    assert not (tmp_path / "home/.codex/gsd-file-manifest.json").exists()


def test_backup_creation_refuses_symlink_race_before_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    original_mode = stat.S_IMODE(outside.stat().st_mode)
    real_mkdir = os.mkdir
    raced = False

    def plant_backup_symlink(
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal raced
        if not raced and path == "backups" and dir_fd is not None:
            raced = True
            (home / ".cache/feature-fix-swarm/backups").symlink_to(
                outside, target_is_directory=True
            )
            raise FileExistsError(path)
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", plant_backup_symlink)

    with pytest.raises(ffs_installer.ActionableError, match="private backup root"):
        ffs_installer.Backup("install", "user")

    assert raced
    assert stat.S_IMODE(outside.stat().st_mode) == original_mode
    assert list(outside.iterdir()) == []


def test_private_cache_supports_an_intentional_cache_parent_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cache_target = tmp_path / "cache-target"
    cache_target.mkdir()
    (home / ".cache").symlink_to(cache_target, target_is_directory=True)
    backup = ffs_installer.Backup("install", "user")
    source = tmp_path / "source.txt"
    source.write_text("private payload\n")

    backup.before(source)
    backup.finish()

    assert backup.directory.resolve().is_relative_to(cache_target.resolve())
    assert (backup.directory / "objects/0000").read_text() == "private payload\n"


def test_backup_supports_an_intentional_source_parent_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    claude_target = tmp_path / "claude-target"
    claude_target.mkdir()
    (home / ".claude").symlink_to(claude_target, target_is_directory=True)
    source = home / ".claude/config.json"
    source.write_text('{"private":"payload"}\n')
    backup = ffs_installer.Backup("install", "user")

    backup.before(source)
    backup.finish()

    assert (backup.directory / "objects/0000").read_text() == source.read_text()


def test_private_json_has_restrictive_mode_before_content_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "private.json"
    real_dump = json.dump
    observed_mode: int | None = None

    def inspect_mode(value: object, handle: object, *args: object, **kwargs: object) -> None:
        nonlocal observed_mode
        observed_mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
        real_dump(value, handle, *args, **kwargs)

    monkeypatch.setattr(json, "dump", inspect_mode)

    ffs_installer.write_json_file(destination, {"token": "redacted"}, mode=0o600)

    assert observed_mode == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_backup_payload_writes_stay_on_open_directory_after_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    backup = ffs_installer.Backup("install", "user")
    held = backup.directory.with_name(f"{backup.directory.name}-held")
    backup.directory.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    backup.directory.symlink_to(outside, target_is_directory=True)
    source = tmp_path / "source.txt"
    source.write_text("private payload\n")

    backup.before(source)
    backup.finish()

    assert (held / "objects/0000").read_text() == "private payload\n"
    assert (held / "manifest.json").is_file()
    assert list(outside.iterdir()) == []


def test_rollback_refuses_symlinked_backup_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "source.txt"
    source.write_text("private payload\n")
    backup = ffs_installer.Backup("install", "user")
    backup.before(source)
    backup.finish()
    held = backup.directory.with_name(f"{backup.directory.name}-held")
    backup.directory.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    backup.directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ffs_installer.ActionableError, match="backup not found or unsafe"):
        ffs_installer.rollback(backup.backup_id)

    assert list(outside.iterdir()) == []


def test_backup_source_swap_to_symlink_is_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "source.txt"
    source.write_text("expected bytes\n")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not be copied\n")
    backup = ffs_installer.Backup("install", "user")
    real_open = os.open
    swapped = False

    def swap_before_source_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        if (
            not swapped
            and path == source.name
            and flags & getattr(os, "O_NOFOLLOW", 0)
            and not flags & getattr(os, "O_DIRECTORY", 0)
            and kwargs.get("dir_fd") is not None
        ):
            swapped = True
            source.unlink()
            source.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_source_open)

    with pytest.raises(ffs_installer.ActionableError, match="source changed"):
        backup.before(source)

    assert swapped
    copied = backup.directory / "objects/0000"
    assert not copied.exists()
    assert outside.read_text() == "must not be copied\n"
    os.close(backup.directory_fd)


def test_project_legacy_migration_refuses_symlinked_final_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    outside = tmp_path / "outside"
    legacy = outside / "feature-spec"
    legacy.mkdir(parents=True)
    legacy_file = legacy / "SKILL.md"
    legacy_file.write_text((ROOT / "skills/feature-spec/SKILL.md").read_text())
    codex = project / ".codex"
    codex.mkdir()
    (codex / "skills").symlink_to(outside, target_is_directory=True)

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 1
    assert "symlinked project directory" in result.stderr
    assert legacy_file.is_file()


def test_user_legacy_migration_refuses_symlinked_final_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    outside = tmp_path / "outside"
    legacy = outside / "feature-spec"
    legacy.mkdir(parents=True)
    legacy_file = legacy / "SKILL.md"
    legacy_file.write_text((ROOT / "skills/feature-spec/SKILL.md").read_text())
    codex = home / ".codex"
    codex.mkdir()
    (codex / "skills").symlink_to(outside, target_is_directory=True)
    backup = ffs_installer.Backup("install", "user")

    with pytest.raises(ffs_installer.ActionableError, match="unsafe legacy skill root"):
        ffs_installer.migrate_legacy(ROOT, [codex / "skills"], backup)

    assert legacy_file.is_file()


def test_user_legacy_rollback_refuses_post_migration_root_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    legacy_root = home / ".codex/skills"
    legacy = legacy_root / "feature-spec/SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text((ROOT / "skills/feature-spec/SKILL.md").read_text())
    backup = ffs_installer.Backup("install", "user")

    preserved = ffs_installer.migrate_legacy(ROOT, [legacy_root], backup)

    assert preserved == []
    assert not legacy.exists()
    held = home / ".codex/skills-held"
    legacy_root.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    legacy_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ffs_installer.ActionableError, match="managed path changed"):
        backup.restore_uncommitted()

    assert not (outside / "feature-spec/SKILL.md").exists()
    assert any((backup.directory / "objects").iterdir())


def test_project_legacy_migration_refuses_child_swap_to_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    legacy = project / ".codex/skills/feature-spec"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(
        (ROOT / "skills/feature-spec/SKILL.md").read_text()
    )
    outside = tmp_path / "outside/feature-spec"
    outside.mkdir(parents=True)
    outside_file = outside / "SKILL.md"
    outside_file.write_text((ROOT / "skills/feature-spec/SKILL.md").read_text())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    backup = ffs_installer.Backup("install", "project", project)
    real_open = os.open
    swapped = False

    def swap_before_child_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        if (
            not swapped
            and path == "feature-spec"
            and flags & getattr(os, "O_DIRECTORY", 0)
            and kwargs.get("dir_fd") is not None
        ):
            swapped = True
            legacy.rename(project / ".codex/skills/feature-spec-held")
            legacy.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_child_open)

    with pytest.raises(ffs_installer.ActionableError, match="legacy skill changed"):
        ffs_installer.migrate_legacy(
            ROOT,
            [project / ".codex/skills"],
            backup,
            project=project,
        )

    assert swapped
    assert outside_file.is_file()


def test_failure_after_first_ffs_write_restores_gsd_and_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv("FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py"))
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))

    real_replace_tree = ffs_installer.replace_tree
    failed = False

    def fail_after_write(
        source: Path,
        destination: Path,
        backup: ffs_installer.Backup,
        **kwargs: object,
    ) -> None:
        nonlocal failed
        real_replace_tree(source, destination, backup, **kwargs)
        if not failed and str(destination).startswith(str(project)):
            failed = True
            raise RuntimeError("synthetic post-GSD failure")

    monkeypatch.setattr(ffs_installer, "replace_tree", fail_after_write)

    with pytest.raises(RuntimeError, match="synthetic post-GSD failure"):
        ffs_installer.install(ROOT, "project", project)

    assert list((project / ".feature-fix-swarm/vendor/skills").glob("*")) == []
    assert not (home / ".claude/gsd-file-manifest.json").exists()
    assert not (home / ".codex/gsd-file-manifest.json").exists()


def test_failure_rollback_preserves_concurrent_project_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv(
        "FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py")
    )
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))
    real_replace_tree = ffs_installer.replace_tree
    changed: Path | None = None

    def change_then_fail(
        source: Path,
        destination: Path,
        backup: ffs_installer.Backup,
        **kwargs: object,
    ) -> None:
        nonlocal changed
        real_replace_tree(source, destination, backup, **kwargs)
        if changed is None and str(destination).startswith(str(project)):
            ffs_installer.remove_path(destination)
            destination.mkdir()
            changed = destination / "concurrent.txt"
            changed.write_text("preserve me\n")
            raise RuntimeError("synthetic concurrent change")

    monkeypatch.setattr(ffs_installer, "replace_tree", change_then_fail)

    with pytest.raises(ffs_installer.ActionableError, match="changed during rollback"):
        ffs_installer.install(ROOT, "project", project)

    assert changed is not None
    assert changed.read_text() == "preserve me\n"
    assert not (home / ".claude/gsd-file-manifest.json").exists()
    assert not (home / ".codex/gsd-file-manifest.json").exists()
