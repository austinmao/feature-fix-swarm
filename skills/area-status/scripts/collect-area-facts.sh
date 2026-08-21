#!/usr/bin/env bash
# Deterministic, read-only Stage-1 facts for /area-status.
set -u

AREA="${1:-}"
BASE_OVERRIDE=""
LAST_EMIT_COUNT=0
LAST_NONDOC_COUNT=0

if [ -z "$AREA" ]; then
  echo "usage: collect-area-facts.sh <area> [--base <ref>]"
  exit 1
fi
if [[ ! "$AREA" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "malformed-input: rejected area '$AREA'"
  exit 1
fi
shift
if [ "${1:-}" = "--base" ]; then
  BASE_OVERRIDE="${2:-}"
  if [ -z "$BASE_OVERRIDE" ] || [ "$#" -ne 2 ]; then
    echo "usage: collect-area-facts.sh <area> [--base <ref>]"
    exit 1
  fi
elif [ "$#" -ne 0 ]; then
  echo "usage: collect-area-facts.sh <area> [--base <ref>]"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT" || exit 1
FENCE_SH=""
for candidate in "$ROOT/packages/feature-fix-swarm/scripts/gsd/fence-data.sh" \
                 "$SCRIPT_DIR/../../../scripts/gsd/fence-data.sh" \
                 "$ROOT/scripts/gsd/fence-data.sh"; do
  [ -f "$candidate" ] && FENCE_SH="$candidate" && break
done

sanitize_name() {
  # A visible replacement makes path truncation explicit while preventing a
  # newline-bearing path from forging a fact-section header.
  printf '%s' "$1" | tr '[:cntrl:]' '?'
}

emit_none_or_capped() {
  local raw="$1" sanitized count
  sanitized="$(mktemp "${TMPDIR:-/tmp}/area-status.XXXXXX")" || return 1
  while IFS= read -r -d '' name; do
    sanitize_name "$name"
    printf '\n'
  done < "$raw" > "$sanitized" || { rm -f "$sanitized"; return 1; }
  sort "$sanitized" -o "$sanitized" || { rm -f "$sanitized"; return 1; }
  count="$(sed '/^$/d' "$sanitized" | wc -l | tr -d ' ')"
  LAST_EMIT_COUNT="$count"
  if [ "$count" -eq 0 ]; then
    echo "(none)"
  else
    head -30 "$sanitized"
    if [ "$count" -gt 30 ]; then
      echo "truncated: showing 30 of $count"
    fi
  fi
  rm -f "$sanitized"
}

emit_spec_state() {
  local raw
  echo "== SPEC_ESTATE =="
  [ -d specs ] || { echo "(none)"; return 0; }
  raw="$(mktemp "${TMPDIR:-/tmp}/area-specs.XXXXXX")" || return 1
  find specs -maxdepth 1 -type d -name "*$AREA*" -print0 > "$raw"
  rc=$?
  if [ "$rc" -ne 0 ]; then rm -f "$raw"; return "$rc"; fi
  emit_none_or_capped "$raw"
  rc=$?
  rm -f "$raw"
  return "$rc"
}

is_doc_artifact() {
  # A path is treated as documentation/planning/evidence, not real code, when
  # it lives under one of these trees or carries a .md extension. This is
  # the classification the gap-round-2 fix needs: a literal-token path hit
  # inside an evidence report or spec doc must not count as "real code
  # surface exists" for the CITED_SURFACES dispatch decision below — that
  # was the exact self-collision the gap-round re-run hit (a report file
  # named `PATH-001-<area>.md` made CODE_SURFACE non-empty and suppressed
  # citation mining before it could find the area's actual code surface).
  case "$1" in
    specs/*|docs/*|openwiki/*|.planning/*) return 0 ;;
    *.md) return 0 ;;
  esac
  return 1
}

emit_code_paths() {
  local section="$1" mode="$2" raw path filtered
  echo "== $section =="
  raw="$(mktemp "${TMPDIR:-/tmp}/area-code.XXXXXX")" || return 1
  filtered="$(mktemp "${TMPDIR:-/tmp}/area-filtered.XXXXXX")" || { rm -f "$raw"; return 1; }
  git ls-files -z > "$raw"
  rc=$?
  if [ "$rc" -ne 0 ]; then rm -f "$raw" "$filtered"; return "$rc"; fi
  while IFS= read -r -d '' path; do
    case "$path" in *"$AREA"*) ;; *) continue ;; esac
    if [ "$mode" = "tests" ]; then
      case "$path" in tests/*|test/*|*/tests/*|*/test/*|*.bats|*test*) ;; *) continue ;; esac
    fi
    printf '%s\0' "$path" >> "$filtered"
  done < "$raw" || { rm -f "$raw" "$filtered"; return 1; }
  emit_none_or_capped "$filtered"
  rc=$?
  if [ "$section" = "CODE_SURFACE" ]; then
    local nondoc_count=0 p
    while IFS= read -r -d '' p; do
      is_doc_artifact "$p" || nondoc_count=$((nondoc_count + 1))
    done < "$filtered"
    echo "code-surface-nondoc-count: $nondoc_count"
    LAST_NONDOC_COUNT="$nondoc_count"
  fi
  rm -f "$raw" "$filtered"
  return "$rc"
}

# PATH-001 gap round: when the literal-token CODE_SURFACE scan has no real
# (non-doc) code hits, the area's real owning surface may still be reachable
# through spec/doc prose that discusses the concept under a different name
# but cites concrete repo paths (2026-08-21 finding: web/src/components/media/*
# and pipelines/website/contracts/native-photo-fill.yaml carried zero literal
# occurrences of "photo-picker" anywhere, yet were the area's actual surface,
# and were cited by path in five predecessor specs). This mines those specs
# for cited paths and lists the ones that resolve in the working tree.
# Fail-soft, read-only, exits 0 like every other section.
#
# gap-round-2: gated on the NON-DOC code-surface count, not the raw
# CODE_SURFACE count. A literal-token hit that is itself a doc/evidence
# artifact (e.g. this skill's own `PATH-001-<area>.md` report) must not
# suppress citation mining — that self-collision is exactly what made the
# gap-round-1 re-run miss the area's real surface a second time.
emit_cited_surfaces() {
  local nondoc_count="$1"
  echo "== CITED_SURFACES =="
  if [ "$nondoc_count" -ne 0 ]; then
    echo "(skipped: code surface non-empty)"
    return 0
  fi

  local search_dirs=() d
  for d in specs docs openwiki; do
    [ -d "$d" ] && search_dirs+=("$d")
  done
  if [ "${#search_dirs[@]}" -eq 0 ]; then
    echo "(none)"
    return 0
  fi

  local hit_files cand_raw resolved rc
  hit_files="$(mktemp "${TMPDIR:-/tmp}/area-cited-hits.XXXXXX")" || return 1
  # Content match (git grep, fixed-string), not path match: this is the
  # mechanism CODE_SURFACE lacks.
  git grep -lIF -- "$AREA" -- "${search_dirs[@]}" > "$hit_files" 2>/dev/null
  if [ ! -s "$hit_files" ]; then
    echo "(none)"
    rm -f "$hit_files"
    return 0
  fi

  cand_raw="$(mktemp "${TMPDIR:-/tmp}/area-cited-cand.XXXXXX")" || { rm -f "$hit_files"; return 1; }
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    # Repo-relative path grammar: at least one internal slash, ending in a
    # dotted extension. The trailing `grep -v '://'` drops URLs, which this
    # grammar would otherwise also match.
    grep -oE '[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9]+' "$f" 2>/dev/null
  done < "$hit_files" | grep -v '://' | sort -u > "$cand_raw"
  rc=$?
  rm -f "$hit_files"
  if [ "$rc" -gt 1 ]; then rm -f "$cand_raw"; return 1; fi

  resolved="$(mktemp "${TMPDIR:-/tmp}/area-cited-resolved.XXXXXX")" || { rm -f "$cand_raw"; return 1; }
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    git ls-files --error-unmatch -- "$cand" >/dev/null 2>&1 && printf '%s\n' "$cand" >> "$resolved"
  done < "$cand_raw"
  rm -f "$cand_raw"

  if [ ! -s "$resolved" ]; then
    echo "(none)"
    rm -f "$resolved"
    return 0
  fi
  local count
  count="$(wc -l < "$resolved" | tr -d ' ')"
  head -30 "$resolved"
  [ "$count" -gt 30 ] && echo "truncated: showing 30 of $count"
  rm -f "$resolved"
  return 0
}

file_mtime() {
  # BSD stat's -f must be tried before GNU stat's -c: GNU stat also accepts
  # -f but with a different meaning, so the order is not interchangeable.
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null
}

# Freshness timestamp for one evidence path, tracked-commit-time first. A plain
# mtime read would report every file as fresh right after a clone or branch
# switch, since checkout resets mtimes to "now" for every file it writes — the
# exact false-green this feature exists to prevent. Untracked (or
# staged-but-unborn) paths fall back to mtime; the caller is responsible for
# announcing that fallback via the returned "untracked" flag.
evidence_time() {
  local path="$1" ct
  ct="$(git log -1 --format=%ct -- "$path" 2>/dev/null)"
  if [ -n "$ct" ]; then
    printf '%s 0\n' "$ct"
    return 0
  fi
  printf '%s 1\n' "$(file_mtime "$path")"
}

find_evidence_paths() {
  # Every regular file under a resolved SPEC_ESTATE entry's evidence/
  # directory, plus any tracked evidence/ path under .planning/ matching the
  # area token.
  local specdir
  if [ -d specs ]; then
    for specdir in specs/*"$AREA"*; do
      [ -d "$specdir/evidence" ] || continue
      find "$specdir/evidence" -type f -print0 2>/dev/null
    done
  fi
  if [ -d .planning ]; then
    git ls-files -z -- '.planning' 2>/dev/null | while IFS= read -r -d '' p; do
      case "$p" in *evidence*"$AREA"*|*"$AREA"*evidence*) printf '%s\0' "$p" ;; esac
    done
  fi
}

emit_area_facts() {
  local head_sha base_ref behind rc
  EMIT_STATUS=0

  echo "== MEASURED_REF =="
  head_sha="$(git rev-parse HEAD 2>/dev/null)"
  if [ -z "$head_sha" ]; then
    echo "SECTION-ERROR: MEASURED_REF unable to resolve HEAD"
    EMIT_STATUS=1
    return "$EMIT_STATUS"
  fi
  echo "measured-ref: $head_sha"

  echo "== BEHIND_COUNT =="
  base_ref="UNKNOWN-BASELINE"
  if [ -n "$BASE_OVERRIDE" ] && git rev-parse --verify "${BASE_OVERRIDE}^{commit}" >/dev/null 2>&1; then
    base_ref="$BASE_OVERRIDE"
  elif [ -z "$BASE_OVERRIDE" ] && git rev-parse --verify '@{upstream}' >/dev/null 2>&1; then
    base_ref='@{upstream}'
  fi
  echo "base-ref: $base_ref"
  if [ "$base_ref" = "UNKNOWN-BASELINE" ]; then
    echo "behind-count: UNKNOWN"
    echo "UNKNOWN-BASELINE: no upstream or detached HEAD; colored verdicts refused"
  else
    behind="$(git rev-list --count "HEAD..$base_ref" 2>/dev/null)"
    if [[ ! "$behind" =~ ^[0-9]+$ ]]; then
      echo "SECTION-ERROR: BEHIND_COUNT unable to compare $base_ref"
      EMIT_STATUS=1
      return "$EMIT_STATUS"
    fi
    echo "behind-count: $behind"
    [ "$behind" -gt 0 ] && echo "DRIFT: $behind commits behind $base_ref"
  fi

  if ! emit_spec_state; then
    echo "SECTION-ERROR: SPEC_ESTATE unreadable source"
    EMIT_STATUS=1
    return "$EMIT_STATUS"
  fi
  if ! emit_code_paths "CODE_SURFACE" code; then
    echo "SECTION-ERROR: CODE_SURFACE unreadable source"
    EMIT_STATUS=1
    return "$EMIT_STATUS"
  fi
  local nondoc_count="$LAST_NONDOC_COUNT"
  if ! emit_cited_surfaces "$nondoc_count"; then
    echo "SECTION-ERROR: CITED_SURFACES unreadable source"
    EMIT_STATUS=1
    return "$EMIT_STATUS"
  fi
  if ! emit_code_paths "TEST_INVENTORY" tests; then
    echo "SECTION-ERROR: TEST_INVENTORY unreadable source"
    EMIT_STATUS=1
    return "$EMIT_STATUS"
  fi

  local now newest_path="" newest_time="" newest_untracked=0 path ct t untracked
  now="$(date +%s)"
  while IFS= read -r -d '' path; do
    read -r t untracked <<< "$(evidence_time "$path")"
    [[ "$t" =~ ^[0-9]+$ ]] || continue
    if [ -z "$newest_time" ] || [ "$t" -gt "$newest_time" ]; then
      newest_time="$t"
      newest_path="$path"
      newest_untracked="$untracked"
    fi
  done < <(find_evidence_paths)

  echo "== NEWEST_EVIDENCE =="
  if [ -z "$newest_path" ]; then
    echo "newest-evidence: none"
  else
    [ "$newest_untracked" -eq 1 ] && echo "uncommitted-evidence: $newest_path"
    echo "newest-evidence: $newest_path (age-days: $(( (now - newest_time) / 86400 )))"
  fi

  echo "== POST_EVIDENCE_COMMITS =="
  # Two glob shapes, unioned: `**/*AREA*` matches the token in the final
  # path component (basename) only — a git pathspec `*` never crosses `/` —
  # while `**/*AREA*/**` matches the token in any DIRECTORY component. Using
  # only the first left CODE_SURFACE (which matches the token anywhere in
  # the path) drift-scan-blind to any file under an area-named directory
  # whose own filename doesn't repeat the token (gap round, EDGE-007).
  local pathspec=":(glob)**/*$AREA*" pathspec_dir=":(glob)**/*$AREA*/**" post_raw post_seen post_count=0
  echo "pathspec-scanned: $pathspec $pathspec_dir"
  post_raw="$(mktemp "${TMPDIR:-/tmp}/area-post-evidence.XXXXXX")" || return 1
  post_seen="$(mktemp "${TMPDIR:-/tmp}/area-post-seen.XXXXXX")" || { rm -f "$post_raw"; return 1; }
  if [ -n "$newest_time" ]; then
    # Two sources, unioned: (1) a pathspec-diff scan, which is the only way
    # to see a commit that DELETED or RENAMED an area path out of the
    # resolved surface; (2) a subject-text scan, which is the only way to
    # see a commit that references the area but touches no file matching it
    # (e.g. an --allow-empty marker or revert). Neither alone is complete —
    # git's history simplification hides a truly empty commit from ANY
    # pathspec-restricted log, no matter how broad the pathspec.
    while IFS=' ' read -r ct sha rest; do
      [[ "$ct" =~ ^[0-9]+$ ]] || continue
      [ "$ct" -gt "$newest_time" ] || continue
      grep -qxF "$sha" "$post_seen" 2>/dev/null && continue
      printf '%s\n' "$sha" >> "$post_seen"
      printf '%s %s\n' "$ct" "$(sanitize_name "$sha $rest")" >> "$post_raw"
      post_count=$((post_count + 1))
    done < <(
      git log --format='%ct %h %s' --diff-filter=ADMR -- "$pathspec" "$pathspec_dir" 2>/dev/null
      git log --format='%ct %h %s' 2>/dev/null | grep -F -- "$AREA"
    )
  fi
  if [ "$post_count" -eq 0 ]; then
    echo "(none)"
  else
    sort -t' ' -k1,1nr "$post_raw" | cut -d' ' -f2- | head -30
    [ "$post_count" -gt 30 ] && echo "truncated: showing 30 of $post_count"
  fi
  rm -f "$post_raw" "$post_seen"

  echo "== VERDICT_HINT =="
  if [ "$post_count" -gt 0 ] || [ -z "$newest_path" ]; then
    echo "VERDICT_HINT: UNMEASURED"
  else
    echo "VERDICT_HINT: MEASURABLE"
  fi

  # All source enumerations were empty, not merely unavailable.
  if ! find specs -maxdepth 1 -type d -name "*$AREA*" -print -quit 2>/dev/null | grep -q . \
    && ! git ls-files | grep -F -- "$AREA" >/dev/null 2>&1; then
    echo "WARN: area '$AREA' matched no specs, code paths, or tests"
  fi
  return "$EMIT_STATUS"
}

if [ -n "$FENCE_SH" ]; then
  # shellcheck source=/dev/null
  . "$FENCE_SH"
  set -o pipefail
  emit_area_facts | fence_data AREA
  pipeline_status=("${PIPESTATUS[@]}")
  set +o pipefail
  if [ "${pipeline_status[0]}" -ne 0 ] || [ "${pipeline_status[1]}" -ne 0 ]; then
    exit 1
  fi
else
  echo "collect-area-facts: WARN fence-data.sh not found in candidate chain; emitting unfenced" >&2
  echo "UNFENCED: fence-data.sh unresolved; stream is not prompt-delimited"
  emit_area_facts || exit 1
fi
