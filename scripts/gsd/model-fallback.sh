#!/usr/bin/env bash
# model-fallback.sh — 3-leg cross-vendor fallback for unavailable premium
# models in .planning/config.json, WITH recovery.
#
# gsd-core's model_overrides are returned verbatim (model-resolver.cjs step 1) —
# no availability check. When claude-fable-5 drops off the OAuth subscription,
# every spawn of that agent errors. This lever probes availability once
# (cached 24h) and rewrites model_overrides + dynamic_routing.tier_models
# BEFORE a run starts — then RESTORES them once fable comes back.
#
# Chain: fable -> [probe gpt-5.6-sol cross-vendor compensation, for
# marker-mode only] -> opus. Codex models can NEVER be Claude subagent pins,
# so the actual config rewrite is always fable->opus; the codex-sol probe
# only decides whether we record mode=codex-sol (cross-vendor xhigh
# compensation available, e.g. via plan-adversary.sh) or mode=opus-only.
#
# Both value forms are matched and the substitution PRESERVES form — gsd-core
# aliases ("fable" -> "opus", the form templates/gsd-config.base.json and real
# consumer configs pin) and full IDs ("claude-fable-5" -> "claude-opus-5").
#
# Recovery: once fable is available again, restore ONLY the JSON paths this
# lever itself rewrote — the marker (.planning/fable-fallback.json) records
# each path's exact prior value: {"mode": ..., "paths": {"<json.path>":
# "<original value>"}}, so mixed-form configs restore per-path. A blanket
# opus->fable substitution would incorrectly flip intentional opus pins
# (gsd-verifier etc.) that were never fable to begin with — this is the
# correctness crux, see tests/bats/model-fallback.bats.
#
# Marker shape: {"mode": ..., "paths": {"<json.path>": <original>},
#                "installed": {"<json.path>": <what this lever wrote>}}
# Path segments are dot-joined and resolved against the LIVE container type at
# restore time: a segment is a dict key, or an integer list index when the
# container it addresses is a list ("review.default_reviewers.0"). Because that
# join is ambiguous for a dict key containing a literal ".", such keys are NOT
# rewritten at all (warned, fable pin left in place) — corrupting an intentional
# opus pin is worse than skipping one substitution. A container whose type
# changed (list->dict) is likewise treated as unresolvable and skipped.
#
# Corrupt config.json is FATAL in both directions (named, not a traceback) — the
# run cannot proceed on it whatever this lever does. An unusable MARKER is not:
# it is only a recovery hint, so it warns and exits 0.
#
# Recovery is otherwise fail-soft and idempotent:
#   - unreadable/unusable marker  -> WARN, config untouched, marker kept, exit 0
#   - path no longer resolves     -> skipped and NAMED
#   - value != what this lever wrote -> a deliberate later edit; left alone and
#     NAMED, never overwritten with a stale original ("installed" is the CAS).
#     Only markers carrying "installed" get this check; an older marker without
#     it falls back to unconditional restore.
#   - value already == original    -> counted done, dropped from the marker
# The marker is then rewritten with ONLY the unresolved paths, or deleted when
# none remain. Writes are same-dir temp + fsync + os.replace, and the rewrite
# writes the MARKER FIRST — interrupted the other way round, a config stranded on
# opus with no marker is unrecoverable.
#
# KNOWN LIMITATION — positional paths vs. a reordered list. Marker paths carry no
# segment types and no element identity, so if a LIST holding pins is reordered
# while the fallback is active, "x.0.model" resolves to a different element than it
# did at rewrite time. The compare-and-swap does not save you when the element it
# lands on legitimately holds the same installed value: the original is written to
# the wrong element and the real one is stranded on opus. Closing this needs typed
# segments plus element fingerprints in the marker — a format redesign, tracked as
# fix:model-fallback-positional-paths-vs-list-reorder. Unreached by any config in
# this repo (pins live in dicts; the only pin-bearing list is a flat string array).
#
# Two deliberate rough edges:
#   - a dict key containing "." skips its ENTIRE subtree, so fable pins nested
#     under such a key also survive un-substituted. Safe direction (a live pin is
#     loud), and unreachable in every config in this repo.
#   - a conflict entry is retained on every later run and only drains when the
#     value is reverted to its original, so a marker can persist indefinitely on
#     a deliberate edit. That keeps plan-adversary.sh's low-blast skip disabled.
#
# NOT handled: two concurrent runs racing the same marker (TOCTOU on the
# check/remove). gsd-run.sh single-flights the drive via its pid file, so this
# is unreached in practice; closing it properly needs flock on the pair.
#
# Usage: model-fallback.sh [<planning-dir>]   (default .planning)
#   GSD_MODEL_PROBE_CMD        override the claude probe command (tests)
#   GSD_MODEL_PROBE_CMD_CODEX  override the codex-sol probe command (tests)
#   GSD_FALLBACK_CACHE         override cache dir (tests; default ~/.cache/gsd-model-probe)
#
# Fail-soft: if the probe MECHANISM breaks, config stays unchanged and we warn —
# an unavailable model then fails loudly at first spawn, same as without this lever.
set -euo pipefail

