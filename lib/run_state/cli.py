"""CLI: python -m run_state.cli <command> ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from run_state.marker import MarkerFile
from run_state.state import RunStore, DEFAULT_DB, VALID_STATES


def _store() -> RunStore:
    db = Path(os.environ.get("RUN_STATE_DB", str(DEFAULT_DB)))
    return RunStore(db)


def _marker() -> MarkerFile:
    p = os.environ.get("RUN_STATE_MARKER")
    return MarkerFile(Path(p)) if p else MarkerFile()


def cmd_start(args: argparse.Namespace) -> int:
    store = _store()
    run_id = store.create_run(
        skill=args.skill,
        objective=args.objective,
        session_id=args.session_id,
        tokens_budget=args.tokens,
        worktree=args.worktree,
    )
    _marker().set(run_id)
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
        "continuation_count": run.continuation_count,
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
    _marker().clear()
    return 0


def cmd_abort(args: argparse.Namespace) -> int:
    _store().update_state(args.run_id, "aborted")
    _marker().clear()
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    _store().update_state(args.run_id, "paused")
    _marker().clear()
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    store = _store()
    run = store.get_run(args.run_id)
    if run is None:
        print(json.dumps({"error": "not_found"}), file=sys.stderr)
        return 1
    if run.state not in ("paused", "budget_limited"):
        print(json.dumps({"error": "invalid_state", "state": run.state}), file=sys.stderr)
        return 1
    store.update_state(args.run_id, "active")
    _marker().set(args.run_id)
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
    s.add_argument("--tokens", type=int, default=None)
    s.add_argument("--worktree", default=None)
    s.add_argument("--session-id", default=None)
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("status")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("update")
    s.add_argument("run_id")
    s.add_argument("--phase", default=None)
    s.add_argument("--tokens", type=int, default=None, help="delta to add to tokens_used")
    s.add_argument("--state", default=None, choices=list(VALID_STATES))
    s.set_defaults(func=cmd_update)

    for name, fn in (("complete", cmd_complete), ("abort", cmd_abort),
                     ("pause", cmd_pause), ("resume", cmd_resume)):
        s = sub.add_parser(name)
        s.add_argument("run_id")
        s.set_defaults(func=fn)

    s = sub.add_parser("list")
    s.add_argument("--state", default=None, choices=list(VALID_STATES))
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("audit")
    s.add_argument("run_id")
    s.add_argument("--kind", required=True, choices=["fix", "feature"])
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

    if result.verdict == "pass":
        store.update_state(args.run_id, "complete")
        _marker().clear()
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
