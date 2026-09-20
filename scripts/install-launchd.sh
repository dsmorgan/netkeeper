#!/bin/sh
# Install (or remove) the user LaunchAgent that runs `netkeeper serve` at login
# and restarts it after a crash. macOS only; docs/architecture.md section 16.
#
# The agent runs scripts/serve-launchd.sh, which applies migrations and then
# execs `netkeeper serve` from this repository's .venv. stdout and stderr go to
# <data_dir>/logs/. Re-running replaces the plist and reloads the agent.
set -eu
# pipefail is not POSIX; turn it on where the shell has it (bash, zsh, newer dash).
(set -o pipefail) 2>/dev/null && set -o pipefail

LABEL="fun.tnkr.netkeeper"

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
wrapper="$repo_root/scripts/serve-launchd.sh"
netkeeper_bin="$repo_root/.venv/bin/netkeeper"
agents_dir="$HOME/Library/LaunchAgents"
plist="$agents_dir/$LABEL.plist"
domain="gui/$(id -u)"
service="$domain/$LABEL"

usage() {
  cat <<USAGE
Usage: scripts/install-launchd.sh [--data-dir PATH] [--host HOST] [--port N] [--dry-run]
       scripts/install-launchd.sh --uninstall [--dry-run]
       scripts/install-launchd.sh --help

Install a user LaunchAgent ($LABEL) that runs \`netkeeper serve\` at login and
restarts it after a crash. Re-running replaces the agent.

Options:
  --data-dir PATH  Data directory for the agent (sets NETKEEPER_DATA). Default: the
                   app's platform default, ~/Library/Application Support/netkeeper.
  --host HOST      Interface for \`serve\` (default: web.host from the config).
  --port N         Port for \`serve\`, 1 to 65535 (default: web.port from the config).
  --uninstall      Stop the agent and remove its plist.
  --dry-run        Print the plist and the launchctl commands; change nothing.
  --help           This text.

Options that take a value also accept --name=value.

Logs:   <data_dir>/logs/serve.log and serve.err.log
Status: launchctl print $service
USAGE
}

die() {
  echo "install-launchd.sh: $1" >&2
  echo >&2
  usage >&2
  exit 2
}

set_data_dir() {
  [ -n "$1" ] || die "--data-dir needs a path"
  data_dir=$1
}

set_host() {
  [ -n "$1" ] || die "--host needs a value"
  host=$1
}

set_port() {
  case $1 in
    '' | *[!0-9]*) die "--port needs a number from 1 to 65535, got '$1'" ;;
  esac
  # The length check keeps the numeric comparisons inside the shell's integer range.
  if [ ${#1} -gt 5 ] || [ "$1" -lt 1 ] || [ "$1" -gt 65535 ]; then
    die "--port needs a number from 1 to 65535, got '$1'"
  fi
  port=$1
}

data_dir=""
host=""
port=""
dry_run=0
uninstall=0
while [ $# -gt 0 ]; do
  case $1 in
    --data-dir)
      [ $# -ge 2 ] || die "--data-dir needs a path"
      set_data_dir "$2"
      shift 2
      ;;
    --data-dir=*) set_data_dir "${1#*=}"; shift ;;
    --host)
      [ $# -ge 2 ] || die "--host needs a value"
      set_host "$2"
      shift 2
      ;;
    --host=*) set_host "${1#*=}"; shift ;;
    --port)
      [ $# -ge 2 ] || die "--port needs a number"
      set_port "$2"
      shift 2
      ;;
    --port=*) set_port "${1#*=}"; shift ;;
    --dry-run) dry_run=1; shift ;;
    --uninstall) uninstall=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown argument '$1'" ;;
  esac
done

# NETKEEPER_LAUNCHD_UNAME lets the tests exercise both branches on any host.
uname_s=${NETKEEPER_LAUNCHD_UNAME:-$(uname -s)}
if [ "$uname_s" != "Darwin" ]; then
  echo "install-launchd.sh: launchd is macOS only, and this is $uname_s." >&2
  echo "On Linux, run netkeeper in the container (docs/architecture.md section 16)." >&2
  exit 1
fi

if [ -n "$data_dir" ]; then
  case $data_dir in
    /*) ;;
    *) data_dir="$PWD/$data_dir" ;;
  esac
  effective_data_dir=$data_dir
else
  # Must match netkeeper.paths.data_dir() on macOS.
  effective_data_dir="$HOME/Library/Application Support/netkeeper"
fi
log_dir="$effective_data_dir/logs"

# Print a command, then run it unless this is a dry run.
run() {
  echo "+ $*"
  if [ "$dry_run" -eq 0 ]; then
    "$@"
  fi
}

is_loaded() {
  launchctl print "$service" >/dev/null 2>&1
}

bootout_if_loaded() {
  if [ "$dry_run" -eq 1 ]; then
    echo "+ launchctl bootout $service    # only when the agent is loaded"
    return 0
  fi
  if is_loaded; then
    run launchctl bootout "$service"
    # bootout returns before the service is gone; a bootstrap that races it fails.
    i=0
    while is_loaded && [ "$i" -lt 50 ]; do
      sleep 0.1
      i=$((i + 1))
    done
  fi
}

xml_escape() {
  printf '%s\n' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'
}

render_plist() {
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$wrapper")</string>
PLIST
  if [ -n "$host" ]; then
    cat <<PLIST
    <string>--host</string>
    <string>$(xml_escape "$host")</string>
PLIST
  fi
  if [ -n "$port" ]; then
    cat <<PLIST
    <string>--port</string>
    <string>$port</string>
PLIST
  fi
  cat <<PLIST
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$repo_root")</string>
PLIST
  if [ -n "$data_dir" ]; then
    cat <<PLIST
  <key>EnvironmentVariables</key>
  <dict>
    <key>NETKEEPER_DATA</key>
    <string>$(xml_escape "$data_dir")</string>
  </dict>
PLIST
  fi
  cat <<PLIST
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$log_dir/serve.log")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$log_dir/serve.err.log")</string>
</dict>
</plist>
PLIST
}

if [ "$uninstall" -eq 1 ]; then
  if [ "$dry_run" -eq 0 ] && [ ! -f "$plist" ] && ! is_loaded; then
    echo "The $LABEL agent is not installed; nothing to do."
    exit 0
  fi
  bootout_if_loaded
  run rm -f "$plist"
  if [ "$dry_run" -eq 1 ]; then
    echo "Dry run: nothing was changed."
  else
    echo "Removed the $LABEL agent. Data and logs were left in place."
  fi
  exit 0
fi

if [ ! -x "$netkeeper_bin" ]; then
  if [ "$dry_run" -eq 1 ]; then
    echo "install-launchd.sh: warning: $netkeeper_bin is missing; run 'make install' first." >&2
  else
    echo "install-launchd.sh: $netkeeper_bin is missing; run 'make install' in $repo_root first." >&2
    exit 1
  fi
fi

run mkdir -p "$log_dir" "$agents_dir"
if [ "$dry_run" -eq 1 ]; then
  echo "Would write $plist:"
  render_plist
  echo
else
  render_plist >"$plist"
  plutil -lint "$plist" >/dev/null
  echo "Wrote $plist"
fi
bootout_if_loaded
# enable clears a `launchctl disable` left over from before; bootstrap would fail on it.
run launchctl enable "$service"
run launchctl bootstrap "$domain" "$plist"

echo
if [ "$dry_run" -eq 1 ]; then
  echo "Dry run: nothing was changed."
else
  echo "Installed the $LABEL agent; netkeeper serve starts now and at every login."
fi
echo "  status:    launchctl print $service"
echo "  logs:      $log_dir/serve.log and serve.err.log"
echo "  uninstall: $script_dir/install-launchd.sh --uninstall"
