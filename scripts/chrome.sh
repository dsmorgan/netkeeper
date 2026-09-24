#!/bin/sh
# Start the netkeeper Chrome profile with its remote-debugging port, for a person
# to run by hand.
#
# netkeeper itself never starts a browser (ADR 0002): `netkeeper browser launch`
# only prints the command, and nothing in the package runs this script. This is
# that printed command plus the checks a person otherwise does by hand: is it
# already running, is the port taken by something else, is the profile open
# without the port, and is a crash-left profile lock in the way.
set -eu

PROFILE_DIRNAME="chrome-profile"   # netkeeper.services.browser_launch.CHROME_PROFILE_DIRNAME
DEFAULT_PORT=9222                  # netkeeper.services.browser_launch default CDP port
WAIT_SECONDS=30

usage() {
  cat <<USAGE
Usage: scripts/chrome.sh [--port N] [--data-dir PATH | --profile-dir PATH] [--dry-run]
       scripts/chrome.sh --status [--port N] [--data-dir PATH | --profile-dir PATH]
       scripts/chrome.sh --help

Start Chrome on the netkeeper profile with --remote-debugging-port, then wait
until the port answers. If that Chrome is already up, say so and change nothing.

Options:
  --port N            Debugging port. Default: the port in \$NETKEEPER_CDP_URL,
                      else $DEFAULT_PORT.
  --data-dir PATH     netkeeper data directory. Default: \$NETKEEPER_DATA, else
                      ~/Library/Application Support/netkeeper. The profile is
                      <data-dir>/$PROFILE_DIRNAME.
  --profile-dir PATH  Use this Chrome profile directory instead.
  --status            Report whether the port answers and whether the profile
                      is open. Changes nothing.
  --dry-run           Print what would happen; change nothing.
  --help              This text.

After a crash, Chrome can leave a lock in the profile that makes the next start
fail with "profile in use". When no running Chrome has the profile open, this
removes that lock (SingletonLock, SingletonCookie, SingletonSocket) first.
USAGE
}

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

data_dir=${NETKEEPER_DATA:-"$HOME/Library/Application Support/netkeeper"}
profile=
port=
mode=start
dry_run=no

while [ $# -gt 0 ]; do
  case $1 in
    --port) [ $# -ge 2 ] || die "--port needs a number"; port=$2; shift 2 ;;
    --port=*) port=${1#--port=}; shift ;;
    --data-dir) [ $# -ge 2 ] || die "--data-dir needs a path"; data_dir=$2; shift 2 ;;
    --data-dir=*) data_dir=${1#--data-dir=}; shift ;;
    --profile-dir) [ $# -ge 2 ] || die "--profile-dir needs a path"; profile=$2; shift 2 ;;
    --profile-dir=*) profile=${1#--profile-dir=}; shift ;;
    --status) mode=status; shift ;;
    --dry-run) dry_run=yes; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

[ -n "$profile" ] || profile="$data_dir/$PROFILE_DIRNAME"

if [ -z "$port" ]; then
  # http://127.0.0.1:9222 -> 9222; anything without a port keeps the default.
  port=$(printf '%s' "${NETKEEPER_CDP_URL:-}" | sed -n 's#^[a-z]*://[^/:]*:\([0-9][0-9]*\).*#\1#p')
  [ -n "$port" ] || port=$DEFAULT_PORT
fi
case $port in
  ''|*[!0-9]*) die "--port must be a number, not '$port'" ;;
esac

# The browser's own answer on the port, or nothing.
cdp_version() {
  curl -fsS --max-time 2 "http://127.0.0.1:$port/json/version" 2>/dev/null || true
}

# PIDs of Chrome processes started on this profile. Matches the exact flag, so a
# Chrome on another profile (your everyday one) never counts.
profile_pids() {
  # The flag goes through the environment, not awk's arguments, so this pipeline
  # never appears in its own match.
  ps -axo pid=,command= | CHROME_SH_FLAG="--user-data-dir=$profile" \
    awk 'index($0, ENVIRON["CHROME_SH_FLAG"]) { print $1 }'
}

port_listener() {
  lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 { print $1 " (pid " $2 ")"; exit }'
}

