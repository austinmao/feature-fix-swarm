#!/usr/bin/env python3
"""Closed, local-only data boundary for the FFS retro collector."""

from __future__ import annotations

import contextlib
import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import subprocess
import sys
from typing import Iterator


SCHEMA = "ffs.retro/v1"
EVENT_CLASSES = frozenset({
    "security", "scrub", "grant-bypass", "data-loss", "dead-executor",
    "unrecovered-stall", "fallback", "retry", "gate-warn", "optimization",
    "operator-intervention", "unknown",
})
P0_CLASSES = frozenset({"security", "scrub", "grant-bypass", "data-loss"})
P1_CLASSES = frozenset({"dead-executor", "unrecovered-stall"})
P2_CLASSES = frozenset({"fallback", "retry", "gate-warn"})
SCRIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.(?:sh|py)$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+$")
SIG_RE = re.compile(r"^[0-9a-f]{64}$")
HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
FIX_RE = re.compile(
    r"^(?:scripts/gsd/[A-Za-z0-9][A-Za-z0-9_.-]*\.sh|"
    r"lib/[A-Za-z0-9][A-Za-z0-9_.-]*\.py|"
    r"skills/[A-Za-z0-9][A-Za-z0-9_-]*/SKILL\.md|"
    r"\.github/workflows/[A-Za-z0-9][A-Za-z0-9_.-]*\.yml)$"
)
MODEL_TIERS = frozenset({"haiku", "sonnet", "opus", "gpt-5", "gpt-5-mini", "unknown"})
SEVERITIES = frozenset({"info", "warning", "error", "critical", "P0", "P1", "P2", "P3"})
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
RELEASE_RE = re.compile(r"^##\s+(?:v([0-9]+)\.([0-9]+)\.([0-9]+)|([0-9]+)\.([0-9]+)\.([0-9]+)|\[([0-9]+)\.([0-9]+)\.([0-9]+)\])(?:\s|$)")


