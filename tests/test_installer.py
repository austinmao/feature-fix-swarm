from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
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
    real_sha256 = ffs_installer.sha256_file
    monkeypatch.setattr(
        ffs_installer,
        "sha256_file",
        lambda path: historical_hash if Path(path) == legacy else real_sha256(Path(path)),
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

    def fail_after_write(source: Path, destination: Path, backup: ffs_installer.Backup) -> None:
        nonlocal failed
        real_replace_tree(source, destination, backup)
        if not failed and str(destination).startswith(str(project)):
            failed = True
            raise RuntimeError("synthetic post-GSD failure")

    monkeypatch.setattr(ffs_installer, "replace_tree", fail_after_write)

    with pytest.raises(RuntimeError, match="synthetic post-GSD failure"):
        ffs_installer.install(ROOT, "project", project)

    assert list((project / ".feature-fix-swarm/vendor/skills").glob("*")) == []
    assert not (home / ".claude/gsd-file-manifest.json").exists()
    assert not (home / ".codex/gsd-file-manifest.json").exists()
