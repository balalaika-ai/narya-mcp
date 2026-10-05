#!/bin/bash
# narya-mcp launcher: run Narya on a remote machine, in a workspace holding a copy of the project.
#
#   ssh-workspace.sh run <reldir> <narya args...>   sync the sources to the host, then run Narya there in <reldir>
#   ssh-workspace.sh signal <pid> <SIGNAL>          deliver a signal to a remote Narya started by this launcher
#
# Each run gets a fresh workspace copied from a precompiled base directory on the host (deleted when the run ends),
# or, with NMCP_WS set, a persistent named workspace (runs in it are serialised with flock).  Narya records absolute
# source paths in .nyo files, so the run takes place in a private mount namespace in which the workspace is
# bind-mounted over the base directory: every workspace sees the same paths as the base.  A watchdog kills Narya if
# its resident memory exceeds a limit or the ssh connection goes away.
#
# Configuration (environment, e.g. from the [env] table of narya-mcp.toml):
#   NMCP_ROOT          local project root (default: the current directory, which narya-mcp sets to the root)
#   NMCP_HOST          ssh host (required)
#   NMCP_REMOTE_BASE   precompiled base directory on the host, e.g. ~/narya-base (required)
#   NMCP_REMOTE_NARYA  Narya executable on the host (required)
#   NMCP_SYNC          space-separated directories of the project to copy (default: "src")
#   NMCP_WS            persistent workspace name under ~/nmcp-ws (default: a temporary workspace per run)
#   NMCP_MAX_RSS_GB    kill Narya above this resident memory (default 20)
#   NMCP_MIN_AVAIL_GB  wait until the host has this much available memory before starting (default 24)
#   NMCP_FROZEN_STAMP  if set (a file name relative to the workspace), delete .nyo files newer than it before the run:
#                      only .nyo files written by -source-only runs are kept (a file compiled while its imports come
#                      from .nyo files gets a much larger .nyo); -source-only runs in a named workspace refresh it.
set -u
HOST=${NMCP_HOST:?set NMCP_HOST}
case "${1:-}" in
  signal)
    # INT is not sent to a Narya waiting for its next command (it would die); exit status 3 says so.
    pid=${2##*:}
    case "$pid$3" in *[!A-Za-z0-9]*) echo "bad signal arguments" >&2; exit 2 ;; esac
    exec ssh -o ConnectTimeout=20 "$HOST" "if [ $3 = INT ] && grep -qxE 'pipe_read|pipe_wait' /proc/$pid/wchan 2>/dev/null; then exit 3; fi; kill -s $3 $pid" ;;
  run) shift ;;
  *) echo "usage: $0 run <reldir> <narya args...> | signal <pid> <SIGNAL>" >&2; exit 2 ;;
esac
reldir=${1:?missing reldir}; shift
ROOT=${NMCP_ROOT:-$PWD}
STAGE="$(date +%s)-$$-$RANDOM"
dirs=()
for d in ${NMCP_SYNC:-src}; do [ -d "$ROOT/$d" ] && dirs+=("$ROOT/$d"); done
# Stage the sources (no lock needed); the run copies them into its workspace.
COPYFILE_DISABLE=1 rsync -a --exclude '*.nyo' -e "ssh -o ConnectTimeout=20" \
  --rsync-path="mkdir -p ~/nmcp-stage && rsync" "${dirs[@]}" "$HOST:nmcp-stage/$STAGE/" \
  || { echo "narya-mcp: ssh-workspace: rsync to $HOST failed" >&2; exit 97; }

read -r -d '' REMOTE <<'REMOTE_SCRIPT'
set -u
stage=$1; reldir=$2; base=$3; narya=$4; ws_name=$5; max_rss=$6; min_avail=$7; frozen=$8; pid_prefix=$9; shift 9
base=${base/#\~/$HOME}; narya=${narya/#\~/$HOME}
mkdir -p ~/nmcp-ws || exit 97
find ~/nmcp-stage -mindepth 1 -maxdepth 1 -mmin +180 -exec rm -rf {} + 2>/dev/null
if [ -n "$ws_name" ]; then
  ws=~/nmcp-ws/$ws_name
  exec 9>"$ws.lock"; flock 9
  [ -d "$ws" ] || cp -a "$base" "$ws"
  temp=0
else
  ws=$(mktemp -d ~/nmcp-ws/run-XXXXXX)
  rmdir "$ws" && cp -a "$base" "$ws"
  temp=1
fi
for d in ~/nmcp-stage/"$stage"/*; do
  [ -d "$d" ] && rsync -a --delete --exclude '*.nyo' "$d/" "$ws/$(basename "$d")/"
done
rm -rf ~/nmcp-stage/"$stage"
if [ -n "$frozen" ]; then
  [ -e "$ws/$frozen" ] || touch -r "$base/$frozen" "$ws/$frozen" 2>/dev/null || touch "$ws/$frozen"
  find "$ws" -name '*.nyo' -newer "$ws/$frozen" -delete
fi
while [ "$(awk '/MemAvailable/ {print int($2 / 1048576)}' /proc/meminfo)" -lt "$min_avail" ]; do
  echo "narya-mcp: waiting for $min_avail GB of available memory on $(hostname)" >&2; sleep 10
done
me=$$; sshd=$PPID
# Watchdog: memory limit, lost connection, peak memory report, and cleanup of a temporary workspace.
(
  # The ssh channel may be gone by the time we report: never die of SIGPIPE before cleaning up.
  trap '' PIPE
  exec </dev/null >/dev/null
  peak=0
  while kill -0 "$me" 2>/dev/null; do
    kb=$(ps -o rss= -p "$me" 2>/dev/null | tr -d ' ')
    [ -n "$kb" ] && [ "$kb" -gt "$peak" ] && peak=$kb
    if [ "${kb:-0}" -ge $((max_rss * 1048576)) ]; then
      echo "narya-mcp: killed, resident memory above $max_rss GB" >&2; kill -KILL "$me"; break
    fi
    if ! kill -0 "$sshd" 2>/dev/null; then kill -KILL "$me"; break; fi
    sleep 2
  done
  [ "$temp" = 1 ] && rm -rf "$ws"
  case " $* " in *" -source-only "*) [ -n "$frozen" ] && [ "$temp" = 0 ] && touch "$ws/$frozen" ;; esac
  echo "narya-mcp-maxrss $peak" >&2 2>/dev/null
) &
echo "narya-mcp-pid ${pid_prefix}$$" >&2
exec unshare -rm sh -c 'mount --bind "$1" "$2" && cd "$2/$3" && shift 3 && exec nice -n 10 "$@"' \
  sh "$ws" "$base" "$reldir" "$narya" "$@"
REMOTE_SCRIPT

args=("$STAGE" "$reldir" "${NMCP_REMOTE_BASE:?set NMCP_REMOTE_BASE}" "${NMCP_REMOTE_NARYA:?set NMCP_REMOTE_NARYA}"
  "${NMCP_WS:-}" "${NMCP_MAX_RSS_GB:-20}" "${NMCP_MIN_AVAIL_GB:-24}" "${NMCP_FROZEN_STAMP:-}" "${NMCP_PID_PREFIX:-}" "$@")
exec ssh -o ServerAliveInterval=30 -o ConnectTimeout=20 "$HOST" "bash -c $(printf %q "$REMOTE") nmcp $(printf '%q ' "${args[@]}")"
