#!/usr/bin/env bash
# socratic-slice.sh — the one deterministic emitter that turns a spec's
# socratic.md frontmatter into a delimited domain slice.
#
# Usage: socratic-slice.sh <spec_dir_or_socratic.md> [--mode plan|arm|verify]
#   plan/arm are synonyms for the default emission mode.
#
# Exit 0: always, EXCEPT:
#   Exit 2: usage/invocation error (zero args, unknown flag, unknown --mode
#           value, --mode with no value) — usage errors carry a usage
#           message on stderr and NO status line; there is no armed-or-
#           skipped fact about a run that never started.
#   (Exit 3 is reserved for socratic-slice.sh --validate, added by a later
#    plan; this script never returns it.)
#
# Kill switch: SOCRATIC=off — empty stdout, exit 0, one status line
#   "socratic: skipped (SOCRATIC=off)". Checked first, before any filesystem
#   work.
#
# stdout: empty, or exactly one SOCRATIC_DATA_START ... SOCRATIC_DATA_END
#   block wrapping the resolved question/pack/assumption content.
# stderr: exactly one status line per PARSED invocation (exit 0) —
#   "socratic: armed domains=<csv|none> packs=<csv|none>" or
#   "socratic: skipped (<reason>)" — plus zero or more
#   "socratic: WARN <detail>" lines for per-item degradations.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

START_TOKEN="SOCRATIC_DATA_START"
END_TOKEN="SOCRATIC_DATA_END"
ESCAPED_TOKEN="SOCRATIC_DATA_ESCAPED"

warn() { echo "socratic: WARN $*" >&2; }
status() { echo "socratic: $*" >&2; }
usage() {
  echo "usage: socratic-slice.sh <spec_dir_or_socratic.md> [--mode plan|arm|verify]" >&2
}

# --- argument parsing --------------------------------------------------------
POSITIONAL=""
HAVE_POSITIONAL=0
MODE="plan"
while [ $# -gt 0 ]; do
  case "$1" in
    --mode)
      if [ $# -lt 2 ]; then
        usage
        exit 2
      fi
      case "$2" in
        --*)
          usage
          exit 2
          ;;
      esac
      case "$2" in
        plan|arm) MODE="plan" ;;
        verify) MODE="verify" ;;
        *)
          usage
          exit 2
          ;;
      esac
      shift 2
      ;;
    --*)
      usage
      exit 2
      ;;
    *)
      if [ "$HAVE_POSITIONAL" -eq 1 ]; then
        usage
        exit 2
      fi
      POSITIONAL="$1"
      HAVE_POSITIONAL=1
      shift
      ;;
  esac
done

if [ "$HAVE_POSITIONAL" -eq 0 ]; then
  usage
  exit 2
fi

# --- kill switch, checked first, before any filesystem work ------------------
if [ "${SOCRATIC:-on}" = "off" ]; then
  status "skipped (SOCRATIC=off)"
  exit 0
fi

