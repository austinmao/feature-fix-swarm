#!/usr/bin/env bash
# security-surface.sh — sourceable lib: the shared security-surface keyword
# pattern (one home for the list). Sourced by security-model-fence.sh
# (content-grep mode) and review-tier.sh (path-match mode).
#
# No side effects on source (definitions only) — safe to source under `set -u`.

# shellcheck disable=SC2034  # consumed by sourcing scripts, not this file
KEYWORDS='auth|rls|row[ _-]?level|payment|stripe|crypto|jwt|jwks|oauth|owasp|secret|credential|password'
