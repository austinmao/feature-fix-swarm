#!/usr/bin/env bash
# Resolve which gsd planning project a spec run uses.
#
# A repo can hold several gsd projects: the root one (.planning/ROADMAP.md)
# and per-spec ones (.planning/<project>/ROADMAP.md, selected in gsd-core by
# GSD_PROJECT). Reading the root ROADMAP for a spec that plans under its own
# project silently executes another project's phases, so feature-implement
# resolves the scope here once and fails closed on any ambiguity.
#
# Usage: planning-scope.sh <spec-id>      (NNN or NNN-slug)
# Prints one line: <project|-> <planning-root> <mode>
#   mode=session  GSD_PROJECT is already set in this process env and valid
#   mode=derived  the spec has .planning/<project>/ but GSD_PROJECT is unset;
#                 interactive execution needs a session relaunched with it
#   mode=root     no per-spec project; the root .planning/ project applies
# Exit 2: usage error, invalid GSD_PROJECT, a GSD_PROJECT with no ROADMAP,
#         or a GSD_PROJECT that contradicts the spec's own project.
set -uo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: planning-scope.sh <spec-id>" >&2
  exit 2
fi

SPEC_ID="$1"
if ! [[ "$SPEC_ID" =~ ^[0-9]{3}(-[A-Za-z0-9][A-Za-z0-9._-]*)?$ ]] || [[ "$SPEC_ID" == *..* ]]; then
  echo "planning-scope: ERROR: spec id must be NNN or NNN-slug: $SPEC_ID" >&2
  exit 2
fi
SPEC_NUM="${SPEC_ID:0:3}"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

has_project() { [ -f "$REPO_ROOT/.planning/$1/ROADMAP.md" ]; }

SPEC_PROJECT=""
for candidate in "$SPEC_ID" "$SPEC_NUM"; do
  if has_project "$candidate"; then
    SPEC_PROJECT="$candidate"
    break
  fi
done

ENV_PROJECT="${GSD_PROJECT:-}"
ENV_PROJECT="${ENV_PROJECT#"${ENV_PROJECT%%[![:space:]]*}"}"
ENV_PROJECT="${ENV_PROJECT%"${ENV_PROJECT##*[![:space:]]}"}"

if [ -n "$ENV_PROJECT" ]; then
  if ! [[ "$ENV_PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || [[ "$ENV_PROJECT" == *..* ]]; then
    echo "planning-scope: ERROR: GSD_PROJECT must be one path segment ([A-Za-z0-9._-], no '..')" >&2
    exit 2
  fi
  if ! has_project "$ENV_PROJECT"; then
    echo "planning-scope: ERROR: GSD_PROJECT=$ENV_PROJECT but .planning/$ENV_PROJECT/ROADMAP.md is missing" >&2
    exit 2
  fi
  if [ -n "$SPEC_PROJECT" ] && [ "$SPEC_PROJECT" != "$ENV_PROJECT" ]; then
    echo "planning-scope: ERROR: GSD_PROJECT=$ENV_PROJECT but spec $SPEC_ID plans under .planning/$SPEC_PROJECT/ — unset GSD_PROJECT or set it to $SPEC_PROJECT" >&2
    exit 2
  fi
  printf '%s %s %s\n' "$ENV_PROJECT" ".planning/$ENV_PROJECT" session
  exit 0
fi

if [ -n "$SPEC_PROJECT" ]; then
  printf '%s %s %s\n' "$SPEC_PROJECT" ".planning/$SPEC_PROJECT" derived
  exit 0
fi

printf '%s %s %s\n' - .planning root
