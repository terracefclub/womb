# terrace-cms-tool: run one of the CMS bundle's operator tools (bin/roster-invite, bin/wp-import) as the CMS account,
# in a transient systemd unit with the CMS units' own settings (platform/website.nix). Root only.
#
#   terrace-cms-tool roster-invite|wp-import ARGS...
#
# The module prepends TOOL_RUNNER (the runner script), TOOL_LOCK (a root-only lock file) and TOOL_PROPERTIES
# (NAME=VALUE unit properties, including EnvironmentFile= naming the protected CMS environment file). This script never reads that file: systemd does,
# before it drops privileges. Arguments reach the tool unchanged and its output and exit status come back.
# Exit codes of its own: 64 usage (no or unknown tool), 72 the lock file is not a root-owned regular file,
# 73 another operator tool is starting or running, 75 a CMS migration is running, 77 not root.
#
# One tool at a time: the wrapper holds TOOL_LOCK from its checks until the tool ends (systemd-run inherits the
# descriptor), so two wrappers cannot both pass the checks and start. A tool unit whose wrapper is gone (a dropped
# session) no longer holds the lock, so the wrapper also refuses while any terrace-cms-tool-* unit is active. What
# remains is a migration started at the same moment: its own check and this one can both pass within milliseconds.

usage() {
  echo 'Usage: terrace-cms-tool roster-invite|wp-import ARGS...' >&2
  exit 64
}

if [[ "$(id -u)" != 0 ]]; then
  echo 'terrace-cms-tool must run as root.' >&2
  exit 77
fi
[[ $# -ge 1 ]] || usage
case "$1" in
  roster-invite | wp-import) ;;
  *) usage ;;
esac

# The lock file is root's alone (0600): a user who could open it could hold a lock on it and refuse every run. It is
# never removed or replaced, so every wrapper locks the same inode: two wrappers cannot each lock a file of their own.
# A link, a directory or any other non-regular file is refused before it is opened (a link is never followed, a FIFO
# never blocks the open). The regular file is opened without truncation, locked, and only then checked through the
# open descriptor: it must be the file at the path (not a link swapped in), owned by root, and an older wrapper's wider
# mode is narrowed to 0600 under the lock. Anything else is refused with exit 72 and left for the operator to inspect.
# The owner is compared with $EUID, which is 0 here (checked above); the unit test runs as an ordinary user.
lock_refused() {
  echo "The operator tool lock $TOOL_LOCK is not a root-owned regular file ($1); nothing was run. Inspect it, remove it by hand and run the tool again." >&2
  exit 72
}
umask 077
if [[ -L "$TOOL_LOCK" ]]; then
  lock_refused 'a symbolic link'
elif [[ -e "$TOOL_LOCK" && ! -f "$TOOL_LOCK" ]]; then
  lock_refused 'not a regular file'
fi
exec 9>>"$TOOL_LOCK"
if ! flock -n 9; then
  echo 'Another terrace-cms-tool run is starting or running; run the tools one at a time.' >&2
  exit 73
fi
lock_info=$(stat -L -c '%u %f %d %i' /dev/fd/9) || lock_refused 'its status cannot be read'
path_info=$(stat -c '%d %i' -- "$TOOL_LOCK") || lock_refused 'it is gone from its path'
read -r lock_uid lock_mode lock_device lock_inode <<<"$lock_info"
read -r path_device path_inode <<<"$path_info"
if [[ "$lock_device:$lock_inode" != "$path_device:$path_inode" ]]; then
  lock_refused 'it changed while it was being opened'
elif (((16#$lock_mode & 0170000) != 0100000)); then
  lock_refused 'not a regular file'
elif [[ "$lock_uid" != "$EUID" ]]; then
  lock_refused "owned by uid $lock_uid"
elif (((16#$lock_mode & 07777) != 0600)); then
  chmod 0600 /dev/fd/9
fi
active=$(systemctl list-units --plain --no-legend --state=active,activating,deactivating 'terrace-cms-tool-*' | cut -d' ' -f1 | tr '\n' ' ')
if [[ -n "${active// /}" ]]; then
  echo "An operator tool unit is still active: ${active% }. Wait for it, or stop it: systemctl stop 'terrace-cms-tool-*'" >&2
  exit 73
fi
state=$(systemctl show terrace-cms-migrate.service -p ActiveState --value)
case "$state" in
  active | activating | reloading | deactivating)
    echo 'A CMS migration is in progress; run the tool after it has finished.' >&2
    exit 75
    ;;
esac

tool=$1
shift
properties=()
for property in "${TOOL_PROPERTIES[@]}"; do
  properties+=(-p "$property")
done
# A fixed description: systemd's default (the command line, with the operator's email and file paths) would be
# written to the system journal in every start and stop message.
exec systemd-run --quiet --wait --pipe --collect --service-type=exec \
  --unit="terrace-cms-tool-$tool-$(date +%s%N)" --description="Terrace CMS operator tool ($tool)" \
  "${properties[@]}" -- "$TOOL_RUNNER" "$tool" "$@"
