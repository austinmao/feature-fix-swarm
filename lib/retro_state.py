#!/usr/bin/env python3
"""Private, fail-closed state and filing transaction for ffs-retro.

This module deliberately has no repository or command injection surface.  The
only public write target is the tracker constant below and the only child it
starts is the fixed ``gh`` program found through PATH.
"""
from __future__ import annotations

import argparse
import difflib
import fcntl
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

REPOSITORY = "austinmao/feature-fix-swarm"
CONSENT_MAJOR = "1"
META_RE = re.compile(r"<!-- ffs-retro fingerprint:([0-9a-f]{16}) priority:(P[0-3]) occurrences:([0-9]+) -->")


class RetroStateError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code.startswith("RETRO:") else f"RETRO:{code}"
        super().__init__(self.code)


@dataclass(frozen=True)
class ConsentDecision:
    state: str
    granted: bool = False
    asked_at: str | None = None


@dataclass(frozen=True)
class Match:
    number: int
    exact: bool


@dataclass(frozen=True)
class FilingOutcome:
    code: str
    fatal: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _root() -> Path:
    return Path.home() / ".cache" / "feature-fix-swarm"


def _paths() -> tuple[Path, Path, Path]:
    root = _root()
    return root / "consent.json", root / "retro-ledger.jsonl", root / "retro.lock"


def _check_dir(path: Path, create: bool) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.lstat()
    except OSError as exc:
        raise RetroStateError("unsafe-state") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RetroStateError("unsafe-state")


def _check_regular(path: Path, *, missing_ok: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise RetroStateError("unsafe-state")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RetroStateError("unsafe-state")
    return True


class RetroLock:
    """A stable lock inode; never lock a replaceable state file."""
    def __init__(self, path: Path) -> None:
        self.path, self.fd = path, None

    def __enter__(self) -> "RetroLock":
        _check_dir(self.path.parent, True)
        _check_regular(self.path, missing_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self.fd = os.open(self.path, flags, 0o600)
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise RetroStateError("unsafe-state")
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self
        except Exception:
            if self.fd is not None:
                os.close(self.fd)
            raise

    def __exit__(self, *_: object) -> None:
        assert self.fd is not None
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _decision(path: Path) -> ConsentDecision:
    try:
        exists = _check_regular(path, missing_ok=True)
    except RetroStateError:
        return ConsentDecision("unsafe")
    if not exists:
        return ConsentDecision("absent")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if set(value) != {"granted", "asked_at", "version"} or not isinstance(value["granted"], bool) or not isinstance(value["asked_at"], str) or not isinstance(value["version"], str):
            raise ValueError
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ConsentDecision("corrupt")
    if value["version"] != CONSENT_MAJOR:
        return ConsentDecision("major-mismatch")
    return ConsentDecision("granted" if value["granted"] else "revoked", value["granted"], value["asked_at"])


def load_consent(path: Path) -> ConsentDecision:
    return _decision(path)


def _atomic(path: Path, raw: bytes) -> None:
    _check_dir(path.parent, True)
    _check_regular(path, missing_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try: tmp.unlink()
        except FileNotFoundError: pass


def write_consent(path: Path, *, granted: bool, asked_at: str, version: str) -> None:
    if not isinstance(granted, bool) or not isinstance(asked_at, str) or version != CONSENT_MAJOR:
        raise RetroStateError("invalid-consent")
    _atomic(path, json.dumps({"granted": granted, "asked_at": asked_at, "version": version}, sort_keys=True, separators=(",", ":")).encode())


def _ledger(path: Path) -> list[dict]:
    if not _check_regular(path, missing_ok=True):
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RetroStateError("invalid-ledger") from exc
    if not all(isinstance(row, dict) for row in rows):
        raise RetroStateError("invalid-ledger")
    return rows


def _save_ledger(path: Path, rows: list[dict]) -> None:
    raw = b"".join(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n" for row in rows)
    _atomic(path, raw)


def append_ledger(path: Path, row: dict) -> None:
    # Caller owns RetroLock for transaction paths; this helper remains safe for
    # the narrow auth-failure command too.
    rows = _ledger(path); rows.append(row); _save_ledger(path, rows)


def title_similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right).ratio()


def _issues(raw: object) -> list[dict]:
    if not isinstance(raw, list): return []
    result = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("number"), int) and item["number"] > 0 and isinstance(item.get("title"), str) and isinstance(item.get("body", ""), str):
            result.append(item)
    return result


def select_issue_match(issues: list[dict], *, fingerprint: str, title: str, threshold: float) -> Match | None:
    exact = []
    for issue in issues:
        meta = META_RE.search(issue.get("body", ""))
        if meta and meta.group(1) == fingerprint:
            exact.append(issue["number"])
    if exact: return Match(min(exact), True)
    similar = [issue["number"] for issue in issues if title_similarity(title, issue["title"]) >= threshold]
    return Match(min(similar), False) if similar else None


def _gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    # Bounded: every gh call runs while the exclusive RetroLock is held, so a
    # hung transport must fail typed instead of wedging every other filer.
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, check=False, timeout=60)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["gh", *args], 124, "", "gh-timeout")


