# terrace-backup-restore: restore one snapshot into an isolated directory and a
# throwaway PostgreSQL cluster, then reconcile against the manifests that the
# backup embedded in the snapshot. It never touches live data, never listens
# on the network (socket only) and never contacts mail or payment services.

usage() {
  cat >&2 <<'USAGE'
usage: terrace-backup-restore --target TARGET --dest DIR [--snapshot ID|latest] [--host HOST] [--keep-running]
       terrace-backup-restore --verify-only DIR    re-run reconciliation against a running restored cluster
       terrace-backup-restore --stop DIR           stop the restored cluster
DEST must not exist (or be empty) and must not overlap live data.
USAGE
  exit 2
}

TARGET="" DEST="" SNAPSHOT=latest SNAP_HOST="" KEEP=0 MODE=restore
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET=${2:?}; shift 2 ;;
    --dest) DEST=${2:?}; shift 2 ;;
    --snapshot) SNAPSHOT=${2:?}; shift 2 ;;
    --host) SNAP_HOST=${2:?}; shift 2 ;;
    --keep-running) KEEP=1; shift ;;
    --verify-only) MODE=verify; DEST=${2:?}; shift 2 ;;
    --stop) MODE=stop; DEST=${2:?}; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n $DEST ]] || usage
[[ $(id -u) == 0 ]] || die "run as root"
DEST=$(realpath -m "$DEST")
SOCK=$DEST/pgsock
FILES=$DEST/files
DUMPS_R=$FILES$STATE/dumps
REPORT=$DEST/reconciliation.txt
FAILS=0
LEAVE_RUNNING=0

say() { printf '%s\n' "$*" | tee -a "$REPORT" >&2; }
fail() {
  say "MISMATCH: $*"
  FAILS=$((FAILS + 1))
}

rpg() { runuser -u "$RESTORE_USER" -- "$@"; }
rpsql() { rpg psql -X -q -h "$SOCK" -U "$RESTORE_USER" "$@"; }

