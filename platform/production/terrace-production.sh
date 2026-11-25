# terrace-production: adopt a production host (start its writers once, then at every boot), arm the timers that
# a restored adoption holds, activate a signed system closure or roll back to the previous generation, and report
# the state. Root only. platform/production/base.nix prepends the host's settings (paths, unit lists, host name,
# role, deploy keys, stability window) and builds it with writeShellApplication (errexit, nounset, pipefail).
# Unit tests: tests/platform/test_terrace_production.py (stub systemctl and tools); deploy path in
# docs/host-updates.md ("Deploy path").
#
#   terrace-production adopt --new [--first-deployment-without-repository HOSTNAME] | adopt --restored
#   terrace-production arm-timers [--in-service] | status
#   terrace-production activate STORE_PATH [--boot] [--cms-stays-down] [--accept-upstream-schema-change]
#   terrace-production rollback [--boot] [--cms-stays-down] [--accept-upstream-schema-change]
#
# Settings: restored populated adopted adopting replacement state_dir hold service_hold hostname stability_seconds
# role instance cms_marker store_dir profile current_system booted_system and the arrays targets writers helpers
# conditional timers held service_held deploy_keys busy quiesce.
[[ $(id -u) == 0 ]] || { echo 'terrace-production: run as root' >&2; exit 1; }
refuse() { echo "terrace-production: not adopted: $*" >&2; exit 1; }
# Whether UNIT is held now: in a group whose marker exists.
is_held() {
  { [[ -e $hold && " ${held[*]} " == *" $1 "* ]]; } || { [[ -e $service_hold && " ${service_held[*]} " == *" $1 "* ]]; }
}
timer_list() { local unit; for unit in "$@"; do printf '%s.timer ' "$unit"; done; }
# A host whose backup repository already holds snapshots of it is a replacement, whatever its disk says.
# The repository is per role: ANY snapshot in it means this role already has a deployment
# (also when a replacement was given a different host name).
repository_check() {
  local target out rc
  for target in "${targets[@]}"; do
    rc=0
    out=$(timeout 120 terrace-backup restic "$target" snapshots --json 2>/dev/null) || rc=$?
    if [[ $rc == 0 ]]; then
      if [[ $(jq length <<<"$out") != 0 ]]; then
        refuse "backup repository $target already holds snapshots; this is a replacement: restore, then adopt --restored"
      fi
    elif [[ $rc == 10 ]]; then
      : # not initialised yet: a genuinely new deployment
    elif [[ ${1:-} == --first-deployment-without-repository && ${2:-} == "$hostname" ]]; then
      echo "terrace-production: WARNING: adopting as new without seeing backup repository $target (restic exit $rc). Only for a first deployment whose repository does not exist yet, after confirming no earlier host existed." >&2
    elif [[ $rc == 124 ]]; then
      refuse "backup repository $target did not answer within 120 s; fix access (a replacement must be restored, not adopted as new)"
    else
      refuse "backup repository $target unreachable (restic exit $rc); fix access (a replacement must be restored, not adopted as new)"
    fi
  done
}
# Success means every writer is active (or skipped by its documented condition) and every helper succeeded.
settled() {
  local unit state condition problems=()
  for unit in "${writers[@]}"; do
    state=$(systemctl show "$unit.service" -p ActiveState --value)
    condition=$(systemctl show "$unit.service" -p ConditionResult --value)
    if [[ $state != active ]] && ! { [[ " ${conditional[*]} " == *" $unit "* ]] && [[ $state == inactive && $condition == no ]]; }; then
      problems+=("$unit.service:$state")
    fi
  done
  for unit in "${helpers[@]}"; do
    if systemctl is-failed --quiet "$unit.service" || [[ $(systemctl show "$unit.service" -p Result --value) != success ]]; then
      problems+=("$unit.service:$(systemctl show "$unit.service" -p Result --value)")
    fi
  done
  for unit in "${timers[@]}"; do
    # A timer held after a restored adoption is skipped by its start condition until `arm-timers`.
    is_held "$unit" && continue
    [[ $(systemctl show "$unit.timer" -p ActiveState --value) == active ]] || problems+=("$unit.timer")
  done
  if (( ${#problems[@]} )); then
    echo "terrace-production: not adopted yet; not running: ${problems[*]}. Fix them and run the same adopt command again (it resumes)." >&2
    return 1
  fi
}

# ---- Deploy path: activate a signed closure, roll back a generation (docs/host-updates.md, "Deploy path") ----
# Every check runs before anything changes; every change (profile, switch, record) runs inside the transient unit
# terrace-switch, so a dropped SSH session cannot interrupt it.
self=$(readlink -f "${BASH_SOURCE[0]}")
records=$state_dir/deployments.log
deny() { echo "terrace-production: nothing changed: $*" >&2; exit 1; }
deploy_usage() { echo 'usage: terrace-production activate STORE_PATH [--boot] [--cms-stays-down] [--accept-upstream-schema-change] | rollback [--boot] [--cms-stays-down] [--accept-upstream-schema-change]' >&2; exit 2; }
manifest_file() { printf '%s/etc/terrace/system.json' "$1"; }
# A manifest field as text ("" for null or missing).
field() { jq -r "$2 // \"\" | tostring" "$(manifest_file "$1")"; }
# The generation number the system profile points at ("none" without a profile).
gen_of() {
  local link
  link=$(readlink "$profile" 2>/dev/null) || { echo none; return 0; }
  link=${link##*/}; link=${link%-link}; echo "${link##*-}"
}
# The newest generation older than $1 ("" if none).
previous_gen() {
  local link n best=""
  [[ $1 =~ ^[0-9]+$ ]] || { echo ""; return 0; }
  for link in "$profile"-*-link; do
    [[ -L $link ]] || continue
    n=${link##*/}; n=${n%-link}; n=${n##*-}
    [[ $n =~ ^[0-9]+$ ]] || continue
    if (( n < $1 )) && { [[ -z $best ]] || (( n > best )); }; then best=$n; fi
  done
  echo "$best"
}
describe() {
  local womb website
  if [[ -f $(manifest_file "$1") ]]; then
    womb=$(field "$1" .wombRevision); website=$(field "$1" .website.revision)
    echo "Womb ${womb:-unrecorded}, website ${website:-none}"
  else
    echo 'no Terrace manifest'
  fi
}
# A NixOS system built by this profile for THIS host: canonical, valid, switchable, manifest naming this host.
check_system() {
  local target=$1 rest got
  rest=${target#"$store_dir"/}
  [[ $target == "$store_dir"/* && $rest =~ ^[0-9a-z]{32}-[A-Za-z0-9+._?=-]+$ ]] || deny "$target is not a store path; give the system toplevel ($store_dir/...-nixos-system-...)"
  [[ -d $target && ! -L $target ]] || deny "$target does not exist on this host; copy it first"
  nix-store --check-validity "$target" 2>/dev/null || deny "$target is not a valid path in this host's Nix store; copy it first"
  [[ -x $target/bin/switch-to-configuration ]] || deny "$target is not a NixOS system (no bin/switch-to-configuration)"
  [[ -f $(manifest_file "$target") ]] || deny "$target has no Terrace system manifest (etc/terrace/system.json); not built by the production profile"
  got=$(jq -r '[.schema, .role, .hostName, .instance] | map(tostring) | join(" ")' "$(manifest_file "$target")" 2>/dev/null) || deny "$target has an unreadable system manifest"
  [[ $got == "1 $role $hostname $instance" ]] || deny "$target was built for (schema role host instance) '$got'; this host is '1 $role $hostname $instance'"
  [[ $(readlink -f "$current_system") != "$target" ]] || deny "$target is already the running system"
}
check_idle() {
  local unit state
  for unit in "${busy[@]}"; do
    state=$(systemctl show "$unit.service" -p ActiveState --value)
    case $state in active | activating | deactivating | reloading) deny "$unit.service is $state; wait until it has finished" ;; esac
  done
}
# Paths already in a system generation on this host arrived by a root-trusted route (installation or an earlier
# activation); every other path of the closure must carry a valid signature by a trusted deploy key, contents rehashed.
check_signatures() {
  local target=$1 known closure link out new=()
  (( ${#deploy_keys[@]} )) || deny 'no deploy key is trusted on this host (terrace.production.deploy.trustedKeys is empty): the deploy path is off'
  known=$(for link in "$current_system" "$booted_system" "$profile"-*-link; do if [[ -e $link ]]; then nix-store -qR "$link"; fi; done | LC_ALL=C sort -u)
  closure=$(nix-store -qR "$target" | LC_ALL=C sort -u)
  mapfile -t new < <(LC_ALL=C comm -13 <(printf '%s\n' "$known") <(printf '%s\n' "$closure"))
  if (( ${#new[@]} )); then
    if ! out=$(nix --extra-experimental-features nix-command store verify --sigs-needed 1 --option trusted-public-keys "${deploy_keys[*]}" "${new[@]}" 2>&1); then
      printf '%s\n' "$out" | head -n 20 >&2
      deny "${#new[@]} path(s) of $target are in no system generation of this host and not all carry a valid signature by a trusted deploy key (or their contents do not match)"
    fi
  fi
  echo "Signatures: ${#new[@]} new path(s) verified against ${#deploy_keys[@]} trusted deploy key(s)."
}
# A new kernel, initrd or module set is loaded only by a reboot: `switch` would leave the old kernel running.
check_boot() {
  local target=$1 item changed=()
  for item in kernel initrd kernel-modules; do
    [[ $(readlink -f "$booted_system/$item") == "$(readlink -f "$target/$item")" ]] || changed+=("$item")
  done
  if (( ${#changed[@]} )) && [[ $mode != boot ]]; then
    deny "${changed[*]} differ(s) from the booted system; repeat with --boot, then reboot (docs/host-updates.md section 5, U6)"
  fi
}
check_upstream() {
  local target=$1 svc from to changes=()
  [[ -f $(manifest_file "$current_system") ]] || deny "the running system has no Terrace manifest; it cannot be compared with $target"
  from=$(field "$current_system" .versions.postgresql); to=$(field "$target" .versions.postgresql)
  [[ ${from%%.*} == "${to%%.*}" ]] || deny "PostgreSQL major version changes ($from -> $to); no upgrade path exists (docs/host-updates.md step 2)"
  for svc in keycloak headscale; do
    from=$(field "$current_system" ".versions.$svc"); to=$(field "$target" ".versions.$svc")
    [[ $from == "$to" ]] || changes+=("$svc $from -> $to")
  done
  if (( ${#changes[@]} )); then
    (( accept_upstream )) || deny "upstream version change: ${changes[*]}. Its database schema may be migrated when it starts, a point of no return (owner decision U3). Repeat with --accept-upstream-schema-change once decided"
    echo "WARNING: upstream version change accepted (U3): ${changes[*]}; a generation rollback may not start the old version on the new schema." >&2
  fi
}
# Forward-only CMS migrations: never move to a release whose migration list lacks what the database has applied.
check_cms() {
  local target=$1 unit marker="" exists ledger extra target_rev
  [[ -n $cms_marker ]] || return 0
  [[ $(jq -c .website "$(manifest_file "$current_system")") != "$(jq -c .website "$(manifest_file "$target")")" ]] || return 0
  for unit in "${quiesce[@]}"; do
    [[ $(systemctl show "$unit.service" -p ActiveState --value) != active ]] || deny "the release changes and $unit.service is running; quiesce first (docs/host-updates.md step 6)"
  done
  target_rev=$(field "$target" .website.revision)
  if [[ -e $cms_marker ]]; then marker=$(cat "$cms_marker"); fi
  if [[ $(systemctl show postgresql.service -p ActiveState --value) != active ]]; then
    [[ -z $marker ]] && return 0
    deny "PostgreSQL is not running, so the CMS migration ledger cannot be compared with the target release; start postgresql.service and repeat"
  fi
  exists=$(runuser -u postgres -- psql -XAtw -v ON_ERROR_STOP=1 -d postgres -c "select count(*) from pg_database where datname = 'terrace_cms'" 2>/dev/null) || deny 'cannot query PostgreSQL for the CMS migration ledger'
  [[ $exists == 1 ]] || return 0
  exists=$(runuser -u postgres -- psql -XAtw -v ON_ERROR_STOP=1 -d terrace_cms -c "select to_regclass('public.payload_migrations') is not null" 2>/dev/null) || deny 'cannot query the CMS database for its migration ledger'
  [[ $exists == t ]] || return 0
  ledger=$(runuser -u postgres -- psql -XAtw -v ON_ERROR_STOP=1 -d terrace_cms -c 'select name from payload_migrations' 2>/dev/null) || deny 'cannot read the CMS migration ledger'
  [[ -n $ledger ]] || return 0
  jq -e '.website.migrations | type == "array"' "$(manifest_file "$target")" >/dev/null || deny "$target records no CMS migration list, so nothing shows its release accepts this database"
  extra=$(LC_ALL=C comm -23 <(LC_ALL=C sort -u <<<"$ledger") <(jq -r '.website.migrations[]' "$(manifest_file "$target")" | LC_ALL=C sort -u) | tr '\n' ' ')
  [[ -n $extra ]] || return 0
  if [[ $marker == "$target_rev" ]]; then
    deny "the CMS ledger records migrations the target release does not have (${extra% }) while the marker holds the target's revision $target_rev (a failed or partial migration, H9): its CMS would start on a newer schema. Not allowed by any flag; docs/host-updates.md section 4.3"
  fi
  (( cms_stays_down )) || deny "the CMS database has migrations the target release does not have (${extra% }); migrations are forward-only, so its CMS will refuse to start (its gate wants $target_rev, the marker holds ${marker:-nothing}). The way back for the data is a restore onto a replacement (docs/host-updates.md section 4.3). To change the system anyway with the CMS down, repeat with --cms-stays-down"
  echo "WARNING: the CMS stays down on $target (marker ${marker:-absent}, gate wants $target_rev). Never start terrace-cms-migrate.service on it (H1)." >&2
}
# Inside the terrace-switch unit: register the generation, switch, and record the result.
# A switch to a release whose migration has not run starts the CMS and website (terrace-production.target wants
# them): the CMS fails its gate and the website its health check, and switch-to-configuration exits 4. That outcome,
# and only it, is recorded as switched with those units down: exit 4, every failed unit one of the quiesce units,
# and the marker not holding the target's revision. Anything else that failed is a failed switch.
apply() {
  local action=$1 mode=$2 target=$3 generation=${4:-} from before after rc=0 result marker="" unit expected=1 failed=() starting=()
  from=$(readlink -f "$current_system"); before=$(gen_of)
  # A start limit hit by an earlier gate refusal must not keep a unit from starting where its gate is open.
  if (( ${#quiesce[@]} )); then systemctl reset-failed "${quiesce[@]/%/.service}" 2>/dev/null || true; fi
  if [[ $action == activate ]]; then nix-env -p "$profile" --set "$target" || rc=$?
  else nix-env -p "$profile" --switch-generation "$generation" || rc=$?; fi
  if [[ $rc == 0 ]]; then "$target/bin/switch-to-configuration" "$mode" || rc=$?; fi
  after=$(gen_of)
  if [[ $rc == 4 ]]; then
    mapfile -t failed < <(systemctl list-units --state=failed --type=service --plain --no-legend | cut -d' ' -f1)
    if [[ -n $cms_marker && -e $cms_marker ]]; then marker=$(cat "$cms_marker"); fi
    [[ -n $cms_marker && $marker != "$(field "$target" .website.revision)" && ${#failed[@]} -gt 0 ]] || expected=0
    for unit in "${failed[@]}"; do [[ " ${quiesce[*]/%/.service} " == *" $unit "* ]] || expected=0; done
    # The website has no gate and restarts on failure: when the switch returns it can still be retrying its health check.
    for unit in "${quiesce[@]}"; do
      [[ $(systemctl show "$unit.service" -p ActiveState --value) != activating ]] || starting+=("$unit.service")
    done
  fi
  if [[ $rc == 0 ]]; then result=success
  elif [[ $rc == 4 && $expected == 1 ]]; then result="switched; down until the CMS migration: ${failed[*]}"; rc=0
  else result="failed (exit $rc)"; fi
  install -d -m 0700 "$state_dir"
  jq -cn --arg time "$(date -u +%FT%TZ)" --arg action "$action" --arg mode "$mode" --arg from "$from" --arg to "$target" \
    --arg before "$before" --arg after "$after" --arg womb "$(field "$target" .wombRevision)" --arg website "$(field "$target" .website.revision)" --arg result "$result" \
    --arg failed "${failed[*]}" --arg starting "${starting[*]}" \
    '{time: $time, action: $action, mode: $mode, from: $from, to: $to, generationBefore: $before, generationAfter: $after, wombRevision: $womb, websiteRevision: $website, result: $result, failedUnits: ($failed | split(" ") | map(select(. != ""))), startingUnits: ($starting | split(" ") | map(select(. != "")))}' >>"$records"
  chmod 0600 "$records"
  exit "$rc"
}
deploy() {
  local action=$1 target="" generation="" rc=0
  shift
  mode=switch cms_stays_down=0 accept_upstream=0
  while (( $# )); do
    case $1 in
      --boot) mode=boot ;;
      --cms-stays-down) cms_stays_down=1 ;;
      --accept-upstream-schema-change) accept_upstream=1 ;;
      -*) deploy_usage ;;
      *) [[ $action == activate && -z $target ]] || deploy_usage; target=$1 ;;
    esac
    shift
  done
  if [[ $action == activate ]]; then
    [[ -n $target ]] || deploy_usage
  else
    generation=$(previous_gen "$(gen_of)")
    [[ -n $generation ]] || deny "no generation older than the current one ($(gen_of)) in $profile"
    target=$(readlink -f "$profile-$generation-link")
  fi
  check_system "$target"
  check_idle
  if [[ $action == activate ]]; then check_signatures "$target"; fi
  check_boot "$target"
  check_upstream "$target"
  check_cms "$target"
  echo "Checks passed; ${action} $target ($(describe "$target")) with switch-to-configuration $mode in unit terrace-switch."
  systemd-run --unit=terrace-switch --collect --service-type=exec --wait --quiet -p "Description=terrace-production $action" -- "$self" __apply "$action" "$mode" "$target" "$generation" || rc=$?
  tail -n 1 "$records" 2>/dev/null || true
  if [[ $rc != 0 ]]; then
    echo "terrace-production: $action FAILED (exit $rc). Read: journalctl -u terrace-switch.service -o cat --no-pager; systemctl --failed" >&2
    exit 1
  fi
  if [[ $mode == boot ]]; then echo "Generation $(gen_of) is the boot default; reboot to run it."; else echo "Running generation $(gen_of): $(readlink -f "$current_system")."; fi
  if [[ $(tail -n 1 "$records" | jq -r .result) == "switched;"* ]]; then
    echo "The CMS gate is closed for this release: $(tail -n 1 "$records" | jq -r '.failedUnits | join(" ")') stay failed until the migration (docs/host-updates.md steps 10 and 11)."
    pending=$(tail -n 1 "$records" | jq -r '.startingUnits | join(" ")')
    if [[ -n $pending ]]; then
      echo "Still starting without a gate (H7): $pending; it retries its health check against the stopped CMS and fails within minutes. Stop it now: systemctl stop $pending (step 7)."
    fi
  fi
}
deploy_status() {
  local running generation previous
  running=$(readlink -f "$current_system")
  echo "running system: $running ($(describe "$running"))"
  generation=$(gen_of)
  echo "generation: $generation (the boot default)"
  if [[ $generation != none && $(readlink -f "$profile") != "$running" ]]; then
    echo "WARNING: the boot default, generation $generation ($(readlink -f "$profile")), is not the running system: a reboot boots generation $generation. Expected after activate --boot; otherwise a switch failed after registering it (see the last activation and docs/host-updates.md section 1.5)."
  fi
  previous=$(previous_gen "$generation")
  if [[ -n $previous ]]; then echo "previous generation: $previous $(readlink -f "$profile-$previous-link") ($(describe "$(readlink -f "$profile-$previous-link")"))"; else echo 'previous generation: none'; fi
  if (( ${#deploy_keys[@]} )); then echo "deploy path: on, ${#deploy_keys[@]} trusted deploy key(s)"; else echo 'deploy path: off (no deploy key trusted)'; fi
  if [[ -s $records ]]; then echo "last activation: $(tail -n 1 "$records")"; fi
}
case "${1:-}" in
  adopt)
    mode=${2:-}
    case "$mode" in
      --new | --restored) ;;
      *) echo 'usage: terrace-production adopt --new [--first-deployment-without-repository HOSTNAME] | adopt --restored' >&2; exit 2 ;;
    esac
    [[ -e "$adopted" ]] && { echo 'Already adopted.'; exit 0; }
    case "$mode" in
      --new)
        [[ -e $replacement ]] && refuse "this host was delivered as a replacement; restore, then adopt --restored"
        if [[ $(cat "$adopting" 2>/dev/null) != new ]]; then
          # A new deployment starts from nothing: refuse a host that already holds state or a restore.
          if [[ -e $populated || -e $restored ]]; then
            refuse "this host holds data or a restore; use adopt --restored after a reconciled restore"
          fi
        fi
        repository_check "${3:-}" "${4:-}" ;;
      --restored)
        if ! grep -qx 'RECONCILIATION OK' "$restored" 2>/dev/null || [[ ! -e $populated ]]; then
          refuse "no reconciled restore promoted into place; restore first (docs/production-profile.md)"
        fi ;;
      *) echo 'usage: terrace-production adopt --new [--first-deployment-without-repository HOSTNAME] | adopt --restored' >&2; exit 2 ;;
    esac
    [[ -e $adopting && $(cat "$adopting") != "${mode#--}" ]] && refuse "an adoption as $(cat "$adopting") is in progress; repeat that one"
    # Fail closed: the writers start only on a host whose secrets verify.
    terrace-secrets verify
    install -d -m 0700 "$state_dir"
    echo "${mode#--}" >"$adopting"
    chmod 0600 "$adopting"
    # After a restore the backup, check and freshness timers stay off until the operator has verified the
    # restored services (a scheduled backup ends in `forget --prune` on the repository the restore came from),
    # and the sweep until the replacement is declared in service, after any erasure replay. The markers are
    # written before the target starts (and again by a resumed adoption), so no start of the target, at
    # adoption or at boot, arms a held timer; only `arm-timers` (and `arm-timers --in-service`) remove them.
    if [[ $mode == --restored ]]; then
      install -m 0600 /dev/null "$hold"
      if (( ${#service_held[@]} )); then install -m 0600 /dev/null "$service_hold"; fi
    fi
    reset=()
  for unit in "${writers[@]}" "${helpers[@]}"; do reset+=("$unit.service"); done
  systemctl reset-failed "${reset[@]}" 2>/dev/null || true
    systemctl start terrace-production.target || true
    # A writer can report "started" before it fails: nginx, for one, is active for a few
    # seconds while it retries a port it cannot bind. Require the same processes to be
    # running after a stability window, not just a passing snapshot.
    declare -A seen=()
    for unit in "${writers[@]}"; do
      seen[$unit]=$(systemctl show "$unit.service" -p MainPID -p NRestarts --value | tr '\n' ' ')
    done
    sleep "$stability_seconds"
    unstable=()
    for unit in "${writers[@]}"; do
      [[ $(systemctl show "$unit.service" -p MainPID -p NRestarts --value | tr '\n' ' ') == "${seen[$unit]}" ]] || unstable+=("$unit.service")
    done
    stable=yes
    if (( ${#unstable[@]} )); then
      echo "terrace-production: not adopted yet; restarted during the stability window: ${unstable[*]}. Fix them and run the same adopt command again (it resumes)." >&2
      stable=no
    fi
    if ! settled || [[ $stable == no ]]; then exit 1; fi
    install -m 0600 /dev/null "$adopted"
    date -u +%FT%TZ >"$adopted"
    rm -f "$adopting"
    if [[ $mode == --restored ]]; then
      rm -f "$replacement"
      echo 'Adopted: the writers start now and at every boot.'
      echo "Held until you have verified the restored services: $(timer_list "${held[@]}")(they stay off across reboots)."
      echo 'Arm them with: terrace-production arm-timers'
      if (( ${#service_held[@]} )); then
        echo "Held until the replacement is declared in service, after any erasure replay: $(timer_list "${service_held[@]}")(they stay off across reboots)."
        echo 'Arm it then with: terrace-production arm-timers --in-service'
      fi
    else
      echo 'Adopted: the writers start now and at every boot.'
    fi
    ;;
  arm-timers)
    case "${2:-}" in
      "" | --in-service) ;;
      *) echo 'usage: terrace-production arm-timers [--in-service]' >&2; exit 2 ;;
    esac
    [[ -e "$adopted" ]] || { echo 'terrace-production: not adopted; finish terrace-production adopt first' >&2; exit 1; }
    systemctl is-active --quiet terrace-production.target || { echo 'terrace-production: the writers are not running (terrace-production.target is not active); nothing armed' >&2; exit 1; }
    if [[ ${2:-} == --in-service ]]; then
      # The stages keep their order: the backup timers come first, after the first backup by hand, so the sweep
      # stage never arms backups that were not verified (their runs prune the repository the restore came from).
      if [[ -e $hold ]]; then
        list=$(timer_list "${held[@]}")
        echo "terrace-production: the backup timers are still held (${list% }); run terrace-production arm-timers first, after the first backup by hand (restore promotion step 21); nothing armed" >&2
        exit 1
      fi
      rm -f "$service_hold"
    else
      rm -f "$hold"
    fi
    armed=()
    still=()
    for unit in "${timers[@]}"; do
      if is_held "$unit"; then
        still+=("$unit.timer")
      elif [[ $(systemctl show "$unit.timer" -p ActiveState --value) != active ]]; then
        systemctl start "$unit.timer"
        armed+=("$unit.timer")
      fi
    done
    off=()
    for unit in "${timers[@]}"; do
      is_held "$unit" && continue
      [[ $(systemctl show "$unit.timer" -p ActiveState --value) == active ]] || off+=("$unit.timer")
    done
    if (( ${#off[@]} )); then
      echo "terrace-production: not armed: ${off[*]}; check them with systemctl status" >&2
      exit 1
    fi
    if (( ${#armed[@]} )); then echo "Armed: ${armed[*]}."; else echo 'Already armed: nothing to arm.'; fi
    if (( ${#still[@]} )); then
      echo "Still held until the replacement is declared in service: ${still[*]}. Arm with: terrace-production arm-timers --in-service"
    else
      if [[ ${2:-} == --in-service ]] && (( ! ${#service_held[@]} )); then
        echo 'This role holds no timer until service (it has no sweep).'
      fi
      echo 'Every writer timer is active now and at every boot.'
    fi
    ;;
  status)
    if [[ -e "$adopted" ]]; then echo "adopted $(cat "$adopted")"; elif [[ -e $adopting ]]; then echo "adoption as $(cat "$adopting") not finished"; else echo 'not adopted: no writer starts at boot'; fi
    if [[ -e $hold ]]; then echo "timers held after a restored adoption: $(timer_list "${held[@]}")(run terrace-production arm-timers once the restored services are verified)"; fi
    if [[ -e $service_hold ]]; then echo "timers held until the replacement is declared in service: $(timer_list "${service_held[@]}")(run terrace-production arm-timers --in-service after any erasure replay)"; fi
    systemctl is-active terrace-production.target || true
    deploy_status
    ;;
  activate | rollback) deploy "$@" ;;
  __apply) shift; apply "$@" ;;
  *) echo 'usage: terrace-production adopt --new [--first-deployment-without-repository HOSTNAME] | adopt --restored | arm-timers [--in-service] | status | activate STORE_PATH [--boot] [--cms-stays-down] [--accept-upstream-schema-change] | rollback [--boot] [--cms-stays-down] [--accept-upstream-schema-change]' >&2; exit 2 ;;
esac
