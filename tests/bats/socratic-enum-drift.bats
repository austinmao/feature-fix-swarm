#!/usr/bin/env bats
# Opt-in enum-drift check (spec-005 follow-up e74a7070): DOMAIN_ENUM_ORDER and
# PACK_ENUM in socratic-slice.sh are frozen at pin 8c7e1fd. Against a REAL
# resolved vendor tree, each enum entry must map 1:1 to a file — and the tree
# must carry no extra domain/pack files the enums don't know. Skips when no
# vendor tree resolves (CI has none), which is what makes it opt-in: it only
# bites on machines where the pinned skill is actually installed. Runbook
# step 5 in docs/dependencies.md was previously the only control.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SLICE="$REPO_ROOT/scripts/gsd/socratic-slice.sh"
  [ -f "$SLICE" ] || skip "socratic-slice.sh missing"

  # Resolve exactly like the script: FFS_SOCRATIC_DIR authoritative, else the
  # four candidate roots.
  VENDOR_DIR=""
  if [ -n "${FFS_SOCRATIC_DIR:-}" ]; then
    [ -d "$FFS_SOCRATIC_DIR" ] && VENDOR_DIR="$FFS_SOCRATIC_DIR"
  else
    local c
    for c in \
      "$REPO_ROOT/.agents/skills/socratic" \
      "$REPO_ROOT/.claude/skills/socratic" \
      "$HOME/.agents/skills/socratic" \
      "$HOME/.claude/skills/socratic"; do
      if [ -d "$c" ]; then VENDOR_DIR="$c"; break; fi
    done
  fi
  [ -n "$VENDOR_DIR" ] || skip "no socratic vendor tree resolved (opt-in test)"

  # Pull the enums out of the production script itself — a hardcoded copy here
  # would be the drift this test exists to catch.
  DOMAIN_STEMS="$(bash -c 'eval "$1"; for e in "${DOMAIN_ENUM_ORDER[@]}"; do echo "${e#*:}"; done' \
    _ "$(sed -n '/^DOMAIN_ENUM_ORDER=(/,/^)/p' "$SLICE")")"
  PACKS="$(bash -c 'eval "$1"; for p in "${PACK_ENUM[@]}"; do echo "$p"; done' \
    _ "$(sed -n '/^PACK_ENUM=(/,/^)/p' "$SLICE")")"
  [ -n "$DOMAIN_STEMS" ] || skip "could not extract DOMAIN_ENUM_ORDER"
  [ -n "$PACKS" ] || skip "could not extract PACK_ENUM"
}

@test "every domain enum stem resolves to a question file in the vendor tree" {
  local stem missing=""
  while IFS= read -r stem; do
    if [ ! -f "$VENDOR_DIR/questions/$stem.md" ] \
       && [ ! -f "$VENDOR_DIR/questions/core/$stem.md" ]; then
      missing="$missing $stem"
    fi
  done <<< "$DOMAIN_STEMS"
  [ -z "$missing" ] || { echo "enum stems with no vendor file:$missing"; false; }
}

@test "vendor tree has no domain question files outside the enum" {
  local f stem extra=""
  for f in "$VENDOR_DIR"/questions/*.md "$VENDOR_DIR"/questions/core/*.md; do
    [ -f "$f" ] || continue
    stem="$(basename "$f" .md)"
    # Only NN-prefixed files participate in the domain enum contract.
    [[ "$stem" =~ ^[0-9][0-9]- ]] || continue
    grep -qx "$stem" <<< "$DOMAIN_STEMS" || extra="$extra $stem"
  done
  [ -z "$extra" ] || { echo "vendor domain files missing from enum:$extra"; false; }
}

@test "every pack enum entry has packs/<name>/core.md" {
  local p missing=""
  while IFS= read -r p; do
    [ -f "$VENDOR_DIR/packs/$p/core.md" ] || missing="$missing $p"
  done <<< "$PACKS"
  [ -z "$missing" ] || { echo "enum packs with no core.md:$missing"; false; }
}

@test "vendor tree has no packs outside the enum" {
  local d p extra=""
  for d in "$VENDOR_DIR"/packs/*/; do
    [ -f "${d}core.md" ] || continue
    p="$(basename "$d")"
    # `_template` is upstream's scaffold for authoring a new pack, not a
    # selectable one — it ships a core.md but must never appear in PACK_ENUM.
    case "$p" in _*) continue ;; esac
    grep -qx "$p" <<< "$PACKS" || extra="$extra $p"
  done
  [ -z "$extra" ] || { echo "vendor packs missing from enum:$extra"; false; }
}
