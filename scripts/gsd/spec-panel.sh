#!/usr/bin/env bash
# spec-panel.sh — spec-004 AC-010: dual-vendor blind spec-authoring panel,
# LAST phase, default-off behind SPEC_PANEL=on. Principal never drafts: each
# draft is a fresh, non-inheriting scripts/gsd/adversary-host.sh dispatch (a
# brand-new subprocess with only the fixed brief on stdin — never the
# principal's session) — no hand-rolled vendor CLI calls.
#
# Usage: spec-panel.sh <brief-file> <spec-dir>
#   brief-file  plain-text spec brief — the SAME brief both drafts see
#   spec-dir    e.g. specs/NNN-slug — artifacts land under <spec-dir>/panel/
#
# SPEC_PANEL=off (default): no-op, exit 0 — nothing dispatched, nothing
# written (US7/AC-010: default flip is reserved for a follow-up change,
# gated on an EVAL-D pass; this lever never flips itself).
#
# Panel (both vendor CLIs reachable): one judgment-tier draft per vendor,
# blind to each other (independent dispatches, same fixed brief, no
# cross-visibility), synthesized by grafting with per-section [voice: ...]
# attribution across four fixed rubric axes (Coverage / Assumptions /
# Failure modes / Testability — the axes EVAL-D scores). Artifacts:
# <spec-dir>/panel/draft-anthropic.md, draft-openai.md, synthesis.md.
#
# Degrade (exactly one vendor CLI reachable): same-vendor author (judgment
# tier) + refuter (frontier tier) instead of a same-vendor panel (shared
# blind spots) — labelled relation "self" (AC-010 pair assignment is a
# GUESS -> EVAL-D arm). Artifacts: draft-<vendor>.md, refute-<vendor>.md,
# synthesis.md.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/gsd/adversary-host.sh
. "$SCRIPT_DIR/adversary-host.sh"

if [ "${SPEC_PANEL:-off}" != "on" ]; then
  exit 0
fi

BRIEF_FILE="${1:-}"
SPEC_DIR="${2:-}"
if [ -z "$BRIEF_FILE" ] || [ -z "$SPEC_DIR" ]; then
  echo "usage: spec-panel.sh <brief-file> <spec-dir>" >&2
  exit 2
fi
if [ ! -f "$BRIEF_FILE" ] || [ ! -r "$BRIEF_FILE" ]; then
  echo "spec-panel: brief file unreadable: $BRIEF_FILE" >&2
  exit 1
fi

PANEL_DIR="$SPEC_DIR/panel"
mkdir -p "$PANEL_DIR" 2>/dev/null || {
  echo "spec-panel: cannot create $PANEL_DIR" >&2
  exit 1
}

SP_TIMEOUT="${SPEC_PANEL_TIMEOUT:-180}"

SP_DRAFT_BRIEF='You are drafting ONE independent candidate spec for the brief below. This is a BLIND, independent submission — you cannot see any other draft and must not reference or presuppose one. Structure your answer with exactly these four section headers, each followed by substantive content:

## Coverage
## Assumptions
## Failure modes
## Testability

BRIEF:'

_sp_build_draft_prompt() {
  printf '%s\n\n%s' "$SP_DRAFT_BRIEF" "$(cat "$BRIEF_FILE")"
}

_sp_build_refute_prompt() {
  printf '%s\n\n%s\n\n%s' \
    'You are refuting/critiquing the draft below. Output the SAME four section headers (## Coverage / ## Assumptions / ## Failure modes / ## Testability); under each, give ONLY your fixes, gaps, or additions — do not repeat content you agree with.' \
    'DRAFT:' "$1"
}

_sp_bin_available() {
  command -v "$1" >/dev/null 2>&1
}

_sp_write() {
  printf '%s\n' "$2" > "$1"
}

# _sp_extract_section <content> <heading>
# ponytail: exact "## <heading>" match — our own brief pins vendors to this
# shape; loosen to fuzzy heading matching if live prose drifts from it.
_sp_extract_section() {
  printf '%s\n' "$1" | awk -v h="## $2" '
    $0 == h { found=1; next }
    found && /^## / { found=0 }
    found { print }
  ' | sed '/^[[:space:]]*$/d'
}

