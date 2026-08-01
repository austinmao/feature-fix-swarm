from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_gpt56_eval", ROOT / "scripts/run-gpt56-eval.py"
)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def test_eval_refuses_custom_provider_before_codex_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.invalid")
    with pytest.raises(SystemExit, match="custom model providers are unsupported"):
        RUNNER.run_one(
            binary="must-not-run",
            codex_home=tmp_path,
            schema_path=tmp_path / "schema.json",
            fixture={
                "id": "volume-low-1",
                "tier": "volume",
                "blast_radius": "low",
                "task": "bounded",
                "must_include": ["bounded"],
            },
            model="gpt-5.6-luna",
            effort="low",
            repetition=1,
            corpus_hash="a" * 64,
            cli_version="0.146.0",
            cwd=tmp_path,
        )


def test_eval_auth_cas_uses_runner_compatible_file_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = home / ".codex/auth.json"
    source.parent.mkdir()
    source.write_text('{"refresh":"initial"}\n')
    os.chmod(source, 0o600)
    refreshed = tmp_path / "refreshed.json"
    refreshed.write_text('{"refresh":"rotated"}\n')
    os.chmod(refreshed, 0o600)

    RUNNER.sync_refreshed_auth(source, refreshed, RUNNER.sha256(source))

    assert source.read_text() == refreshed.read_text()
    lock = home / ".cache/feature-fix-swarm/codex-auth.lock"
    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o600


def test_eval_synchronizes_auth_when_evaluation_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = ROOT / "evals/gpt56/corpus.json"
    auth = tmp_path / "auth.json"
    auth.write_text('{"refresh":"initial"}\n')
    os.chmod(auth, 0o600)
    output = tmp_path / "results.json"
    synchronized: list[Path] = []

    monkeypatch.setattr(RUNNER, "codex_version", lambda _binary: "0.146.0")
    monkeypatch.setattr(
        RUNNER,
        "run_one",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        RUNNER,
        "sync_refreshed_auth",
        lambda _source, refreshed, _initial: synchronized.append(refreshed),
    )

    with pytest.raises(KeyboardInterrupt):
        RUNNER.main(
            [
                "--corpus",
                str(corpus),
                "--output",
                str(output),
                "--codex-bin",
                "unused",
                "--auth",
                str(auth),
            ]
        )

    assert len(synchronized) == 1
