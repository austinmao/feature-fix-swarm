#!/usr/bin/env bash
# trusted-runtime.sh — review-gate fences eval this file as committed on the base
# ref. ffs_trusted_runtime <ref> extracts lib/, scripts/ and skills/ of the FFS
# runtime at <ref> (packaged or flat layout) once per object into
# <git-common-dir>/ffs-trusted/<object-id> and sets FFS_T; on failure FFS_T keeps
# its prior value. The gate so never runs helper code the reviewed diff changed.
# The cache sits in the git dir, which a branch's content cannot write; entries
# unused for 7 days are removed. A read-only git dir falls back to a temp copy.
# bash 3.2 and zsh safe.
ffs_trusted_runtime() {
  local ref="$1" tree="$1" top paths id cache dir old
  [ -n "$ref" ] && top="$(git rev-parse --show-toplevel)" || return 1
  git cat-file -e "${ref}:packages/feature-fix-swarm/lib/gates.py" 2>/dev/null && tree="${ref}:packages/feature-fix-swarm"
  id="$(git -C "$top" rev-parse --verify -q "$tree")" || return 1  # commit id (flat) or tree id (packaged)
  cache="$(cd "$top" && cd "$(git rev-parse --git-common-dir)" && pwd)/ffs-trusted" || return 1
  if [ ! -f "$cache/$id/.complete" ]; then
    paths="$(git -C "$top" ls-tree --name-only "$tree" lib scripts skills 2>/dev/null)"
    [ -n "$paths" ] || return 1
    { mkdir -p "$cache" && dir="$(mktemp -d "$cache/tmp.XXXXXX")"; } 2>/dev/null || { cache="" && dir="$(mktemp -d)"; } || return 1
    # shellcheck disable=SC2046 # one path per line, no spaces (lib/scripts/skills)
    git -C "$top" archive "$tree" $(printf '%s\n' "$paths") | tar -x -C "$dir" && [ -f "$dir/lib/gates.py" ] && : > "$dir/.complete" || { rm -rf "$dir"; return 1; }
    # shellcheck disable=SC2034 # read by the fence that evaluated this file
    [ -n "$cache" ] || { FFS_T="$dir"; return 0; }
    [ -e "$cache/$id" ] || mv "$dir" "$cache/$id"
    rm -rf "$dir" "${cache:?}/${id:?}/${dir##*/}"  # lost race: drop ours, or the copy mv nested inside
    find "$cache" -mindepth 1 -maxdepth 1 ! -name "$id" -mtime +7 -print | while IFS= read -r old; do
      mv "$old" "$cache/del.$$" && rm -rf "$cache/del.$$"  # rename first: an interrupted prune never leaves a half entry
    done
  fi
  [ -f "$cache/$id/.complete" ] || return 1
  touch "$cache/$id"
  # shellcheck disable=SC2034 # read by the fence that evaluated this file
  FFS_T="$cache/$id"
}
