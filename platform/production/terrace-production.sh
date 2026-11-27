# terrace-production: adopt a production host (start its writers once, then at every boot), arm the timers that
# a restored adoption holds, activate a signed system closure or roll back to the previous generation, and report
# the state. Root only. platform/production/base.nix prepends the host's settings (paths, unit lists, host name,
# role, deploy keys, stability window) and builds it with writeShellApplication (errexit, nounset, pipefail).
# Unit tests: tests/platform/test_terrace_production.py (stub systemctl and tools); deploy path in
# docs/host-updates.md ("Deploy path").
#
#   terrace-production adopt --new [--first-deployment-without-repository HOSTNAME] | adopt --restored
#   terrace-production arm-timers [--in-service] | status
#   terrace-production activate STORE_PATH [--boot] [--cms-stays-down] [--accept-upstream-schema-change] [--dry-run]
#   terrace-production rollback [--generation N] [--boot] [--cms-stays-down] [--accept-upstream-schema-change] [--dry-run]
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
# Every check runs before anything changes, under one lock for the whole command; every change (profile, switch,
# records) runs inside the transient unit terrace-switch, which repeats the state checks first, so a dropped SSH
# session cannot interrupt it and a decision made against an older state is never applied.
self=$(readlink -f "${BASH_SOURCE[0]}")
records=$state_dir/deployments.log
lock=$state_dir/deploy.lock
switch_log=$state_dir/last-switch.log
deny() { echo "terrace-production: nothing changed: $*" >&2; exit 1; }
deploy_usage() { echo 'usage: terrace-production activate STORE_PATH [--boot] [--cms-stays-down] [--accept-upstream-schema-change] [--dry-run] | rollback [--generation N] [--boot] [--cms-stays-down] [--accept-upstream-schema-change] [--dry-run]' >&2; exit 2; }
manifest_file() { printf '%s/etc/terrace/system.json' "$1"; }
# A manifest field as text ("" for null or missing).
field() { jq -r "$2 // \"\" | tostring" "$(manifest_file "$1")"; }
# The generation number the system profile points at ("none" without a profile).
gen_of() {
  local link
  link=$(readlink "$profile" 2>/dev/null) || { echo none; return 0; }
  link=${link##*/}; link=${link%-link}; echo "${link##*-}"
}
# The numbers of every generation, one per line.
generations() {
  local link n
  for link in "$profile"-*-link; do
    [[ -L $link ]] || continue
    n=${link##*/}; n=${n%-link}; n=${n##*-}
    if [[ $n =~ ^[0-9]+$ ]]; then echo "$n"; fi
  done
}
# What the last record about generation $1 holding system $2 says: the result of a finished record that left it there,
# or "unfinished (...)" for a started record for that system that no finished record follows (the unit was killed or
# the host lost power mid-switch); "" when no record concerns it (the installation's generation, or one made by hand).
# Exit 1 when the log cannot be read.
gen_result() {
  [[ -s $records ]] || return 0
  # shellcheck disable=SC2016 # jq variables, not shell
  jq -rs --arg n "$1" --arg to "$2" '. as $r | [range(0; length) as $i | $r[$i] | select(.to == $to and
      ((.phase == "finished" and .generationAfter == $n) or (.phase == "started" and (($r[$i + 1].phase // "") != "finished"))))
    | if .phase == "started" then "unfinished (started \(.time // "at an unknown time"), never finished)" else .result end] | last // ""' "$records" 2>/dev/null
}
# What `rollback` goes to from generation $1: the newest older generation whose last record is neither a failure nor an
# unfinished attempt. Sets rollback_gen ("" if none) and skipped (those passed over, "N:result"); exit 1 when the log
# cannot be read. A generation without any record stays eligible: nothing says it failed. So does one whose last record
# is "switched; down until the CMS migration": that switch did what it should (its gate refused an unmigrated database).
rollback_target() {
  local n result
  rollback_gen="" skipped=()
  [[ $1 =~ ^[0-9]+$ ]] || return 0
  for n in $(generations | LC_ALL=C sort -rn); do
    (( n < $1 )) || continue
    result=$(gen_result "$n" "$(readlink -f "$profile-$n-link")") || return 1
    if [[ $result == failed* || $result == unfinished* ]]; then skipped+=("$n:$result"); continue; fi
    rollback_gen=$n
    return 0
  done
}
# The newest generation whose system is $1 ("" if none).
gen_for() {
  local n best=""
  for n in $(generations); do
    if [[ $(readlink -f "$profile-$n-link") == "$1" ]] && { [[ -z $best ]] || (( n > best )); }; then best=$n; fi
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
# A NixOS system built by this profile for THIS host: canonical, valid, switchable, manifest naming this host, and
# (unless $2 is running-ok) not the running system.
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
  [[ ${2:-} == running-ok || $(readlink -f "$current_system") != "$target" ]] || deny "$target is already the running system"
}
# Nothing that a switch would disturb is running: another switch, a backup or its check, (app) a migration, the
# sweep or an operator tool. $1 is a unit to leave out (the switch unit itself, when re-checked inside it).
check_idle() {
  local unit state tools
  for unit in "${busy[@]}"; do
    [[ $unit != "${1:-}" ]] || continue
    state=$(systemctl show "$unit.service" -p ActiveState --value)
    case $state in active | activating | deactivating | reloading) deny "$unit.service is $state; wait until it has finished" ;; esac
  done
  if [[ -n $cms_marker ]]; then
    tools=$(systemctl list-units --plain --no-legend --state=active,activating,deactivating,reloading 'terrace-cms-tool-*') || deny 'cannot list the terrace-cms-tool units, so nothing shows that no operator tool runs'
    [[ -z $tools ]] || deny "an operator tool runs ($(cut -d' ' -f1 <<<"$tools" | tr '\n' ' ')); wait until it has finished"
  fi
}
# Paths already in a system generation on this host arrived by a root-trusted route (installation or an earlier
# activation); every other path of the closure must carry a valid signature by a trusted DEPLOY key (only those
# keys are given to the verification, not the host's whole trust list), contents rehashed.
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
  [[ -n $cms_marker && ( -z $from || -z $to ) ]] && deny "a manifest records no PostgreSQL version (running '$from', target '$to'); nothing shows the major version is unchanged"
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
  local target=$1 unit state marker="" exists ledger extra target_rev
  [[ -n $cms_marker ]] || return 0
  [[ $(jq -c .website "$(manifest_file "$current_system")") != "$(jq -c .website "$(manifest_file "$target")")" ]] || return 0
  for unit in "${quiesce[@]}"; do
    state=$(systemctl show "$unit.service" -p ActiveState --value)
    case $state in
      active | activating | reloading)
        # A dry run goes on with the other checks (step 6 stops the site only later) and reports it; the real run refuses.
        if (( dry_run )); then
          not_quiesced+=("$unit.service:$state")
        else
          deny "the release changes and $unit.service is $state; quiesce first: systemctl stop ${quiesce[*]/%/.service} (after a gated activation they can still be retrying; docs/host-updates.md step 6)"
        fi ;;
    esac
  done
  target_rev=$(field "$target" .website.revision)
  if [[ -e $cms_marker ]]; then marker=$(cat "$cms_marker"); fi
  if [[ $(systemctl show postgresql.service -p ActiveState --value) != active ]]; then
    # No marker means no migration ever completed here, so every release's CMS gate stays closed whatever the
    # database holds; with a marker, the ledger must be read.
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
record() { jq -cn "$@" >>"$records"; chmod 0600 "$records"; }
words() { printf '%s\n' "$@" | jq -R . | jq -cs 'map(select(. != ""))'; }
in_list() { local item=$1; shift; [[ " $* " == *" $item "* ]]; }
# Whether unit $1 was already failed before the switch (before_failed, set in apply) and has the same invocation now.
untouched() {
  local entry
  for entry in "${before_failed[@]}"; do
    if [[ ${entry%:*} == "$1" ]]; then [[ ${entry##*:} == "$(systemctl show "$1" -p InvocationID --value)" ]]; return; fi
  done
  return 1
}
# Until none of the CMS and website units is starting, stopping or reloading AND no start job is queued or running for
# them, for terrace-production.target or for terrace-production-boot (which starts that target with --no-block, so its
# jobs are not the switch's own and can still be queued when the switch returns), for at most settle_seconds. After a
# gated switch the units fail their gate at once and restart (Restart=on-failure, 10 s apart) until their start limit:
# the record must say how they ended, not where they were. Sets settle_timed_out and still_moving.
settle_seconds=300
# How long the client keeps waiting for systemd-run --wait after the switch unit has ended (deploy()).
switch_gone_seconds=10
settle() {
  local unit waited=0 jobs
  while :; do
    still_moving=()
    for unit in "${quiesced[@]}"; do
      case $(systemctl show "$unit" -p ActiveState --value) in activating | deactivating | reloading) still_moving+=("$unit") ;; esac
    done
    # A listing that fails counts as a queued job: nothing shows there is none.
    if ! jobs=$(systemctl list-jobs --no-legend "${quiesced[@]}" terrace-production.target terrace-production-boot.service 2>/dev/null); then
      still_moving+=("jobs:unlistable")
    elif [[ -n $jobs ]]; then
      still_moving+=("jobs:$(awk '{printf "%s%s", sep, $2; sep=","}' <<<"$jobs")")
    fi
    if (( ! ${#still_moving[@]} )); then settle_timed_out=0; return 0; fi
    if (( waited >= settle_seconds )); then settle_timed_out=1; return 0; fi
    sleep 2
    waited=$((waited + 2))
  done
}
# Inside the terrace-switch unit: check that the state the decision was made against still holds, record the start,
# register the generation, switch, and record the result.
# A switch to a release whose migration has not run starts the CMS and website again (terrace-production-boot is
# restarted and starts terrace-production.target, which wants them): both fail their migration gate (requireMigration,
# platform/website.nix) and retry until their start limit. Those start jobs are not the switch's own, and when it lists
# the units they are usually in their restart delay with no failed main process, so switch-to-configuration can exit 0
# (seen in the deploy VM proof since the website is gated) or 4 (when something held it until they had failed). So after
# exit 0 or 4 the command waits until the CMS and website stop retrying and no start job for them is left (settle), and
# classifies the switch when it exited 4, when either unit then failed or is still starting, or, in switch mode, when
# the gate is closed for the target (the marker does not hold its revision): then the units must have been refused by
# the gate (failed), never merely left inactive. Recorded as switched with those units down only when: the gate is
# closed for the target, every one of them failed (or, with no marker at all, was skipped by its start condition),
# none is still starting after the wait or inactive though the marker exists, and no other unit of any type failed,
# is in auto-restart, or is named in switch-to-configuration's own list of failed units. Anything else is a failed
# switch, even after exit 0, and its result names what was wrong: units failed, units not started, or the wait timing
# out with what was still starting. A unit outside that set that is merely "activating" when the
# switch has returned is not counted: switch-to-configuration waits for the jobs it queued, so such a unit was started
# by something else (a timer) and says nothing about this switch.
apply() {
  local action=$1 mode=$2 target=$3 generation=$4 expected_from=$5 expected_gen=$6
  local from before after rc=0 result marker="" unit name active sub line expected=1
  local failed=() unexpected=() prefailed=() reported=() quiesced=() names=() text="" switch_exit=null not_started=() down=() gate_closed=0 state
  local reset_units=() before_failed=() preexisting=() revert=null revert_from revert_to revert_rc revert_result now
  from=$(readlink -f "$current_system"); before=$(gen_of)
  [[ $from == "$expected_from" && $before == "$expected_gen" ]] || deny "the running system or the profile changed after the checks (checked: $expected_from, generation $expected_gen; now: $from, generation $before); run the command again"
  check_idle terrace-switch
  if [[ $action == cancel ]]; then check_system "$target" running-ok; else check_system "$target"; fi
  for unit in "${quiesce[@]}"; do quiesced+=("$unit.service"); done
  # Recorded, then cleared: a start limit hit by an earlier gate refusal must not keep a unit from starting where its gate is
  # open. The migration unit too (app): a failed or timed-out migration leaves it failed, and the activation of a corrected
  # build (fixing forward, docs/host-updates.md step 10) must not be judged by that earlier failure.
  reset_units=("${quiesced[@]}")
  if [[ -n $cms_marker ]]; then reset_units+=(terrace-cms-migrate.service); fi
  for unit in "${reset_units[@]}"; do if systemctl is-failed --quiet "$unit"; then prefailed+=("$unit"); fi; done
  install -d -m 0700 "$state_dir"
  # shellcheck disable=SC2016 # jq variables, not shell
  record --arg time "$(date -u +%FT%TZ)" --arg action "$action" --arg mode "$mode" --arg from "$from" --arg to "$target" --arg before "$before" \
    --argjson prefailed "$(words "${prefailed[@]}")" \
    '{phase: "started", time: $time, action: $action, mode: $mode, from: $from, to: $to, generationBefore: $before, failedBefore: $prefailed}'
  if (( ${#reset_units[@]} )); then systemctl reset-failed "${reset_units[@]}" 2>/dev/null || true; fi
  # Every other unit already failed (or restarting after a failure) before the switch, with its invocation. One still
  # failed afterwards with the SAME invocation was neither started nor stopped by the switch: it is named as a warning
  # (preexistingFailedUnits), not as a unit the switch broke. One that ran again during the switch (a new invocation)
  # and failed counts as broken, so does a unit that keeps restarting (each restart is a new invocation).
  while read -r name _ active sub _; do
    [[ $active == failed || $sub == auto-restart ]] || continue
    before_failed+=("$name:$(systemctl show "$name" -p InvocationID --value)")
  done < <(systemctl list-units --all --plain --no-legend --full)
  case $action in
    activate) nix-env -p "$profile" --set "$target" || rc=$? ;;
    *) nix-env -p "$profile" --switch-generation "$generation" || rc=$? ;;
  esac
  if [[ $rc == 0 ]]; then
    install -m 0600 /dev/null "$switch_log"
    "$target/bin/switch-to-configuration" "$mode" 2>&1 | tee "$switch_log" || rc=${PIPESTATUS[0]}
    switch_exit=$rc
  fi
  after=$(gen_of)
  local classify=0
  settle_timed_out=0 still_moving=()
  if [[ -n $cms_marker && -e $cms_marker ]]; then marker=$(cat "$cms_marker"); fi
  if [[ -n $cms_marker && $mode == switch && $marker != "$(field "$target" .website.revision)" ]]; then gate_closed=1; fi
  if [[ $rc == 0 || $rc == 4 ]]; then
    settle
    if [[ $rc == 4 ]] || (( gate_closed || settle_timed_out )); then classify=1; fi
    for unit in "${quiesced[@]}"; do
      case $(systemctl show "$unit" -p ActiveState --value) in failed | activating | deactivating | reloading) classify=1 ;; esac
    done
  fi
  if (( classify )); then
    # switch-to-configuration's own list, by its exact text (nixpkgs switch-to-configuration-ng: `warning: the following
    # units failed: a, b`; pinned by a unit test and asserted against the real switch in the deploy VM proof). If upstream
    # changes the text, this list stays empty and only the listing below remains, which still sees every failed unit
    # and every unit in auto-restart: the check gets weaker, it does not let a failure it can see through.
    line=$(grep -m 1 '^warning: the following units failed: ' "$switch_log" || true)
    if [[ -n $line ]]; then IFS=',' read -r -a reported <<<"${line#warning: the following units failed: }"; fi
    while read -r name _ active sub _; do
      [[ $active == failed || $sub == auto-restart || ( $active == activating && -n $name ) ]] || continue
      if in_list "$name" "${quiesced[@]}"; then
        # Still starting after the wait: not the gate's prompt refusal, so not explained by it.
        if [[ $active == failed ]]; then failed+=("$name"); else unexpected+=("$name"); fi
      elif [[ $active == failed || $sub == auto-restart ]]; then
        if untouched "$name"; then in_list "$name" "${preexisting[@]}" || preexisting+=("$name"); else unexpected+=("$name"); fi
      fi
    done < <(systemctl list-units --all --plain --no-legend --full)
    for name in "${reported[@]}"; do
      name=${name// /}
      [[ -n $name ]] || continue
      in_list "$name" "${quiesced[@]}" "${unexpected[@]}" "${preexisting[@]}" && continue
      if untouched "$name"; then preexisting+=("$name"); else unexpected+=("$name"); fi
    done
    # With the gate closed every gated unit must have been refused by it: running is not possible without a bypass, and
    # inactive means nothing started it (or its start never ran), which must not pass as a clean switch.
    if (( gate_closed )); then
      for unit in "${quiesced[@]}"; do
        state=$(systemctl show "$unit" -p ActiveState --value)
        if in_list "$unit" "${failed[@]}"; then down+=("$unit")
        elif [[ $state == active || $state == reloading ]]; then in_list "$unit" "${unexpected[@]}" || unexpected+=("$unit")
        elif [[ $state == inactive && -z $marker ]]; then down+=("$unit")
        elif [[ $state == inactive ]]; then not_started+=("$unit")
        fi
      done
    fi
    if (( gate_closed )); then
      (( ${#down[@]} == ${#quiesced[@]} && ${#unexpected[@]} == 0 && ${#not_started[@]} == 0 && ! settle_timed_out )) || expected=0
    else
      # The gate open: judged because of exit 4 (or a gated unit's state), it is a good switch only when what failed had
      # all failed before it, untouched, and nothing else failed or is still starting.
      (( ${#failed[@]} == 0 && ${#unexpected[@]} == 0 && ${#preexisting[@]} > 0 && ! settle_timed_out )) || expected=0
    fi
  fi
  if (( classify && expected )); then
    if (( gate_closed )); then result="switched; down until the CMS migration: ${down[*]}"; else result=success; fi
    rc=0
  elif [[ $rc == 0 || $rc == 4 ]] && (( classify )); then
    # Exit 4 names its units in the record's lists; exit 0 says here which units failed. Units not started and a wait
    # that timed out are always named in the result, which the client prints.
    names=("${failed[@]}" "${unexpected[@]}") text=""
    if [[ $rc == 0 ]] && (( ${#names[@]} )); then text="units failed: ${names[*]}"; fi
    if (( ${#not_started[@]} )); then text+="${text:+; }not started though the gate is closed for this release, THE SITE IS DOWN: ${not_started[*]}"; fi
    if (( settle_timed_out )); then text+="${text:+; }the wait for the CMS and website timed out after ${settle_seconds} s, still starting: ${still_moving[*]}"; fi
    result="failed (exit $rc${text:+, then $text})"
    if [[ $rc == 0 ]]; then rc=1; fi
  elif [[ $rc == 0 ]]; then result=success
  else result="failed (exit $rc)"; fi
  # A switch that failed after its generation was registered, without switching (the boot loader step comes first): the
  # boot default would be a system that never ran. Make the running system the boot default again, once, in this unit;
  # the failed generation stays in the profile, recorded as failed, so rollback keeps passing over it. Never after a
  # success or "switched; down until the CMS migration", never in boot mode (there the pending default is intended).
  now=$(readlink -f "$current_system")
  if [[ $mode == switch && $result != success && $result != switched* && $(readlink -f "$profile") != "$now" ]]; then
    revert_from=$(gen_of) revert_to=$(gen_for "$now") revert_rc=0
    if [[ -z $revert_to ]]; then
      revert_result="not possible: the running system is in no generation of $profile"
    else
      nix-env -p "$profile" --switch-generation "$revert_to" || revert_rc=$?
      if [[ $revert_rc == 0 ]]; then "$now/bin/switch-to-configuration" boot >>"$switch_log" 2>&1 || revert_rc=$?; fi
      if [[ $revert_rc == 0 ]]; then revert_result=success; else revert_result="failed (exit $revert_rc)"; fi
    fi
    # shellcheck disable=SC2016 # jq variables, not shell
    revert=$(jq -cn --arg from "$revert_from" --arg to "$revert_to" --arg result "$revert_result" '{from: $from, to: $to, result: $result}')
  fi
  # shellcheck disable=SC2016 # jq variables, not shell
  record --arg time "$(date -u +%FT%TZ)" --arg action "$action" --arg mode "$mode" --arg from "$from" --arg to "$target" \
    --arg before "$before" --arg after "$after" --arg womb "$(field "$target" .wombRevision)" --arg website "$(field "$target" .website.revision)" --arg result "$result" --argjson switch_exit "$switch_exit" \
    --argjson failed "$(words "${failed[@]}")" --argjson unexpected "$(words "${unexpected[@]}")" --argjson not_started "$(words "${not_started[@]}")" \
    --argjson timed_out "$( (( settle_timed_out )) && echo true || echo false)" --argjson still "$(words "${still_moving[@]}")" \
    --argjson preexisting "$(words "${preexisting[@]}")" --argjson revert "$revert" --arg boot_after "$(gen_of)" \
    '{phase: "finished", time: $time, action: $action, mode: $mode, from: $from, to: $to, generationBefore: $before, generationAfter: $after, wombRevision: $womb, websiteRevision: $website, result: $result, switchExit: $switch_exit, failedUnits: $failed, unexpectedUnits: $unexpected, notStartedUnits: $not_started, waitTimedOut: $timed_out, stillStarting: $still, preexistingFailedUnits: $preexisting, revert: $revert, bootDefaultAfter: $boot_after}'
  exit "$rc"
}
deploy() {
  local action=$1 target="" generation="" chosen="" rc=0 running lines last result item old_umask current waiter run_pid line seen=0 gone=0 abandoned=0 revert
  shift
  mode=switch cms_stays_down=0 accept_upstream=0 dry_run=0 not_quiesced=()
  while (( $# )); do
    case $1 in
      --generation)
        [[ $action == rollback && -z $chosen && ${2:-} =~ ^[1-9][0-9]*$ ]] || deploy_usage
        chosen=$2
        shift ;;
      --boot) mode=boot ;;
      --cms-stays-down) cms_stays_down=1 ;;
      --accept-upstream-schema-change) accept_upstream=1 ;;
      --dry-run) dry_run=1 ;;
      -*) deploy_usage ;;
      *) [[ $action == activate && -z $target ]] || deploy_usage; target=$1 ;;
    esac
    shift
  done
  [[ $action != activate || -n $target ]] || deploy_usage
  # One command at a time, from its first check to its record: a root-only lock file. Opened for append, it is created
  # if absent and never replaced, so two first-ever runs lock the same file; chmod fixes the mode on that same inode.
  install -d -m 0700 "$state_dir"
  old_umask=$(umask)
  umask 077
  exec 9>>"$lock"
  umask "$old_umask"
  chmod 0600 "$lock"
  flock -n 9 || deny 'another terrace-production activate or rollback is running on this host; wait for it to finish'
  running=$(readlink -f "$current_system")
  if [[ $action == rollback && -z $chosen && $(gen_of) != none && $(readlink -f "$profile") != "$running" ]]; then
    # The boot default is not the running system (activate --boot before its reboot, or a switch that failed after
    # registering its generation): rollback makes the running system's generation the boot default again.
    action=cancel mode=boot target=$running
    generation=$(gen_for "$running")
    [[ -n $generation ]] || deny "the running system $running is in no generation of $profile; set the boot default by hand (docs/host-updates.md section 1.5)"
    check_system "$target" running-ok
    check_idle
    echo "Checks passed; the boot default goes back to generation $generation, the running system $target (switch-to-configuration boot in unit terrace-switch; nothing running changes)."
  else
    if [[ $action == rollback && -n $chosen ]]; then
      # An explicit choice: a generation older than the current one, with every check of a rollback. "Current" is the
      # running system's (newest) generation; when the running system is in no generation, the boot default's. So with
      # a pending boot default (activate --boot, or a failed switch) the pending generation counts as newer.
      [[ -L $profile-$chosen-link ]] || deny "generation $chosen does not exist in $profile"
      current=$(gen_for "$running")
      [[ -n $current ]] || current=$(gen_of)
      if ! [[ $current =~ ^[0-9]+$ ]] || (( chosen >= current )); then deny "generation $chosen is not older than the current generation ${current} (the running system's): rollback only goes back; to go forward, activate that system: terrace-production activate $(readlink -f "$profile-$chosen-link")"; fi
      generation=$chosen
      target=$(readlink -f "$profile-$generation-link")
      result=$(gen_result "$generation" "$target") || result="(the deployments log $records cannot be read)"
      if [[ $result == failed* || $result == unfinished* || $result == "(the"* ]]; then echo "WARNING: generation $generation was chosen explicitly; its last record: $result" >&2; fi
    elif [[ $action == rollback ]]; then
      rollback_target "$(gen_of)" || deny "the deployments log $records cannot be read, so nothing shows which generations failed; choose one with rollback --generation N"
      for item in "${skipped[@]}"; do echo "Skipping generation ${item%%:*}: its last record is '${item#*:}' (rollback --generation ${item%%:*} chooses it anyway)."; done
      [[ -n $rollback_gen ]] || deny "no generation older than the current one ($(gen_of)) in $profile whose last record is not a failure; choose one with rollback --generation N"
      generation=$rollback_gen
      target=$(readlink -f "$profile-$generation-link")
    fi
    check_system "$target"
    check_idle
    if [[ $action == activate ]]; then check_signatures "$target"; fi
    check_boot "$target"
    check_upstream "$target"
    check_cms "$target"
    if (( ${#not_quiesced[@]} )); then
      echo "Every other check passed; ${action} $target ($(describe "$target")) with switch-to-configuration $mode in unit terrace-switch."
    else
      echo "Checks passed; ${action} $target ($(describe "$target")) with switch-to-configuration $mode in unit terrace-switch."
    fi
  fi
  if (( dry_run )); then
    echo 'Dry run: nothing changed.'
    if (( ${#not_quiesced[@]} )); then
      # Exit 3, never 0: not a clean pass.
      echo "terrace-production: dry run NOT READY: the release changes and ${not_quiesced[*]} still run(s); the real command is refused until you stop them: systemctl stop ${quiesce[*]/%/.service} (docs/host-updates.md step 6)" >&2
      exit 3
    fi
    return 0
  fi
  lines=0
  if [[ -e $records ]]; then lines=$(wc -l <"$records"); fi
  # systemd-run --wait, watched: in the update drill it once did not return after its unit had been stopped (a client that
  # never returns also keeps the lock). If the unit has ended and systemd-run still waits switch_gone_seconds later, stop
  # waiting for it; the records below decide.
  exec {waiter}< <(systemd-run --unit=terrace-switch --collect --service-type=exec --wait --quiet -p "Description=terrace-production $action" -- "$self" __apply "$action" "$mode" "$target" "$generation" "$running" "$(gen_of)" >&2 &
    echo "pid $!"; wait "$!" && echo 'rc 0' || echo "rc $?")
  read -r _ run_pid <&"$waiter"
  while :; do
    if read -r -t 1 -u "$waiter" line; then rc=${line#rc }; break
    elif (( $? <= 128 )); then rc=1; break; fi
    case $(systemctl show terrace-switch.service -p ActiveState --value 2>/dev/null) in
      active | activating | deactivating | reloading) seen=1 gone=0 ;;
      *) if (( seen )); then gone=$((gone + 1)); fi ;;
    esac
    if (( gone >= switch_gone_seconds )); then abandoned=1; kill "$run_pid" 2>/dev/null || true; break; fi
  done
  exec {waiter}<&-
  # Only the records this run wrote.
  last=""
  if [[ -e $records ]]; then last=$(tail -n +"$((lines + 1))" "$records"); fi
  if [[ -n $last ]]; then printf '%s\n' "$last"; last=$(tail -n 1 <<<"$last"); fi
  if (( abandoned )); then
    echo "terrace-production: the terrace-switch unit has ended, but systemd-run --wait had not returned ${switch_gone_seconds} s later; stopped waiting for it, the records decide" >&2
    rc=1
    result=$(jq -r 'select(.phase == "finished") | .result' <<<"${last:-null}" 2>/dev/null || true)
    if [[ $result == success || $result == switched* ]]; then rc=0; fi
  fi
  # The unit's exit status alone does not show that the switch ended: a unit stopped mid-switch (`systemctl stop`)
  # ends "successfully" for systemd (SIGTERM is a clean exit), so systemd-run --wait returns 0. Only this run's
  # finished record does (seen in the update drill, platform/production/update-drill-test.nix).
  if [[ $rc == 0 && $(jq -r '.phase // ""' <<<"${last:-null}" 2>/dev/null) != finished ]]; then
    echo "terrace-production: the terrace-switch unit ended without writing its finished record (stopped or killed mid-switch); the system may be half switched: read terrace-production status. Nothing was set back: to make the running system the boot default again, run terrace-production rollback" >&2
    rc=1
  fi
  revert=$(jq -c '.revert // empty' <<<"${last:-null}" 2>/dev/null || true)
  if [[ -n $revert && $(jq -r .result <<<"$revert") == success ]]; then
    echo "terrace-production: the switch failed before the new system ran, so the boot default was set back to generation $(jq -r .to <<<"$revert"), the running system; generation $(jq -r .from <<<"$revert") stays in the list, recorded as failed (rollback passes over it)." >&2
  elif [[ -n $revert && $(gen_of) == "$(jq -r .to <<<"$revert")" ]]; then
    echo "terrace-production: WARNING: setting the boot default back to the running system did not work ($(jq -r .result <<<"$revert")): the profile names the running system again, but its boot loader step failed, so the boot menu may still default to generation $(jq -r .from <<<"$revert"). Do not reboot: fix the cause, then run terrace-production rollback --dry-run and read docs/host-updates.md section 1.5." >&2
  elif [[ -n $revert ]]; then
    echo "terrace-production: WARNING: setting the boot default back to the running system did not work ($(jq -r .result <<<"$revert")): the boot default is still generation $(gen_of), which a reboot would boot. Do not reboot: fix the cause, then run terrace-production rollback, which makes the running system the boot default again (docs/host-updates.md section 1.5)." >&2
  fi
  if [[ -n $last && $(jq -r '(.preexistingFailedUnits // []) | length' <<<"$last" 2>/dev/null) != 0 ]]; then
    echo "WARNING: already failed before this switch and left untouched by it, so not counted against it: $(jq -r '.preexistingFailedUnits | join(" ")' <<<"$last"). Look at them: systemctl --failed" >&2
  fi
  if [[ $rc != 0 ]]; then
    if [[ -n $last ]]; then echo "terrace-production: $action result: $(jq -r '.result // "none"' <<<"$last" 2>/dev/null)" >&2; fi
    echo "terrace-production: $action FAILED (exit $rc). Read: journalctl -u terrace-switch.service -o cat --no-pager; systemctl --failed; terrace-production status" >&2
    exit 1
  fi
  if [[ $mode == boot ]]; then echo "Generation $(gen_of) is the boot default."; else echo "Running generation $(gen_of): $(readlink -f "$current_system")."; fi
  if [[ $action == activate && $mode == boot ]]; then echo 'Reboot to run it, or cancel it with terrace-production rollback.'; fi
  if [[ -n $last && $(jq -r .result <<<"$last") == "switched;"* ]]; then
    echo "THE SITE IS DOWN until the CMS migration: the gate is closed for this release, so $(jq -r '.failedUnits | join(" ")' <<<"$last") stay failed until it (docs/host-updates.md steps 10 and 11)."
  fi
}
deploy_status() {
  local running generation last item
  running=$(readlink -f "$current_system")
  echo "running system: $running ($(describe "$running"))"
  generation=$(gen_of)
  echo "generation: $generation (the boot default)"
  if [[ $generation != none && $(readlink -f "$profile") != "$running" ]]; then
    echo "WARNING: the boot default, generation $generation ($(readlink -f "$profile")), is not the running system: a reboot boots generation $generation. Expected after activate --boot; otherwise a switch failed after registering it. terrace-production rollback makes the running system the boot default again (docs/host-updates.md section 1.5)."
  fi
  # The generation a plain `rollback` goes to.
  if ! rollback_target "$generation"; then
    echo "previous generation: unknown (the deployments log $records cannot be read)"
  elif [[ -n $rollback_gen ]]; then
    echo "previous generation: $rollback_gen $(readlink -f "$profile-$rollback_gen-link") ($(describe "$(readlink -f "$profile-$rollback_gen-link")"))"
  else
    echo 'previous generation: none'
  fi
  for item in "${skipped[@]}"; do echo "skipped by rollback, its last record failed: generation ${item%%:*} ('${item#*:}')"; done
  if (( ${#deploy_keys[@]} )); then echo "deploy path: on, ${#deploy_keys[@]} trusted deploy key(s)"; else echo 'deploy path: off (no deploy key trusted)'; fi
  if [[ -s $records ]]; then
    last=$(tail -n 1 "$records")
    echo "last activation: $last"
    if [[ $(jq -r '(.revert.result // "success")' <<<"$last" 2>/dev/null) != success ]]; then
      echo "WARNING: the last switch failed and setting the boot default back to the running system did not work ($(jq -r .revert.result <<<"$last")); the boot menu may still default to generation $(jq -r .revert.from <<<"$last"): do not reboot before it is fixed (docs/host-updates.md section 1.5)"
    fi
    if [[ $(jq -r '.phase // ""' <<<"$last") == started ]]; then
      echo "WARNING: the last activation started at $(jq -r .time <<<"$last") and never finished (its unit was stopped or the host rebooted mid-switch); check the running system and the boot default above"
    fi
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
    deploy_status
    ;;
  activate | rollback) deploy "$@" ;;
  __apply) shift; apply "$@" ;;
  *) echo 'usage: terrace-production adopt --new [--first-deployment-without-repository HOSTNAME] | adopt --restored | arm-timers [--in-service] | status | activate STORE_PATH [--boot] [--cms-stays-down] [--accept-upstream-schema-change] [--dry-run] | rollback [--generation N] [--boot] [--cms-stays-down] [--accept-upstream-schema-change] [--dry-run]' >&2; exit 2 ;;
esac
