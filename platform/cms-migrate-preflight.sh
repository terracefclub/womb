# terrace-cms-migrate-preflight: the last ExecStartPre step of terrace-cms-migrate.service (platform/website.nix),
# run as root after every guard, so a refusal anywhere leaves the CMS and website running.
#
# 1. Refuse, before anything is stopped, while a CMS operator tool unit is starting, running or stopping: the site
#    stays up and the reason goes to the migration unit's journal. A listing that cannot be read refuses too.
# 2. Otherwise stop the CMS, the website and the sweep, but only those actually active (or activating, reloading,
#    deactivating). A unit that is inactive with a start job queued behind the migration (all three are ordered
#    After= it) keeps that job, which runs once the migration has finished.
#
# The stop is synchronous and cannot deadlock on this starting unit: when one unit is stopped and another started,
# systemd orders the stop before the start whatever their ordering dependency (systemd.unit(5), After=).
# Exit status: 0 to proceed, 1 refused.

if ! units=$(systemctl list-units --plain --no-legend --state=active,activating,deactivating 'terrace-cms-tool-*'); then
  echo 'Cannot list CMS operator tool units; migration refused before anything was stopped.' >&2
  exit 1
fi
if [[ -n "$units" ]]; then
  names=$(printf '%s\n' "$units" | cut -d' ' -f1 | tr '\n' ' ')
  echo "A CMS operator tool is running (${names% }); migration refused before anything was stopped. Start the migration after it has finished." >&2
  exit 1
fi

running=()
for unit in terrace-cms.service terrace-web.service terrace-cms-sweep.service; do
  state=$(systemctl show "$unit" -p ActiveState --value)
  case "$state" in
    inactive | failed) ;;
    *) running+=("$unit") ;;
  esac
done
if [[ ${#running[@]} -gt 0 ]]; then
  systemctl stop "${running[@]}"
fi
