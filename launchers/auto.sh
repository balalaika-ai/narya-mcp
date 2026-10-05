#!/bin/bash
# narya-mcp launcher: run on the remote host (ssh-workspace.sh) when it answers, otherwise locally (local-mirror.sh).
# Set NMCP_LOCAL=1 to force the local mirror.  The host's availability is cached for NMCP_PROBE_TTL seconds (60).
# Takes the configuration of both launchers.
set -u
here=$(cd "$(dirname "$0")" && pwd)
case "${1:-}" in
  signal)
    case "${2:-}" in
      ssh:*) exec "$here/ssh-workspace.sh" "$@" ;;
      *) exec "$here/local-mirror.sh" "$@" ;;
    esac ;;
  run) ;;
  *) echo "usage: $0 run <reldir> <narya args...> | signal <token> <SIGNAL>" >&2; exit 2 ;;
esac
host=${NMCP_HOST:?set NMCP_HOST}
probe=${TMPDIR:-/tmp}/narya-mcp-probe-$host
up=0
if [ "${NMCP_LOCAL:-0}" != 1 ]; then
  now=$(date +%s)
  if [ -f "$probe" ] && [ $((now - $(stat -f %m "$probe" 2>/dev/null || stat -c %Y "$probe"))) -lt "${NMCP_PROBE_TTL:-60}" ]; then
    up=$(cat "$probe")
  else
    ssh -o ConnectTimeout=8 -o BatchMode=yes "$host" true </dev/null >/dev/null 2>&1 && up=1
    echo "$up" > "$probe"
  fi
fi
if [ "$up" = 1 ]; then
  NMCP_PID_PREFIX=ssh: exec "$here/ssh-workspace.sh" "$@"
fi
[ "${NMCP_LOCAL:-0}" = 1 ] || echo "narya-mcp: $host unavailable, running locally" >&2
NMCP_PID_PREFIX=local: exec "$here/local-mirror.sh" "$@"