browser_name() {
  printf '%s' "$1" | sed -n 's/.*"Browser"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

version=$(cdp_version)
pids=$(profile_pids)

if [ "$mode" = status ]; then
  printf 'profile  %s\n' "$profile"
  if [ -n "$version" ]; then
    printf 'port     %s answers: %s\n' "$port" "$(browser_name "$version")"
  else
    listener=$(port_listener)
    if [ -n "$listener" ]; then
      printf 'port     %s is held by %s, which is not answering as Chrome\n' "$port" "$listener"
    else
      printf 'port     %s: nothing is listening\n' "$port"
    fi
  fi
  if [ -n "$pids" ]; then
    printf 'chrome   running on this profile (pid %s)\n' "$(printf '%s' "$pids" | tr '\n' ' ' | sed 's/ $//')"
  else
    printf 'chrome   not running on this profile\n'
  fi
  exit 0
fi

if [ -n "$version" ]; then
  if [ -n "$pids" ]; then
    printf 'Already running: %s on port %s, profile %s\n' "$(browser_name "$version")" "$port" "$profile"
    printf 'Next: netkeeper preflight\n'
    exit 0
  fi
  die "port $port already answers as $(browser_name "$version"), but not on this profile. Quit that browser, or pick another port with --port and set NETKEEPER_CDP_URL to match."
fi

listener=$(port_listener)
[ -z "$listener" ] || die "port $port is held by $listener, which is not a debuggable Chrome. Free it, or use --port."

if [ -n "$pids" ]; then
  die "Chrome is running on this profile without the debugging port (pid $(printf '%s' "$pids" | tr '\n' ' ' | sed 's/ $//')). Quit that window with Cmd-Q, then run this again: a running Chrome cannot gain the port."
fi

# A Chrome that crashed leaves these behind. With no process on the profile they
# are stale, and Chrome can refuse to start ("profile in use") when the lock
# names another host -- which happens when this Mac's hostname changes.
stale=
for name in SingletonLock SingletonCookie SingletonSocket; do
  if [ -L "$profile/$name" ] || [ -e "$profile/$name" ]; then
    stale="$stale $name"
  fi
done
if [ -n "$stale" ]; then
  printf 'Removing a stale profile lock left by a crash:%s\n' "$stale"
  for name in $stale; do
    if [ "$dry_run" = yes ]; then
      printf '  would: rm %s\n' "$profile/$name"
    else
      rm -f "$profile/$name"
    fi
  done
fi

if [ "$dry_run" = yes ]; then
  printf '  would: mkdir -p %s\n' "$profile"
else
  mkdir -p "$profile"
fi

# The same two flags `netkeeper browser launch` prints; nothing else, so this
# Chrome looks like any other.
case $(uname -s) in
  Darwin)
    set -- open -na "Google Chrome" --args \
      "--remote-debugging-port=$port" "--user-data-dir=$profile"
    ;;
  *)
    chrome_bin=
    for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
      if command -v "$candidate" >/dev/null 2>&1; then chrome_bin=$candidate; break; fi
    done
    if [ -z "$chrome_bin" ]; then
      [ "$dry_run" = yes ] || die "no Chrome found (looked for google-chrome, chromium)"
      chrome_bin=google-chrome
    fi
    set -- "$chrome_bin" "--remote-debugging-port=$port" "--user-data-dir=$profile"
    ;;
esac

if [ "$dry_run" = yes ]; then
  printf '  would: %s\n' "$*"
  exit 0
fi

printf 'Starting Chrome on port %s, profile %s\n' "$port" "$profile"
if [ "$1" = open ]; then
  "$@"
else
  nohup "$@" >/dev/null 2>&1 &
fi

waited=0
while [ "$waited" -lt "$WAIT_SECONDS" ]; do
  version=$(cdp_version)
  if [ -n "$version" ]; then
    printf 'Ready: %s\n' "$(browser_name "$version")"
    printf 'Next: log in to LinkedIn in that window if it asks, then run: netkeeper preflight\n'
    exit 0
  fi
  sleep 1
  waited=$((waited + 1))
done
die "Chrome did not answer on port $port within ${WAIT_SECONDS}s. Run: scripts/chrome.sh --status"
