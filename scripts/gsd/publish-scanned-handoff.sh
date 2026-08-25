#!/usr/bin/env bash
# publish-scanned-handoff.sh — the single copy-then-scan-then-publish harness
# shared by every handoff writer (continue-compact, goal-wrap, spec-status).
#
# Order is fixed and is the whole point of this script: copy the artifact to
# a private 0600 temp file, scan THAT copy — never a git index blob, which
# would mean the bytes already reached the object database before detection
# — and only on a clean scan does anything reach the destination. A finding
# touches neither the git index nor the destination.
#
# Usage:
#   publish-scanned-handoff.sh <artifact-path> --commit <message>
#   publish-scanned-handoff.sh <artifact-path> --publish-to <dest>
#
# --commit stages the SCANNED bytes directly via plumbing and commits them,
# scoped to <artifact-path> only. It never routes the scanned bytes back
# through the mutable working tree before the commit: `git commit` given a
# pathspec re-reads the WORKING TREE for that path (verified empirically —
# it does not trust the index alone), which would silently let a
# post-staging working-tree write into the commit. So the commit step here
# takes NO pathspec and instead refuses to run at all if the index already
# holds unrelated staged changes, which is the actual scoping guarantee.
# After a verified commit, the working tree is synced from the scanned copy.
#
# --publish-to copies the scanned bytes to <dest>.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCANNER="$SCRIPT_DIR/scan-handoff-credentials.sh"

usage() {
  echo "usage: publish-scanned-handoff.sh <artifact-path> --commit <message> | --publish-to <dest>" >&2
  exit 2
}

ARTIFACT="${1:-}"
MODE="${2:-}"
ARG="${3:-}"

[ -z "$ARTIFACT" ] && usage
[ "$#" -ne 3 ] && usage
case "$MODE" in
  --commit|--publish-to) ;;
  *) usage ;;
esac
[ -z "$ARG" ] && usage
if [ ! -f "$ARTIFACT" ]; then
  echo "publish-scanned-handoff: artifact not found: $ARTIFACT" >&2
  exit 1
fi

TMP_COPY=""
cleanup() {
  [ -n "$TMP_COPY" ] && rm -f "$TMP_COPY"
}
trap cleanup EXIT

TMP_COPY="$(mktemp)"
chmod 600 "$TMP_COPY"
cp "$ARTIFACT" "$TMP_COPY"

# The scan target is always this private copy. Never scan a git index blob
# (`git show :path` / `git cat-file`) — that would mean the bytes already
# reached the object database before this check ran.
if [ -x "$SCANNER" ]; then
  # Capture the scanner's own exit code directly — `if ! cmd; then` would
  # report the NEGATED result via `$?` inside the block (always 0 there),
  # losing the scanner's actual typed status (credential-output-guard.sh
  # pins exit 2 exactly; that must propagate, not collapse to a bare 1).
  "$SCANNER" "$TMP_COPY"
  scan_rc=$?
  if [ "$scan_rc" -ne 0 ]; then
    echo "publish-scanned-handoff: BLOCKED — credential scan found a match; nothing staged or published" >&2
    exit "$scan_rc"
  fi
else
  echo "publish-scanned-handoff: WARN: credential scanner absent; continuing" >&2
fi

if [ "$MODE" = "--publish-to" ]; then
  DEST="$ARG"
  DEST_DIR="$(dirname "$DEST")"
  [ -d "$DEST_DIR" ] || mkdir -p "$DEST_DIR"
  cp "$TMP_COPY" "$DEST"
  exit 0
fi

# ── --commit ─────────────────────────────────────────────────────────────
ARTIFACT_DIR="$(cd "$(dirname "$ARTIFACT")" && pwd -P)"
ARTIFACT_ABS="$ARTIFACT_DIR/$(basename "$ARTIFACT")"
REPO_ROOT="$(git -C "$ARTIFACT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
  echo "publish-scanned-handoff: not inside a git repository: $ARTIFACT" >&2
  exit 1
