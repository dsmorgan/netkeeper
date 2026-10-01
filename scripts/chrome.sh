#!/bin/sh
# Start the netkeeper Chrome profile with its remote-debugging port, for a person
# to run by hand.
#
# netkeeper itself never starts a browser (ADR 0002): `netkeeper browser launch`
# only prints the command, and nothing in the package runs this script. This is
# that printed command plus the checks a person otherwise does by hand: is it
# already running, is the port taken by something else, is the profile open
# without the port, and is a crash-left profile lock in the way.
#
# The port and profile come from netkeeper itself (`browser launch --json`), so
# this starts Chrome where `serve` and `preflight` will look for it.
set -eu

PROFILE_DIRNAME="chrome-profile"   # netkeeper.services.browser_launch.CHROME_PROFILE_DIRNAME
DEFAULT_PORT=9222                  # netkeeper.services.browser_launch.DEFAULT_CDP_PORT
WAIT_SECONDS=30

usage() {
  cat <<USAGE
Usage: scripts/chrome.sh [--port N] [--data-dir PATH | --profile-dir PATH] [--dry-run]
       scripts/chrome.sh --status [--port N] [--data-dir PATH | --profile-dir PATH]
       scripts/chrome.sh --help

Start Chrome on the netkeeper profile with --remote-debugging-port, then wait
until the port answers. If that Chrome is already up, say so and change nothing.

The port and profile default to what netkeeper uses: linkedin.cdp_url from your
config, and <data dir>/$PROFILE_DIRNAME. If netkeeper cannot be run, they fall
back to port $DEFAULT_PORT and \$NETKEEPER_DATA (or the macOS default), with a warning.

Options:
  --port N            Debugging port, instead of linkedin.cdp_url's.
  --data-dir PATH     netkeeper data directory, instead of \$NETKEEPER_DATA.
  --profile-dir PATH  Use this Chrome profile directory instead.
  --status            Report whether the port answers and whether the profile
                      is open. Changes nothing.
  --dry-run           Print what would happen; change nothing.
  --help              This text.

After a crash, Chrome can leave a lock in the profile that makes the next start
fail with "profile in use". When no running Chrome holds the profile, this
removes that lock (SingletonLock, SingletonCookie, SingletonSocket) first.
USAGE
}

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

