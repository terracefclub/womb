# Website member-lifecycle steps, shared verbatim by the VM all-services rehearsal
# (platform/full/scenario.py, which receives this file's text) and the cloud lease
# service proof (infra/nixos/rehearsal_services.py, which imports it). Each step
# runs as root on the application host as `bash -ceu "set -o pipefail" + PROLOGUE
# + script` with one argument, the CA bundle the guest protocol client trusts.
# A failing step is reported only by its fixed label; no output is kept.
# Each entry: (label, ceiling in seconds, script).

LIFECYCLE_PROLOGUE = r"""
export SSL_CERT_FILE="$1"
outbox=/var/lib/terrace-cms-outbox
protocol() {
  local expected=$1
  shift
  local out
  out=$(python3 /etc/gateway-protocol.py "$@" 2>/dev/null)
  [[ "$out" == "$expected"* ]]
}
"""

# Before backup: one invitation is redeemed (local-alumni); the other
# (local-member) is approved but left pending so its token outlives the restore.
# The application requests come from this host's own address through the gateway.
LIFECYCLE_ORIGINAL_STEPS = (
    ("apply", 150, r"""
protocol 'Application rate-limit checks passed: ' apply-burst
"""),
    ("approve", 150, r"""
protocol 'Approval checks passed: ' approve local-alumni local-member
test "$(ls "$outbox"/*-invitation.json | wc -l)" = 2
test "$(stat -c %U:%a "$outbox"/*-invitation.json | sort -u)" = terrace_cms:600
"""),
    ("redeem", 90, r"""
protocol 'Redemption checks passed: ' redeem local-alumni
"""),
    ("sign-in", 90, r"""
protocol 'Member sign-in checks passed: ' sign-in local-alumni
"""),
    ("link", 90, r"""
protocol 'Application link checks passed: ' link local-alumni
"""),
)

# After the originals-off restore, on the replacement.
LIFECYCLE_REPLACEMENT_STEPS = (
    ("ownership", 30, r"""
test "$(id -u terrace_cms)" = 44121
test "$(stat -c %U:%a "$outbox")" = terrace_cms:700
test "$(ls "$outbox"/*-invitation.json | wc -l)" = 2
test "$(stat -c %U:%a "$outbox"/*-invitation.json | sort -u)" = terrace_cms:600
runuser -u terrace_cms -- grep -l local-member@terrace.invalid "$outbox"/*-invitation.json >/dev/null
"""),
    # The restored pending token still works once, and only once.
    ("redeem", 90, r"""
protocol 'Redemption checks passed: ' redeem local-member
"""),
    ("sign-in", 90, r"""
protocol 'Member sign-in checks passed: ' sign-in local-member
"""),
    ("link", 90, r"""
protocol 'Application link checks passed: ' link local-member
"""),
    # The running CMS writes each offboarding notice into the restored outbox; an
    # administrator erasure leaves the invitation files to the sweep.
    ("erase", 180, r"""
protocol 'Erasure checks passed: ' erase local-alumni
protocol 'Erasure checks passed: ' erase local-member
test "$(ls "$outbox"/*-offboarding.json | wc -l)" = 2
test "$(stat -c %U:%a "$outbox"/*-offboarding.json | sort -u)" = terrace_cms:600
grep -l local-alumni@terrace.invalid "$outbox"/*-invitation.json >/dev/null
grep -l local-member@terrace.invalid "$outbox"/*-invitation.json >/dev/null
"""),
    # The real timer unit: a persistent timer whose last trigger predates a
    # scheduled elapse catches up as soon as it is started.
    ("sweep", 240, r"""
before=$(systemctl show terrace-cms-sweep.service -p InvocationID --value)
systemctl stop terrace-cms-sweep.timer
mkdir -p /var/lib/systemd/timers
touch -d '3 days ago' /var/lib/systemd/timers/stamp-terrace-cms-sweep.timer
systemctl start terrace-cms-sweep.timer
now=$before
for attempt in $(seq 1 100); do
  now=$(systemctl show terrace-cms-sweep.service -p InvocationID --value)
  if test "$now" != "$before" && test "$(systemctl show terrace-cms-sweep.service -p ActiveState --value)" = inactive; then
    break
  fi
  sleep 2
done
test "$now" != "$before"
test "$(systemctl show terrace-cms-sweep.service -p ActiveState --value)" = inactive
test "$(systemctl show terrace-cms-sweep.service -p Result --value)" = success
test "$(systemctl show terrace-cms-sweep.service -p User --value)" = terrace_cms
"""),
    ("redaction", 30, r"""
for file in "$outbox"/*-invitation.json; do
  jq -e '.to == "Former member" and .body == "Redacted after erasure."' "$file" >/dev/null
done
test "$(ls "$outbox"/*-erasure-confirmation.json | wc -l)" = 2
test "$(stat -c %U:%a "$outbox"/*-erasure-confirmation.json "$outbox"/*-invitation.json | sort -u)" = terrace_cms:600
if grep -rlE 'local-(alumni|member)@terrace[.]invalid' "$outbox" >/dev/null; then exit 1; fi
"""),
)
