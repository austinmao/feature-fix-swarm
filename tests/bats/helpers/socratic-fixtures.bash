# Shared bats fixture helpers for socratic-slice.sh's suite and plan 02-02's
# socratic-spec-step.bats. bats shares no functions across files, so both
# suites `load` this file rather than forking it.

# make_vendor_tree <root>
#
# Builds a fixture socratic vendor tree at <root> mirroring the real layout
# at the pin: a SKILL.md, top-level FULL question files (each carrying its
# own sentinel, a "## Verification" section with its own sentinel, and a
# following "## Something else" heading so block-extraction boundaries are
# exercisable), questions/core/ CORE files with NO Verification heading
# (core files run ~120 tokens and carry no Verification block at the pin),
# and THREE real in-enum packs (the cap is two, so a two-pack fixture cannot
# express a legitimate over-cap case).
make_vendor_tree() {
  local root="$1"
  mkdir -p "$root/questions/core"
  mkdir -p "$root/packs/operations" "$root/packs/threat-modeling" "$root/packs/software-design"

  cat > "$root/SKILL.md" <<'EOF'
# fixture SKILL.md
SKILL_MD_SENTINEL
EOF

  cat > "$root/questions/00-requirements.md" <<'EOF'
# Requirements

FULL_REQUIREMENTS_SENTINEL

## Verification
VERIFICATION_REQUIREMENTS_SENTINEL

## Something else
SOMETHING_ELSE_REQUIREMENTS_SENTINEL
EOF

  cat > "$root/questions/05-security.md" <<'EOF'
# Security

FULL_SECURITY_SENTINEL

## Verification
VERIFICATION_SECURITY_SENTINEL

## Something else
SOMETHING_ELSE_SECURITY_SENTINEL
EOF

  cat > "$root/questions/07-testing.md" <<'EOF'
# Testing

FULL_TESTING_SENTINEL

## Verification
VERIFICATION_TESTING_SENTINEL

## Something else
SOMETHING_ELSE_TESTING_SENTINEL
EOF

  cat > "$root/questions/core/00-requirements.md" <<'EOF'
CORE_REQUIREMENTS_SENTINEL
EOF

  cat > "$root/questions/core/05-security.md" <<'EOF'
CORE_SECURITY_SENTINEL
EOF

  cat > "$root/questions/core/07-testing.md" <<'EOF'
CORE_TESTING_SENTINEL
EOF

  cat > "$root/packs/operations/core.md" <<'EOF'
PACK_OPERATIONS_SENTINEL
EOF

  cat > "$root/packs/threat-modeling/core.md" <<'EOF'
PACK_THREAT_MODELING_SENTINEL
EOF

  cat > "$root/packs/software-design/core.md" <<'EOF'
PACK_SOFTWARE_DESIGN_SENTINEL
EOF
}

# make_spec_dir <dir> <frontmatter-body> [<body>] [<header>]
#
# Writes <dir>/socratic.md with an optional <header> block of raw lines
# BEFORE the opening `---`, then `---`, then <frontmatter-body> verbatim,
# then the closing `---`, then the optional <body>. Each caller supplies
# only what it varies.
make_spec_dir() {
  local dir="$1" frontmatter="$2" body="${3:-}" header="${4:-}"
  mkdir -p "$dir"
  {
    if [ -n "$header" ]; then
      printf '%s\n' "$header"
    fi
    echo "---"
    printf '%s\n' "$frontmatter"
    echo "---"
    if [ -n "$body" ]; then
      printf '%s\n' "$body"
    fi
  } > "$dir/socratic.md"
}

# stage_script_root <root>
#
# Copies scripts/gsd/socratic-slice.sh into <root>/scripts/gsd/, preserving
# the executable bit. Because the helper derives its repo root as
# SCRIPT_DIR/../.., invoking the STAGED copy makes the stage-two candidate
# locations (.agents/skills/socratic, .claude/skills/socratic) resolve
# INSIDE <root> instead of the real checkout — the seam that makes the
# no-fall-through test falsifiable.
stage_script_root() {
  local root="$1"
  local repo_root
  repo_root="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  mkdir -p "$root/scripts/gsd"
  cp "$repo_root/scripts/gsd/socratic-slice.sh" "$root/scripts/gsd/socratic-slice.sh"
  # fence-data.sh ships beside socratic-slice.sh (REQ-401b: the slice is a
  # gate and hard-fails without its fence sibling)
  cp "$repo_root/scripts/gsd/fence-data.sh" "$root/scripts/gsd/fence-data.sh"
  chmod +x "$root/scripts/gsd/socratic-slice.sh"
}
