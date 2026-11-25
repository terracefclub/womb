# terrace-production: adopt a production host (start its writers once, then at every boot), arm the timers that
# a restored adoption holds, and report the state. Root only. platform/production/base.nix prepends the host's
# settings (paths, unit lists, host name, stability window) and builds it with writeShellApplication (errexit,
# nounset, pipefail). Unit tests: tests/platform/test_terrace_production.py (stub systemctl and tools).
#
#   terrace-production adopt --new [--first-deployment-without-repository HOSTNAME] | adopt --restored
#   terrace-production arm-timers [--in-service] | status
#
# Settings: restored populated adopted adopting replacement state_dir hold service_hold hostname stability_seconds
# and the arrays targets writers helpers conditional timers held service_held.
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
    ;;
  *) echo 'usage: terrace-production adopt --new [--first-deployment-without-repository HOSTNAME] | adopt --restored | arm-timers [--in-service] | status' >&2; exit 2 ;;
esac
