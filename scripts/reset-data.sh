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

# A name in $archives nothing else has taken. Two runs in one second must not
# share one, or the second overwrites the first.
archive_path() {
  candidate="$archives/netkeeper-$stamp.sqlite3"
  n=1
  while [ -e "$candidate" ] || [ -e "$candidate-wal" ]; do
    candidate="$archives/netkeeper-$stamp-$n.sqlite3"
    n=$((n + 1))
  done
  printf '%s\n' "$candidate"
}

# Move $1's main file aside as .failed for inspection (removing an old one
# first), and discard any -wal/-shm it accumulated along the way. Prints the
# .failed path, or nothing when $1 itself was never created -- there is
# nothing to quarantine, just the generic "nothing deleted" the caller falls
# back to. Used both when the raw-copy fallback fails partway and when the
# result does not verify, so a partial or corrupt archive never sits in
# archives/ looking like a good one (#183 review bug 5 and should-fix 1).
quarantine_archive() {
  target=$1
  rm -f -- "$target-wal" "$target-shm"
  [ -e "$target" ] || return 0
  failed="$target.failed"
  rm -f -- "$failed"
  mv -- "$target" "$failed"
  printf '%s\n' "$failed"
}

# Copy the live database to $1 and prove the copy is readable before anything is
# deleted. VACUUM INTO writes one compacted file with the WAL already folded in.
# If SQLite refuses, copy the raw files, then fold the WAL into the copy, so an
# archive is always one self-contained file -- and verify it either way.
archive_db() {
  target=$1
  run mkdir -p "$archives"
  if [ "$dry_run" = yes ]; then
    printf '  would: sqlite3 %s "VACUUM INTO %s", then verify it\n' "$db" "$target"
    return 0
  fi
  quoted=$(printf '%s' "$target" | sed "s/'/''/g")
  if ! sqlite3 "$db" "VACUUM INTO '$quoted'"; then
    printf 'warning: VACUUM INTO failed; copying the raw files instead\n' >&2
    rm -f -- "$target"
    for suffix in '' '-wal' '-shm'; do
      [ -f "$db$suffix" ] || continue
      if ! cp -- "$db$suffix" "$target$suffix"; then
        failed=$(quarantine_archive "$target")
        if [ -n "$failed" ]; then
          die "could not copy $db$suffix; moved the partial archive to $failed for inspection. The database was left in place."
        fi
        die "could not copy $db$suffix; nothing deleted"
      fi
    done
    sqlite3 "$target" 'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE;' >/dev/null 2>&1 || true
    rm -f -- "$target-wal" "$target-shm"
  fi
  if ! verified "$target"; then
    failed=$(quarantine_archive "$target")
    if [ -n "$failed" ]; then
      die "the archive did not pass SQLite's quick_check; moved it to $failed for inspection. The database was left in place."
    fi
    die "the archive $target did not pass SQLite's quick_check; the database was left in place"
  fi
  printf 'archived %s (%s, verified)\n' "$target" "$(human_size "$target")"
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
    archive_db "$target"
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
      archive_db "$target"
    fi
    remove_db
    run mkdir -p "$data_dir"
    run cp -- "$source" "$db"
    printf 'restored %s\n' "$db"
    printf '\nStart the server; it migrates the restored database to head:\n'
    printf '  make serve\n'
    ;;
esac
