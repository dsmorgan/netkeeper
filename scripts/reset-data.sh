#!/bin/sh
# Archive the database, then remove it so the next `netkeeper serve` starts clean.
#
# The server rebuilds everything a fresh install needs on startup: migrations to
# head, the local user, the default auto-tag rules, and the "Validated" list. So a
# reset is only ever "put the old file somewhere safe and delete it".
#
# Archives live in <data_dir>/archives/ and are never pruned. That is a different
# directory from <data_dir>/backups/, which `netkeeper backup` prunes to
# backup.keep; a pre-reset copy has to outlive that.
set -eu
# pipefail is not POSIX; turn it on where the shell has it (bash, zsh, newer dash).
(set -o pipefail) 2>/dev/null && set -o pipefail

DB_NAME="netkeeper.sqlite3"
ARCHIVES_DIRNAME="archives"

usage() {
  cat <<USAGE
Usage: scripts/reset-data.sh [--data-dir PATH] [--yes] [--dry-run]
       scripts/reset-data.sh --list [--data-dir PATH]
       scripts/reset-data.sh --restore FILE [--data-dir PATH] [--yes] [--dry-run]
       scripts/reset-data.sh --help

Archive the database into <data_dir>/archives/, then delete it. The next
\`netkeeper serve\` migrates a new one and seeds the defaults.

Options:
  --data-dir PATH  Data directory to reset. Default: \$NETKEEPER_DATA, else the
                   app's platform default, ~/Library/Application Support/netkeeper.
  --list           List the archives, newest first. Changes nothing.
  --restore FILE   Put an archive back. FILE is a name from --list or any path.
                   The database in place is archived first, so this is reversible.
  --yes            Skip the confirmation prompt.
  --dry-run        Print what would happen; change nothing.
  --help           This text.

Options that take a value also accept --name=value.

The server must be stopped: it holds the database open, and a reset underneath a
running server leaves it writing into a deleted file.
USAGE
}

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

# The default data directory, matching netkeeper/paths.py.
default_data_dir() {
  if [ -n "${NETKEEPER_DATA:-}" ]; then
    printf '%s\n' "$NETKEEPER_DATA"
  else
    printf '%s\n' "$HOME/Library/Application Support/netkeeper"
  fi
}

data_dir=$(default_data_dir)
mode=reset
restore_from=
assume_yes=no
dry_run=no

while [ $# -gt 0 ]; do
  case $1 in
    --data-dir) [ $# -ge 2 ] || die "--data-dir needs a path"; data_dir=$2; shift 2 ;;
    --data-dir=*) data_dir=${1#--data-dir=}; shift ;;
    --restore) [ $# -ge 2 ] || die "--restore needs a file"; mode=restore; restore_from=$2; shift 2 ;;
    --restore=*) mode=restore; restore_from=${1#--restore=}; shift ;;
    --list) mode=list; shift ;;
    --yes|-y) assume_yes=yes; shift ;;
    --dry-run) dry_run=yes; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

db="$data_dir/$DB_NAME"
archives="$data_dir/$ARCHIVES_DIRNAME"

run() {
  if [ "$dry_run" = yes ]; then
    printf '  would: %s\n' "$*"
  else
    "$@"
  fi
}

# Both checks below guard the one destructive thing this script does. A missing
# tool must stop it, never read as "nothing holds the database".
require_tools() {
  command -v lsof >/dev/null 2>&1 || die "lsof is needed to check that nothing has the database open"
  command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is needed to archive and verify the database"
  if [ -n "${NETKEEPER_DATABASE_URL:-}" ]; then
    die "NETKEEPER_DATABASE_URL is set, so the server does not use $db. This script only resets the SQLite file in the data directory."
  fi
}

# Every process holding the database or its sidecars open, one pid per line.
# lsof exits 1 both when nothing matches (the common case) and on some errors,
# so an error is told apart by what it wrote to stderr -- except a WARNING,
# which lsof prints for a mount unrelated to our files (a stale network share)
# while still answering the question we asked. A WARNING is two or three
# lines -- "lsof: WARNING: ..." then one or two indented continuation lines
# ("Output information may be incomplete.", "assuming ... from mount table")
# that do not themselves start with "lsof: WARNING" -- so both are filtered by
# what they look like (indented) rather than matched line by line; a real
# error does not indent its lines. Only something left over after that means
# lsof could not answer (#183 review bug 7).
holders() {
  set --
  for suffix in '' '-wal' '-shm'; do
    [ -e "$db$suffix" ] && set -- "$@" "$db$suffix"
  done
  [ $# -gt 0 ] || return 0
  err=$(mktemp)
  out=$(lsof -t -- "$@" 2>"$err") || true
  real_err=$(grep -v -E '^lsof: WARNING|^[[:space:]]' "$err") || true
  if [ -z "$out" ] && [ -n "$real_err" ]; then
    printf 'error: lsof could not check the database:\n' >&2
    printf '%s\n' "$real_err" >&2
    rm -f "$err"
    exit 1
  fi
  rm -f "$err"
  printf '%s\n' "$out" | sort -u | sed '/^$/d'
}

refuse_if_in_use() {
  pids=$(holders)
  [ -n "$pids" ] || return 0
  printf 'error: the database is open in another process:\n' >&2
  # shellcheck disable=SC2046 # a comma list of pids: no spaces to split.
  ps -o pid=,command= -p $(printf '%s' "$pids" | tr '\n' ',' | sed 's/,$//') >&2 || true
  printf '\nStop the server first, then run this again.\n' >&2
  exit 1
}

human_size() {
  bytes=$(wc -c <"$1" | tr -d ' ')
  awk -v b="$bytes" 'BEGIN {
    split("B KB MB GB", unit, " ")
    i = 1
    while (b >= 1024 && i < 4) { b /= 1024; i++ }
    printf (i == 1 ? "%d %s\n" : "%.1f %s\n"), b, unit[i]
  }'
}

# A real, readable database: a non-empty file, opened read-only (sqlite3 would
# otherwise create an empty one and call it "ok"), that passes SQLite's own
# consistency check and has at least one table in it.
verified() {
  [ -s "$1" ] || return 1
  [ "$(sqlite3 -readonly "$1" 'PRAGMA quick_check' 2>/dev/null)" = ok ] || return 1
  tables=$(sqlite3 -readonly "$1" 'SELECT count(*) FROM sqlite_master' 2>/dev/null) || return 1
  [ "${tables:-0}" -gt 0 ]
}

list_archives() {
  # Newest first: the UTC stamp in the name sorts by time, and a plain string
  # sort already agreed on that. Two archives can share a stamp (archive_path's
  # "-1", "-2" suffixes, for two resets inside one second); plain sorting put
  # the bare name ahead of its own "-1" even though "-1" was written after it
  # ('.' sorts after '-'), listing the newer one second (#183 review bug 6). A
  # tab-separated sort key -- the stamp, then the suffix zero-padded so it
  # compares as a number -- fixes both at once; a tab rather than a space
  # splits the key from the path even though the data directory's own path can
  # itself contain a space (the macOS default). A glob, not ls, so that path
  # stays one field no matter how it is spelled.
  for path in "$archives"/netkeeper-*.sqlite3; do
    [ -f "$path" ] || continue
    printf '%s\n' "$path"
  done | awk '
    {
      path = $0
      name = path
      sub(/^.*\//, "", name)
      sub(/^netkeeper-/, "", name)
      sub(/\.sqlite3$/, "", name)
      if (match(name, /-[0-9]+$/)) {
        suffix = substr(name, RSTART + 1, RLENGTH - 1) + 0
        stamp = substr(name, 1, RSTART - 1)
      } else {
        suffix = 0
        stamp = name
      }
      printf "%s\t%010d\t%s\n", stamp, suffix, path
    }' | sort -r | cut -f3- | while IFS= read -r path; do
    printf '%s  %s\n' "$(basename "$path")" "$(human_size "$path")"
  done | grep . || printf 'no archives in %s\n' "$archives"
}

confirm() {
  [ "$assume_yes" = no ] || return 0
  [ "$dry_run" = no ] || return 0
  printf '%s [y/N] ' "$1"
  read -r reply || reply=
  case $reply in
    y|Y|yes|YES) return 0 ;;
    *) printf 'nothing changed\n'; exit 0 ;;
  esac
}

# A name in $archives nothing else has taken, for the one-line preview shown
# before the confirmation prompt. Best effort only, and not what decides the
# real name: a second reset could still land on the same guess between this
# check and archive_db() actually publishing, which is exactly the race
# archive_db()'s own hard-link publish step is safe against (below). Two runs
# guessing the same stamp is also why the loop checks at all -- one run in a
# second is the common case, but never assume it is the only one.
archive_path() {
  candidate="$archives/netkeeper-$stamp.sqlite3"
  n=1
  while [ -e "$candidate" ] || [ -e "$candidate-wal" ]; do
    candidate="$archives/netkeeper-$stamp-$n.sqlite3"
    n=$((n + 1))
  done
  printf '%s\n' "$candidate"
}

# Moves $1/db aside as .failed for inspection, setting $quarantined to its
# path; leaves $quarantined empty when $1/db never existed (nothing to
# quarantine, just the generic "nothing published" the caller falls back to).
#
# Called directly, never as `x=$(quarantine_partial ...)`: a command
# substitution runs in its own subshell, so a failed mv's die() in there
# would exit only that subshell, its message the only thing said, while the
# original sat in $tmp -- about to be removed once the real abort finally
# happened anyway -- rather than naming where it actually, accurately, still
# is (#183 re-review round 3 nit 3). There is no "the" archive to rename
# here either way, only this run's own, still-private work: nothing another
# concurrent run has already published is ever touched by this (#183
# re-review should-fix 1).
quarantine_partial() {
  tmp=$1
  quarantined=
  [ -e "$tmp/db" ] || return 0
  quarantined="$archives/netkeeper-$stamp.sqlite3.failed"
  n=1
  while [ -e "$quarantined" ]; do
    quarantined="$archives/netkeeper-$stamp-$n.sqlite3.failed"
    n=$((n + 1))
  done
  mv -- "$tmp/db" "$quarantined" ||
    die "could not move the partial archive to $quarantined; it is still in $tmp, which this run is about to remove"
}

# Copy the live database into a run-private temporary file under $archives and
# prove it is readable there, before anything public exists. VACUUM INTO
# writes one compacted file with the WAL already folded in; if SQLite refuses,
# the raw files are copied and the WAL folded into the copy instead, so an
# archive is always one self-contained file -- verified either way, while it
# is still nobody else's.
#
# Publishing is a hard link (same filesystem: the temp directory is under
# $archives for exactly this), which fails atomically with EEXIST if the name
# is already taken, so two resets landing on the same stamp each end up with
# their own archive instead of one clobbering the other's. A plain `cp` or
# `mv` into the final name could not do that: this run's own raw-copy fallback
# used to `rm -f` straight at the public name, and quarantining an unverified
# result used to rename that same public path -- either one could catch
# another run's already-finished, already-verified archive in the middle
# (#183 re-review should-fix 1).
archive_db() {
  run mkdir -p "$archives"
  if [ "$dry_run" = yes ]; then
    printf '  would: sqlite3 %s "VACUUM INTO %s", then verify it\n' "$db" "$target"
    return 0
  fi
  tmp=$(mktemp -d "$archives/.partial.XXXXXX") || die "could not create a temporary directory under $archives"
  # An interrupted reset (Ctrl-C, a killed session) must not leave a hidden
  # full copy of the database sitting in archives/ under a dot-prefixed name
  # (#183 re-review should-fix 2). The EXIT trap is the backstop for every
  # exit path, including the ordinary ones that already clean up themselves;
  # the signal traps turn an interrupt into a normal exit so that trap runs,
  # instead of the shell's own default response to the signal bypassing it.
  trap 'rm -rf -- "$tmp"' EXIT
  # Conventional 128+signal, not one catch-all code: a killed session's own
  # exit status still says which signal it was (#183 re-review round 3 nit 4).
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  work="$tmp/db"
  quoted=$(printf '%s' "$work" | sed "s/'/''/g")
  if ! sqlite3 "$db" "VACUUM INTO '$quoted'"; then
    printf 'warning: VACUUM INTO failed; copying the raw files instead\n' >&2
    for suffix in '' '-wal' '-shm'; do
      [ -f "$db$suffix" ] || continue
      if ! cp -- "$db$suffix" "$work$suffix"; then
        quarantine_partial "$tmp"
        if [ -n "$quarantined" ]; then
          die "could not copy $db$suffix; moved the partial copy to $quarantined for inspection. The database was left in place."
        fi
        die "could not copy $db$suffix; nothing published. The database was left in place."
      fi
    done
    # A failed fold is fatal, not a warning this swallows (#183 re-review
    # round 3 should-fix 2): the point of folding is making $work
    # self-contained, and either the checkpoint itself failing, or -wal still
    # carrying real content afterward (TRUNCATE did not fully run), means it
    # is not -- quietly publishing it anyway would be an archive missing
    # whatever was still in the WAL, with no sign that anything was wrong.
    if ! sqlite3 "$work" 'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE;' >/dev/null 2>&1 ||
      [ -s "$work-wal" ]; then
      quarantine_partial "$tmp"
      if [ -n "$quarantined" ]; then
        die "could not fold the WAL; moved the partial copy to $quarantined for inspection. The database was left in place."
      fi
      die "could not fold the WAL; the database was left in place."
    fi
    rm -f -- "$work-wal" "$work-shm"
  fi
  if ! verified "$work"; then
    quarantine_partial "$tmp"
    if [ -n "$quarantined" ]; then
      die "the archive did not pass SQLite's quick_check; moved it to $quarantined for inspection. The database was left in place."
    fi
    die "the archive did not pass SQLite's quick_check; nothing published. The database was left in place."
  fi
  candidate="$archives/netkeeper-$stamp.sqlite3"
  n=1
  while ! ln -- "$work" "$candidate" 2>/dev/null; do
    # -e alone misses a dangling symlink (its target does not exist, so -e
    # reads false even though the name itself is taken); -L catches that too,
    # so a dangling symlink at a candidate name is skipped like any other
    # taken one rather than tried again and mistaken for a real failure.
    if [ -e "$candidate" ] || [ -L "$candidate" ]; then
      # Another run published this name first (or, non-concurrently, an
      # earlier reset this same second already has it): try the next one.
      candidate="$archives/netkeeper-$stamp-$n.sqlite3"
      n=$((n + 1))
      continue
    fi
    quarantine_partial "$tmp"
    publish_fail_note="(a filesystem without hard-link support -- some network mounts -- fails this way too)"
    if [ -n "$quarantined" ]; then
      die "could not publish the archive to $candidate; moved it to $quarantined for inspection. The database was left in place. $publish_fail_note"
    fi
    die "could not publish the archive to $candidate. The database was left in place. $publish_fail_note"
  done
  # Some `ln` implementations link *inside* a directory (or a symlink to one)
  # found at the destination name, instead of failing: $candidate itself is
  # then still whatever it was before, not the archive it looks like. Caught
  # here, before remove_db() ever runs, rather than after the live database
  # is already gone on the strength of a publish that never actually happened
  # where expected (#183 re-review should-fix 4).
  if ! { [ -f "$candidate" ] && [ ! -L "$candidate" ] && [ "$candidate" -ef "$work" ]; }; then
    quarantine_partial "$tmp"
    note="a directory or a symlink to one was already there"
    if [ -n "$quarantined" ]; then
      die "$candidate did not end up as the archive itself ($note); moved the real copy to $quarantined for inspection. The database was left in place."
    fi
    die "$candidate did not end up as the archive itself ($note). The database was left in place."
  fi
  rm -rf -- "$tmp"
  printf 'archived %s (%s, verified)\n' "$candidate" "$(human_size "$candidate")"
}

# The file and both sidecars, whichever exist. A -wal left next to a restored
# file would be replayed onto it, so this runs even when the main file is gone.
remove_db() {
  for suffix in '' '-wal' '-shm'; do
    [ -e "$db$suffix" ] || continue
    run rm -- "$db$suffix"
  done
}

stamp=$(date -u +%Y%m%dT%H%M%SZ)

case $mode in
  list)
    list_archives
    ;;

  reset)
    require_tools
    if [ ! -f "$db" ]; then
      printf 'no database at %s: already clean\n' "$db"
      exit 0
    fi
    refuse_if_in_use
    target=$(archive_path)
    printf 'database: %s (%s)\n' "$db" "$(human_size "$db")"
    printf 'archive:  %s\n' "$target"
    confirm 'Archive it and start from an empty database?'
    # Again: a server started while the prompt waited holds it now.
    refuse_if_in_use
    archive_db
    remove_db
    [ "$dry_run" = yes ] || printf 'removed %s\n' "$db"
    printf '\nStart the server to build a new one:\n'
    printf '  make serve      # or: make dev, for reload\n'
    ;;

  restore)
    # A bare name means an archive; anything with a slash is a path as given.
    case $restore_from in
      */*) source=$restore_from ;;
      *) source="$archives/$restore_from" ;;
    esac
    [ -f "$source" ] || die "$source: no such file"
    require_tools
    verified "$source" || die "$source did not pass SQLite's quick_check; not restoring it"
    refuse_if_in_use
    target=$(archive_path)
    printf 'restoring: %s (%s)\n' "$source" "$(human_size "$source")"
    if [ -f "$db" ]; then
      printf 'the database in place is archived first, to %s\n' "$target"
    fi
    confirm "Replace $db with this archive?"
    refuse_if_in_use
    if [ -f "$db" ]; then
      archive_db
    fi
    remove_db
    run mkdir -p "$data_dir"
    run cp -- "$source" "$db"
    printf 'restored %s\n' "$db"
    printf '\nStart the server; it migrates the restored database to head:\n'
    printf '  make serve\n'
    ;;
esac
