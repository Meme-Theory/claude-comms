#!/usr/bin/env bash
# ClaudeComms IRC server launcher.
#   ./run-server.sh                 -> localhost only
#   ./run-server.sh 0.0.0.0 6667    -> reachable on the network
set -euo pipefail
BIND_HOST="${1:-127.0.0.1}"
PORT="${2:-6667}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python "$DIR/../server/ircd.py" --host "$BIND_HOST" --port "$PORT"