PLANNING_DIR="${1:-.planning}"
CONFIG="$PLANNING_DIR/config.json"
[ -f "$CONFIG" ] || { echo "[model-fallback] ERROR: $CONFIG not found" >&2; exit 1; }

CACHE_DIR="${GSD_FALLBACK_CACHE:-$HOME/.cache/gsd-model-probe}"
mkdir -p "$CACHE_DIR"

# Wall-clock bound for the real-CLI probes. This lever runs as a WALL at the
# top of /feature-implement — an unbounded probe hang stalls the run before
# phase 1 (2026-07-12 dead-codex forensics: this was the only site with no
# timeout on ANY branch). A bounded/refused probe reads as "unavailable",
# which is the lever's existing fail-soft direction.
. "$(dirname "${BASH_SOURCE[0]}")/run-bounded.sh"
PROBE_TIMEOUT="${GSD_MODEL_PROBE_TIMEOUT:-120}"

MARKER="$PLANNING_DIR/fable-fallback.json"
FABLE="claude-fable-5"
OPUS="claude-opus-5"
CODEX_SOL="gpt-5.6-sol"

probe_claude_model() {
  # exit 0 = available, 1 = unavailable. 24h cache per model (cache file name).
  local model="$1" cache="$CACHE_DIR/$1.status"
  if [ -f "$cache" ] && [ -n "$(find "$cache" -mmin -1440 2>/dev/null)" ]; then
    [ "$(cat "$cache")" = "ok" ]; return
  fi
  local cmd="${GSD_MODEL_PROBE_CMD:-}"
  if [ -n "$cmd" ]; then
    if $cmd "$model" >/dev/null 2>&1; then echo ok > "$cache"; else echo fail > "$cache"; fi
  else
    if run_bounded "$PROBE_TIMEOUT" env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
        claude -p "ok" --model "$model" --max-turns 1 </dev/null >/dev/null 2>&1; then
      echo ok > "$cache"
    else
      echo fail > "$cache"
    fi
  fi
  [ "$(cat "$cache")" = "ok" ]
}

probe_codex_model() {
  # Separate cache key from probe_claude_model (distinct cache filename per model).
  local model="$1" cache="$CACHE_DIR/$1.status"
  if [ -f "$cache" ] && [ -n "$(find "$cache" -mmin -1440 2>/dev/null)" ]; then
    [ "$(cat "$cache")" = "ok" ]; return
  fi
  local cmd="${GSD_MODEL_PROBE_CMD_CODEX:-}"
  if [ -n "$cmd" ]; then
    if $cmd "$model" >/dev/null 2>&1; then echo ok > "$cache"; else echo fail > "$cache"; fi
  else
    # Subscription-only: strip BOTH vendors' API keys. The codex CLI prefers
    # OPENAI_API_KEY over the logged-in ChatGPT session when one is present,
    # which silently bills the probe to a metered key instead of the plan.
    if run_bounded "$PROBE_TIMEOUT" env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
        codex exec -c "model=\"$model\"" -c 'sandbox_mode="read-only"' "ok" </dev/null >/dev/null 2>&1; then
      echo ok > "$cache"
    else
      echo fail > "$cache"
    fi
  fi
  [ "$(cat "$cache")" = "ok" ]
}

# Match BOTH value forms: gsd-core alias "fable" and full ID "claude-fable-5"
# (the alias grep is exact-quoted, so it does NOT match inside the full ID).
HAS_FABLE_LITERAL=false
if grep -qE '"(fable|claude-fable-5)"' "$CONFIG"; then HAS_FABLE_LITERAL=true; fi

if [ ! -f "$MARKER" ] && [ "$HAS_FABLE_LITERAL" = false ]; then
  echo "[model-fallback] $FABLE not present in config — no-op"
  exit 0
fi