def _query(fingerprint: str, title: str, search: bool = False) -> list[dict]:
    args = ["issue", "list", "--repo", REPOSITORY, "--state", "all", "--limit", "200", "--json", "number,title,body"]
    if search: args.extend(["--search", f"ffs-retro fingerprint:{fingerprint}"])
    response = _gh(args)
    if response.returncode != 0: return []
    try: return _issues(json.loads(response.stdout))
    except (ValueError, json.JSONDecodeError): return []


def _body(finding: dict, occurrences: int, comment: bool = False) -> tuple[str, str]:
    fp, priority = finding["fingerprint"], finding["priority"]
    title = f"FFS retro {priority}: {finding['script']} {finding['event_class']}"
    marker = f"<!-- ffs-retro fingerprint:{fp} priority:{priority} occurrences:{occurrences} -->"
    text = ("Additional occurrence recorded by ffs-retro.\n\n" if comment else "Automatically filed by ffs-retro.\n\n") + marker + "\n"
    return title, text


def _write(args: list[str], text: str) -> subprocess.CompletedProcess[str]:
    fd, name = tempfile.mkstemp(prefix=".ffs-retro-body-", suffix=".md")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text); handle.flush(); os.fsync(handle.fileno())
        return _gh([*args, "--repo", REPOSITORY, "--body-file", name])
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass


def _pace(rows: list[dict]) -> None:
    stamps = [row.get("paced_at") for row in rows if isinstance(row.get("paced_at"), (int, float))]
    if stamps:
        time.sleep(max(0.0, 2.0 - (time.time() - max(stamps))))


def _row(action: str, finding: dict | None = None, **extra: object) -> dict:
    row: dict = {"ts": _now(), "run_id": os.environ.get("GSD_RUN_ID", "local"), "action": action, "outcome": extra.pop("outcome", "ok"), "status": extra.pop("status", "recorded")}
    if finding:
        row.update({key: finding[key] for key in ("fingerprint", "priority")})
    row.update(extra)
    return row


def _file_one(rows: list[dict], finding: dict) -> FilingOutcome:
    fp = finding["fingerprint"]
    occurrences = 1 + sum(1 for row in rows if row.get("fingerprint") == fp and row.get("action") in {"accrue", "create", "comment", "intent"})
    creates = [row for row in rows if row.get("action") == "create" and row.get("fingerprint") == fp and isinstance(row.get("issue_number"), int)]
    title, create_body = _body(finding, occurrences)
    if creates:
        issue = min(row["issue_number"] for row in creates)
        return _comment(rows, finding, issue, occurrences)
    if any(row.get("action") == "intent" and row.get("fingerprint") == fp for row in rows):
        rows.append(_row("defer", finding, outcome="pending-intent", occurrences=occurrences)); return FilingOutcome("pending-intent")
    try: floor = int(os.environ.get("RETRO_P3_OCCURRENCE_FLOOR", "3")); cap = int(os.environ.get("RETRO_MAX_NEW_ISSUES", "3"))
    except ValueError: floor, cap = 3, 3
    floor, cap = max(0, floor), max(0, cap)
    if finding["priority"] == "P3" and occurrences < floor:
        rows.append(_row("accrue", finding, occurrences=occurrences)); return FilingOutcome("accrued")
    try: threshold = float(os.environ.get("RETRO_TITLE_SIM", "0.8"))
    except ValueError: threshold = 0.8
    listed = _query(fp, title)
    match = select_issue_match(listed, fingerprint=fp, title=title, threshold=threshold)
    if not match:
        searched = _query(fp, title, True)
        match = select_issue_match(searched, fingerprint=fp, title=title, threshold=threshold)
    if match: return _comment(rows, finding, match.number, occurrences)
    if sum(1 for row in rows if row.get("action") == "create" and row.get("run_id") == os.environ.get("GSD_RUN_ID", "local")) >= cap:
        rows.append(_row("accrue", finding, outcome="cap", occurrences=occurrences)); return FilingOutcome("cap")
    # A second exact query closes the check/create race while still locked.
    late = select_issue_match(_query(fp, title, True), fingerprint=fp, title=title, threshold=2.0)
    if late: return _comment(rows, finding, late.number, occurrences)
    rows.append(_row("intent", finding, outcome="pre-create", occurrences=occurrences))
    # This is intentionally published before the external create.  A process
    # death after this point must bias toward no duplicate, never another
    # create based on an eventually-consistent search result.
    _save_ledger(_paths()[1], rows)
    _pace(rows)
    response = _write(["issue", "create", "--title", title], create_body)
    if response.returncode == 0:
        match_num = re.search(r"/(\d+)(?:\s*)$", response.stdout)
        number = int(match_num.group(1)) if match_num else 0
        # The create supersedes its published intent; keeping both would
        # inflate the fingerprint's occurrence count by one forever.
        rows[:] = [row for row in rows if not (row.get("action") == "intent" and row.get("fingerprint") == fp)]
        rows.append(_row("create", finding, issue_number=number, occurrences=occurrences, paced_at=time.time())); return FilingOutcome("created")
    status = next((str(code) for code in (403, 404, 422) if str(code) in response.stderr), None)
    # The failed child made no public create, so retain its typed outcome
    # rather than leaving a misleading permanent intent record.
    rows[:] = [row for row in rows if not (row.get("action") == "intent" and row.get("fingerprint") == fp)]
    rows.append(_row("create", finding, outcome=f"gh-{status}" if status else "gh-failure", status="failed", occurrences=occurrences, paced_at=time.time()))
    return FilingOutcome("known-failure" if status else "write-failure", not bool(status))


