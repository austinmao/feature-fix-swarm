#!/usr/bin/env bash
# model-equivalents.sh — sourceable lib: cross-vendor (Claude <-> Codex) model
# equivalence map. Function definitions only — no side effects on source,
# safe under `set -u`.
#
# Codex CLI 0.144 effort enum (API-validated): none|minimal|low|medium|high|xhigh.
# "ultra"/"max" are CLI-accepted aliases that do NOT appear in the enum —
# canonical top is xhigh. See docs/fable-pilotfish-alignment.md (v4.5.1 note).
#
# Map (docs/fable-pilotfish-alignment.md ~L90-98, plan-adversary.sh defaults):
#   fable  -> gpt-5.6-sol   / xhigh
#   opus   -> gpt-5.6-sol   / xhigh
#   sonnet -> gpt-5.6-terra / high
#   haiku  -> gpt-5.6-luna  / medium
#
# Reverse (codex -> claude) intentionally collapses sol -> opus, never fable —
# fable availability flaps (see model-fallback.sh); opus is the stable pin.
#
# Each function accepts either a short alias (fable, opus, sonnet, haiku) or
# a full model ID (claude-fable-5, claude-opus-4-8, claude-sonnet-5,
# claude-haiku-4-5-20251001, gpt-5.6-sol, ...) via substring match.
# Unknown input: echoes the input unchanged, returns 1 (fail-soft).

codex_equiv_model() {
  case "$1" in
    *fable*|*opus*) echo "gpt-5.6-sol" ;;
    *sonnet*) echo "gpt-5.6-terra" ;;
    *haiku*) echo "gpt-5.6-luna" ;;
    *) echo "$1"; return 1 ;;
  esac
}

codex_equiv_effort() {
  case "$1" in
    *fable*|*opus*) echo "xhigh" ;;
    *sonnet*) echo "high" ;;
    *haiku*) echo "medium" ;;
    *) echo "$1"; return 1 ;;
  esac
}

claude_equiv_model() {
  case "$1" in
    *sol*) echo "opus" ;;
    *terra*) echo "sonnet" ;;
    *luna*) echo "haiku" ;;
    *) echo "$1"; return 1 ;;
  esac
}