if probe_claude_model "$FABLE"; then
  if [ -f "$MARKER" ]; then
    # Do not wrap a here-doc command substitution in an outer double quote.
    # macOS Bash 3.2 misparses the nested Python quotes at execution time even
    # though `bash -n` accepts the file. Assignment context already suppresses
    # word splitting and pathname expansion.
    RESULT=$(MARKER="$MARKER" CONFIG="$CONFIG" python3 - <<'EOF'
import json, os, sys
marker_path = os.environ["MARKER"]; config_path = os.environ["CONFIG"]

def atomic_write(path, obj):
    # Same-directory temp + fsync + rename. An in-place truncating dump could be
    # interrupted mid-write, leaving a config.json the very next step of the run
    # cannot parse.
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2)
        fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)
    # fsync the DIRECTORY too. POSIX does not guarantee a rename is durable (or
    # ordered against a later rename) without it, so without this the marker-
    # before-config write-ahead below is an intention, not a guarantee: a power
    # loss could keep the new opus config and lose the marker rename.
    dfd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)

# A corrupt config.json is a LOUD failure: the run cannot proceed on it whatever
# this lever does, so exiting 0 would only defer the failure somewhere less
# obvious. stderr, because `set -e` kills the caller before it can echo $RESULT.
try:
    with open(config_path) as fh: cfg = json.load(fh)
except (OSError, ValueError) as exc:
    print(f"FATAL: {config_path} is unreadable or not valid JSON "
          f"({type(exc).__name__}: {exc}) — fix it and re-run.", file=sys.stderr)
    sys.exit(1)

# The marker, by contrast, is only a recovery HINT. An unreadable one must NOT
# fail the first wall of a run — this load used to sit outside every guard, so a
# marker truncated by a SIGTERM mid-write exited nonzero and took the run with it.
try:
    with open(marker_path) as fh: marker = json.load(fh)
    if not isinstance(marker, dict): raise ValueError("marker is not a JSON object")
    originals = marker.get("paths")
    if not isinstance(originals, dict): raise ValueError("marker has no usable 'paths' map")
    # A PRESENT but non-dict "installed" is a malformed marker, not an old one.
    # Coercing it to {} silently disabled the compare-and-swap and let a stale
    # original overwrite a deliberate edit — degrading safety without saying so.
    if "installed" in marker and not isinstance(marker["installed"], dict):
        raise ValueError("marker 'installed' is present but not an object")
except (OSError, ValueError) as exc:
    print(f"WARN: marker unreadable ({type(exc).__name__}: {exc}) — config left "
          f"untouched, marker kept for inspection")
    sys.exit(0)

# installed: the value THIS lever wrote at each path. Restoring only where the
# live value still matches it means a deliberate later edit gets reported instead
# of silently overwritten with a stale "original". Absent entirely = a marker from
# before this field existed; those fall back to unconditional restore.
installed = marker.get("installed") or {}

# Marker paths are dot-joined. New rewrites refuse dot-bearing keys outright, but a
# marker written by an EARLIER version can carry a path with two valid readings —
# resolving the wrong one writes the original into the wrong node and flips an
# intentional opus pin. resolve() below detects that per path rather than
# condemning every multi-segment path in a config that merely contains a dot key.

# paths: {"<json.path>": "<exact original value>"} — form-preserving restore.
# Fail-soft PER PATH: a config hand-edited between rewrite and recovery (list
# shortened, key renamed) leaves a marker path unresolvable. Raising there used
# to abort before the write, so ONE bad path stranded every resolvable path on
# opus AND exited nonzero.
restored = 0
writes = 0
skipped = []            # RAW paths only — indexes `originals` when pruning
skip_why = {}           # path -> why it was skipped (display only)
conflicts = []          # (path, human-readable reason)
for path, original in originals.items():
    keys = path.split(".")
    try:
        d = cfg
        ambiguous = False
        # A segment addressing a list element is an integer index, not a dict key.
        for i in range(len(keys) - 1):
            # Legacy-marker protection: if the whole remaining suffix ALSO exists as
            # one literal key right here, this path has two valid readings and we
            # cannot tell which one the rewrite meant. Skipping is recoverable;
            # restoring into the wrong node is not.
            if isinstance(d, dict) and ".".join(keys[i:]) in d:
                ambiguous = True
                break
            d = d[int(keys[i])] if isinstance(d, list) else d[keys[i]]
        if ambiguous:
            # NB: `skipped` holds RAW paths — it indexes `originals` when the marker
            # is pruned below. Reasons live in `skip_why`.
            skipped.append(path)
            skip_why[path] = "ambiguous: a dot-bearing key gives this path two readings"
            continue
        last = int(keys[-1]) if isinstance(d, list) else keys[-1]
        current = d[last]
    except (KeyError, IndexError, TypeError, ValueError):
        skipped.append(path)
        continue
    if current == original:
        # Already back — e.g. this run follows a crash between the marker write
        # and the config write. Nothing to do, and nothing to keep.
        restored += 1
        continue
    expected = installed.get(path)
    if expected is not None and current != expected:
        conflicts.append((path, f"{path} (now {current!r}, this lever wrote {expected!r})"))
        continue
    d[last] = original
    restored += 1
    writes += 1

