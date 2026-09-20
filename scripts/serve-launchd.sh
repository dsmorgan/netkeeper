#!/bin/sh
# What the fun.tnkr.netkeeper LaunchAgent runs: apply migrations, then replace
# this shell with `netkeeper serve`. Any arguments are passed through to serve.
#
# serve migrates at startup too; the explicit upgrade is belt and braces and
# leaves a readable line in the log before the server starts.
set -eu

# Follow symlinks to the script's real location so the repo root is right when
# the script is invoked through a link. Relative link targets resolve against
# the link's directory. Bounded so a symlink loop cannot hang us.
resolve_script_path() {
  rsp_path=$1
  rsp_hops=0
  while [ -L "$rsp_path" ] && [ "$rsp_hops" -lt 40 ]; do
    rsp_target=$(readlink "$rsp_path")
    case $rsp_target in
      /*) rsp_path=$rsp_target ;;
      *) rsp_path=$(dirname "$rsp_path")/$rsp_target ;;
    esac
    rsp_hops=$((rsp_hops + 1))
  done
  printf '%s\n' "$rsp_path"
}

script_dir=$(cd "$(dirname "$(resolve_script_path "$0")")" && pwd -P)
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