# One spelling for a path this script compares against another: absolute,
# symlinks resolved, no trailing slash. Used for $profile itself, and for a
# lock-file holder's own --user-data-dir, so two different spellings of the
# same real directory still compare equal.
canonicalize() {
  path=$1
  case $path in
    /*) ;;
    *) path="$PWD/$path" ;;
  esac
  if [ -d "$path" ]; then
    path=$(cd "$path" && pwd -P)
  elif [ -d "$(dirname "$path")" ]; then
    path="$(cd "$(dirname "$path")" && pwd -P)/$(basename "$path")"
  fi
  printf '%s\n' "$path"
}

data_dir=
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

case $port in
  *[!0-9]*) die "--port must be a number, not '$port'" ;;
esac

command -v curl >/dev/null 2>&1 || die "curl is needed to check the debugging port"
command -v lsof >/dev/null 2>&1 || die "lsof is needed to check what holds the port"

# --- where: ask netkeeper, unless told ------------------------------------------

repo=$(cd "$(dirname "$0")/.." && pwd -P)
netkeeper_bin=
if [ -x "$repo/.venv/bin/netkeeper" ]; then
  netkeeper_bin="$repo/.venv/bin/netkeeper"
elif command -v netkeeper >/dev/null 2>&1; then
  netkeeper_bin=$(command -v netkeeper)
fi

# The python netkeeper was installed with, so a real json parser is available
# without assuming one sits beside `netkeeper` on PATH (#183 review bug 3/
# should-fix 2: sed parsing the JSON directly instead mangled a profile path
# with a quote, a backslash, or non-ASCII text -- json.dumps escapes the last
# as \uXXXX). Three shapes, tried in order, and read with `head` so a symlinked
# entry point (a `uv tool`/pipx install) resolves through it transparently:
#   - a direct interpreter shebang (`#!/path/to/python3`, what `uv sync`/pip
#     normally write), accepted only when its own basename looks like python;
#   - an `env` shebang (`#!/usr/bin/env python3`), resolved the same way
#     through PATH;
#   - anything else -- notably `uv tool`'s own `#!/bin/sh` relaunch shim --
#     which is never handed the snippet (it is not python), falling back
#     instead to plain `python3` on PATH: the snippet only needs stdlib json,
#     never the entry point's own interpreter specifically (#183 re-review
#     should-fix 2).
netkeeper_python=
if [ -n "$netkeeper_bin" ]; then
  shebang=$(head -n1 "$netkeeper_bin" 2>/dev/null) || shebang=
  case $shebang in
    '#!/usr/bin/env '*)
      word=${shebang#'#!/usr/bin/env '}
      word=${word%% *}
      case $(basename "$word") in
        python*) netkeeper_python=$(command -v "$word" 2>/dev/null) || netkeeper_python= ;;
      esac
      ;;
    '#!'*)
      candidate=${shebang#'#!'}
      candidate=${candidate%% *}
      case $(basename "$candidate") in
        python*) [ -x "$candidate" ] && netkeeper_python=$candidate ;;
      esac
      ;;
  esac
fi
[ -n "$netkeeper_python" ] || netkeeper_python=$(command -v python3 2>/dev/null) || netkeeper_python=

if [ -z "$port" ] || [ -z "$profile" ]; then
  answer=
  if [ -n "$netkeeper_bin" ]; then
    if [ -n "$data_dir" ]; then
      answer=$(NETKEEPER_DATA="$data_dir" "$netkeeper_bin" browser launch --json 2>/dev/null) || answer=
    else
      answer=$("$netkeeper_bin" browser launch --json 2>/dev/null) || answer=
    fi
  fi
  # One field per line: port, profile, remote note (empty when none).
  fields=
  if [ -n "$answer" ] && [ -n "$netkeeper_python" ]; then
    fields=$(printf '%s' "$answer" | "$netkeeper_python" -c '
import json, sys
d = json.load(sys.stdin)
print(d["port"]); print(d["profile"]); print(d["remote"] or "")' 2>/dev/null) || fields=
  fi
  if [ -n "$fields" ]; then
    asked_port=$(printf '%s\n' "$fields" | sed -n 1p)
    asked_profile=$(printf '%s\n' "$fields" | sed -n 2p)
    remote=$(printf '%s\n' "$fields" | sed -n 3p)
    if [ -z "$port" ] && [ -n "$remote" ]; then
      die "$remote"
    fi
    [ -n "$port" ] || port=$asked_port
    [ -n "$profile" ] || profile=$asked_profile
  else
    if [ -n "$answer" ] && [ -z "$netkeeper_python" ]; then
      printf 'warning: netkeeper answered but no Python was found to parse it; using the defaults\n' >&2
    else
      printf 'warning: could not ask netkeeper for its port and profile; using the defaults\n' >&2
    fi
    [ -n "$port" ] || port=$DEFAULT_PORT
    if [ -z "$profile" ]; then
      [ -n "$data_dir" ] || data_dir=${NETKEEPER_DATA:-"$HOME/Library/Application Support/netkeeper"}
      profile="$data_dir/$PROFILE_DIRNAME"
    fi
  fi
fi

# #183 review bug 4: the port can come from netkeeper's own JSON, not just
# --port, and that answer was never checked before being handed to curl/lsof.
case $port in
  *[!0-9]*) die "the port from \`netkeeper browser launch --json\` must be a number, not '$port'" ;;
esac

# One spelling of the profile, however it was given. Chrome gets this
# spelling, and every check compares against it.
profile=$(canonicalize "$profile")

# --- what is running ------------------------------------------------------------

# The browser's own answer on the port, or nothing.
cdp_version() {
  curl -fsS --max-time 2 "http://127.0.0.1:$port/json/version" 2>/dev/null || true
}

# PIDs of processes started with exactly this profile: the flag must end at a
# space or the end of the line, so .../chrome-profile never matches
# .../chrome-profile-old. The flag goes through the environment, not awk's
# arguments, so this pipeline never appears in its own match.
profile_pids() {
  ps -axo pid=,command= | CHROME_SH_FLAG="--user-data-dir=$profile" awk '
    { line = $0 " " }
    index(line, ENVIRON["CHROME_SH_FLAG"] " ") { print $1 }'
}

# The pid Chrome wrote into the profile's lock, when that process is alive, is a
# Chrome, is not one of its own helper processes, and actually has this profile
# open. Chrome's own check for the first two, and it does not care how the path
# was spelled when that Chrome was started; the last two guard against a pid
# the crashed netkeeper Chrome once had, since reused by something else --
# your everyday Chrome's main window, or one of its many `--type=` helpers --
# which is not this profile's holder just because it is also a Chrome (#183 review).
lock_holder() {
  target=$(readlink "$profile/SingletonLock" 2>/dev/null) || return 0
  pid=${target##*-}
  case $pid in ''|*[!0-9]*) return 0 ;; esac
  kill -0 "$pid" 2>/dev/null || return 0
  # The executable's own name, not its arguments or directory: a crashed
  # Chrome's pid reused by anything else is no holder.
  exe=$(ps -o comm= -p "$pid" 2>/dev/null) || return 0
  case $(basename "$exe" | tr '[:upper:]' '[:lower:]') in
    *chrome*|*chromium*) ;;
    *) return 0 ;;
  esac
  cmd=$(ps -o command= -p "$pid" 2>/dev/null) || return 0
  # A renderer, GPU, or utility process never holds SingletonLock itself; a
  # reused pid landing on one belongs to some other Chrome entirely.
  case $cmd in
    *" --type="*) return 0 ;;
  esac
  # The reused-pid case proper: confirm it has *this* profile open. Its own
  # --user-data-dir, canonicalized the same way $profile is, is one
  # independent sign (catches a spelling profile_pids() cannot match); lsof
  # showing a file open under it is the other (#183 review nit 3).
  tail=$(printf '%s' "$cmd" | sed -n 's/.*--user-data-dir=//p')
  if [ -n "$tail" ]; then
    flag=${tail%% --*}
    if [ "$(canonicalize "$flag")" = "$profile" ]; then
      printf '%s\n' "$pid"
      return 0
    fi
    # A --user-data-dir naming a different profile is positive evidence this
    # pid is not this profile's holder, not merely the absence of evidence
    # that it is -- lsof's fail-closed rule below is for when there is no
    # flag to go on at all, not for overriding this (#183 re-review nit 3).
    return 0
  fi
  lsof_out=$(lsof -a -p "$pid" -Fn 2>/dev/null)
  if [ -z "$lsof_out" ]; then
    # Nothing back at all -- a startup race, or lsof lacking permission to
    # inspect it (sandboxing can hide even a same-user process's open files) --
    # is not proof there is no holder; fail closed rather than clear a lock a
    # real Chrome still has (#183 review nit 3).
    printf '%s\n' "$pid"
    return 0
  fi
  printf '%s\n' "$lsof_out" | grep -qF "$profile/" || return 0
  printf '%s\n' "$pid"
}

port_listener() {
  lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 { print $1 " (pid " $2 ")"; exit }'
}

browser_name() {
  printf '%s' "$1" | sed -n 's/.*"Browser"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

first_line() {
  printf '%s\n' "$1" | sed -n 1p
}

# The --remote-debugging-port on a holder's own command line, when it has one
# and it is not $port: #183 review bug 2, "without the debugging port" is the
# wrong message for a Chrome that has one, just a different one.
holder_other_port() {
  ps -o command= -p "$1" 2>/dev/null | sed -n 's/.*--remote-debugging-port=\([0-9]*\).*/\1/p'
}

