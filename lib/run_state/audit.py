"""Adversarial completion audit via `codex exec` subprocess.

Default auditor: codex (GPT-5) in read-only sandbox with high reasoning effort.
Caller passes a hostile prompt that asks the model to PROVE the work is NOT done.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

VALID_VERDICTS = ("pass", "fail", "error")


@dataclass
class AuditResult:
    verdict: str
    reasoning: str = ""
    missing: List[str] = field(default_factory=list)
    raw: str = ""


def _extract_json(text: str) -> Optional[dict]:
    """Find the first balanced JSON object in text. Tolerates leading/trailing chatter."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def run_audit(
    *,
    prompt: str,
    cwd: Path,
    auditor: str = "codex",
    timeout_seconds: int = 600,
) -> AuditResult:
    """Run an adversarial audit. Returns AuditResult with verdict in {pass, fail, error}."""
    if auditor == "codex":
        cmd = [
            "codex", "exec",
            prompt,
            "-C", str(cwd),
            "-s", "read-only",
            "-c", 'model_reasoning_effort="high"',
        ]
    else:
        return AuditResult(verdict="error", reasoning=f"unknown auditor: {auditor}")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return AuditResult(verdict="error", reasoning=f"audit timed out after {timeout_seconds}s")
    except FileNotFoundError:
        return AuditResult(verdict="error", reasoning="codex CLI not installed")

    raw = proc.stdout or ""
    if proc.returncode != 0:
        return AuditResult(
            verdict="error",
            reasoning=f"audit subprocess exit code {proc.returncode}: {(proc.stderr or '')[:500]}",
            raw=raw,
        )

    obj = _extract_json(raw)
    if obj is None:
        return AuditResult(verdict="error", reasoning="failed to parse JSON from audit output", raw=raw)
    verdict = obj.get("verdict", "")
    if verdict not in ("pass", "fail"):
        return AuditResult(verdict="error", reasoning=f"invalid verdict value: {verdict!r}", raw=raw)
    return AuditResult(
        verdict=verdict,
        reasoning=str(obj.get("reasoning", ""))[:2000],
        missing=list(obj.get("missing", []))[:50],
        raw=raw,
    )