check_dest() {
  local p real
  [[ $DEST != / ]] || die "refusing to restore into /"
  if [[ -e $DEST ]]; then
    [[ -d $DEST && -z $(ls -A "$DEST") ]] || die "destination exists and is not empty: $DEST"
  fi
  for p in "${PROTECTED_PATHS[@]}"; do
    real=$(realpath -m "$p")
    if [[ $DEST == "$real" || $DEST == "$real"/* || $real == "$DEST"/* ]]; then
      die "destination $DEST overlaps live path $real; choose an isolated directory"
    fi
  done
}

start_db() {
  rpg pg_ctl -D "$DEST/pgdata" -w -t 120 -l "$DEST/pgdata/postgres.log" \
    -o "-c listen_addresses= -c unix_socket_directories=$SOCK -c fsync=off -c max_connections=20 -c shared_buffers=64MB" start >/dev/null
}

stop_db() { rpg pg_ctl -D "$DEST/pgdata" -m fast -w stop >/dev/null; }

cleanup_db() {
  local rc=$?
  trap - EXIT
  if [[ $LEAVE_RUNNING -eq 0 && -f $DEST/pgdata/postmaster.pid ]]; then
    if ! stop_db; then
      log "could not stop isolated database at $DEST/pgdata"
      rc=1
    fi
  fi
  exit "$rc"
}

restore_files() {
  local args=(restore "$SNAPSHOT" --target "$FILES" --quiet)
  # Restic preserves original permissions inside this root-only boundary.
  install -d -m 0700 "$FILES"
  if [[ $SNAPSHOT == latest ]]; then
    args+=(--tag terrace)
    [[ -z $SNAP_HOST ]] || args+=(--host "$SNAP_HOST")
  fi
  select_target "$TARGET"
  restic "${ROPTS[@]}" "${args[@]}"
  [[ -d $DUMPS_R ]] || die "snapshot contains no staged dumps at $DUMPS_R"
  say "restored snapshot $SNAPSHOT from target $TARGET into $FILES"
}

restore_databases() {
  local dump db n=0
  shopt -s nullglob
  local dumps=("$DUMPS_R"/postgresql/*.dump)
  shopt -u nullglob
  [[ ${#dumps[@]} -gt 0 ]] || { say "no PostgreSQL dumps in snapshot"; return 0; }
  install -d -o "$RESTORE_USER" -m 0700 "$DEST/pgdata" "$SOCK"
  rpg initdb -D "$DEST/pgdata" -U "$RESTORE_USER" --auth-local=trust --no-instructions --no-locale -E UTF8 >"$DEST/initdb.log"
  start_db
  # Roles carry password hashes: keep output out of the terminal, errors in a 0600 file.
  rpsql -v ON_ERROR_STOP=1 -d postgres -f - <"$DUMPS_R/postgresql/globals.sql" >/dev/null 2>"$DEST/globals.err" \
    || die "role restore failed (see $DEST/globals.err)"
  for dump in "${dumps[@]}"; do
    db=$(basename "$dump" .dump)
    rpg pg_restore --create --exit-on-error --dbname=postgres -h "$SOCK" -U "$RESTORE_USER" <"$dump" 2>"$DEST/restore-$db.err" \
      || die "pg_restore failed for $db (see $DEST/restore-$db.err)"
    n=$((n + 1))
  done
  say "restored $n database(s) into the isolated cluster at $SOCK"
}

reconcile_databases() {
  local m db tables rows
  shopt -s nullglob
  for m in "$DUMPS_R"/postgresql/*.rowcounts.tsv; do
    db=$(basename "$m" .rowcounts.tsv)
    if ! rpsql -d "$db" -A -t -F $'\t' -c "$ROWCOUNT_SQL" 2>/dev/null | LC_ALL=C sort >"$DEST/actual-$db.tsv"; then
      fail "database $db: cannot read restored tables"
      continue
    fi
    if diff -u "$m" "$DEST/actual-$db.tsv" >"$DEST/diff-$db.txt"; then
      tables=$(wc -l <"$m")
      rows=$(awk -F'\t' '{s += $2} END {print s + 0}' "$m")
      say "OK database $db: $tables tables, $rows rows match the backup-time counts"
    else
      fail "database $db: row counts differ from backup-time counts (see $DEST/diff-$db.txt)"
    fi
  done
  shopt -u nullglob
}

reconcile_files() {
  local m name root base expected actual
  shopt -s nullglob
  for m in "$DUMPS_R"/manifests/*.sha256; do
    name=$(basename "$m" .sha256)
    root=$(cat "$DUMPS_R/manifests/$name.root")
    base=$FILES$root
    if [[ ! -d $base ]]; then
      fail "file set $name: restored directory missing ($root)"
      continue
    fi
    expected=$(wc -l <"$m")
    actual=$(cd "$base" && find . -type f | wc -l)
    if [[ $expected != "$actual" ]]; then
      fail "file set $name: $expected files in manifest, $actual restored"
      continue
    fi
    if [[ $expected -gt 0 ]] && ! (cd "$base" && sha256sum --check --strict --quiet "$m" >"$DEST/hashfail-$name.txt" 2>&1); then
      fail "file set $name: sha256 mismatch (see $DEST/hashfail-$name.txt)"
      continue
    fi
    say "OK file set $name: $expected files, all sha256 hashes match"
  done
  shopt -u nullglob
}

reconcile_sqlite() {
  local f name
  shopt -s nullglob
  for f in "$DUMPS_R"/sqlite/*.sqlite; do
    name=$(basename "$f" .sqlite)
    if [[ $(sqlite3 "$f" 'PRAGMA integrity_check') != ok ]]; then
      fail "sqlite $name: integrity_check failed"
    elif diff -u "$DUMPS_R/sqlite/$name.rowcounts.tsv" <(sqlite_counts "$f") >"$DEST/diff-sqlite-$name.txt"; then
      say "OK sqlite $name: integrity ok, table row counts match"
    else
      fail "sqlite $name: row counts differ (see $DEST/diff-sqlite-$name.txt)"
    fi
  done
  shopt -u nullglob
}

reconcile() {
  reconcile_databases
  reconcile_files
  reconcile_sqlite
  if [[ $FAILS -eq 0 ]]; then
    say "RECONCILIATION OK"
  else
    say "RECONCILIATION FAILED: $FAILS mismatch(es)"
    return 1
  fi
}

case "$MODE" in
  stop)
    stop_db
    ;;
  verify)
    [[ -d $DEST/files ]] || die "not a restored directory: $DEST"
    : >"$REPORT"
    reconcile
    ;;
  restore)
    [[ -n $TARGET ]] || usage
    check_dest
    trap cleanup_db EXIT
    install -d -m 0711 "$DEST"
    : >"$REPORT"
    restore_files
    restore_databases
    rc=0
    reconcile || rc=1
    if [[ $KEEP -eq 1 && $rc -eq 0 && -d $DEST/pgdata ]]; then
      LEAVE_RUNNING=1
      say "restored cluster left running; connect with: runuser -u $RESTORE_USER -- psql -h $SOCK -U $RESTORE_USER -d DBNAME"
      say "stop it with: terrace-backup-restore --stop $DEST"
    fi
    exit "$rc"
    ;;
esac
