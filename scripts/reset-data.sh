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

# Every process holding the database open, one per line. Empty when it is free.
# lsof exits non-zero when nothing matches, which is the common case, not an error.
holders() {
  [ -f "$db" ] || return 0
  lsof -t -- "$db" 2>/dev/null || true
}

refuse_if_in_use() {
  pids=$(holders)
  [ -n "$pids" ] || return 0
  printf 'error: the database is open in another process:\n' >&2
  # shellcheck disable=SC2086 # deliberate word splitting: one -p per pid is not needed.
  ps -o pid=,command= -p $pids >&2 || true
  printf '\nStop the server first, then run this again.\n' >&2
  exit 1
}

human_size() {
  # BSD stat; the fallback keeps this working if stat is missing.
  bytes=$(stat -f %z -- "$1" 2>/dev/null || wc -c <"$1")
  awk -v b="$bytes" 'BEGIN {
    split("B KB MB GB", unit, " ")
    i = 1
    while (b >= 1024 && i < 4) { b /= 1024; i++ }
    printf (i == 1 ? "%d %s\n" : "%.1f %s\n"), b, unit[i]
  }'
}

list_archives() {
  if [ ! -d "$archives" ]; then
    printf 'no archives in %s\n' "$archives"
    return 0
  fi
  found=no
  # Newest first. -t on the glob, not on the directory, so stray files stay out.
  for path in $(ls -t "$archives"/netkeeper-*.sqlite3 2>/dev/null); do
    found=yes
    printf '%s  %s\n' "$(basename "$path")" "$(human_size "$path")"
  done
  [ "$found" = yes ] || printf 'no archives in %s\n' "$archives"
}

confirm() {
  [ "$assume_yes" = no ] || return 0
  [ "$dry_run" = no ] || return 0
  printf '%s [y/N] ' "$1"
  read -r reply
  case $reply in
    y|Y|yes|YES) return 0 ;;
    *) printf 'nothing changed\n'; exit 0 ;;
  esac
}

# Copy the live database to $1. VACUUM INTO checkpoints the WAL into a single
# compacted file, so the copy needs no sidecars. If SQLite refuses (a corrupt
# database is exactly when you most want the copy), fall back to the raw files.
archive_db() {
  target=$1
  run mkdir -p "$archives"
  if [ "$dry_run" = yes ]; then
    printf '  would: sqlite3 %s "VACUUM INTO %s"\n' "$db" "$target"
    return 0
  fi
  if sqlite3 "$db" "VACUUM INTO '$target'" 2>/dev/null; then
    printf 'archived %s (%s)\n' "$target" "$(human_size "$target")"
    return 0
  fi
  printf 'warning: VACUUM INTO failed; copying the raw files instead\n' >&2
  for suffix in '' '-wal' '-shm'; do
    [ -f "$db$suffix" ] || continue
    cp -- "$db$suffix" "$target$suffix"
  done
  printf 'archived %s and its sidecars\n' "$target"
}

remove_db() {
  for suffix in '' '-wal' '-shm'; do
    [ -f "$db$suffix" ] || continue
    run rm -- "$db$suffix"
  done
  [ "$dry_run" = yes ] || printf 'removed %s\n' "$db"
}

stamp=$(date -u +%Y%m%dT%H%M%SZ)

case $mode in
  list)
    list_archives
    ;;

  reset)
    if [ ! -f "$db" ]; then
      printf 'no database at %s: already clean\n' "$db"
      exit 0
    fi
    refuse_if_in_use
    printf 'database: %s (%s)\n' "$db" "$(human_size "$db")"
    printf 'archive:  %s/netkeeper-%s.sqlite3\n' "$archives" "$stamp"
    confirm 'Archive it and start from an empty database?'
    archive_db "$archives/netkeeper-$stamp.sqlite3"
    remove_db
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
    refuse_if_in_use
    printf 'restoring: %s (%s)\n' "$source" "$(human_size "$source")"
    if [ -f "$db" ]; then
      printf 'the database in place is archived first, to %s/netkeeper-%s.sqlite3\n' \
        "$archives" "$stamp"
    fi
    confirm "Replace $db with this archive?"
    if [ -f "$db" ]; then
      archive_db "$archives/netkeeper-$stamp.sqlite3"
      remove_db
    fi
    run mkdir -p "$data_dir"
    run cp -- "$source" "$db"
    printf 'restored %s\n' "$db"
    printf '\nStart the server; it migrates the restored database to head:\n'
    printf '  make serve\n'
    ;;
esac
