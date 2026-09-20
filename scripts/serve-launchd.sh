#!/bin/sh
# What the fun.tnkr.netkeeper LaunchAgent runs: apply migrations, then replace
# this shell with `netkeeper serve`. Any arguments are passed through to serve.
#
# serve migrates at startup too; the explicit upgrade is belt and braces and
# leaves a readable line in the log before the server starts.
set -eu

script_dir=$(cd "$(dirname "$0")" && pwd -P)
repo_root=$(cd "$script_dir/.." && pwd -P)
netkeeper=${NETKEEPER_BIN:-$repo_root/.venv/bin/netkeeper}

if [ ! -x "$netkeeper" ]; then
  echo "serve-launchd.sh: $netkeeper is missing; run 'make install' in $repo_root first." >&2
  exit 1
fi

echo "serve-launchd.sh: $(date -u +%Y-%m-%dT%H:%M:%SZ) applying migrations"
"$netkeeper" db upgrade
echo "serve-launchd.sh: $(date -u +%Y-%m-%dT%H:%M:%SZ) starting netkeeper serve $*"
exec "$netkeeper" serve "$@"
