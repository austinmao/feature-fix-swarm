"""Independent acceptance for real FFS directory install manifests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_m0_source_evidence import artifact, clean_env, git, invoke, source_fixture, write_json


def directory_digest(root: Path) -> str:
    # This is the published FFS dir fingerprint wire format, including directories.
    digest = hashlib.sha256()
    for child in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = child.relative_to(root).as_posix().encode()
        if child.is_symlink():
            value = b"L\0" + relative + b"\0" + os.readlink(child).encode()
        elif child.is_file():
            value = b"F\0" + relative + b"\0" + hashlib.sha256(child.read_bytes()).hexdigest().encode()
        elif child.is_dir():
            value = b"D\0" + relative
        else:
            raise AssertionError("unexpected fixture entry")
        digest.update(value + b"\n")
    return digest.hexdigest()


def directory_fixture(tmp_path: Path, mutation: str) -> Path:
    manifest_path, _ = source_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    evidence_path = Path(manifest["full_inventory"]["source_runtime"]["artifacts"][0]["locator"])
    evidence = json.loads(evidence_path.read_text())
    row = evidence["observations"]["canonical_source"]["manifests"][0]
    repo = Path(manifest["repository"])
    source = repo / "skills/demo"
    staged = repo / ".agents/skills/demo"
    (source / "references").mkdir()
    (source / "references/details.md").write_text("committed reference\n")
    (source / "latest.md").symlink_to("references/details.md")
    subprocess.run(["git", "-C", str(repo), "add", "skills/demo"], check=True, env=clean_env())
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=M0 Acceptance", "-c",
                    "user.email=m0@example.invalid", "commit", "-qm", "directory fixture"],
                   check=True, env=clean_env())
    row["repository"]["head"] = row["repository"]["generation"] = git(repo, "rev-parse", "HEAD")
    shutil.rmtree(staged)
    shutil.copytree(source, staged, symlinks=True)
    if mutation == "dirty-generation":
        (source / "references/details.md").write_text("uncommitted reference\n")
        (staged / "references/details.md").write_bytes((source / "references/details.md").read_bytes())
    entries = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        target = staged / relative
        entry = {"relative_path": relative}
        if path.is_symlink():
            entry.update(type="symlink", source_link_target=os.readlink(path), staged_link_target=os.readlink(target))
        elif path.is_file():
            entry.update(type="file", source_artifact=artifact(path), staged_artifact=artifact(target))
        else:
            entry.update(type="directory")
        entries.append(entry)
    mapping = {"managed_path": ".agents/skills/demo", "type": "directory",
               "source_root": str(source), "staged_root": str(staged),
               "fingerprint": "dir:" + directory_digest(source), "entries": entries}
    if mutation == "omitted-leaf":
        mapping["entries"] = [entry for entry in entries if entry["relative_path"] != "references/details.md"]
    elif mutation == "extra-staged-leaf":
        (staged / "unowned.md").write_text("unowned staged bytes\n")
    elif mutation == "symlink-escape":
        (staged / "latest.md").unlink()
        (staged / "latest.md").symlink_to("../../../../outside.md")
    elif mutation == "traversal-entry":
        mapping["entries"][0]["relative_path"] = "../SKILL.md"
    row["mappings"] = [mapping]
    install_path = Path(row["install_manifest"]["locator"])
    install = json.loads(install_path.read_text())
    install["paths"] = {mapping["managed_path"]: {"fingerprint": mapping["fingerprint"]}}
    write_json(install_path, install)
    row["install_manifest"] = artifact(install_path)
    write_json(evidence_path, evidence)
    manifest["full_inventory"]["source_runtime"]["artifacts"] = [artifact(evidence_path)]
    return write_json(manifest_path, manifest)


def test_fixture_real_directory_manifest_with_committed_file_and_symlink(tmp_path: Path) -> None:
    manifest = directory_fixture(tmp_path, "valid")
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode == 0, payload
    assert payload["canonical_source_verified"] is True


@pytest.mark.parametrize("mutation", ["omitted-leaf", "extra-staged-leaf", "dirty-generation",
                                      "symlink-escape", "traversal-entry"])
def test_fixture_directory_mapping_refuses_incomplete_or_unowned_generation(tmp_path: Path, mutation: str) -> None:
    manifest = directory_fixture(tmp_path, mutation)
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode != 0
    assert payload["status"] == "UNMET"
    assert payload["errors"][0]["code"] == "SOURCE_CANONICAL"


@pytest.mark.parametrize("scope", ["user", "linked-project"])
def test_fixture_directory_mapping_uses_actual_scope_and_common_repository(tmp_path: Path, scope: str) -> None:
    manifest_path = directory_fixture(tmp_path, "valid")
    manifest = json.loads(manifest_path.read_text())
    evidence_path = Path(manifest["full_inventory"]["source_runtime"]["artifacts"][0]["locator"])
    evidence = json.loads(evidence_path.read_text())
    row = evidence["observations"]["canonical_source"]["manifests"][0]
    mapping = row["mappings"][0]
    install_path = Path(row["install_manifest"]["locator"])
    install = json.loads(install_path.read_text())
    repo = Path(manifest["repository"])
    if scope == "user":
        row["scope"] = install["scope"] = "user"
        row["project_root"] = None
        install["source"] = str(repo)
        mapping["managed_path"] = mapping["staged_root"]
    else:
        linked = tmp_path / "linked-canonical"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(linked), "HEAD"],
                       check=True, capture_output=True, env=clean_env())
        staged = linked / ".agents/skills/demo"
        staged.parent.mkdir(parents=True)
        shutil.copytree(linked / "skills/demo", staged, symlinks=True)
        row["project_root"] = row["resolved_source_root"] = row["repository"]["root"] = str(linked)
        mapping["source_root"] = str(linked / "skills/demo")
        mapping["staged_root"] = str(staged)
        for entry in mapping["entries"]:
            if entry["type"] == "file":
                entry["source_artifact"] = artifact(linked / "skills/demo" / entry["relative_path"])
                entry["staged_artifact"] = artifact(staged / entry["relative_path"])
        install_path = linked / ".feature-fix-swarm/install-manifest.json"
    install["paths"] = {mapping["managed_path"]: {"fingerprint": mapping["fingerprint"]}}
    write_json(install_path, install)
    row["install_manifest"] = artifact(install_path)
    write_json(evidence_path, evidence)
    manifest["full_inventory"]["source_runtime"]["artifacts"] = [artifact(evidence_path)]
    write_json(manifest_path, manifest)
    run, payload = invoke(tmp_path, manifest_path)
    assert run.returncode == 0, payload
    assert payload["canonical_source_verified"] is True
