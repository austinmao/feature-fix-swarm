#!/usr/bin/env bash
# Sourceable pid/lease lock primitive.  Callers retain their own lifecycle work.

ffs_lock_file_epoch() {
  local value
  value="$(stat -f %m "$1" 2>/dev/null || true)"
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    value="$(stat -c %Y "$1" 2>/dev/null || true)"
  fi
  [[ "$value" =~ ^[0-9]+$ ]] && printf '%s\n' "$value"
}

ffs_lock_refuse_symlink() {
  local path label="$1"
  shift
  for path in "$@"; do
    [ ! -L "$path" ] || {
      echo "$label: refusing symlinked lock path: $path" >&2
      return 78
    }
  done
}

ffs_lock_claim() {
  local pidfile="$1" machine="$2" claimed_epoch
  claimed_epoch="$(date +%s)"
  (
    set -C
    {
      printf '%s\n' "$$"
      printf 'machine=%s\n' "$machine"
      printf 'claimed_epoch=%s\n' "$claimed_epoch"
    } > "$pidfile"
  ) 2>/dev/null
}

ffs_lock_foreign_epoch() {
  local heartbeat="$1" claimed="$2" value best=0
  value="$(ffs_lock_file_epoch "$heartbeat" 2>/dev/null || true)"
  [[ "$claimed" =~ ^[0-9]+$ ]] && best="$claimed"
  if [[ "$value" =~ ^[0-9]+$ ]] && [ "$value" -gt "$best" ]; then best="$value"; fi
  [ "$best" -gt 0 ] && printf '%s\n' "$best"
}

ffs_lock_release_reclaim() {
  local reclaim="$1"
  [ ! -L "$reclaim" ] || return 0
  rm -f "$reclaim/owner"
  rmdir "$reclaim" 2>/dev/null || true
}

