# terrace-cms-tool: run one of the CMS bundle's operator tools (bin/roster-invite, bin/wp-import) as the CMS account,
# in a transient systemd unit with the CMS units' own settings (platform/website.nix). Root only.
#
#   terrace-cms-tool roster-invite|wp-import ARGS...
#
# The module prepends TOOL_RUNNER (the runner script), TOOL_LOCK (a root-only lock file) and TOOL_PROPERTIES
# (NAME=VALUE unit properties, including EnvironmentFile= naming the protected CMS environment file). This script never reads that file: systemd does,
# before it drops privileges. Arguments reach the tool unchanged and its output and exit status come back.
# Exit codes of its own: 64 usage (no or unknown tool), 73 another operator tool is starting or running,
# 75 a CMS migration is running, 77 not root.
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

exec 9>"$TOOL_LOCK"
if ! flock -n 9; then
  echo 'Another terrace-cms-tool run is starting or running; run the tools one at a time.' >&2
  exit 73
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
