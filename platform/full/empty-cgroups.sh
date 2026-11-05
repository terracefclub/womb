set -euo pipefail
[[ $# -eq 0 ]] || exit 1

# Preserve find's status, including a failed batched cat. Empty output alone
# must never authorize capture or shutdown after an inspection error.
pids="$(find /sys/fs/cgroup -name cgroup.procs \
  \( -path '/sys/fs/cgroup/system.slice/k3s.service/*' -o -path '*/kubepods*' \) \
  -exec cat {} + 2>/dev/null)" || {
  echo 'Cgroup inspection failed.' >&2
  exit 1
}
test -z "$pids"