class RetroReject(ValueError):
    """A stable, value-free rejection suitable for the shell boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> None:
    if not code.startswith("RETRO:"):
        code = f"RETRO:{code}"
    raise RetroReject(code)


def _string(value: object, code: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str):
        _reject("invalid-schema")
    if len(value) > 500:
        _reject("value-too-long")
    if "\x00" in value or any(ord(char) < 32 for char in value):
        _reject("unsafe-value")
    if pattern is not None and not pattern.fullmatch(value):
        _reject(code)
    return value


def _number(value: object, code: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _reject("invalid-schema")
    return value


def _exit_code(value: object) -> int:
    number = _number(value, "invalid-exit-code")
    if (isinstance(number, float) and not number.is_integer()) or not 0 <= int(number) <= 255:
        _reject("invalid-exit-code")
    return int(number)


def parse_digest(path: Path) -> list[dict]:
    """Parse JSONL, tolerating only a malformed final non-empty record."""
    if not path.exists():
        return []
    try:
        rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        _reject("invalid-digest")
    events: list[dict] = []
    nonempty = [index for index, line in enumerate(rows) if line.strip()]
    last = nonempty[-1] if nonempty else -1
    for index, line in enumerate(rows):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            if index == last:
                continue
            _reject("invalid-digest")
        if not isinstance(event, dict):
            if index == last:
                continue
            _reject("invalid-digest")
        events.append(event)
    return events


def _event_class(value: object) -> str:
    # Safe future identifiers deliberately collapse to the literal unknown.
    if isinstance(value, str) and TOKEN_RE.fullmatch(value):
        return value if value in EVENT_CLASSES else "unknown"
    _reject("invalid-event")


def _finding_from_event(event: dict, finding: dict | None = None) -> dict | None:
    if not isinstance(event, dict):
        _reject("invalid-event")
    script = event.get("script")
    if script == "retro.sh":
        return None
    item = {
        "script": _string(script, "invalid-script", SCRIPT_RE),
        "event_class": _event_class(event.get("event_class", event.get("class"))),
        "gate": _string(event.get("gate", "none"), "invalid-gate", TOKEN_RE),
        "exit_code": _exit_code(event.get("exit_code", 0)),
        "ffs_minor": _string(event.get("ffs_minor", "0.0"), "invalid-version", VERSION_RE),
    }
    for source in (event, finding or {}):
        if "model_tier" in source:
            tier = _string(source["model_tier"], "invalid-model-tier")
            if tier not in MODEL_TIERS:
                _reject("invalid-model-tier")
            item["model_tier"] = tier
        if "severity" in source:
            severity = _string(source["severity"], "invalid-severity")
            if severity not in SEVERITIES:
                _reject("invalid-severity")
            item["severity"] = severity
        if "suggested_fix" in source:
            item["suggested_fix"] = _string(source["suggested_fix"], "unsafe-value", FIX_RE)
        if "sig" in source:
            raw_sig = source["sig"]
            if isinstance(raw_sig, str) and SIG_RE.fullmatch(raw_sig):
                item["sig_derived"] = hashlib.sha256(raw_sig.encode("ascii")).hexdigest()[:16]
    return item


def _matching_finding(event: dict, findings: list[dict]) -> dict | None:
    event_sig = event.get("sig")
    if isinstance(event_sig, str):
        for finding in findings:
            if isinstance(finding, dict) and finding.get("sig") == event_sig:
                return finding
    # Findings-queue rows do not necessarily repeat the digest signature.  The
    # closed script/gate pair is the only fallback correlation we admit.
    for finding in findings:
        if isinstance(finding, dict) and finding.get("script") == event.get("script") and finding.get("gate") == event.get("gate"):
            return finding
    return None


def collect_payload(events: list[dict], findings: list[dict]) -> dict:
    if not isinstance(events, list) or not isinstance(findings, list):
        _reject("invalid-input")
    # Retain the raw signature only long enough to make ordering independent of
    # input order.  It is never put in the candidate payload.
    ordered: list[tuple[dict, str]] = []
    for event in events:
        selected = _finding_from_event(event, _matching_finding(event, findings))
        if selected is not None:
            matched = _matching_finding(event, findings) or {}
            raw_sig = event.get("sig", matched.get("sig", ""))
            if raw_sig and (not isinstance(raw_sig, str) or not SIG_RE.fullmatch(raw_sig)):
                raw_sig = ""
            ordered.append((selected, raw_sig))
    ordered.sort(key=lambda pair: (
        pair[0]["script"], pair[0]["event_class"], pair[0]["gate"], pair[0]["exit_code"],
        pair[0]["ffs_minor"], pair[1],
    ))
    return {"schema": SCHEMA, "findings": [item for item, _raw_sig in ordered]}


def classify_priority(finding: dict, class_metrics: dict | None = None) -> str:
    event_class = _event_class(finding.get("event_class"))
    if event_class in P0_CLASSES:
        return "P0"
    if event_class in P1_CLASSES:
        return "P1"
    metric = (class_metrics or {}).get(event_class)
    if isinstance(metric, dict):
        ratio = metric.get("wall_active_ratio")
        if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and math.isfinite(ratio) and ratio >= 2:
            return "P1"
    if event_class in P2_CLASSES:
        return "P2"
    return "P3"


def stable_fingerprint(finding: dict) -> str:
    parts = (
        _string(finding.get("script"), "invalid-script", SCRIPT_RE),
        _event_class(finding.get("event_class")),
        _string(finding.get("gate"), "invalid-gate", TOKEN_RE),
        str(_exit_code(finding.get("exit_code"))),
        _string(finding.get("ffs_minor"), "invalid-version", VERSION_RE),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _validate_metrics(metrics: object) -> dict:
    if not isinstance(metrics, dict) or set(metrics) - {
        "wall_seconds", "active_seconds", "wall_active_ratio", "intervention_free"
    }:
        _reject("invalid-schema")
    accepted: dict = {}
    for name in ("wall_seconds", "active_seconds", "wall_active_ratio"):
        if name in metrics:
            value = _number(metrics[name], "invalid-schema")
            if value < 0 or (name == "wall_active_ratio" and value < 1):
                _reject("invalid-schema")
            accepted[name] = value
    if "intervention_free" in metrics:
        if not isinstance(metrics["intervention_free"], bool):
            _reject("invalid-schema")
        accepted["intervention_free"] = metrics["intervention_free"]
    return accepted


def validate_payload(candidate: dict, consumer_identity: tuple[str, ...] = (), class_metrics: dict | None = None) -> dict:
    if not isinstance(candidate, dict) or set(candidate) - {"schema", "findings", "metrics"}:
        _reject("invalid-schema")
    if candidate.get("schema") != SCHEMA or not isinstance(candidate.get("findings"), list):
        _reject("invalid-schema")
    identity = tuple(part for part in consumer_identity if isinstance(part, str) and part)
    checked: list[dict] = []
    for finding in candidate["findings"]:
        if not isinstance(finding, dict) or set(finding) - {
            "script", "event_class", "gate", "exit_code", "ffs_minor", "model_tier",
            "sig_derived", "severity", "suggested_fix", "priority", "fingerprint",
        }:
            _reject("invalid-schema")
        clean = _finding_from_event(finding)
        assert clean is not None
        for key in ("model_tier", "severity", "suggested_fix"):
            if key in finding and key not in clean:
                _reject("invalid-schema")
        if "sig_derived" in finding:
            clean["sig_derived"] = _string(finding["sig_derived"], "invalid-signature", HEX16_RE)
        priority = finding.get("priority", classify_priority(clean, class_metrics))
        if priority not in {"P0", "P1", "P2", "P3"} or priority != classify_priority(clean, class_metrics):
            _reject("invalid-schema")
        clean["priority"] = priority
        fingerprint = finding.get("fingerprint", stable_fingerprint(clean))
        if not isinstance(fingerprint, str) or fingerprint != stable_fingerprint(clean) or not HEX16_RE.fullmatch(fingerprint):
            _reject("invalid-schema")
        clean["fingerprint"] = fingerprint
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        if any(part in encoded for part in identity):
            _reject("consumer-identity")
        checked.append(clean)
    output: dict = {"schema": SCHEMA, "findings": checked}
    if "metrics" in candidate:
        output["metrics"] = _validate_metrics(candidate["metrics"])
    return output


@contextlib.contextmanager
def secure_payload_copy(payload: dict, directory: Path | None = None) -> Iterator[Path]:
    directory = directory or Path(tempfile.gettempdir())
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".ffs-retro-", suffix=".json", dir=directory)
    path = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            raw = canonical_json(payload)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _secure_parent(path: Path) -> None:
    parent = path.parent
    # The caller has already selected a state root.  Refuse a swapped final
    # root while avoiding re-walking platform-managed temporary ancestors.
    try:
        metadata = parent.lstat()
    except FileNotFoundError:
        parent.mkdir(parents=True, mode=0o700)
        metadata = parent.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _reject("unsafe-state")
    os.chmod(parent, 0o700)


def record_ledger_entry(path: Path, entry: dict) -> None:
    """Append one canonical line while holding the ledger inode's lock."""
    _secure_parent(path)
    if os.path.lexists(path):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            _reject("unsafe-state")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        line = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        os.write(fd, line)
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def canonical_json(payload: dict) -> bytes:
    """The one serialization used for scanner input and successful stdout."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def derive_metrics(events: list[dict]) -> dict:
    """Derive only facts whose complete prerequisite set is trustworthy."""
    output: dict = {}
    if not events:
        return output
    times = [_timestamp(event.get("ts")) if isinstance(event, dict) else None for event in events]
    durations = [_duration(event.get("duration_seconds")) if isinstance(event, dict) else None for event in events]
    if all(item is not None for item in times):
        parsed = [item for item in times if item is not None]
        if all(parsed[index] >= parsed[index - 1] for index in range(1, len(parsed))):
            output["wall_seconds"] = (parsed[-1] - parsed[0]).total_seconds()
    if all(item is not None for item in durations):
        output["active_seconds"] = sum(item for item in durations if item is not None)
    wall, active = output.get("wall_seconds"), output.get("active_seconds")
    if isinstance(wall, (int, float)) and isinstance(active, (int, float)) and active > 0 and wall >= active:
        output["wall_active_ratio"] = wall / active
    classes: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            classes = []
            break
        try:
            classes.append(_event_class(event.get("event_class", event.get("class"))))
        except RetroReject:
            classes = []
            break
    if len(classes) == len(events):
        output["intervention_free"] = "operator-intervention" not in classes
    return output


def derive_class_metrics(events: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[dict]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            event_class = _event_class(event.get("event_class", event.get("class")))
        except RetroReject:
            continue
        groups.setdefault(event_class, []).append(event)
    output = {event_class: derive_metrics(group) for event_class, group in groups.items()}
    # A class ratio represents elapsed time against one completed class phase,
    # rather than an unrelated cross-class aggregate.  Keep only derivable data.
    for event_class, group in groups.items():
        metric = output[event_class]
        wall = metric.get("wall_seconds")
        durations = [_duration(event.get("duration_seconds")) for event in group]
        if isinstance(wall, (int, float)) and len(group) > 1 and all(value is not None for value in durations):
            active_phase = min(value for value in durations if value is not None)
            if active_phase > 0 and wall >= active_phase:
                metric["wall_active_ratio"] = wall / active_phase
            else:
                metric.pop("wall_active_ratio", None)
    return output


def normalize_ffs_minor(changelog_text: str | None) -> str:
    if not isinstance(changelog_text, str):
        return "0.0"
    for line in changelog_text.splitlines():
        match = RELEASE_RE.match(line)
        if match:
            values = [value for value in match.groups() if value is not None]
            return f"{values[0]}.{values[1]}"
    return "0.0"


def ensure_cache_dir(path: Path) -> None:
    """Create a private cache directory without traversing symlink components."""
    target = Path(path)
    current = Path(target.anchor) if target.is_absolute() else Path(".")
    for component in target.parts[1:] if target.is_absolute() else target.parts:
        current = current / component
        created = False
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
            created = True
        # macOS exposes /var as a system compatibility symlink to /private/var.
        # It is outside the caller-controlled cache path, unlike every later
        # component (notably ~/.cache), which remains fail-closed.
        system_alias = current == Path("/var")
        if (stat.S_ISLNK(info.st_mode) and not system_alias) or not stat.S_ISDIR(info.st_mode):
            _reject("unsafe-state")
        # System ancestors (for example /private on macOS) are not ours to
        # chmod; every directory created or owned by this user is private.
        if created or current == target:
            os.chmod(current, 0o700)


def load_state_file(path: Path, empty: object) -> tuple[object, str | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return empty, None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        return empty, "RETRO:unsafe-state"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError, json.JSONDecodeError):
        return empty, "RETRO:invalid-state"


def write_state_atomic(path: Path, payload: bytes) -> None:
    if not isinstance(payload, bytes):
        _reject("invalid-state")
    ensure_cache_dir(path.parent)
    if os.path.lexists(path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            _reject("unsafe-state")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _load_findings(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        _reject("invalid-findings")
    if isinstance(parsed, dict):
        parsed = parsed.get("findings", [])
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        _reject("invalid-findings")
    return parsed


def _minor_from_changelog(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "0.0"
    return normalize_ffs_minor(content)


def _with_version(events: list[dict], ffs_minor: str) -> list[dict]:
    result: list[dict] = []
    for event in events:
        row = dict(event)
        row["ffs_minor"] = ffs_minor
        result.append(row)
    return result


def _owner_identity() -> tuple[str, ...]:
    """Resolve only enough remote data to reject it; never serialise it."""
    try:
        remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"], check=False,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ()
    match = re.search(r"(?:github\.com[:/])([^/]+)/([^/.]+)(?:\.git)?$", remote)
    return tuple(match.groups()) if match else ()


def _run_cli(args: argparse.Namespace) -> int:
    raw_events = parse_digest(Path(args.digest))
    events = _with_version(raw_events, _minor_from_changelog(Path(args.changelog)))
    if not events:
        print("RETRO:no-events")
        return 0
    payload = collect_payload(events, _load_findings(Path(args.findings)))
    metrics = derive_metrics(raw_events)
    class_metrics = derive_class_metrics(raw_events)
    if metrics:
        payload["metrics"] = metrics
    for finding in payload["findings"]:
        finding["priority"] = classify_priority(finding, class_metrics)
        finding["fingerprint"] = stable_fingerprint(finding)
    accepted = validate_payload(payload, _owner_identity(), class_metrics)
    if args.command == "collect":
        sys.stdout.buffer.write(canonical_json(accepted) + b"\n")
        return 0
    scanner = Path(args.scanner)
    if not scanner.is_file() or not os.access(scanner, os.X_OK):
        _reject("scanner-unavailable")
    state_root = Path(args.state_root)
    with secure_payload_copy(accepted, state_root) as handoff:
        scanned = subprocess.run([str(scanner), str(handoff)], check=False)
        if scanned.returncode != 0:
            _reject("scanner-rejected")
        for finding in accepted["findings"]:
            record_ledger_entry(state_root / "retro-ledger.jsonl", {
                "status": "accepted", "fingerprint": finding["fingerprint"],
                "priority": finding["priority"],
            })
        # The scanner saw exactly these bytes; only then publish successful output.
        sys.stdout.buffer.write(canonical_json(accepted) + b"\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", choices=("collect", "analyze"))
    parser.add_argument("--digest", required=True)
    parser.add_argument("--findings", required=True)
    parser.add_argument("--changelog", required=True)
    parser.add_argument("--state-root")
    parser.add_argument("--scanner")
    args = parser.parse_args(argv)
    if args.command == "analyze" and (not args.state_root or not args.scanner):
        _reject("invalid-command")
    try:
        return _run_cli(args)
    except RetroReject as exc:
        print(exc.code, file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print("RETRO:local-error", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
