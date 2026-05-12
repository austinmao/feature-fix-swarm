"""CLI: python -m run_state.cli <command> ..."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from run_state.state import RunStore, DEFAULT_DB, VALID_STATES


def _parse_tokens(value):
    """Parse '250K' / '1.5M' / '1B' / '2T' / '250000' to int.

    Accepts case-insensitive suffix. Returns int, or raises
    argparse.ArgumentTypeError on invalid input.
    """
    if value is None:
        return None
    s = str(value).strip().upper()
    m = re.match(r'^(\d+(?:\.\d+)?)\s*([KMBT]?)$', s)
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid token count: {value!r} (expected '250000' or '250K'/'1.5M'/'1B'/'2T')"
        )
    num, suffix = m.group(1), m.group(2)
    multipliers = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
        "T": 1_000_000_000_000,
    }
    return int(float(num) * multipliers[suffix])


def _store() -> RunStore:
    db = Path(os.environ.get("RUN_STATE_DB", str(DEFAULT_DB)))
    return RunStore(db)


def cmd_start(args: argparse.Namespace) -> int:
    store = _store()
    run_id = store.create_run(
        skill=args.skill,
        objective=args.objective,
        session_id=args.session_id,
        tokens_budget=args.tokens,
        worktree=args.worktree,
    )
    print(json.dumps({"run_id": run_id}))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run = _store().get_run(args.run_id)
    if run is None:
        print(json.dumps({"error": "not_found", "run_id": args.run_id}), file=sys.stderr)
        return 1
    print(json.dumps({
        "run_id": run.id,
        "skill": run.skill,
        "state": run.state,
        "phase": run.current_phase,
        "objective": run.objective,
        "tokens_used": run.tokens_used,
        "tokens_budget": run.tokens_budget,
        "audit_attempts": run.audit_attempts,
        "last_audit_verdict": run.last_audit_verdict,
    }))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    store = _store()
    if args.phase:
        store.update_phase(args.run_id, args.phase)
    if args.tokens is not None:
        store.inc_tokens(args.run_id, args.tokens)
    if args.state:
        store.update_state(args.run_id, args.state)
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    _store().update_state(args.run_id, "complete")
    return 0


def cmd_abort(args: argparse.Namespace) -> int:
    _store().update_state(args.run_id, "aborted")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    runs = _store().list_runs(state=args.state)
    payload = [
        {"run_id": r.id, "skill": r.skill, "state": r.state,
         "objective": r.objective[:80], "created_at": r.created_at}
        for r in runs
    ]
    print(json.dumps(payload, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="run-state")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("--skill", required=True, choices=["feature", "fix"])
    s.add_argument("--objective", required=True)
    s.add_argument("--tokens", type=_parse_tokens, default=None)
    s.add_argument("--worktree", default=None)
    s.add_argument("--session-id", default=None)
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("status")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("update")
    s.add_argument("run_id")
    s.add_argument("--phase", default=None)
    s.add_argument("--tokens", type=_parse_tokens, default=None, help="delta to add to tokens_used (accepts K/M/B/T suffix)")
    s.add_argument("--state", default=None, choices=list(VALID_STATES))
    s.set_defaults(func=cmd_update)

    for name, fn in (("complete", cmd_complete), ("abort", cmd_abort)):
        s = sub.add_parser(name)
        s.add_argument("run_id")
        s.set_defaults(func=fn)

    s = sub.add_parser("list")
    s.add_argument("--state", default=None, choices=list(VALID_STATES))
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("audit")
    s.add_argument("run_id")
    s.add_argument("--kind", required=True, choices=["fix", "feature", "phase"])
    s.add_argument("--context", action="append", help="KEY=VALUE for prompt substitution; repeat as needed")
    s.add_argument("--cwd", default=None)
    s.set_defaults(func=cmd_audit)

    args = p.parse_args(argv)
    return args.func(args)

def cmd_audit(args: argparse.Namespace) -> int:
    """Run adversarial audit. Updates run state based on verdict."""
    from run_state.audit import run_audit
    import sqlite3

    prompt_dir = Path(__file__).resolve().parent / "prompts"
    template_path = prompt_dir / f"{args.kind}_audit.txt"
    if not template_path.exists():
        print(json.dumps({"error": "unknown_kind", "kind": args.kind}), file=sys.stderr)
        return 1
    prompt = template_path.read_text(encoding="utf-8")
    for kv in args.context or []:
        if "=" not in kv:
            print(json.dumps({"error": "bad_context", "value": kv}), file=sys.stderr)
            return 1
        k, v = kv.split("=", 1)
        prompt = prompt.replace("{{" + k + "}}", v)

    cwd = Path(args.cwd or os.getcwd())
    store = _store()
    store.update_state(args.run_id, "pending_audit")

    result = run_audit(prompt=prompt, cwd=cwd)

    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "UPDATE runs SET audit_attempts = audit_attempts + 1, last_audit_verdict = ? WHERE id = ?",
            (result.verdict, args.run_id),
        )
        conn.execute(
            "INSERT INTO events (run_id, event_type, payload_json, created_at) VALUES (?, 'audit', ?, datetime('now'))",
            (args.run_id, json.dumps({"verdict": result.verdict, "reasoning": result.reasoning, "missing": result.missing})),
        )
        conn.commit()
    finally:
        conn.close()

    # v3.0 codex-gate Pass 1 P2 fix: also append to ~/.claude/state/audits.jsonl
    # so native `/goal` condition checker can grep audit history without
    # needing to open SQLite. One line per audit; append-only; never rewritten.
    audits_log = Path.home() / ".claude" / "state" / "audits.jsonl"
    try:
        audits_log.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_id": args.run_id,
            "kind": args.kind,
            "verdict": result.verdict,
            "reasoning": result.reasoning[:500],
            "missing": result.missing,
        }
        with audits_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        # Best-effort log; SQLite remains source of truth.
        pass

    if result.verdict == "pass":
        # v3.0: native /goal handles continuation; no marker to manage.
        # kind=fix → terminal complete. kind=phase / kind=feature stay
        # active so the caller (the skill) advances to the next wedge or
        # final canary stage.
        if args.kind == "fix":
            store.update_state(args.run_id, "complete")
        elif args.kind == "phase":
            # Per-phase audit pass: phase done, but feature pipeline still
            # running. Stay active; caller advances to next wedge.
            store.update_state(args.run_id, "active")
        else:  # feature — keep run alive for canary
            store.update_state(args.run_id, "active")
    else:
        store.update_state(args.run_id, "active")

    print(json.dumps({
        "run_id": args.run_id,
        "verdict": result.verdict,
        "reasoning": result.reasoning,
        "missing": result.missing,
    }))
    return 0 if result.verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
