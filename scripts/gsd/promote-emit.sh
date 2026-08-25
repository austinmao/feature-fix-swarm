#!/usr/bin/env bash
# promote-emit.sh — finish-tail promote EMIT (spec-007 phase 4, REQ-401 Seam 3).
# EMITS — never executes — the staging→prod promote command for each
# `deploy:staging-*` grant of the given run. One fully-formed line per grant
# on stdout, in the VERBATIM lib/gates.py:2885-2897 flag shape. This script
# has NO store-write path and never runs the emitted command (INT-002).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ "$#" -ne 1 ] || [ -z "${1:-}" ]; then
  echo "usage: promote-emit.sh <run_id>" >&2
  exit 1
fi
exec python3 - "$ROOT" "$1" <<'PYEOF'
import re
import sys

ROOT, RUN_ID = sys.argv[1], sys.argv[2]
sys.path.insert(0, ROOT + "/lib")
import gates  # store resolution authority: _store_path() — $GATES_STORE wins

# wall 31a608dc: every interpolated value must match its shape gate BEFORE
# emission and is single-quoted in the emitted command; any non-conforming
# store value → typed value-free refusal, nothing emitted.
_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._:-]{0,127}")


def refuse(field, shape):
    print(f"PROMOTE-EMIT-REFUSED: {field} in the evidence store does not "
          f"match the expected shape ({shape}) — nothing emitted",
          file=sys.stderr)
    sys.exit(1)


if not gates.LEDGER_RUN_ID_PAT.fullmatch(RUN_ID):
    refuse("run id", "spec-NNN | adhoc-<slug> | run-N")

data = gates._load_store(gates._store_path())  # read-only; never _save_store
auto = data.get("_autonomy") if isinstance(data.get("_autonomy"), dict) else {}
grants = auto.get(RUN_ID, {}).get("grants", {})
staging = sorted(a for a in grants
                 if isinstance(a, str) and a.startswith("deploy:staging-"))
if not staging:
    sys.exit(0)  # no staging grant → silent, empty stdout

promos = data.get("_promotions") if isinstance(data.get("_promotions"), dict) else {}
# sig 156cf77a + 1ea31e0b (diff review 2026-08-10 hardening): `_promotions`
# records are the ONLY run+surface binding the store schema has (fields:
# surface/artifact/evidence_ids/recorded_at). Top-level gate records carry
# NO ownership, so there is deliberately NO fallback pool over them — an
# unbound passing artifact from another run/surface must never be emitted;
# a run whose staging promote was never recorded gets the advisory below.


def qualifying(entry):
    # _evidence_resolves semantics (gates.py:1003-1014): runner-executed,
    # exit 0, artifact-bound — caller-recorded evidence is fabricable.
    g = entry.get("gate", {}) if isinstance(entry, dict) else {}
    return (g.get("exit_code") == 0 and g.get("executed_by") == "run_gate"
            and isinstance(g.get("artifact"), str))


def gate_shapes(art, evs):
    if not gates._valid_artifact(art):
        refuse("artifact", "name@sha256:<64-hex> or 40-hex commit sha")
    for e in evs:
        if not _ID_RE.fullmatch(e):
            refuse("evidence id", "identifier")


out, errs = [], []
for action in staging:
    surface = action[len("deploy:staging-"):]
    if not _ID_RE.fullmatch(surface):
        refuse("surface", "identifier")
    # sig 1ea31e0b: promotion-bound candidates first — the store's own
    # run+surface association; ts = recorded_at.
    cands = []
    for r in (promos.get(RUN_ID) or []):
        if not isinstance(r, dict) or r.get("surface") != surface:
            continue
        art, evs = r.get("artifact"), [e for e in (r.get("evidence_ids") or [])
                                       if isinstance(e, str)]
        if not (isinstance(art, str) and evs):
            continue
        gate_shapes(art, evs)
        if all(qualifying(data.get(e, {}))
               and data[e]["gate"]["artifact"] == art for e in evs):
            cands.append((float(r.get("recorded_at") or 0), art, evs))
    if not cands:
        # advisory: value-free, ONE stderr line, still exit 0
        errs.append("PROMOTE-EMIT-ADVISORY: staging deploy grant present but "
                    "no artifact-bound passing runner evidence resolves for "
                    "this run — record one via gates.py run-gate --artifact "
                    "<digest> before promotion")
        continue
    cands.sort(key=lambda c: c[0])
    _, art, evs = cands[-1]  # sig 7cec17e8: LATEST by ts wins
    for _, sk, _evs in cands[:-1]:
        if sk != art:  # skipped candidates NAMED — deterministic, auditable
            errs.append(f"PROMOTE-EMIT-SKIPPED: '{sk}' (run '{RUN_ID}' "
                        f"surface '{surface}' — superseded by the latest "
                        f"candidate)")
    ev_flags = " ".join(f"--evidence '{e}'" for e in evs)
    out.append(f"python3 lib/gates.py promote '{RUN_ID}' --from staging "
               f"--to prod --surface '{surface}' --artifact '{art}' "
               f"{ev_flags}")

for line in errs:
    print(line, file=sys.stderr)
for line in out:
    print(line)
sys.exit(0)
PYEOF