# _sp_graft <out-file> <label-a> <content-a> [<label-b> <content-b>]
# Grafts one or two drafts into one principal-authored synthesis, section by
# section over the four fixed axes, each graft attributed [voice: <label>].
_sp_graft() {
  local out="$1" label_a="$2" content_a="$3" label_b="${4:-}" content_b="${5:-}"
  local axis section body
  body="# Synthesis (grafted, principal-authored)"$'\n'
  for axis in Coverage Assumptions "Failure modes" Testability; do
    body="${body}"$'\n'"## ${axis}"$'\n'
    section="$(_sp_extract_section "$content_a" "$axis")"
    [ -n "$section" ] && body="${body}"$'\n'"[voice: ${label_a}] ${section}"$'\n'
    if [ -n "$label_b" ]; then
      section="$(_sp_extract_section "$content_b" "$axis")"
      [ -n "$section" ] && body="${body}"$'\n'"[voice: ${label_b}] ${section}"$'\n'
    fi
  done
  printf '%s\n' "$body" > "$out"
}

CLAUDE_OK=0
_sp_bin_available "${ADVERSARY_BIN_CLAUDE:-claude}" && CLAUDE_OK=1
CODEX_OK=0
_sp_bin_available "${ADVERSARY_BIN_CODEX:-codex}" && CODEX_OK=1

if [ "$CLAUDE_OK" -eq 1 ] && [ "$CODEX_OK" -eq 1 ]; then
  prompt="$(_sp_build_draft_prompt)"
  draft_anthropic="$(adversary_invoke claude "$SP_TIMEOUT" claude-opus-5 "" "$prompt")"
  rc_a=$?
  draft_openai="$(adversary_invoke codex "$SP_TIMEOUT" gpt-5.6-sol high "$prompt")"
  rc_b=$?
  if [ "$rc_a" -ne 0 ] || [ "$rc_b" -ne 0 ]; then
    echo "spec-panel: blind draft dispatch failed (claude rc=$rc_a, codex rc=$rc_b)" >&2
    exit 1
  fi
  _sp_write "$PANEL_DIR/draft-anthropic.md" "$draft_anthropic"
  _sp_write "$PANEL_DIR/draft-openai.md" "$draft_openai"
  _sp_graft "$PANEL_DIR/synthesis.md" anthropic "$draft_anthropic" openai "$draft_openai"
  jq -n '{mode:"panel", relation:"cross-vendor"}' > "$PANEL_DIR/record.json"
  echo "spec-panel: PANEL synthesis written to $PANEL_DIR/synthesis.md"
  exit 0
fi

if [ "$CLAUDE_OK" -eq 1 ]; then
  vendor=anthropic; kind=claude; author_model=claude-opus-5; author_effort=""
  refuter_model=claude-fable-5; refuter_effort=""
elif [ "$CODEX_OK" -eq 1 ]; then
  vendor=openai; kind=codex; author_model=gpt-5.6-sol; author_effort=high
  refuter_model=gpt-5.6-sol; refuter_effort=xhigh
else
  echo "spec-panel: no vendor CLI reachable — cannot draft" >&2
  exit 1
fi

prompt="$(_sp_build_draft_prompt)"
draft="$(adversary_invoke "$kind" "$SP_TIMEOUT" "$author_model" "$author_effort" "$prompt")"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "spec-panel: author draft dispatch failed (rc=$rc)" >&2
  exit 1
fi
_sp_write "$PANEL_DIR/draft-${vendor}.md" "$draft"

refute_prompt="$(_sp_build_refute_prompt "$draft")"
refute="$(adversary_invoke "$kind" "$SP_TIMEOUT" "$refuter_model" "$refuter_effort" "$refute_prompt")"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "spec-panel: refuter dispatch failed (rc=$rc)" >&2
  exit 1
fi
_sp_write "$PANEL_DIR/refute-${vendor}.md" "$refute"

_sp_graft "$PANEL_DIR/synthesis.md" "${vendor}-author" "$draft" "${vendor}-refuter" "$refute"
jq -n --arg vendor "$vendor" '{mode:"degrade", vendor:$vendor, relation:"self"}' > "$PANEL_DIR/record.json"
echo "spec-panel: DEGRADE ($vendor) synthesis written to $PANEL_DIR/synthesis.md (relation: self)"
exit 0