fi
case "$ARTIFACT_ABS" in
  "$REPO_ROOT"/*) REL_PATH="${ARTIFACT_ABS#"$REPO_ROOT"/}" ;;
  *)
    echo "publish-scanned-handoff: artifact is outside its own repo root: $ARTIFACT" >&2
    exit 1
    ;;
esac

# Refuse to run if the index already holds unrelated staged changes.
# Committing without a pathspec (required below to dodge git commit's
# pathspec re-add-from-working-tree behavior) would otherwise sweep that
# unrelated staged content into this commit too.
OTHER_STAGED="$(git -C "$REPO_ROOT" diff --cached --name-only -- . ":(exclude)$REL_PATH" 2>/dev/null || true)"
if [ -n "$OTHER_STAGED" ]; then
  echo "publish-scanned-handoff: refusing to commit — other paths are already staged ($OTHER_STAGED); commit or unstage them first" >&2
  exit 1
fi

BLOB="$(git -C "$REPO_ROOT" hash-object -w "$TMP_COPY")"

# Race-free publish (review-gate 2026-08-09): the commit is built in a
# PRIVATE temp index — the shared index is never written, so a concurrent
# `git add` cannot inject unscanned bytes into this commit, and HEAD is
# advanced with a compare-and-swap update-ref that fails instead of
# clobbering if HEAD moved. Replaces the pathless `git commit --no-verify`,
# whose stage-to-commit window could sweep unrelated staged content.
PARENT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
TMP_INDEX="$(mktemp "${TMPDIR:-/tmp}/psh-index.XXXXXX")"
rm -f "$TMP_INDEX"
if ! GIT_INDEX_FILE="$TMP_INDEX" git -C "$REPO_ROOT" read-tree "$PARENT" \
  || ! GIT_INDEX_FILE="$TMP_INDEX" git -C "$REPO_ROOT" update-index --add --cacheinfo 100644,"$BLOB","$REL_PATH"; then
  rm -f "$TMP_INDEX"
  echo "publish-scanned-handoff: temp-index build failed for $REL_PATH" >&2
  exit 1
fi
TREE="$(GIT_INDEX_FILE="$TMP_INDEX" git -C "$REPO_ROOT" write-tree)" || {
  rm -f "$TMP_INDEX"
  echo "publish-scanned-handoff: write-tree failed for $REL_PATH" >&2
  exit 1
}
rm -f "$TMP_INDEX"
COMMIT="$(git -C "$REPO_ROOT" commit-tree "$TREE" -p "$PARENT" -m "$ARG")" || {
  echo "publish-scanned-handoff: commit-tree failed for $REL_PATH" >&2
  exit 1
}
if ! git -C "$REPO_ROOT" update-ref -m "publish-scanned-handoff: $REL_PATH" HEAD "$COMMIT" "$PARENT"; then
  echo "publish-scanned-handoff: HEAD moved during publish — refusing to clobber; re-run" >&2
  exit 1
fi

# Belt-and-braces invariants (construction guarantees both; a failure here
# means something rewrote HEAD outside the CAS and is worth a loud FATAL).
COMMITTED_PATHS="$(git -C "$REPO_ROOT" diff-tree --no-commit-id --name-only -r "$COMMIT")"
if [ "$COMMITTED_PATHS" != "$REL_PATH" ]; then
  echo "publish-scanned-handoff: FATAL: commit swept paths beyond the scanned artifact ($COMMITTED_PATHS) — undoing" >&2
  # CAS-guarded undo: retract exactly OUR commit; if another process already
  # advanced HEAD past it, refuse rather than resetting their commit away.
  git -C "$REPO_ROOT" update-ref -m "publish-scanned-handoff: undo $REL_PATH" HEAD "$PARENT" "$COMMIT" \
    || echo "publish-scanned-handoff: HEAD advanced past the bad commit — manual review required" >&2
  exit 1
fi

# Keep the SHARED index's entry for the artifact in sync with new HEAD so
# `git status` stays clean (scoped to exactly this path; no other entries
# are touched).
git -C "$REPO_ROOT" update-index --add --cacheinfo 100644,"$BLOB","$REL_PATH" || true

COMMITTED_HASH="$(git -C "$REPO_ROOT" rev-parse "$COMMIT:$REL_PATH" 2>/dev/null || echo "")"
if [ "$COMMITTED_HASH" != "$BLOB" ]; then
  echo "publish-scanned-handoff: FATAL: committed bytes for $REL_PATH do not match the scanned copy (got ${COMMITTED_HASH:-none}, expected $BLOB) — undoing" >&2
  # CAS-guarded undo (see above): never reset a commit that isn't ours.
  git -C "$REPO_ROOT" update-ref -m "publish-scanned-handoff: undo $REL_PATH" HEAD "$PARENT" "$COMMIT" \
    || echo "publish-scanned-handoff: HEAD advanced past the bad commit — manual review required" >&2
  # Re-stage the known-clean scanned blob so a later commit still has it
  # ready — never leave post-mismatch (potentially tampered) bytes staged.
  git -C "$REPO_ROOT" update-index --add --cacheinfo 100644,"$BLOB","$REL_PATH"
  exit 1
fi

# Sync the working tree from the scanned copy — only after the commit is
# verified, so the on-disk file and HEAD agree on exactly the scanned bytes.
cp "$TMP_COPY" "$REPO_ROOT/$REL_PATH"
exit 0
