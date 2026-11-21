# trusted-runtime.sh — review-gate fences eval this file as committed on the base
# ref. ffs_trusted_runtime <ref> copies lib/, scripts/ and skills/ of the FFS
# runtime at <ref> (packaged or flat layout) into a temp dir and sets FFS_T; on
# failure FFS_T keeps its prior value. The gate so never runs helper code the
# reviewed diff changed. bash 3.2 and zsh safe.
ffs_trusted_runtime() {
  local ref="$1" tree="$1" top paths dir
  [ -n "$ref" ] && top="$(git rev-parse --show-toplevel)" || return 1
  git cat-file -e "${ref}:packages/feature-fix-swarm/lib/gates.py" 2>/dev/null && tree="${ref}:packages/feature-fix-swarm"
  paths="$(git -C "$top" ls-tree --name-only "$tree" lib scripts skills 2>/dev/null)"
  [ -n "$paths" ] || return 1
  dir="$(mktemp -d)" || return 1
  # shellcheck disable=SC2046 # one path per line, no spaces (lib/scripts/skills)
  git -C "$top" archive "$tree" $(printf '%s\n' "$paths") | tar -x -C "$dir" || return 1
  # shellcheck disable=SC2034 # read by the fence that evaluated this file
  FFS_T="$dir"
}