# --- vendor tree resolution ---------------------------------------------------
# Stage one: FFS_SOCRATIC_DIR, when set and non-empty, is AUTHORITATIVE — no
# fall-through to stage two, even when it names a path that does not exist.
resolve_vendor_dir() {
  if [ -n "${FFS_SOCRATIC_DIR:-}" ]; then
    if [ -d "$FFS_SOCRATIC_DIR" ]; then
      printf '%s\n' "$FFS_SOCRATIC_DIR"
      return 0
    fi
    return 1
  fi
  local candidates=(
    "$REPO_ROOT/.agents/skills/socratic"
    "$REPO_ROOT/.claude/skills/socratic"
    "$HOME/.agents/skills/socratic"
    "$HOME/.claude/skills/socratic"
  )
  local c
  for c in "${candidates[@]}"; do
    if [ -d "$c" ]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

VENDOR_DIR="$(resolve_vendor_dir)" || {
  status "skipped (vendor tree absent)"
  exit 0
}

# --- socratic.md resolution ----------------------------------------------------
# A positional naming an existing directory reads <dir>/socratic.md; naming
# an existing file uses it as-is; naming NEITHER takes the fail-soft
# no-socratic.md path (exit 0, empty stdout) rather than erroring.
if [ -d "$POSITIONAL" ]; then
  SOCRATIC_MD="$POSITIONAL/socratic.md"
elif [ -f "$POSITIONAL" ]; then
  SOCRATIC_MD="$POSITIONAL"
else
  status "skipped (no socratic.md)"
  exit 0
fi

if [ ! -f "$SOCRATIC_MD" ]; then
  status "skipped (no socratic.md)"
  exit 0
fi

# --- frontmatter parsing -------------------------------------------------------
# One embedded python3 heredoc, adapted from
# requirement-ownership-gate.sh:104-153's parse_scalar/bracket-splitting
# logic, inverted to fail-soft: every stop() there becomes a machine-
# readable line printed to stdout for bash to read back, never a raise.
PARSE_OUTPUT="$(python3 - "$SOCRATIC_MD" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
n = len(lines)


def emit_malformed():
    print("STATUS:malformed")


comment_re = re.compile(r'^\s*<!--.*-->\s*$')

i = 0
while i < n and (lines[i].strip() == "" or comment_re.match(lines[i])):
    i += 1

if i >= n or lines[i].strip() != "---":
    emit_malformed()
    sys.exit(0)

start = i + 1
end = None
for j in range(start, n):
    if lines[j].strip() == "---":
        end = j
        break
if end is None:
    emit_malformed()
    sys.exit(0)

frontmatter = lines[start:end]
body = lines[end + 1:]


def parse_scalar(value):
    return value.split("#", 1)[0].strip().strip("\"'")


def find_field(name):
    for idx, line in enumerate(frontmatter):
        m = re.match(rf'^{name}\s*:\s*(.*)$', line)
        if m:
            return idx, m.group(1).strip()
    return None, None


def parse_list_field(name):
    idx, value = find_field(name)
    if idx is None:
        return None
    uncommented = value.split("#", 1)[0].strip()
    if not uncommented:
        return []
    if not (uncommented.startswith("[") and uncommented.endswith("]")):
        return "MALFORMED"
    inner = uncommented[1:-1].strip()
    if not inner:
        return []
    return [parse_scalar(item) for item in inner.split(",")]


domains_idx, _ = find_field("domains")
if domains_idx is None:
    emit_malformed()
    sys.exit(0)

domains_list = parse_list_field("domains")
if domains_list is None or domains_list == "MALFORMED":
    emit_malformed()
    sys.exit(0)

packs_list = parse_list_field("packs")
if packs_list is None or packs_list == "MALFORMED":
    packs_list = []

depth_idx, depth_raw = find_field("depth")
if depth_idx is None:
    depth_value = "core"
    depth_declared = "0"
else:
    depth_value = parse_scalar(depth_raw)
    if not depth_value:
        depth_value = "core"
    depth_declared = "1"

print("STATUS:ok")
print(f"DOMAINS:{','.join(domains_list)}")
print(f"PACKS:{','.join(packs_list)}")
print(f"DEPTH:{depth_value}")
print(f"DEPTH_DECLARED:{depth_declared}")

assume_re = re.compile(r'^\s*(?:[-*]|\d+\.)\s*ASSUME-\d+\s*:')
for line in body:
    if assume_re.match(line):
        print(f"ASSUME:{line}")
PY
)"

STATUS_FIELD="malformed"
DOMAINS_RAW=""
PACKS_RAW=""
DEPTH_RAW="core"
DEPTH_DECLARED="0"
ASSUME_LINES=()
while IFS= read -r line; do
  case "$line" in
    STATUS:*) STATUS_FIELD="${line#STATUS:}" ;;
    DOMAINS:*) DOMAINS_RAW="${line#DOMAINS:}" ;;
    PACKS:*) PACKS_RAW="${line#PACKS:}" ;;
    DEPTH:*) DEPTH_RAW="${line#DEPTH:}" ;;
    DEPTH_DECLARED:*) DEPTH_DECLARED="${line#DEPTH_DECLARED:}" ;;
    ASSUME:*) ASSUME_LINES+=("${line#ASSUME:}") ;;
  esac
