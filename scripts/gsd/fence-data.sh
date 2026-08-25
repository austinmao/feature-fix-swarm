#!/usr/bin/env bash
# fence-data.sh — the single shared prompt-boundary fence (REQ-401).
# Sourceable, bash 3.2, no associative arrays; safe to source twice.
#
# Untrusted doc bytes crossing into model prompts are wrapped between
# ${TAG}_DATA_START / ${TAG}_DATA_END delimiter lines. Counterfeit PRIMARY
# markers INSIDE the body are rewritten (never dropped) to
# ${TAG}_DATA_ESCAPED in EVERY branch of every consumer, so a doc cannot
# fake an early fence close. Neutralization is byte-identity on marker-free
# input — existing clean-input byte pins (spec-005 AC-005/AC-008,
# socratic-plan-wall-baseline.prompt) survive unmodified.
#
# fence_neutralize <TAG>     stdin -> stdout filter. Callers embedding their
#                            own delimiters (plan-wall: the genuine
#                            PLAN_DATA_START lives inside PW_REVIEW_BRIEF)
#                            neutralize ONLY the untrusted payload, never the
#                            assembled prompt (wall residual 0a909156).
# fence_data <TAG> [body]    full fenced block built on the neutralizer.
#                            With a body arg: START, body + newline, END.
#                            Without: the stream on stdin is embedded
#                            VERBATIM (trailing-newline byte-identity, wall
#                            a12a8559 — callers pipe files/stdin, never
#                            command substitution).

# fence_neutralize <TAG> — stdin->stdout counterfeit-marker rewrite.
# BSD sed terminates an unterminated final line; the tail/head dance strips
# that added byte back off so marker-free input round-trips byte-identical
# for 0, 1, and 2 trailing newlines alike.
fence_neutralize() {
  local _fn_tag="$1" _fn_in _fn_out
  _fn_in="$(mktemp)" || return 1
  _fn_out="$(mktemp)" || { rm -f "$_fn_in"; return 1; }
  cat > "$_fn_in"
  sed -e "s/${_fn_tag}_DATA_START/${_fn_tag}_DATA_ESCAPED/g" \
      -e "s/${_fn_tag}_DATA_END/${_fn_tag}_DATA_ESCAPED/g" \
      "$_fn_in" > "$_fn_out"
  if [ -s "$_fn_in" ] && [ -n "$(tail -c 1 "$_fn_in")" ] \
      && [ -s "$_fn_out" ] && [ -z "$(tail -c 1 "$_fn_out")" ]; then
    # sed added a final newline the input did not have — drop it
    head -c "$(($(wc -c < "$_fn_out") - 1))" "$_fn_out"
  else
    cat "$_fn_out"
  fi
  rm -f "$_fn_in" "$_fn_out"
}

# fence_data <TAG> [body] — emit the full fenced block through the
# neutralizer. Body arg is emitted as a line; stdin is embedded verbatim.
fence_data() {
  local _fd_tag="$1"
  printf '%s_DATA_START\n' "$_fd_tag"
  if [ "$#" -ge 2 ]; then
    printf '%s\n' "$2" | fence_neutralize "$_fd_tag"
  else
    fence_neutralize "$_fd_tag"
  fi
  printf '%s_DATA_END\n' "$_fd_tag"
}
