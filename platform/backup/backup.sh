# terrace-backup: encrypted restic backups of database dumps, media and Headscale state.
# Usage: terrace-backup init TARGET | run [TARGET...] | check [TARGET...] | age | metrics | restic TARGET ARGS...

DUMPS=$STATE/dumps

usage() {
  cat >&2 <<'USAGE'
usage: terrace-backup init TARGET        initialise the restic repository (once; never automatic)
       terrace-backup run [TARGET...]    dump databases, hash files, back up, prune
       terrace-backup check [TARGET...]  restic check (metadata plus a data subset)
       terrace-backup age                print backup age; exit 1 if stale or never run
       terrace-backup metrics            rewrite the Prometheus textfile
       terrace-backup restic TARGET ...  run restic against a configured target
USAGE
  exit 2
}

pick_targets() { # remaining args -> SELECTED
  SELECTED=("$@")
  if [[ ${#SELECTED[@]} -eq 0 ]]; then
    SELECTED=("${TARGETS[@]}")
  fi
}

# ---- staging --------------------------------------------------------------

dump_database() { # name
  local db=$1 out=$DUMPS/postgresql
  DUMP_FILE=$out/$db.dump DUMP_DB=$db as_pg psql -X -q -A -t -F $'\t' -d "$db" \
    -v ON_ERROR_STOP=1 -v rowcounts_file="$out/$db.rowcounts.tsv" <<SQL
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_export_snapshot() AS snap \gset
\setenv TERRACE_PG_SNAPSHOT :snap
\! pg_dump --format=custom --compress=0 --snapshot="\$TERRACE_PG_SNAPSHOT" --file="\$DUMP_FILE" "\$DUMP_DB"
\o /dev/null
SELECT CASE WHEN :SHELL_EXIT_CODE = 0 THEN 1 ELSE 1/0 END;
\o :rowcounts_file
$ROWCOUNT_SQL;
\o
COMMIT;
SQL
  [[ -s $out/$db.dump ]] || die "pg_dump produced no output for $db"
  pg_restore --list <"$out/$db.dump" >/dev/null || die "pg_dump output for $db is unreadable"
  LC_ALL=C sort -o "$out/$db.rowcounts.tsv" "$out/$db.rowcounts.tsv"
  log "dumped database $db ($(wc -l <"$out/$db.rowcounts.tsv") tables)"
}

manifest_fileset() { # name
  local name=$1 root=${FILESET_ROOT[$1]} ex excludes=() fargs=(-type f)
  [[ -d $root ]] || die "file set $name: directory missing: $root"
  IFS='|' read -r -a excludes <<<"${FILESET_EXCLUDES[$name]:-}"
  for ex in "${excludes[@]}"; do
    [[ -z $ex ]] || fargs+=(! -name "$ex")
  done
  (cd "$root" && find . "${fargs[@]}" -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum) >"$DUMPS/manifests/$name.sha256"
  printf '%s\n' "$root" >"$DUMPS/manifests/$name.root"
  log "hashed file set $name ($(wc -l <"$DUMPS/manifests/$name.sha256") files)"
}

snapshot_headscale() {
  local copy=$DUMPS/sqlite/headscale.sqlite
  [[ -f $HEADSCALE_DB ]] || die "Headscale database missing: $HEADSCALE_DB"
  install -d -m 0700 "$DUMPS/sqlite"
  sqlite3 "$HEADSCALE_DB" ".backup '$copy'"
  [[ $(sqlite3 "$copy" 'PRAGMA integrity_check') == ok ]] || die "Headscale database snapshot failed integrity_check"
  sqlite_counts "$copy" >"$DUMPS/sqlite/headscale.rowcounts.tsv"
  log "snapshotted Headscale database"
}

prepare_dumps() {
  rm -rf "$DUMPS"
  # The staging root stays traversable so the PostgreSQL user can reach its own 0700 subdirectory.
  install -d -m 0711 "$DUMPS"
  install -d -m 0700 "$DUMPS/manifests"
  if [[ ${#DATABASES[@]} -gt 0 ]]; then
    install -d -o "$PG_USER" -m 0700 "$DUMPS/postgresql"
    as_pg pg_dumpall --globals-only >"$DUMPS/postgresql/globals.sql"
    local db
    for db in "${DATABASES[@]}"; do dump_database "$db"; done
  fi
  if [[ -n $HEADSCALE_ENABLE ]]; then snapshot_headscale; fi
  local fs
  for fs in "${!FILESET_ROOT[@]}"; do manifest_fileset "$fs"; done
  date -u +%FT%TZ >"$DUMPS/created-at"
}

cleanup_dumps() { rm -rf "$DUMPS"; }

# ---- one target -----------------------------------------------------------

backup_target_inner() { # target; run inside a subshell
  local t=$1 paths=() fs ex excludes=() eargs=() bytes
  select_target "$t" || return 1
  paths+=("$DUMPS")
  for fs in "${!FILESET_ROOT[@]}"; do
    paths+=("${FILESET_ROOT[$fs]}")
    IFS='|' read -r -a excludes <<<"${FILESET_EXCLUDES[$fs]:-}"
    for ex in "${excludes[@]}"; do
      [[ -z $ex ]] || eargs+=(--exclude "${FILESET_ROOT[$fs]}/$ex")
    done
  done
  restic "${ROPTS[@]}" backup --tag terrace --host "$BACKUP_HOST" --quiet "${eargs[@]}" "${paths[@]}" || return 1
  restic "${ROPTS[@]}" forget --tag terrace --group-by host,tags --prune --quiet \
    --keep-last "$KEEP_LAST" --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" || return 1
  if bytes=$(restic "${ROPTS[@]}" stats --mode raw-data --json 2>/dev/null | jq -er .total_size); then
    status_set "$t" repo_bytes "$bytes"
  fi
}

backup_target() { # target
  local t=$1 start end
  start=$(date +%s)
  status_set "$t" backup_last_attempt "$start"
  if (backup_target_inner "$t"); then
    end=$(date +%s)
    status_set "$t" backup_last_success "$end"
    status_set "$t" backup_last_ok 1
    status_set "$t" backup_last_duration "$((end - start))"
    log "target $t: backup succeeded in $((end - start))s"
  else
    end=$(date +%s)
    status_set "$t" backup_last_ok 0
    status_set "$t" backup_last_duration "$((end - start))"
    log "target $t: backup FAILED"
    return 1
  fi
}

cmd_run() {
  pick_targets "$@"
  mkdir -p "$STATE/status"
  exec 9>"$STATE/lock"
  flock -w 3600 9 || die "another terrace-backup operation holds the lock"
  local failed=0 t
  trap cleanup_dumps EXIT
  # Staging runs as a separate process so that `set -e` stays active inside it.
  if ! "$0" _stage; then
    log "staging dumps FAILED; no target was updated"
    for t in "${SELECTED[@]}"; do
      status_set "$t" backup_last_attempt "$(date +%s)"
      status_set "$t" backup_last_ok 0
    done
    write_metrics
    exit 1
  fi
  for t in "${SELECTED[@]}"; do
    backup_target "$t" || failed=1
  done
  write_metrics
  exit "$failed"
}

cmd_init() {
  [[ $# -eq 1 ]] || usage
  select_target "$1"
  restic "${ROPTS[@]}" init
}

cmd_check() {
  pick_targets "$@"
  exec 9>"$STATE/lock"
  flock -w 3600 9 || die "another terrace-backup operation holds the lock"
  local failed=0 t
  for t in "${SELECTED[@]}"; do
    if (select_target "$t" && restic "${ROPTS[@]}" check --read-data-subset="$CHECK_SUBSET"); then
      status_set "$t" check_last_success "$(date +%s)"
      status_set "$t" check_last_ok 1
      log "target $t: check succeeded"
    else
      status_set "$t" check_last_ok 0
      log "target $t: check FAILED"
      failed=1
    fi
  done
  write_metrics
  exit "$failed"
}

cmd_age() {
  local max=${TERRACE_BACKUP_MAX_AGE:-$MAX_AGE} now t last rc=0
  now=$(date +%s)
  for t in "${TARGETS[@]}"; do
    last=$(status_get "$t" backup_last_success)
    if [[ -z $last ]]; then
      echo "target $t: no successful backup recorded"
      rc=1
    elif ((now - last > max)); then
      echo "target $t: STALE, last success $((now - last))s ago (limit ${max}s)"
      rc=1
    else
      echo "target $t: ok, last success $((now - last))s ago (limit ${max}s)"
    fi
  done
  exit "$rc"
}

cmd_restic() {
  [[ $# -ge 1 ]] || usage
  local t=$1
  shift
  select_target "$t"
  exec restic "${ROPTS[@]}" "$@"
}

[[ $# -ge 1 ]] || usage
[[ $(id -u) == 0 ]] || die "run as root"
cmd=$1
shift
case "$cmd" in
  init) cmd_init "$@" ;;
  run) cmd_run "$@" ;;
  check) cmd_check "$@" ;;
  age) cmd_age ;;
  metrics) write_metrics ;;
  _stage) prepare_dumps ;;
  restic) cmd_restic "$@" ;;
  *) usage ;;
esac