# Only touch config.json when something actually changed. A fully-skipped
# recovery used to truncate and rewrite a file it had no edits for.
if writes:
    atomic_write(config_path, cfg)

unresolved = skipped + [p for p, _ in conflicts]
if unresolved:
    # Prune to ONLY the unresolved paths. Retaining the whole set meant restored
    # entries were re-applied on a later run and the marker could never reach a
    # clean state.
    atomic_write(marker_path, {
        "mode": marker.get("mode"),
        "paths": {p: originals[p] for p in unresolved},
        "installed": {p: installed[p] for p in unresolved if p in installed},
    })
    notes = []
    if skipped:
        notes.append(f"{len(skipped)} unresolvable ("
                     + ", ".join(p + (f" [{skip_why[p]}]" if p in skip_why else "")
                                 for p in skipped) + ")")
    if conflicts:
        notes.append(f"{len(conflicts)} changed since fallback, left alone "
                     f"({'; '.join(m for _, m in conflicts)})")
    print(f"restored {restored} path(s) — WARN: " + "; ".join(notes) +
          f"; marker kept with {len(unresolved)} unresolved path(s)")
else:
    os.remove(marker_path)
    print(f"restored {restored} path(s) — marker deleted")
EOF
)
    echo "[model-fallback] $FABLE available — $RESULT"
  else
    echo "[model-fallback] $FABLE available — config unchanged"
  fi
  exit 0
fi

# fable unavailable
if [ "$HAS_FABLE_LITERAL" = false ]; then
  echo "[model-fallback] $FABLE unavailable — already on fallback (marker present), nothing to rewrite"
  exit 0
fi

if probe_codex_model "$CODEX_SOL"; then
  MODE="codex-sol"
else
  MODE="opus-only"
fi

RESULT=$(FABLE="$FABLE" OPUS="$OPUS" CONFIG="$CONFIG" MARKER="$MARKER" MODE="$MODE" python3 - <<'EOF'
import json, os, sys
config_path = os.environ["CONFIG"]; fable = os.environ["FABLE"]; opus = os.environ["OPUS"]
marker_path = os.environ["MARKER"]; mode = os.environ["MODE"]

def atomic_write(path, obj):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2)
        fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)
    # fsync the DIRECTORY too. POSIX does not guarantee a rename is durable (or
    # ordered against a later rename) without it, so without this the marker-
    # before-config write-ahead below is an intention, not a guarantee: a power
    # loss could keep the new opus config and lose the marker rename.
    dfd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)

# Same loud contract as the recovery direction: a corrupt config is fatal and
# named, not a bare traceback. stderr, because `set -e` kills the caller before
# it can echo $RESULT.
try:
    with open(config_path) as fh: cfg = json.load(fh)
except (OSError, ValueError) as exc:
    print(f"FATAL: {config_path} is unreadable or not valid JSON "
          f"({type(exc).__name__}: {exc}) — fix it and re-run.", file=sys.stderr)
    sys.exit(1)
# Form-preserving substitution: alias stays alias, full ID stays full ID.
forms = {"fable": "opus", fable: opus}
paths = {}
installed = {}
ambiguous = []
def sub(container, prefix=""):
    # Walks dicts AND lists. A list value used to fall through to `v in forms`,
    # which hashes the list -> TypeError, aborting the whole rewrite and leaving
    # a dead fable pin in place. List elements are addressed by index.
    items = container.items() if isinstance(container, dict) else enumerate(container)
    for k, v in list(items):
        p = f"{prefix}.{k}" if prefix else str(k)
        # Marker paths are dot-joined, so a dict key containing "." is
        # indistinguishable from a nested path and recovery could write the
        # original into the WRONG node — flipping an intentional opus pin to
        # fable, exactly the corruption the per-path marker exists to prevent.
        # Refuse to rewrite through such a key: a surviving fable pin is loud
        # and recoverable, a silently corrupted config is neither.
        if isinstance(container, dict) and "." in str(k):
            ambiguous.append(p)
            continue
        if isinstance(v, (dict, list)):
            sub(v, p)
        elif isinstance(v, str) and v in forms:
            paths[p] = v
            installed[p] = forms[v]
            container[k] = forms[v]