version=$(cdp_version)
pids=$(profile_pids)
holder=$(lock_holder)

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
  if [ -n "$pids" ] || [ -n "$holder" ]; then
    printf 'chrome   running on this profile (pid %s)\n' "$(first_line "${pids:-$holder}")"
  else
    printf 'chrome   not running on this profile\n'
  fi
  exit 0
fi

if [ -n "$version" ]; then
  if [ -n "$pids" ] || [ -n "$holder" ]; then
    printf 'Already running: %s on port %s, profile %s\n' "$(browser_name "$version")" "$port" "$profile"
    printf 'Next: netkeeper preflight\n'
    exit 0
  fi
  die "port $port already answers as $(browser_name "$version"), but not on this profile. Quit that browser, or pick another port with --port and set linkedin.cdp_url in your config to match."
fi

listener=$(port_listener)
[ -z "$listener" ] || die "port $port is held by $listener, which is not a debuggable Chrome. Free it, or use --port and set linkedin.cdp_url to match."

if [ -n "$pids" ] || [ -n "$holder" ]; then
  running_pid=$(first_line "${pids:-$holder}")
  # Found only via the lock file, not a command-line match: say so, so the
  # person can tell this apart from a guess (#183 review bug 1).
  via=
  [ -n "$pids" ] || via=" (per $profile/SingletonLock)"
  other_port=$(holder_other_port "$running_pid")
  # #183 re-review nit 4: an escape hatch for the one case none of this is
  # proof against -- the heuristics above are still a guess, not a guarantee.
  escape="If pid $running_pid isn't netkeeper's Chrome, remove $profile/SingletonLock."
  if [ -n "$other_port" ] && [ "$other_port" != "$port" ]; then
    die "Chrome is running on this profile on port $other_port, not $port (pid $running_pid$via). Use --port $other_port to match it, or quit that window with Cmd-Q and run this again. $escape"
  fi
  die "Chrome is running on this profile without the debugging port (pid $running_pid$via). Quit that window with Cmd-Q, then run this again: a running Chrome cannot gain the port. $escape"
fi

# A Chrome that crashed leaves these behind. Nothing holds the profile (checked
# above, by command line and by the lock's own pid), so they are stale, and
# Chrome can refuse to start ("profile in use") when the lock names another
# host -- which happens when this Mac's network-assigned hostname changes.
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
