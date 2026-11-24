# terrace-cms-tool: run one of the CMS bundle's operator tools (bin/roster-invite, bin/wp-import) as the CMS account,
# in a transient systemd unit with the CMS units' own settings (platform/website.nix). Root only.
#
#   terrace-cms-tool roster-invite|wp-import ARGS...
#
# The module prepends TOOL_RUNNER (the runner script) and TOOL_PROPERTIES (NAME=VALUE unit properties, including
# EnvironmentFile= naming the protected CMS environment file). This script never reads that file: systemd does,
# before it drops privileges. Arguments reach the tool unchanged and its output and exit status come back.
# Exit codes of its own: 64 usage (no or unknown tool), 75 a CMS migration is running, 77 not root.

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
exec systemd-run --quiet --wait --pipe --collect --service-type=exec \
  --unit="terrace-cms-tool-$tool-$(date +%s%N)" "${properties[@]}" -- "$TOOL_RUNNER" "$tool" "$@"
