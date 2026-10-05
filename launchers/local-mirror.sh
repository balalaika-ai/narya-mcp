#!/bin/bash
# narya-mcp launcher: run Narya in a local mirror of the project.
#
#   local-mirror.sh run <reldir> <narya args...>    sync the sources into the mirror, then run Narya in <reldir>
#   local-mirror.sh signal <pid> <SIGNAL>           deliver a signal to a Narya started by this launcher
#
# The mirror keeps compiled .nyo files out of the project tree.  Configuration (environment, e.g. from the [env]
# table of narya-mcp.toml):
#   NMCP_ROOT       project root (default: the current directory, which narya-mcp sets to the root)
#   NMCP_MIRROR     mirror directory (required)
#   NMCP_NARYA      Narya executable (required)
#   NMCP_SYNC       space-separated directories of the project to mirror (default: "src")
#   NMCP_BLOCK_NYO  if 1, create an empty directory NAME.nyo for every NAME.ny without a compiled file, so that
#                   Narya cannot write .nyo files there (it only warns, W2305) and concurrent runs never write the
#                   same file; precompiled .nyo files are left alone.
#   NMCP_NICE       nice level (default 10)
#   NMCP_SYNC_LOCK  lock file serialising the sync (default: MIRROR/.sync.lock)
set -u
case "${1:-}" in
  signal)
    pid=${2##*:}
    # Interrupting a Narya that already waits for its next command would kill it (SIGINT is only caught while a
    # command runs), so INT is not sent to an idle process; exit status 3 says so.
    if [ "$3" = INT ]; then
      if [ -r "/proc/$pid/wchan" ]; then
        case "$(cat "/proc/$pid/wchan")" in pipe_read | pipe_wait) exit 3 ;; esac
      else
        # No /proc (macOS): a busy Narya accumulates CPU time, an idle one does not.
        t1=$(ps -o time= -p "$pid" 2>/dev/null); sleep 0.3; t2=$(ps -o time= -p "$pid" 2>/dev/null)
        [ "$t1" = "$t2" ] && exit 3
      fi
      exec kill -s INT "$pid"
    fi
    exec kill -s "$3" "$pid" ;;
  run) shift ;;
  *) echo "usage: $0 run <reldir> <narya args...> | signal <pid> <SIGNAL>" >&2; exit 2 ;;
esac
reldir=${1:?missing reldir}; shift
ROOT=${NMCP_ROOT:-$PWD}
MIRROR=${NMCP_MIRROR:?set NMCP_MIRROR}
NARYA=${NMCP_NARYA:?set NMCP_NARYA}
mkdir -p "$MIRROR"
LOCK=${NMCP_SYNC_LOCK:-$MIRROR/.sync.lock}
lock() { if command -v flock >/dev/null; then flock "$LOCK" "$@"; else lockf -k "$LOCK" "$@"; fi; }
# shellcheck disable=SC2016
lock sh -c '
  root=$1; mirror=$2; block=$3; shift 3
  for d in "$@"; do
    [ -d "$root/$d" ] || continue
    mkdir -p "$mirror/$d"
    COPYFILE_DISABLE=1 rsync -a --delete --exclude "*.nyo" "$root/$d/" "$mirror/$d/" || exit 97
    if [ "$block" = 1 ]; then
      find "$mirror/$d" -name "*.ny" | while read -r f; do [ -e "${f%.ny}.nyo" ] || mkdir "${f%.ny}.nyo"; done
    fi
  done' sh "$ROOT" "$MIRROR" "${NMCP_BLOCK_NYO:-0}" ${NMCP_SYNC:-src} \
  || { echo "narya-mcp: local-mirror: sync failed" >&2; exit 97; }
cd "$MIRROR/$reldir" || exit 97
echo "narya-mcp-pid ${NMCP_PID_PREFIX:-}$$" >&2
exec nice -n "${NMCP_NICE:-10}" "$NARYA" "$@"
