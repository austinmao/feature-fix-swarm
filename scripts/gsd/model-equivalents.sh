#!/usr/bin/env bash
# model-equivalents.sh — sourceable lib: cross-vendor (Claude <-> Codex) model
# equivalence map. Function definitions only — no side effects on source,
# safe under `set -u`.
#
# Codex CLI 0.144 effort enum (API-validated): none|minimal|low|medium|high|xhigh.
# "ultra"/"max" are CLI-accepted aliases that do NOT appear in the enum —
# canonical top is xhigh. See docs/fable-pilotfish-alignment.md (v4.5.1 note).
#
# Map (docs/fable-pilotfish-alignment.md ~L90-98, plan-adversary.sh defaults;
# effort split per spec-004 AC-004 — frontier (fable) regains the one bit of
# distinction the model-collapse lost; judgment repinned to gpt-6.2-sol/xhigh):
#   fable  -> gpt-6.2-astra / xhigh
#   opus   -> gpt-6.2-sol   / xhigh
#   sonnet -> gpt-6.1-terra / medium
#   haiku  -> gpt-6-luna    / low
#
# The previous pins (gpt-6-astra, gpt-6-sol, gpt-5.6-terra, gpt-5.6-luna) are
# NOT tier aliases here: like gpt-5.6-sol they are unknown to this map, so an
# explicitly pinned legacy id is never rewritten to a newer model. They are
# retained as pinned by codex-model-sync.sh's native pin table.
#
# gpt-5.6-sol remains a recognized, valid model (a lower admission rung in
# adversary-host.sh's ladder and codex-model-sync.sh's native pin table) — it
# is just no longer the judgment-tier default.
#
# Reverse (codex -> claude) intentionally collapses sol -> opus, never fable —
# fable availability flaps (see model-fallback.sh); opus is the stable pin.
#
# Each function accepts either a short alias (fable, opus, sonnet, haiku) or
# a full model ID (claude-fable-5-2, claude-opus-5-5, claude-sonnet-5-5,
# claude-haiku-5-5, gpt-6.2-sol, ...) via substring match.
# Unknown input: echoes the input unchanged, returns 1 (fail-soft).

codex_equiv_model() {
  case "$1" in
    gpt-6.2-astra|*fable*) echo "gpt-6.2-astra" ;;
    gpt-6.2-sol|*opus*) echo "gpt-6.2-sol" ;;
    gpt-6.1-terra|*sonnet*) echo "gpt-6.1-terra" ;;
    gpt-6-luna|*haiku*) echo "gpt-6-luna" ;;
    *) echo "$1"; return 1 ;;
  esac
}

codex_equiv_effort() {
  case "$1" in
    gpt-6.2-astra|*fable*) echo "xhigh" ;;
    gpt-6.2-sol|*opus*) echo "xhigh" ;;
    gpt-6.1-terra|*sonnet*) echo "medium" ;;
    gpt-6-luna|*haiku*) echo "low" ;;
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