sub(cfg)
rewritten = len(paths)
warn = (f" WARN: {len(ambiguous)} path(s) NOT rewritten — key contains '.', which is "
        f"ambiguous for recovery: {', '.join(ambiguous)}") if ambiguous else ""

if not rewritten:
    # "fable" can match the literal grep as a dict KEY rather than a pinned value.
    # Writing an empty marker would strand a no-op recovery on the next run.
    print(f"{fable} UNAVAILABLE -> {opus} (0 override(s) rewritten){warn}")
    sys.exit(0)

# Carry forward anything a previous PARTIAL restore left unresolved. The marker
# used to be replaced wholesale, so one flap after a partial restore destroyed
# that path's recorded original for good.
if os.path.exists(marker_path):
    try:
        with open(marker_path) as fh: prior = json.load(fh)
        if not isinstance(prior, dict): raise ValueError("marker is not a JSON object")
    except (OSError, ValueError) as exc:
        # An existing-but-unreadable marker is the ONLY copy of some earlier run's
        # originals. Treating it as absent meant the write below replaced it and
        # those originals were gone for good. Refuse instead: nothing is written,
        # so the damaged marker stays available for hand-repair.
        print(f"FATAL: {marker_path} exists but is unreadable "
              f"({type(exc).__name__}: {exc}). It may hold the only record of an "
              f"earlier fallback's original pins, so refusing to overwrite it. "
              f"Inspect or delete it, then re-run.", file=sys.stderr)
        sys.exit(1)
    prior_paths = prior.get("paths")
    prior_installed = prior.get("installed")
    if isinstance(prior_paths, dict):
        for p, original in prior_paths.items():
            if p in paths:
                # This run saw a live fable literal at p, so it just recorded the
                # CURRENT original and the value it actually installed. Letting the
                # prior entry win here strands the pin forever: `installed` would
                # describe a value this run never wrote, so the compare-and-swap
                # below refuses on every future run and the marker never converges
                # (which also keeps plan-adversary.sh's low-blast skip disabled).
                # Reachable whenever the pin's form changed between runs — both the
                # alias and the full ID are live, see FALLBACK-011.
                continue
            # p is NOT in this config as a fable literal, so it survives from a
            # previous partial restore. Its recorded original is the only copy left.
            paths[p] = original
            if isinstance(prior_installed, dict) and p in prior_installed:
                installed[p] = prior_installed[p]

# Write-ahead: marker BEFORE config. Interrupted the other way round, config sat
# on opus with no marker, the next run saw neither a marker nor a fable literal
# (line ~113) and the original pins were unrecoverable.
atomic_write(marker_path, {"mode": mode, "paths": paths, "installed": installed})
atomic_write(config_path, cfg)
print(f"{fable} UNAVAILABLE -> {opus} ({rewritten} override(s) rewritten){warn}")
EOF
)
echo "[model-fallback] $RESULT"
if [ -f "$MARKER" ]; then
  echo "[model-fallback] fallback mode: $MODE (marker: $MARKER)"
  # tested-fallback discipline: a backup you never ran is a hope. The rungs this
  # run just fell back onto are only trustworthy if they carried a LIVE call
  # recently (fallback-rehearsal.sh records that). Missing/stale record = WARN,
  # never a block — an unrehearsed fallback still beats no fallback.
  REHEARSAL_FILE="$CACHE_DIR/rehearsal.json"
  if [ ! -f "$REHEARSAL_FILE" ]; then
    echo "[model-fallback] WARN: fallback REHEARSAL never run — the rungs have" \
         "never carried a live call; run scripts/gsd/fallback-rehearsal.sh"
  elif [ -z "$(find "$REHEARSAL_FILE" -mtime -30 2>/dev/null)" ]; then
    echo "[model-fallback] WARN: fallback REHEARSAL record is stale (>30d) —" \
         "re-run scripts/gsd/fallback-rehearsal.sh"
  fi
else
  # Zero rewrites (e.g. every fable literal sat under a dot-bearing key, or
  # matched only as a KEY) writes no marker. Claiming a fallback mode and naming a
  # marker file that does not exist reads as a successful fallback when the
  # unavailable pin is in fact still live.
  echo "[model-fallback] NO fallback marker written — nothing was rewritten, so any" \
       "remaining $FABLE pin is still live. See the WARN above."
fi
