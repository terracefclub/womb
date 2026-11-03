# Shared helpers for terrace-backup and terrace-backup-restore. The Nix module
# prepends generated configuration (STATE, TARGETS, T_*, DATABASES, FILESET_*,
# PG_*, RETENTION_*, ...); nothing in here holds a secret.

umask 077

log() { printf '%s terrace-backup: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
die() {
  log "ERROR: $*"
  exit 1
}

# A secret file must be a regular root-owned file that neither group nor
# others can touch, and must not live in the world-readable Nix store.
check_secret() {
  local f=$1 mode owner
  [[ $f == /* ]] || die "secret path must be absolute: $f"
  [[ $f != /nix/store/* ]] || die "secret must not live in the Nix store: $f"
  [[ -f $f ]] || die "secret file missing: $f"
  owner=$(stat -c %u "$f")
  mode=$(stat -c %a "$f")
  [[ $owner == 0 ]] || die "secret file must be owned by root: $f"
  if ((8#$mode & 8#077)); then
    die "secret file must not be accessible by group or others (mode $mode, want 0400): $f"
  fi
}

# Point restic at one configured target. Sets ROPTS (global restic options).
select_target() {
  local t=$1
  [[ -n ${T_REPO[$t]:-} ]] || die "unknown or disabled target: $t"
  unset RESTIC_PASSWORD RESTIC_PASSWORD_COMMAND RESTIC_REPOSITORY_FILE RESTIC_FROM_REPOSITORY \
    AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN B2_ACCOUNT_ID B2_ACCOUNT_KEY
  export RESTIC_REPOSITORY=${T_REPO[$t]}
  export RESTIC_PASSWORD_FILE=${T_PASSFILE[$t]}
  export RESTIC_CACHE_DIR=$STATE/cache
  check_secret "$RESTIC_PASSWORD_FILE"
  if [[ -n ${T_ENVFILE[$t]:-} ]]; then
    check_secret "${T_ENVFILE[$t]}"
    set -a
    # shellcheck disable=SC1090
    source "${T_ENVFILE[$t]}"
    set +a
  fi
  ROPTS=()
  if [[ -n ${T_SFTP_KEY[$t]:-} ]]; then
    check_secret "${T_SFTP_KEY[$t]}"
    [[ -f ${T_SFTP_KNOWN_HOSTS[$t]} ]] || die "known_hosts file missing: ${T_SFTP_KNOWN_HOSTS[$t]}"
    ROPTS+=(-o "sftp.args=-i ${T_SFTP_KEY[$t]} -p ${T_SFTP_PORT[$t]} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${T_SFTP_KNOWN_HOSTS[$t]} -o ServerAliveInterval=30 -o ServerAliveCountMax=10")
  fi
  if [[ -n ${UPLOAD_LIMIT_KIB:-} ]]; then
    ROPTS+=(--limit-upload "$UPLOAD_LIMIT_KIB")
  fi
  mkdir -p "$STATE/cache"
}

as_pg() { runuser -u "$PG_USER" -- "$@"; }

# Row counts per user table of a SQLite file: "table<TAB>count", sorted.
sqlite_counts() {
  local file=$1 tb
  while IFS= read -r tb; do
    printf '%s\t%s\n' "$tb" "$(sqlite3 "$file" "SELECT count(*) FROM \"$tb\";")"
  done < <(sqlite3 "$file" "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;") | LC_ALL=C sort
}

# SQL printing "schema.table<TAB>exact row count" for every ordinary user table.
ROWCOUNT_SQL=$(
  cat <<'SQL'
SELECT n.nspname || '.' || c.relname,
       (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', n.nspname, c.relname), false, true, '')))[1]::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
SQL
)

# ---- status and monitoring signal ----------------------------------------

status_file() { printf '%s/status/%s.env' "$STATE" "$1"; }

status_get() { # target key
  local f
  f=$(status_file "$1")
  [[ -f $f ]] || return 0
  sed -n "s/^$2=//p" "$f" | tail -n 1
}

status_num() { # target key -> value or 0
  local v
  v=$(status_get "$1" "$2")
  printf '%s' "${v:-0}"
}

status_set() { # target key value
  local f tmp
  f=$(status_file "$1")
  mkdir -p "$STATE/status"
  tmp=$(mktemp "$f.XXXXXX")
  {
    grep -v "^$2=" "$f" 2>/dev/null || true
    printf '%s=%s\n' "$2" "$3"
  } >"$tmp"
  mv "$tmp" "$f"
}

# Prometheus textfile-collector format; written atomically after every change.
write_metrics() {
  local t tmp out
  mkdir -p "$STATE/metrics"
  out=$STATE/metrics/terrace_backup.prom
  tmp=$(mktemp "$out.XXXXXX")
  {
    echo '# HELP terrace_backup_last_success_timestamp_seconds Unix time of the last fully successful backup run (0 = never).'
    echo '# TYPE terrace_backup_last_success_timestamp_seconds gauge'
    for t in "${TARGETS[@]}"; do
      printf 'terrace_backup_last_success_timestamp_seconds{target="%s"} %s\n' "$t" "$(status_num "$t" backup_last_success)"
    done
    echo '# HELP terrace_backup_last_run_ok 1 when the latest backup attempt succeeded.'
    echo '# TYPE terrace_backup_last_run_ok gauge'
    for t in "${TARGETS[@]}"; do
      printf 'terrace_backup_last_run_ok{target="%s"} %s\n' "$t" "$(status_num "$t" backup_last_ok)"
    done
    echo '# HELP terrace_backup_last_duration_seconds Duration of the latest backup attempt.'
    echo '# TYPE terrace_backup_last_duration_seconds gauge'
    for t in "${TARGETS[@]}"; do
      printf 'terrace_backup_last_duration_seconds{target="%s"} %s\n' "$t" "$(status_num "$t" backup_last_duration)"
    done
    echo '# HELP terrace_backup_repository_bytes Raw repository size after the latest prune, for storage-growth and cost tracking.'
    echo '# TYPE terrace_backup_repository_bytes gauge'
    for t in "${TARGETS[@]}"; do
      printf 'terrace_backup_repository_bytes{target="%s"} %s\n' "$t" "$(status_num "$t" repo_bytes)"
    done
    echo '# HELP terrace_backup_check_last_success_timestamp_seconds Unix time of the last successful restic check (0 = never).'
    echo '# TYPE terrace_backup_check_last_success_timestamp_seconds gauge'
    for t in "${TARGETS[@]}"; do
      printf 'terrace_backup_check_last_success_timestamp_seconds{target="%s"} %s\n' "$t" "$(status_num "$t" check_last_success)"
    done
    echo '# HELP terrace_backup_check_last_ok 1 when the latest restic check passed.'
    echo '# TYPE terrace_backup_check_last_ok gauge'
    for t in "${TARGETS[@]}"; do
      printf 'terrace_backup_check_last_ok{target="%s"} %s\n' "$t" "$(status_num "$t" check_last_ok)"
    done
  } >"$tmp"
  chmod 0644 "$tmp"
  mv "$tmp" "$out"
}