done <<< "$PARSE_OUTPUT"

if [ "$STATUS_FIELD" != "ok" ]; then
  status "skipped (malformed frontmatter)"
  exit 0
fi

# --- domain enum: ordered, closed lookup from slug to NN-prefixed stem -------
DOMAIN_ENUM_ORDER=(
  "requirements:00-requirements"
  "frontend:01-frontend"
  "backend:02-backend"
  "data:03-data"
  "api:04-api"
  "security:05-security"
  "infra:06-infra"
  "testing:07-testing"
  "observability:08-observability"
  "ai-llm:09-ai-llm"
  "mobile:10-mobile"
  "product-ux:11-product-ux"
  "cost-performance:12-cost-performance"
  "compliance:13-compliance"
  "team-maintenance:14-team-maintenance"
)

domain_stem_for() {
  local slug="$1" entry
  for entry in "${DOMAIN_ENUM_ORDER[@]}"; do
    if [ "${entry%%:*}" = "$slug" ]; then
      printf '%s\n' "${entry#*:}"
      return 0
    fi
  done
  return 1
}

DOMAIN_SLUGS=()
if [ -n "$DOMAINS_RAW" ]; then
  IFS=',' read -r -a DOMAIN_SLUGS <<< "$DOMAINS_RAW"
fi

is_declared_domain() {
  local needle="$1" s
  for s in ${DOMAIN_SLUGS[@]+"${DOMAIN_SLUGS[@]}"}; do
    [ "$s" = "$needle" ] && return 0
  done
  return 1
}

SELECTED_DOMAIN_SLUGS=()
for entry in "${DOMAIN_ENUM_ORDER[@]}"; do
  slug="${entry%%:*}"
  if is_declared_domain "$slug"; then
    SELECTED_DOMAIN_SLUGS+=("$slug")
  fi
done

# A declared slug outside the closed enum resolves to no path at all; warn
# once per unknown value, known values still arm (EDGE-002).
for slug in ${DOMAIN_SLUGS[@]+"${DOMAIN_SLUGS[@]}"}; do
  [ -n "$slug" ] || continue
  if ! domain_stem_for "$slug" >/dev/null; then
    warn "unknown domain '$slug'"
  fi
done

# --- depth resolution: an out-of-enum value warns and falls back to core,
# consumption stays fail-soft over a hand-editable field ---------------------
DEPTH_VALUE="core"
if [ "$DEPTH_RAW" = "core" ] || [ "$DEPTH_RAW" = "full" ]; then
  DEPTH_VALUE="$DEPTH_RAW"
elif [ "$DEPTH_DECLARED" = "1" ]; then
  warn "invalid depth '$DEPTH_RAW', falling back to core"
fi

# extract_verification_block <file>
#
# Emits the region beginning at the line whose content is the "##
# Verification" heading and ending immediately before the next line
# starting a heading at the same or a shallower level (or at EOF).
extract_verification_block() {
  local file="$1" in_block=0 line
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$in_block" -eq 0 ]; then
      case "$line" in
        "## Verification"|"## Verification "*) in_block=1 ;;
      esac
      continue
    fi
    case "$line" in
      "# "*|"## "*|"#"|"##")
        return 0
        ;;
    esac
    printf '%s\n' "$line"
  done < "$file"
}