# ffs_lock_acquire PIDFILE HEARTBEAT RECLAIM_DIR MACHINE FOREIGN_LEASE RECLAIM_LEASE LABEL [GUARDED_PATH...]
# Returns 0 owned, 75 ordinary contention, 78 tampered/symlink path.
ffs_lock_acquire() {
  local pidfile="$1" heartbeat="$2" reclaim="$3" machine="$4" foreign_lease="$5"
  local reclaim_lease="$6" label="$7"
  shift 7
  local live_pid owner_machine claimed_epoch heartbeat_epoch reclaim_epoch now age attempt=0
  [ -n "$label" ] || label="ffs-lock"
  case "$foreign_lease" in ''|*[!0-9]*|0) foreign_lease=120 ;; esac
  case "$reclaim_lease" in ''|*[!0-9]*|0) reclaim_lease=30 ;; esac
  ffs_lock_refuse_symlink "$label" "$@" "$pidfile" "$heartbeat" "$reclaim" || return $?

  while [ "$attempt" -lt 20 ]; do
    attempt=$((attempt + 1))
    if [ -d "$reclaim" ]; then
      [ ! -L "$reclaim" ] || { echo "$label: refusing symlinked lock path: $reclaim" >&2; return 78; }
      reclaim_epoch="$(ffs_lock_file_epoch "$reclaim" 2>/dev/null || true)"
      now="$(date +%s)"
      if [[ "$reclaim_epoch" =~ ^[0-9]+$ ]] && [ $((now - reclaim_epoch)) -gt "$reclaim_lease" ]; then
        ffs_lock_release_reclaim "$reclaim"
        continue
      fi
      sleep 0.05
      continue
    fi
    if ffs_lock_claim "$pidfile" "$machine"; then return 0; fi
    ffs_lock_refuse_symlink "$label" "$@" "$pidfile" "$heartbeat" "$reclaim" || return $?
    live_pid="$(head -1 "$pidfile" 2>/dev/null | tr -d '[:space:]' || true)"
    owner_machine="$(sed -n 's/^machine=//p' "$pidfile" 2>/dev/null | head -1)"
    claimed_epoch="$(sed -n 's/^claimed_epoch=//p' "$pidfile" 2>/dev/null | head -1)"
    if { [ -z "$owner_machine" ] || [ "$owner_machine" = "$machine" ]; } && [[ "$live_pid" =~ ^[0-9]+$ ]] && kill -0 "$live_pid" 2>/dev/null; then
      echo "$label: active owner holds $pidfile (pid=$live_pid machine=$owner_machine); refusing duplicate launch" >&2; return 75
    fi
    if [ -n "$owner_machine" ] && [ "$owner_machine" != "$machine" ]; then
      heartbeat_epoch="$(ffs_lock_foreign_epoch "$heartbeat" "$claimed_epoch" 2>/dev/null || true)"; now="$(date +%s)"
      if [[ "$heartbeat_epoch" =~ ^[0-9]+$ ]] && [ $((now - heartbeat_epoch)) -le "$foreign_lease" ]; then
        age=$((now - heartbeat_epoch)); echo "$label: foreign owner holds a fresh lease on $pidfile (pid=$live_pid machine=$owner_machine age=${age}s); refusing duplicate launch" >&2; return 75
      fi
    fi
    if ! mkdir "$reclaim" 2>/dev/null; then sleep 0.05; continue; fi
    printf '%s\nmachine=%s\n' "$$" "$machine" > "$reclaim/owner"
    # Under-mutex re-read closes the stale-owner TOCTOU before unlinking.
    live_pid="$(head -1 "$pidfile" 2>/dev/null | tr -d '[:space:]' || true)"
    owner_machine="$(sed -n 's/^machine=//p' "$pidfile" 2>/dev/null | head -1)"
    claimed_epoch="$(sed -n 's/^claimed_epoch=//p' "$pidfile" 2>/dev/null | head -1)"
    if { [ -z "$owner_machine" ] || [ "$owner_machine" = "$machine" ]; } && [[ "$live_pid" =~ ^[0-9]+$ ]] && kill -0 "$live_pid" 2>/dev/null; then
      ffs_lock_release_reclaim "$reclaim"; echo "$label: active owner holds $pidfile (pid=$live_pid machine=$owner_machine); refusing duplicate launch" >&2; return 75
    fi
    if [ -n "$owner_machine" ] && [ "$owner_machine" != "$machine" ]; then
      heartbeat_epoch="$(ffs_lock_foreign_epoch "$heartbeat" "$claimed_epoch" 2>/dev/null || true)"; now="$(date +%s)"
      if [[ "$heartbeat_epoch" =~ ^[0-9]+$ ]] && [ $((now - heartbeat_epoch)) -le "$foreign_lease" ]; then
        age=$((now - heartbeat_epoch)); ffs_lock_release_reclaim "$reclaim"; echo "$label: foreign owner holds a fresh lease on $pidfile (pid=$live_pid machine=$owner_machine age=${age}s); refusing duplicate launch" >&2; return 75
      fi
    fi
    [ ! -L "$pidfile" ] || { ffs_lock_release_reclaim "$reclaim"; echo "$label: refusing symlinked lock path: $pidfile" >&2; return 78; }
    rm -f "$pidfile"
    if ffs_lock_claim "$pidfile" "$machine"; then ffs_lock_release_reclaim "$reclaim"; return 0; fi
    ffs_lock_release_reclaim "$reclaim"
  done
  echo "$label: lock ownership remained contended; refusing duplicate launch" >&2
  return 75
}

# Remove a pidfile only when it still contains this process and machine.
ffs_lock_release() {
  local pidfile="$1" machine="$2" live_pid owner_machine
  [ ! -L "$pidfile" ] || return 78
  [ -f "$pidfile" ] || return 0
  live_pid="$(head -1 "$pidfile" 2>/dev/null | tr -d '[:space:]' || true)"
  owner_machine="$(sed -n 's/^machine=//p' "$pidfile" 2>/dev/null | head -1)"
  if [ "$live_pid" = "$$" ] && [ "$owner_machine" = "$machine" ]; then
    rm -f "$pidfile"
  fi
  return 0
}