def _comment(rows: list[dict], finding: dict, issue: int, occurrences: int) -> FilingOutcome:
    _title, body = _body(finding, occurrences, True); _pace(rows)
    response = _write(["issue", "comment", str(issue)], body)
    if response.returncode == 0:
        rows.append(_row("comment", finding, issue_number=issue, occurrences=occurrences, paced_at=time.time())); return FilingOutcome("commented")
    status = next((str(code) for code in (403, 404, 422) if str(code) in response.stderr), None)
    rows.append(_row("comment", finding, outcome=f"gh-{status}" if status else "gh-failure", status="failed", issue_number=issue, occurrences=occurrences, paced_at=time.time()))
    return FilingOutcome("known-failure" if status else "write-failure", not bool(status))


def file_payload(path: Path) -> FilingOutcome:
    consent, ledger, lock = _paths()
    with RetroLock(lock):
        if _decision(consent).state != "granted": return FilingOutcome("no-consent")
        try: rows = _ledger(ledger)
        except RetroStateError: return FilingOutcome("ledger-unavailable")
        try:
            payload = json.loads(path.read_text(encoding="utf-8")); findings = payload["findings"]
            if not isinstance(findings, list): raise ValueError
        except (OSError, ValueError, TypeError, json.JSONDecodeError): return FilingOutcome("invalid-payload", True)
        fatal = False
        for finding in findings:
            if isinstance(finding, dict) and finding.get("priority") in {"P0", "P1", "P2", "P3"}:
                outcome = _file_one(rows, finding); fatal |= outcome.fatal
                _save_ledger(ledger, rows)  # intent is durable before the next candidate/write
        return FilingOutcome("filed", fatal)


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] not in {"check-consent", "consent", "record-auth-failure", "file"}:
        return 2
    verb, arg = argv[0], argv[1] if len(argv) == 2 else None
    if len(argv) > 2:
        return 2
    consent, ledger, lock = _paths()
    if verb == "check-consent": print(f"RETRO:consent-{_decision(consent).state}"); return 0
    if verb == "consent":
        if arg not in {"--grant", "--revoke", "--reset"}: return 2
        with RetroLock(lock):
            current = _decision(consent)
            if arg == "--reset":
                if _check_regular(consent, missing_ok=True): consent.unlink()
            else:
                asked = current.asked_at if arg == "--revoke" and current.asked_at else _now()
                write_consent(consent, granted=arg == "--grant", asked_at=asked, version=CONSENT_MAJOR)
        print("RETRO:consent-updated"); return 0
    with RetroLock(lock):
        if verb == "record-auth-failure":
            try: rows = _ledger(ledger)
            except RetroStateError: return 0
            rows.append(_row("auth", outcome="auth-failure", status="failed")); _save_ledger(ledger, rows); return 0
    if verb == "file" and arg: return 1 if file_payload(Path(arg)).fatal else 0
    return 2


if __name__ == "__main__":
    try: raise SystemExit(_cli(os.sys.argv[1:]))
    except RetroStateError as exc: print(exc.code, file=os.sys.stderr); raise SystemExit(1)