# --- resolve selected domains to a file per mode/depth ----------------------
# --mode verify ALWAYS reads the top-level full file regardless of declared
# depth (core files carry no Verification block at the pin); otherwise depth
# full reads the top-level file and depth core (default) reads
# questions/core/.
CONTRIBUTING_DOMAIN_SLUGS=()
DOMAIN_TEXT_BLOCKS=()
USABLE_DOMAIN_COUNT=0
for slug in ${SELECTED_DOMAIN_SLUGS[@]+"${SELECTED_DOMAIN_SLUGS[@]}"}; do
  stem="$(domain_stem_for "$slug")"
  if [ "$MODE" = "verify" ] || [ "$DEPTH_VALUE" = "full" ]; then
    file="$VENDOR_DIR/questions/$stem.md"
  else
    file="$VENDOR_DIR/questions/core/$stem.md"
  fi

  if [ ! -f "$file" ]; then
    warn "domain file missing for '$slug': $file"
    continue
  fi
  USABLE_DOMAIN_COUNT=$((USABLE_DOMAIN_COUNT + 1))

  if [ "$MODE" = "verify" ]; then
    block="$(extract_verification_block "$file")"
    if [ -z "$block" ]; then
      warn "no Verification block for domain '$slug'"
    fi
  else
    block="$(cat "$file")"
  fi

  if [ -n "$block" ]; then
    CONTRIBUTING_DOMAIN_SLUGS+=("$slug")
  fi
  DOMAIN_TEXT_BLOCKS+=("$block")
done

DOMAIN_CONTENT=""
for block in ${DOMAIN_TEXT_BLOCKS[@]+"${DOMAIN_TEXT_BLOCKS[@]}"}; do
  [ -n "$block" ] || continue
  if [ -n "$DOMAIN_CONTENT" ]; then
    DOMAIN_CONTENT="$DOMAIN_CONTENT
$block"
  else
    DOMAIN_CONTENT="$block"
  fi
done

# --- pack enum: closed set of the ten real pack slugs at the pin -----------
PACK_ENUM=(
  "software-design"
  "domain-modeling"
  "data-systems"
  "operations"
  "threat-modeling"
  "ai-engineering"
  "agent-design"
  "legacy-change"
  "testing-design"
  "product-discovery"
)

is_known_pack() {
  local needle="$1" p
  for p in "${PACK_ENUM[@]}"; do
    [ "$p" = "$needle" ] && return 0
  done
  return 1
}

DECLARED_PACKS=()
if [ -n "$PACKS_RAW" ]; then
  IFS=',' read -r -a DECLARED_PACKS <<< "$PACKS_RAW"
fi

# Pipeline order: enum filter FIRST, then drop in-enum packs whose core.md is
# missing from disk (one WARN each), and only THEN cap at two survivors —
# neither an out-of-enum name nor an unresolvable in-enum name ever consumes
# a cap slot.
RESOLVED_PACKS=()
for p in ${DECLARED_PACKS[@]+"${DECLARED_PACKS[@]}"}; do
  [ -n "$p" ] || continue
  if ! is_known_pack "$p"; then
    warn "unknown pack '$p'"
    continue
  fi
  pack_file="$VENDOR_DIR/packs/$p/core.md"
  if [ ! -f "$pack_file" ]; then
    warn "pack file missing for '$p': $pack_file"
    continue
  fi
  RESOLVED_PACKS+=("$p")
done

# Packs resolve to packs/<name>/core.md at every depth (real packs ship no
# full variant) and are emitted only in plan/arm mode, never in verify mode.
HONORED_PACKS=()
PACK_CONTENT=""
if [ "$MODE" != "verify" ]; then
  pack_index=0
  for p in ${RESOLVED_PACKS[@]+"${RESOLVED_PACKS[@]}"}; do
    if [ "$pack_index" -lt 2 ]; then
      HONORED_PACKS+=("$p")
      pack_content="$(cat "$VENDOR_DIR/packs/$p/core.md")"
      if [ -n "$PACK_CONTENT" ]; then
        PACK_CONTENT="$PACK_CONTENT
$pack_content"
      else
        PACK_CONTENT="$pack_content"
      fi
    else
      warn "pack '$p' skipped: cap of 2 reached"
    fi
    pack_index=$((pack_index + 1))
  done
fi

