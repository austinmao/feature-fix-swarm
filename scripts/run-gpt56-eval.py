#!/usr/bin/env python3
"""Run the reproducible 18-fixture GPT-5.6 effort matrix through Codex CLI."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, NoReturn


MATRIX = {
    "judgment": ("gpt-5.6-sol", ("xhigh", "high")),
    "execution": ("gpt-5.6-terra", ("high", "medium")),
    "volume": ("gpt-5.6-luna", ("medium", "low")),
}
SCHEMA = "ffs.gpt56-eval/v1"
AUTH_SYNC_HELPER = Path(__file__).resolve().parent / "gsd/sync-codex-auth.py"
FORBIDDEN_PROVIDER_VARS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "CODEX_MODEL_PROVIDER",
)
FINDING_SEVERITIES = {"BLOCKER", "CRITICAL", "HIGH", "MEDIUM", "LOW"}


def fail(message: str) -> NoReturn:
    raise SystemExit(f"gpt56-eval: {message}")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def codex_version(binary: str) -> str:
    output = subprocess.check_output([binary, "--version"], text=True)
    match = re.search(r"(\d+\.\d+\.\d+)", output)
    if not match:
        fail(f"could not parse Codex CLI version from {output!r}")
    return match.group(1)


def secure_auth_source(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        fail(f"Codex auth must be a regular non-symlink file: {path}")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        fail(f"Codex auth must be owned by the current user with mode 0600: {path}")


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reject_custom_provider(environment: dict[str, str] | os._Environ[str]) -> None:
    configured = [name for name in FORBIDDEN_PROVIDER_VARS if environment.get(name)]
    if configured:
        fail(
            "custom model providers are unsupported for subscription-backed evals: "
            + ", ".join(configured)
        )


def sync_refreshed_auth(source: Path, refreshed: Path, initial_hash: str) -> None:
    """Use the runner's global flock and CAS implementation."""
    lock_path = Path.home() / ".cache/feature-fix-swarm/codex-auth.lock"
    process = subprocess.run(
        [
            "/usr/bin/python3",
            str(AUTH_SYNC_HELPER),
            str(source),
            str(refreshed),
            initial_hash,
            str(lock_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        fail(process.stderr.strip() or f"OAuth synchronization failed ({process.returncode})")


def preserve_auth_recovery(refreshed: Path) -> Path:
    """Preserve an isolated rotated credential if canonical CAS cannot finish."""
    recovery_root = Path.home() / ".cache/feature-fix-swarm"
    recovery_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    recovery = recovery_root / (
        f"auth-recovery-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}.json"
    )
    with refreshed.open("rb") as source, recovery.open("xb") as destination:
        os.chmod(recovery, 0o600)
        shutil.copyfileobj(source, destination)
        destination.flush()
        os.fsync(destination.fileno())
    return recovery


def response_schema(path: Path) -> None:
    atomic_write(
        path,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "minLength": 1}},
        },
    )


def run_one(
    *,
    binary: str,
    codex_home: Path,
    schema_path: Path,
    fixture: dict[str, Any],
    model: str,
    effort: str,
    repetition: int,
    corpus_hash: str,
    cli_version: str,
    cwd: Path,
) -> dict[str, Any]:
    prompt = (
        "This is a bounded model-routing evaluation. Do not use tools. "
        "Return one concise, self-contained answer as JSON matching the supplied schema.\n\n"
        f"Task: {fixture['task']}\n"
        "Required concepts (use these phrases verbatim in the answer): "
        + ", ".join(fixture["must_include"])
    )
    env = os.environ.copy()
    reject_custom_provider(env)
    env.pop("OPENAI_API_KEY", None)
    env["CODEX_HOME"] = str(codex_home)
    started = time.monotonic()
    command = [
            binary,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--json",
            "--output-schema",
            str(schema_path),
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            prompt,
        ]
    try:
        process = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        process = subprocess.CompletedProcess(
            command,
            124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "evaluation timed out",
        )
    latency_ms = round((time.monotonic() - started) * 1000)
    answer = ""
    usage: dict[str, Any] = {}
    parse_errors: list[str] = []
    for line in process.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parse_errors.append("non-JSON CLI event")
            continue
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                answer = str(item.get("text", ""))
        elif event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    try:
        payload = json.loads(answer)
        response_text = str(payload.get("answer", ""))
    except (json.JSONDecodeError, AttributeError):
        response_text = ""
        parse_errors.append("final answer did not match the JSON schema")
    missing_terms = [
        term for term in fixture["must_include"] if term.casefold() not in response_text.casefold()
    ]
    findings = [
        {"severity": "HIGH", "message": f"missing required concept: {term}"}
        for term in missing_terms
    ]
    findings.extend({"severity": "HIGH", "message": message} for message in parse_errors)
    if process.returncode != 0:
        findings.append(
            {"severity": "HIGH", "message": f"Codex CLI exited {process.returncode}"}
        )
    return {
        "fixture_id": fixture["id"],
        "repetition": repetition,
        "tier": fixture["tier"],
        "blast_radius": fixture["blast_radius"],
        "corpus_sha256": corpus_hash,
        "codex_cli_version": cli_version,
        "model_id": model,
        "effort": effort,
        "date": datetime.now(timezone.utc).isoformat(),
        "latency_ms": latency_ms,
        "usage": usage,
        "retries": 0,
        "mandatory_gates_passed": not findings,
        "findings": findings,
        "requirement_coverage_regression": bool(missing_terms),
        "answer_sha256": hashlib.sha256(response_text.encode()).hexdigest(),
    }


