#!/usr/bin/env bash
# digest.sh — G12 observability digest (spec-008 REQ-701/702/703).
#
# Modes:
#   --immediate        cursor-idempotent per-class event emission; exit 0 always
#   --daily            summary fields; degraded fields render `unavailable`; exit 0
#   --record-baseline  operator step: record the drift baseline from gh
#                      (interactive command — fails NONZERO loudly on gh failure)
#
# READ-ONLY on the evidence store and run-state DB (T-04-06). The only files
# written live beside the resolved store: digest-cursor.json and
# drift-baseline.json (atomic replace — crash-safe advance).
#
# Cursor keys are tie-safe POSITIONS, never timestamp comparisons (wall
# a376e0b4, amended OQ-4): append-only evidence lists use {count, last_fp}
# (sha256 of the last emitted row; mismatch => rewritten history => re-init to
# end-of-list + one stderr note); run-state uses sqlite rowid; scan-tamper a
# git sha; drift the last-emitted fingerprint. Native timestamps ride along in
# emitted lines for display only.
#
# DIGEST_NOTIFY_CMD (operator-trusted, OQ-5): run via `sh -c` ONCE PER CLASS,
# fed that class's new lines on stdin. Notify failure retains that class's
# cursor (guaranteed redelivery, T-04-08) and never blocks other classes.
# Event lines -> stdout; diagnostics -> stderr. Observability never gates:
# every degraded input exits 0; only usage errors are nonzero.
set -uo pipefail

MODE=""
case "${1:-}" in
  --immediate) MODE=immediate ;;
  --daily) MODE=daily ;;
  --record-baseline) MODE=record-baseline ;;
  *) echo "usage: digest.sh --immediate | --daily | --record-baseline" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATES_PY="$SCRIPT_DIR/../../lib/gates.py"

# Store reads, class scans, notify subprocess, and atomic per-class cursor
# writes all live in ONE embedded read-only python-stdlib block (RESEARCH
# Pattern 3 — gates.py gains nothing here).
DIGEST_MODE="$MODE" DIGEST_GATES_PY="$GATES_PY" python3 - <<'PY'
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

MODE = os.environ["DIGEST_MODE"]
GATES_PY = os.environ.get("DIGEST_GATES_PY", "")
NOTIFY = os.environ.get("DIGEST_NOTIFY_CMD", "")


def note(msg):
    print("digest: %s" % msg, file=sys.stderr)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kw)


def store_path():
    """Mirror gates.py _store_path(): $GATES_STORE wins, else the main
    checkout's .feature-fix-swarm/evidence.json via git-common-dir."""
    env = os.environ.get("GATES_STORE")
    if env:
        return env
    try:
        probe = _run(["git", "rev-parse", "--git-common-dir"])
        if probe.returncode == 0:
            common = probe.stdout.strip()
            if os.path.basename(common) == ".git":
                parent = os.path.dirname(common) or "."
                return os.path.join(parent, ".feature-fix-swarm", "evidence.json")
    except Exception:
        pass
    return os.path.join(".feature-fix-swarm", "evidence.json")