# --- ASSUME ledger: lines matching a leading list marker followed by
# ASSUME-NNN: extracted from the socratic.md body by the python3 parser
# above. Emitted after domain content and pack cards, inside the same
# single delimiter pair, in BOTH plan and verify modes. -----------------
LEDGER_CONTENT=""
if [ "${#ASSUME_LINES[@]}" -gt 0 ]; then
  LEDGER_CONTENT="## Assumed"
  for assume_line in "${ASSUME_LINES[@]}"; do
    LEDGER_CONTENT="$LEDGER_CONTENT
$assume_line"
  done
fi

# --- skip decision: fires only when THIS MODE'S emittable set is empty -----
# plan/arm emittable set = question files + pack cards + ledger, with the
# ledger alone NOT sufficient (a ledger with no question material behind it
# gives a reviewer nothing to check it against). verify emittable set =
# Verification blocks + ledger, with the ledger alone sufficient (verify
# mode's job is a verdict per ASSUME entry; dropping the ledger would
# silently skip that audit). Usable-domain selection is checked FIRST so no
# input state can claim both empty-ish wordings.
if [ "$MODE" = "verify" ]; then
  if [ "$USABLE_DOMAIN_COUNT" -eq 0 ] && [ -z "$LEDGER_CONTENT" ]; then
    status "skipped (no domains)"
    exit 0
  fi
  if [ -z "$DOMAIN_CONTENT" ] && [ -z "$LEDGER_CONTENT" ]; then
    status "skipped (no verification content)"
    exit 0
  fi
  FULL_CONTENT="$DOMAIN_CONTENT"
  if [ -n "$LEDGER_CONTENT" ]; then
    if [ -n "$FULL_CONTENT" ]; then
      FULL_CONTENT="$FULL_CONTENT
$LEDGER_CONTENT"
    else
      FULL_CONTENT="$LEDGER_CONTENT"
    fi
  fi
else
  if [ "${#CONTRIBUTING_DOMAIN_SLUGS[@]}" -eq 0 ] && [ "${#HONORED_PACKS[@]}" -eq 0 ]; then
    status "skipped (no domains)"
    exit 0
  fi
  FULL_CONTENT="$DOMAIN_CONTENT"
  if [ -n "$PACK_CONTENT" ]; then
    if [ -n "$FULL_CONTENT" ]; then
      FULL_CONTENT="$FULL_CONTENT
$PACK_CONTENT"
    else
      FULL_CONTENT="$PACK_CONTENT"
    fi
  fi
  if [ -n "$LEDGER_CONTENT" ]; then
    if [ -n "$FULL_CONTENT" ]; then
      FULL_CONTENT="$FULL_CONTENT
$LEDGER_CONTENT"
    else
      FULL_CONTENT="$LEDGER_CONTENT"
    fi
  fi
fi

DOMAINS_FIELD="none"
if [ "${#CONTRIBUTING_DOMAIN_SLUGS[@]}" -gt 0 ]; then
  DOMAINS_FIELD="$(IFS=,; echo "${CONTRIBUTING_DOMAIN_SLUGS[*]}")"
fi

PACKS_FIELD="none"
if [ "$MODE" != "verify" ] && [ "${#HONORED_PACKS[@]}" -gt 0 ]; then
  PACKS_FIELD="$(IFS=,; echo "${HONORED_PACKS[*]}")"
fi

# Neutralize delimiter impersonation by SUBSTRING (never whole-line): any
# emitted line containing either token anywhere has each occurrence
# rewritten to SOCRATIC_DATA_ESCAPED before emission — rewrite, not drop,
# so the surrounding ledger entry stays visible to the reviewer.
NEUTRALIZED_CONTENT="$(printf '%s' "$FULL_CONTENT" | sed -e "s/${START_TOKEN}/${ESCAPED_TOKEN}/g" -e "s/${END_TOKEN}/${ESCAPED_TOKEN}/g")"

printf '%s\n' "$START_TOKEN"
printf '%s\n' "$NEUTRALIZED_CONTENT"
printf '%s\n' "$END_TOKEN"

status "armed domains=$DOMAINS_FIELD packs=$PACKS_FIELD"
exit 0
