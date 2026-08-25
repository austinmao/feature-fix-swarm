#!/usr/bin/env bash
# Bounded, retrying apt install for CI.
#
# The plain `apt-get update && apt-get install` this replaces has now been
# killed by its job timeout three times (PRs #117, #119 twice): the job log
# ends mid-`apt-get update` fetching an InRelease file and goes silent for
# eight minutes until the runner cancels it.
#
# That failure mode is a HANG, not a nonzero exit, which is why a bare retry
# loop would not have helped — nothing ever returns to retry. Each attempt is
# therefore wrapped in its own `timeout`, so a stuck mirror is reaped and the
# next attempt gets a fresh connection. Three bounded attempts cost at most
# ~9 minutes worst case but in practice clear on the second.
set -uo pipefail

UPDATE_TIMEOUT="${APT_UPDATE_TIMEOUT:-120}"
INSTALL_TIMEOUT="${APT_INSTALL_TIMEOUT:-180}"
ATTEMPTS="${APT_ATTEMPTS:-3}"
# Seam for the test suite: the real thing needs sudo, a fixture does not.
# `-` not `:-` on purpose — an explicitly EMPTY APT_SUDO must mean "no sudo",
# which `:-` would silently override back to sudo.
APT_SUDO="${APT_SUDO-sudo}"

if [ "$#" -eq 0 ]; then
  echo "apt-install: no packages given" >&2
  exit 2
fi

attempt=1
while [ "$attempt" -le "$ATTEMPTS" ]; do
  if timeout "$UPDATE_TIMEOUT" $APT_SUDO apt-get update -q &&
     timeout "$INSTALL_TIMEOUT" $APT_SUDO apt-get install -y -q "$@"; then
    exit 0
  fi
  echo "apt-install: attempt $attempt/$ATTEMPTS failed or timed out for: $*" >&2
  attempt=$((attempt + 1))
  [ "$attempt" -le "$ATTEMPTS" ] && sleep $(( (attempt - 1) * 5 ))
done

echo "apt-install: giving up after $ATTEMPTS attempts: $*" >&2
exit 1