STORE = store_path()
SIDE_DIR = os.path.dirname(os.path.abspath(STORE)) or "."
CURSOR_PATH = os.path.join(SIDE_DIR, "digest-cursor.json")
BASELINE_PATH = os.path.join(SIDE_DIR, "drift-baseline.json")
DB_PATH = os.environ.get(
    "RUN_STATE_DB",
    os.path.join(os.path.expanduser("~"), ".claude", "state", "runs.db"))


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def fp(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def atomic_write_json(path, obj):
    # Deliberately NO makedirs: a missing store dir means there is nothing to
    # observe (e.g. the finalizer just removed a worktree whose GATES_STORE
    # lived inside it) — recreating it here would resurrect removed
    # directories. Callers treat the failure as degraded (cursor retained).
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix=".digest-tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rows_of(value):
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def kv(pairs):
    return " ".join("%s=%s" % (k, v) for k, v in pairs)


def list_slice(cur_entry, rows):
    """Tie-safe positional slice over an append-only list (wall a376e0b4):
    cursor is {count, last_fp}. Returns the new rows, or None on a
    fingerprint mismatch / shrink (rewritten history — caller re-inits to
    end-of-list, no re-emission storm)."""
    count = cur_entry.get("count") if isinstance(cur_entry, dict) else None
    last_fp = cur_entry.get("last_fp") if isinstance(cur_entry, dict) else None
    if not isinstance(count, int) or count < 0:
        count, last_fp = 0, None
    if count > len(rows) or (count > 0 and fp(rows[count - 1]) != last_fp):
        return None
    return rows[count:]


def list_cursor_value(rows):
    if not rows:
        return {"count": 0, "last_fp": None}
    return {"count": len(rows), "last_fp": fp(rows[-1])}


class Emitter:
    """Per-class flow: stdout -> optional notify -> atomic cursor advance.
    Cursors advance one class at a time (Pitfall 4): a mid-list failure
    leaves earlier classes' advances intact."""

    def __init__(self):
        self.cursor = load_json(CURSOR_PATH)
        self.emitted_any = False

    def flush(self, name, lines, new_cursor_value):
        for line in lines:
            print(line)
        if lines:
            self.emitted_any = True
            if NOTIFY:
                try:
                    proc = subprocess.run(["sh", "-c", NOTIFY],
                                          input="\n".join(lines) + "\n",
                                          text=True, timeout=60)
                    rc = proc.returncode
                except Exception as exc:
                    note("notify errored for %s (%s) — cursor retained" % (name, exc))
                    return
                if rc != 0:
                    note("notify failed for %s (rc=%s) — cursor retained" % (name, rc))
                    return
        if new_cursor_value is None or self.cursor.get(name) == new_cursor_value:
            return
        self.cursor[name] = new_cursor_value
        try:
            atomic_write_json(CURSOR_PATH, self.cursor)
        except Exception as exc:
            note("cursor write failed for %s (%s) — class will redeliver" % (name, exc))


def evidence_list_class(em, name, rows, line_fn):
    new = list_slice(em.cursor.get(name), rows)
    if new is None:
        note("%s cursor fingerprint mismatch — re-initialized to end of list" % name)
        em.flush(name, [], list_cursor_value(rows))
        return
    em.flush(name, [line_fn(r) for r in new], list_cursor_value(rows))


def degradation_ns(ev):
    ns = ev.get("_degradation")
    return ns if isinstance(ns, dict) else {}


def events_list_class(em, name, events, line_fn):
    """A kind-filtered class over the shared top-level `events` list. Each
    class keeps its OWN {count, last_fp} position over the full list, so a
    notify failure in one class never stalls the other (Pitfall 4)."""
    new = list_slice(em.cursor.get(name), events)
    if new is None:
        note("%s cursor fingerprint mismatch — re-initialized to end of list" % name)
        em.flush(name, [], list_cursor_value(events))
        return
    em.flush(name, [line_fn(r) for r in new if r.get("kind") == name],
             list_cursor_value(events))


def finisher_line(r):
    pairs = [("run_id", r.get("run_id", "unattributed")), ("pr", r.get("pr", "?"))]
    if r.get("run_id") == "unattributed":
        # lock-trace events carry lock_path/holder_pid instead of a real run
        # — emitted honestly, no invented join (Pitfall 5).
        pairs += [("lock_path", r.get("lock_path", "?")),
                  ("holder_pid", r.get("holder_pid", "?"))]
    return "finisher-skipped " + kv(pairs + [("ts", r.get("ts", "?"))])


def budget_breach_class(em, reverse_map):
    """Run-state `budget_limit_hit` rows; cursor is the last-emitted sqlite
    rowid (amended OQ-4) — positional and tie-safe by construction."""
    cur = em.cursor.get("budget-breach")
    last_id = cur if isinstance(cur, int) else 0
    if not os.path.exists(DB_PATH):
        return  # degraded input: silent skip, cursor retained
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT id, run_id, payload_json, created_at FROM events "
                "WHERE event_type = 'budget_limit_hit' AND id > ? ORDER BY id",
                (last_id,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        note("run-state db unreadable (%s) — budget-breach cursor retained" % exc)
        return
    lines, max_id = [], last_id
    for rowid, runstore_id, payload_json, created_at in rows:
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except ValueError:
            payload = {}
        ledger = reverse_map.get(runstore_id)
        # REQ-703: the ledger id is canonical; an unmapped row emits with its
        # native runstore id labeled as such — honest, never guessed.
        pairs = [("run_id", ledger if ledger else "unmapped"),
                 ("runstore_id", runstore_id),
                 ("tokens_used", payload.get("tokens_used", "?")),
                 ("tokens_budget", payload.get("tokens_budget", "?")),
                 ("created_at", created_at)]
        lines.append("budget-breach " + kv(pairs))
        max_id = rowid
    em.flush("budget-breach", lines, max_id)


def tripped_rung_class(em, ev):
    """A rung is tripped iff its trailing-20 window is all-fail (the same
    namespace shape gates.py rung_status reads). The cursor is a sha256
    fingerprint of the WINDOW CONTENT per tripped rung — never a
    recorded_at comparison (wall ba54308a): two events sharing a timestamp
    can neither drop nor double-deliver an emission by construction.
    recorded_at rides in the line for display only. An untripped rung is
    dropped from the cursor so a reset-then-retrip emits again."""
    rungs = degradation_ns(ev).get("rungs")
    rungs = rungs if isinstance(rungs, dict) else {}
    cur = em.cursor.get("tripped-rung")
    cur = cur if isinstance(cur, dict) else {}
    lines, new_cur = [], {}
    for rung_id in sorted(rungs):
        entry = rungs.get(rung_id)
        events = rows_of(entry.get("events")) if isinstance(entry, dict) else []
        if not (len(events) == 20
                and all(e.get("outcome") == "fail" for e in events)):
            continue
        window_fp = fp(events)
        new_cur[rung_id] = window_fp
        if cur.get(rung_id) != window_fp:
            lines.append("tripped-rung " + kv([
                ("rung", rung_id), ("attempts", 20),
                ("recorded_at", events[-1].get("recorded_at", "?"))]))
    em.flush("tripped-rung", lines, new_cur)


def promotion_class(em, ev):
    promos = ev.get("_promotions")
    promos = promos if isinstance(promos, dict) else {}
    cur = em.cursor.get("promotion")
    cur = cur if isinstance(cur, dict) else {}
    lines, new_cur = [], {}
    for run_id in sorted(promos):
        rows = rows_of(promos.get(run_id))
        new = list_slice(cur.get(run_id), rows)
        if new is None:
            note("promotion cursor fingerprint mismatch for %s — re-initialized" % run_id)
            new = []
        for r in new:
            # `artifact` (commit sha / digest ref) is the gh join key (REQ-703)
            lines.append("promotion " + kv([
                ("run_id", run_id),
                ("from", r.get("from_env", "?")), ("to", r.get("to_env", "?")),
                ("surface", r.get("surface", "?")),
                ("artifact", r.get("artifact", "?")),
                ("recorded_at", r.get("recorded_at", "?"))]))
        new_cur[run_id] = list_cursor_value(rows)
    em.flush("promotion", lines, new_cur)


def git(*args_):
    return _run(["git"] + list(args_))


def scan_tamper_class(em):
    """Decision 2 (OQ-2): cursor is the last-scanned commit sha. Absent =>
    initialize to current HEAD and emit nothing — no unbounded history scan
    (documented in docs/digest.md). Present => scan each newer first-parent
    commit's diff through `gates.py scan-tamper`; findings emit one line per
    sha; the cursor advances through processed shas."""
    head = git("rev-parse", "HEAD")
    if head.returncode != 0:
        note("scan-tamper: no git HEAD — skipped")
        return
    head_sha = head.stdout.strip()
    cur = em.cursor.get("scan-tamper")
    if not isinstance(cur, str) or not cur:
        em.flush("scan-tamper", [], head_sha)
        return
    if not GATES_PY or not os.path.exists(GATES_PY):
        note("scan-tamper: gates.py not found — cursor retained")
        return
    lst = git("rev-list", "--reverse", "--first-parent", "%s..HEAD" % cur)
    if lst.returncode != 0:
        note("scan-tamper: cursor %s unresolvable — re-initialized to HEAD" % cur[:12])
        em.flush("scan-tamper", [], head_sha)
        return
    lines, last = [], cur
    for sha in lst.stdout.split():
        show = git("show", sha, "--format=")
        if show.returncode != 0:
            note("scan-tamper: git show %s failed — stopping at last scanned sha" % sha[:12])
            break
        scan = _run([sys.executable, GATES_PY, "scan-tamper"], input=show.stdout)
        if scan.returncode not in (0, 1):
            note("scan-tamper: scanner failed on %s — stopping at last scanned sha" % sha[:12])
            break
        findings = [l for l in scan.stdout.splitlines() if l.strip()]
        if scan.returncode == 1 and findings:
            lines.append("scan-tamper " + kv([("sha", sha), ("findings", len(findings))]))
        last = sha
    em.flush("scan-tamper", lines, last)


def drift_class(em):
    """Decision 3 (OQ-3): drift = current gh state vs the recorded baseline.
    Baseline absent => stderr note only; gh unreachable => stderr note,
    cursor retained, exit 0. The cursor stores the last-EMITTED fingerprint
    so unchanged drift never re-emits."""
    if not os.path.exists(BASELINE_PATH):
        note("drift: no baseline recorded — run digest.sh --record-baseline")
        return
    base = load_json(BASELINE_PATH)
    state = gh_reads()
    if state is None:
        note("drift: gh unreachable — cursor retained")
        return
    base_fp = fp({"protection": base.get("protection"),
                  "workflows": base.get("workflows")})
    state_fp = fp(state)
    if state_fp == base_fp or em.cursor.get("drift") == state_fp:
        return
    em.flush("drift", ["drift " + kv([("fingerprint", state_fp[:12]),
                                      ("baseline", base_fp[:12])])], state_fp)


def immediate():
    ev = load_json(STORE)
    em = Emitter()
    mappings = degradation_ns(ev).get("mappings")
    mappings = mappings if isinstance(mappings, dict) else {}
    # REQ-703 reverse lookup runstore_id -> ledger_run_id (RESEARCH Pattern 3)
    reverse_map = {v: k for k, v in mappings.items()
                   if isinstance(k, str) and isinstance(v, str)}

    # class: waiver — append-only `waivers` list; `unattributed` rows emit
    # with that literal label, never a fabricated join (Pitfall 5).
    evidence_list_class(
        em, "waiver", rows_of(ev.get("waivers")),
        lambda r: "waiver " + kv([
            ("run_id", r.get("run_id", "unattributed")),
            ("gate", r.get("gate", "?")),
            ("env_var", r.get("env_var", "?")),
            ("ts", r.get("ts", "?")),
        ]))

    tripped_rung_class(em, ev)

    shared_events = rows_of(ev.get("events"))
    events_list_class(
        em, "loop-cap", shared_events,
        lambda r: "loop-cap " + kv([
            ("run_id", r.get("run_id", "?")), ("loop", r.get("loop", "?")),
            ("round", r.get("round", "?")), ("ts", r.get("ts", "?"))]))
    events_list_class(em, "finisher-skipped", shared_events, finisher_line)

    budget_breach_class(em, reverse_map)
    promotion_class(em, ev)

    evidence_list_class(
        em, "rollback-dryrun", rows_of(ev.get("rollback_dryrun")),
        lambda r: "rollback-dryrun " + kv([
            ("run_id", r.get("run_id", "?")), ("surface", r.get("surface", "?")),
            ("exit_code", r.get("exit_code", "?")),
            ("artifact_sha", r.get("artifact_sha", "?")),
            ("ts", r.get("ts", "?"))]))

    scan_tamper_class(em)
    drift_class(em)

    if not em.emitted_any:
        print("no events")


def daily():
    """REQ-702: seven labeled summary fields, cursor-free (a summary, not an
    event stream). Every degraded source renders `unavailable`; merges,
    branch and worktree fields come from LOCAL git only."""
    print("digest daily")
    ev = load_json(STORE)

    # specs completed/quarantined (decision 5: completed counts run-state
    # `complete` rows; quarantined derives from distinct budget_limit_hit
    # run ids plus .planning/run-state/gsd-run.status)
    completed = quarantined = used = budget = None
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH)
            try:
                completed = conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE state = 'complete'").fetchone()[0]
                quarantined = conn.execute(
                    "SELECT COUNT(DISTINCT run_id) FROM events "
                    "WHERE event_type = 'budget_limit_hit'").fetchone()[0]
                used = conn.execute(
                    "SELECT COALESCE(SUM(tokens_used), 0) FROM runs").fetchone()[0]
                budget = conn.execute(
                    "SELECT COALESCE(SUM(tokens_budget), 0) FROM runs").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            completed = quarantined = used = budget = None
    try:
        with open(os.path.join(".planning", "run-state", "gsd-run.status"),
                  encoding="utf-8") as fh:
            if "quarantined" in fh.read():
                quarantined = (quarantined or 0) + 1
    except OSError:
        pass
    if completed is None:
        print("specs: unavailable")
    else:
        print("specs: completed=%s quarantined=%s" % (completed, quarantined or 0))

    m = git("log", "--merges", "--format=%h", "-n", "20")
    if m.returncode == 0:
        shas = m.stdout.split()
        print("merges: %d%s" % (len(shas),
                                " (%s)" % " ".join(shas) if shas else ""))
    else:
        print("merges: unavailable")

    inv = degradation_ns(ev).get("invocations")
    inv = inv if isinstance(inv, list) else []
    degraded = sum(1 for e in inv if isinstance(e, dict) and e.get("degraded"))
    print("degraded-review: %d/%d" % (degraded, len(inv)))

    if used is None:
        print("tokens: unavailable")
    else:
        print("tokens: used=%s budget=%s" % (used, budget))

    auto = ev.get("_autonomy")
    auto = auto if isinstance(auto, dict) else {}
    pend = sum(len(entry["pending"]) for entry in auto.values()
               if isinstance(entry, dict) and isinstance(entry.get("pending"), list))
    print("pending: %d" % pend)

    b = git("branch", "--format=%(refname:short)")
    w = git("worktree", "list")
    if b.returncode == 0 and w.returncode == 0:
        branches = len([x for x in b.stdout.splitlines() if x.strip()])
        worktrees = len([x for x in w.stdout.splitlines() if x.strip()])
        print("stranded: branches=%d worktrees=%d"
              % (max(branches - 1, 0), max(worktrees - 1, 0)))
    else:
        print("stranded: unavailable")

    # the one gh-backed field: oldest unmerged PR age
    line = "oldest-pr: unavailable"
    try:
        pr = _run(["gh", "pr", "list", "--state", "open", "--json", "createdAt"])
        if pr.returncode == 0:
            items = json.loads(pr.stdout)
            dates = sorted(i.get("createdAt", "") for i in items
                           if isinstance(i, dict) and i.get("createdAt"))
            if not dates:
                line = "oldest-pr: none"
            else:
                from datetime import datetime, timezone
                try:
                    dt = datetime.strptime(dates[0], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc)
                    days = int((datetime.now(timezone.utc) - dt).total_seconds() // 86400)
                    line = "oldest-pr: %dd" % days
                except ValueError:
                    line = "oldest-pr: %s" % dates[0]
    except Exception:
        pass
    print(line)


def gh_reads():
    """The two drift signals (locked row 17): branch-protection JSON and the
    workflows contents listing. Returns None if gh fails (degraded)."""
    try:
        p1 = _run(["gh", "api", "repos/{owner}/{repo}/branches/main/protection"])
        p2 = _run(["gh", "api", "repos/{owner}/{repo}/contents/.github/workflows"])
    except Exception:
        return None
    if p1.returncode != 0 or p2.returncode != 0:
        return None
    return {"protection": p1.stdout, "workflows": p2.stdout}


def record_baseline():
    state = gh_reads()
    if state is None:
        note("record-baseline: gh failed — baseline NOT written")
        return 1
    try:
        # the one interactive/operator mode: creating the store dir is fine here
        os.makedirs(SIDE_DIR, exist_ok=True)
        atomic_write_json(BASELINE_PATH, state)
    except OSError as exc:
        note("record-baseline: write failed (%s)" % exc)
        return 1
    print("baseline recorded: %s" % BASELINE_PATH)
    return 0


if MODE == "record-baseline":
    sys.exit(record_baseline())
try:
    if MODE == "daily":
        daily()
    else:
        immediate()
except Exception as exc:  # observability never gates (REQ-701)
    note("degraded (%s) — exit 0" % exc)
sys.exit(0)
PY