def selection_row_is_clean(row: dict[str, Any]) -> bool:
    for field in ("mandatory_gates_passed", "requirement_coverage_regression"):
        if type(row.get(field)) is not bool:
            fail(f"{field} must be boolean")
    findings = row.get("findings")
    if not isinstance(findings, list) or not all(
        isinstance(item, dict) for item in findings
    ):
        fail("findings must be a list of objects")
    if any(item.get("severity") not in FINDING_SEVERITIES for item in findings):
        fail("finding severity is invalid")
    severe = any(
        str(item.get("severity", "")).upper()
        in {"BLOCKER", "CRITICAL", "HIGH"}
        for item in findings
    )
    return bool(
        row["mandatory_gates_passed"]
        and not row["requirement_coverage_regression"]
        and not severe
    )


def selections(results: list[dict[str, Any]]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for tier, (_model, efforts) in MATRIX.items():
        higher, lower = efforts
        tier_rows = [row for row in results if row["tier"] == tier]
        expected_per_effort = 12
        if any(sum(row["effort"] == effort for row in tier_rows) != expected_per_effort for effort in efforts):
            fail(f"incomplete {tier} evaluation matrix")
        higher_rows = [row for row in tier_rows if row["effort"] == higher]
        if any(not selection_row_is_clean(row) for row in higher_rows):
            fail(f"higher-effort {tier} baseline failed mandatory gates")
        lower_rows = [row for row in tier_rows if row["effort"] == lower]
        clean = all(selection_row_is_clean(row) for row in lower_rows)
        selected[tier] = lower if clean else higher
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path("evals/gpt56/corpus.json"))
    parser.add_argument("--output", type=Path, default=Path("evals/gpt56/results.json"))
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--auth", type=Path, default=Path.home() / ".codex/auth.json")
    args = parser.parse_args(argv)
    corpus = json.loads(args.corpus.read_text())
    if len(corpus) != 18:
        fail("corpus must contain exactly 18 fixtures")
    secure_auth_source(args.auth)
    reject_custom_provider(os.environ)
    initial_auth_hash = sha256(args.auth)
    cli_version = codex_version(args.codex_bin)
    with tempfile.TemporaryDirectory(prefix="ffs-gpt56-eval-") as temporary:
        temporary_root = Path(temporary)
        codex_home = temporary_root / "codex-home"
        codex_home.mkdir(mode=0o700)
        shutil.copy2(args.auth, codex_home / "auth.json")
        os.chmod(codex_home / "auth.json", 0o600)
        schema_path = temporary_root / "response.schema.json"
        response_schema(schema_path)
        refreshed_auth = codex_home / "auth.json"
        results: list[dict[str, Any]] = []
        try:
            for fixture in corpus:
                model, efforts = MATRIX[fixture["tier"]]
                for effort in efforts:
                    for repetition in (1, 2):
                        row = run_one(
                            binary=args.codex_bin,
                            codex_home=codex_home,
                            schema_path=schema_path,
                            fixture=fixture,
                            model=model,
                            effort=effort,
                            repetition=repetition,
                            corpus_hash=sha256(args.corpus),
                            cli_version=cli_version,
                            cwd=args.corpus.resolve().parents[2],
                        )
                        results.append(row)
                        atomic_write(
                            args.output,
                            {"schema": SCHEMA, "complete": False, "results": results},
                        )
            decision = selections(results)
            atomic_write(
                args.output,
                {
                    "schema": SCHEMA,
                    "complete": True,
                    "corpus_sha256": sha256(args.corpus),
                    "codex_cli_version": cli_version,
                    "selected_efforts": decision,
                    "results": results,
                },
            )
        finally:
            try:
                sync_refreshed_auth(args.auth, refreshed_auth, initial_auth_hash)
            except BaseException:
                recovery = preserve_auth_recovery(refreshed_auth)
                print(
                    f"gpt56-eval: preserved refreshed OAuth recovery at {recovery}",
                    file=__import__("sys").stderr,
                )
                raise
    print(json.dumps(decision, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
