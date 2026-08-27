#!/usr/bin/env bash
# autonomy-posture.sh — the single monotonic autonomy-posture resolver
# (spec-006 phase 3, REQ-306 / AC-D02 / AC-D03).
#
# Sourced by land-queue.sh and called exactly once per queue run.  This file
# is the ONLY production source allowed to read FFS_AUTONOMY_POSTURE; every
# posture consumer uses the exported result instead of re-reading env/config
# (enforced by the [POSTURE-SEAM] inventory test in tests/bats/land-queue.bats).
#
#   resolve_autonomy_posture CONFIG_PATH [EXPLICIT_CONFIG]
#
# exports AUTONOMY_POSTURE (zero|floor) and AUTONOMY_POSTURE_SOURCE
# (default|config|env).  Strictness order: zero < floor.  Precedence is
# monotonic — default zero, then the config layer (EXPLICIT_CONFIG, the
# caller's already-validated --posture flag, else `.autonomy.posture` read
# from CONFIG_PATH via json stdlib), then FFS_AUTONOMY_POSTURE — where each
# later layer may only preserve or increase strictness.  Only exact lowercase
# `zero`/`floor` (after trimming ordinary surrounding whitespace) are ever
# adopted; anything else emits one bounded stderr advisory and falls through.
# No input is ever evaluated as shell, and advisories never echo a raw
# untrusted value — only an allowlisted, length-bounded sanitization of it.

_posture_sanitize() { # allowlisted charset + bounded length; never raw input
  local s="${1//[^A-Za-z0-9._-]/}"
  s="${s:0:40}"
  [ -n "$s" ] || s="(unprintable)"
  printf '%s' "$s"
}

_posture_trim() { # ordinary surrounding whitespace only; interior stays
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  printf '%s' "$v"
}

resolve_autonomy_posture() {
  local cfg_path="${1:-}" explicit="${2:-}" eff src cfg envv
  eff="zero"
  src="default"

  # ── config layer: validated flag wins, else the committed config file ──
  cfg=""
  if [ -n "$explicit" ]; then
    cfg="$(_posture_trim "$explicit")"
  elif [ -n "$cfg_path" ] && [ -f "$cfg_path" ]; then
    cfg="$(python3 - "$cfg_path" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        doc = json.load(fh)
except Exception:
    print("__invalid__")
    raise SystemExit(0)
autonomy = doc.get("autonomy") if isinstance(doc, dict) else None
value = autonomy.get("posture") if isinstance(autonomy, dict) else None
if value is None:
    print("")
elif value in ("zero", "floor"):
    print(value)
else:
    print("__invalid__")
PY
)" || cfg="__invalid__"
  fi
  case "$cfg" in
    "") : ;;                                  # absent: keep zero|default
    zero)  eff="zero";  src="config" ;;
    floor) eff="floor"; src="config" ;;
    *) echo "POSTURE-INVALID: config" >&2 ;;  # not zero|floor: advise, keep zero
  esac

  # ── env layer: the single production read of FFS_AUTONOMY_POSTURE ──────
  envv="$(_posture_trim "${FFS_AUTONOMY_POSTURE:-}")"
  case "$envv" in
    "") : ;;
    floor) # strengthen only; an already-committed floor keeps its provenance
      if [ "$eff" = "zero" ]; then eff="floor"; src="env"; fi ;;
    zero)  # never weaken: a committed floor survives an env zero request
      if [ "$eff" = "floor" ]; then
        echo "POSTURE-WEAKEN-IGNORED: env requested zero; committed floor preserved" >&2
      fi ;;
    *) echo "POSTURE-INVALID: $(_posture_sanitize "$envv")" >&2 ;;
  esac

  export AUTONOMY_POSTURE="$eff" AUTONOMY_POSTURE_SOURCE="$src"
}
