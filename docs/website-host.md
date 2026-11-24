# Optional native website host

> **Note, 2026-11-24.** Re-pinned to website `dev` `dd1aef7` (website PRs #23 and #24: the CMS bundle now ships the operator tools `bin/roster-invite` and `bin/wp-import`). No migration and no realm producer changed. The module adds `terrace-cms-tool`, the root-only way to run them as the CMS account, an operator directory outside every backup set, and the `lifecycleLockTimeoutMs` option (see [Operator tools](#operator-tools-terrace-cms-tool)).

> **Note, 2026-11-23.** Re-pinned to website `dev` `eab4189` (website PRs #19 to #22: bulk roster onboarding, review follow-ups, realm admin events). The ledger now has twelve migrations (adds `20261119_165021_roster_journal`). Nothing new to wire: the roster tool is not in the bundle and its journal is ordinary database state (see [Member lifecycle](#member-lifecycle-settings-outbox-and-sweep)); the realm producer's hash changed in `platform/identity-contract.json`.

> **Note, 2026-11-19.** Re-pinned to website `dev` `680ad56` (website PRs #14 to #18, including the packaged CMS `bin/sweep`). The ledger now has eleven migrations, recorded in `platform/website-release.json` and checked in order by the VM test. The module now supplies the member-lifecycle settings listed under [Member lifecycle](#member-lifecycle-settings-outbox-and-sweep).

This disabled-by-default module runs the accepted TFC website and CMS bundles
as separate native NixOS services. It adds no gateway, public listener, DNS,
account, cloud resource or deployment profile. The existing single-node K3s
foundation remains available for later services. Native services are the first
host choice because one database, two immutable bundles and explicit recovery
avoid another application operator/image pipeline on the temporary budget.

The separate optional [HTTPS origin gateway](website-gateway.md) adds trusted
TLS/native SSO boundary checks with the same accepted bundles. Both modules
remain disabled in actual host profiles; CDN activation remains separate.

## Accepted inputs

`platform/website-release.json` pins the exact secret-free Linux x86-64 glibc
archives from TFC source `dd1aef7a06ddfa2a31a7cd215afe0f6f10b23191` (website `dev`, PR #24).
`website-release.nix` checks the raw archive SHA256, source/runtime manifest and
every packaged file hash before producing immutable Nix packages. The archive hash
must match before that archive is copied into the Nix store. It uses local retained
archives, not expiring CI links. The website archives are not byte-reproducible
(two builds of one commit differ in build-ID files and chunk names), so the pin is
one kept build: keep those exact files; a rebuild of the same commit will not match. Node24.21.0 is required; Bun,
package installation, source and builds are absent from runtime services.

Retain the web/CMS pair, manifests and checksum index independently of expiring
CI artifacts before any deployment. The infrastructure operator owns that
release archive and its off-site preservation; this module does not configure
an artifact registry or claim that an external retained copy already exists.
Never put environment files, credentials, uploads or a database into a bundle.

Import `platform/website.nix` or `nixosModules.website`. A reviewed host can
construct `release = import ./platform/website-release.nix { inherit pkgs;
webArchive = "/protected/releases/terrace-web-dd1aef7a06ddfa2a31a7cd215afe0f6f10b23191-linux-x64-gnu.tar.gz";
cmsArchive = "/protected/releases/terrace-cms-dd1aef7a06ddfa2a31a7cd215afe0f6f10b23191-linux-x64-gnu.tar.gz"; };`
and set `terrace.website.webPackage = release.web`, `cmsPackage = release.cms`,
`revision = release.revision` and `enable = true`. This is an integration
example, not an enabled host configuration.

## Runtime and migration boundary

The defaults are loopback website14294 and CMS13048. Launchers force
`127.0.0.1`; the module opens no firewall port. The two service users are
`terrace_web` and `terrace_cms`. The CMS owns only `terrace_cms` in native
PostgreSQL and authenticates over its local peer socket. The website has no
database credential. The module fixes `DATABASE_URI` and `CMS_MEDIA_DIR` after
loading the CMS environment, so they cannot silently point at another dataset.
It can coexist with the native identity module's separate database/role.

Deliver root-owned mode0400 `cms.env` and `web.env` into the root-owned0700
`inputsDirectory` (default `/var/lib/terrace-website-inputs`) through the external
secret manager, or set `inputFiles.cms` and `inputFiles.web` to files the secret
manager renders elsewhere (the production profile uses sops-nix templates on
ramfs; then the input directory is neither used nor backed up). Files must be
regular files, not symlinks. Nix only carries
their paths: it never reads their contents during evaluation or a build. The
input unit checks permissions and that both component manifests match the
reviewed revision. Do not grant the application users access to these files;
systemd reads them before dropping privileges. Never print their contents or
credential-bearing process environments during diagnosis.

Follow the [TFC runtime contract](https://github.com/terracefclub/tfc-website/blob/dev/tools/release/README.md)
and [public policy](https://github.com/terracefclub/tfc-website/blob/dev/cms/README.md).
Both complete CMS editorial/member OIDC configurations are required for the
intended host; use per-user SSO and omit all bootstrap variables. Website
`ORIGIN` must equal `MEMBER_AUTH_ORIGIN`; select explicit CMS/content origins.
The VM below supplies user-free, complete loopback identity configuration but
does not run a provider or exercise sign-in. Actual bundle SSO was tested
separately by TFC !42. Public HTTPS opt-in, native TLS/proxy integration,
per-user login and external route/cache denial remain a separate activation
proof. Do not expose the loopback trial or reuse synthetic identities.

Before a release, verify a recoverable backup and retained compatible bundle,
then explicitly run as root:

```sh
systemctl start terrace-cms-migrate.service
systemctl start terrace-cms.service terrace-web.service
```

The migration action first runs its checks (the identity guard, any guard
another module adds, and a refusal while a CMS operator tool runs); a refused
migration stops nothing. Only then does it stop the CMS, the website and a
running sweep, apply the bundle's explicit migrations and record the successful
revision under `/var/lib/terrace-cms`. A start of the CMS, website or sweep
requested while it runs waits for it (all three are ordered after it; the website
VM proof requests each start while a slowed migration runs and checks that each
stays inactive with its start job waiting, then runs after the migration ended).
It is never enabled at boot or called by normal startup.
Repeat the migration action to check that the ledger is unchanged, then start
both services again. Startup rejects a missing/different revision marker. This
gate prevents accidental unmigrated startup; it does not prove schema downgrade
compatibility. A code rollback does not undo migrations: use a separately
verified database/upload recovery with the matching application pair.

Uploads are retained at `mediaDirectory` (default
`/var/lib/terrace-cms-media`), mode0700 and owned by the CMS user. Code stays
read-only. Only private `/var/cache/terrace-cms` is bound writable over the
bundle's empty `.next/cache`. Cache is derived and excluded from backups.
Service/module removal never deletes the upload or database directories.

## Member lifecycle settings, outbox and sweep

The module owns the lifecycle settings that are paths or public origins, and
exports them in each launcher after loading the protected environment file, so a
runtime input cannot redirect them (as with `DATABASE_URI` and `CMS_MEDIA_DIR`):

| Variable | Component | Source |
| --- | --- | --- |
| `LIFECYCLE_OUTBOX_DIR` | CMS start, migrate, sweep | `outboxDirectory` (default `/var/lib/terrace-cms-outbox`) |
| `LIFECYCLE_PUBLIC_ORIGIN` | CMS start, migrate, sweep | `lifecyclePublicOrigin`; the HTTPS gateway sets `https://<webHostname>` |
| `CMS_PUBLIC_ORIGIN` | website | `cmsPublicOrigin`; the HTTPS gateway sets `https://<cmsHostname>` |
| `ADDRESS_HEADER`, `XFF_DEPTH` | website | `addressHeader`, `xffDepth`; the HTTPS gateway sets `X-Forwarded-For` and `1` and unsets `LIFECYCLE_DIRECT_CLIENTS` |
| `LIFECYCLE_INTAKE_SECRET` | CMS and website | the protected `cms.env` and `web.env` (the same value in both, 32 or more bytes); the start guards refuse a missing or short value |

Optional retention placeholders (`LIFECYCLE_APPLICATION_DAYS`,
`LIFECYCLE_INVITATION_DAYS`, `LIFECYCLE_GRACE_DAYS`) may be added to `cms.env`;
their defaults apply otherwise. The CMS also reads an optional
`LIFECYCLE_LOCK_TIMEOUT_MS` (how long the public apply form and the bulk roster
tool wait for the per-address lock). The module owns it as
`lifecycleLockTimeoutMs` (100 to 10000, unset by default, which keeps the CMS
default of 3000 ms) and exports it to the CMS, its sweep and the operator tools.
The website VM proof sets 500 and holds the lock (see below). Everything else in the website's production
configuration table (`docs/member-lifecycle.md` in the website repository)
is unchanged and delivered through the same protected files.

The outbox directory is created `0700` and owned by the CMS account; the CMS
writes its mirror files `0600`. It holds live one-time invitation links, so it is
included in backups with the database (`extraPaths.website-outbox`) and is not
readable by the website account. Read it as root on the host; there is no mail
transport yet.

`terrace-cms-sweep.service` runs the bundle's `bin/sweep` (without
`--purge-moderated`) as the CMS account with the same environment file, guards
and migration gate as the CMS, and is `Type=oneshot` with a 150 second start
limit (`bin/sweep` bounds itself at 120 seconds and exits 2 when it times out). `terrace-cms-sweep.timer` runs it daily at `sweepSchedule` (default
`03:30`, persistent, so a missed run catches up at boot). Run it by hand after
every administrator-initiated erasure:

```sh
systemctl start terrace-cms-sweep.service
```

A sweep requested while an explicit migration runs waits for it and runs once the
migration has finished: the migration's last start-up step stops the sweep only
when it is actually running, so a start job queued behind the migration is kept
(`platform/cms-migrate-preflight.sh`). Starting a migration stops a running sweep,
which is idempotent and retried (website `680ad56`:
`cms/src/lifecycle/sweep.ts` is safe to repeat; `cms/src/lifecycle/outbox.ts`
writes every mirror file to a private temporary file and renames it into place,
`reconcileMirror` marks a row only once its file is confirmed and retries unmarked
rows, and stale temporary files from an interrupted write are removed). A failed sweep leaves the unit failed with a
labelled line in the private journal; the next run retries.

The CMS and website accounts have static IDs (`cmsUid` 44121, `webUid` 44122)
because uploads and the outbox are restored with their numeric owners. These apply
to new hosts. NixOS does not renumber existing users: a host that already has these
accounts with allocated numbers needs a planned renumbering (stop the services,
change the IDs, `chown` their state) before adopting the static values, and backups
taken before the change still carry the old numbers. No such host exists yet.

## Operator tools: `terrace-cms-tool`

The CMS bundle ships two operator command lines (website
`tools/release/README.md`, "Operator tools"): `bin/roster-invite`, the opt-in
bulk roster onboarding (website `docs/member-lifecycle.md`, "Bulk roster
onboarding"), and `bin/wp-import`, the WordPress import applier (website
`docs/wordpress-import.md`). The module installs one root-only command that runs
them as the CMS account:

```sh
terrace-cms-tool roster-invite|wp-import ARGS...
```

It starts a transient systemd unit whose properties are generated from the same
attribute set as the CMS units (account, hardening, memory and CPU limits, log
namespace, working directory, `EnvironmentFile=` naming the protected `cms.env`),
plus write access to the operator directory below. Inside it the CMS identity
guard and migration gate run, the module's settings are exported exactly as for
`bin/start` and `bin/sweep`, and the tool gets its arguments unchanged. Output
and exit status come back to the caller. The environment file is only named:
systemd reads it before dropping privileges, and the wrapper never sources,
copies or prints it. The wrapper refuses a caller that is not root (exit 77), a
missing or unknown tool (64), a lock file that is not a root-owned regular file
(72), another tool run (73) and a running migration (75).
Its logic is `platform/cms-tool.sh` (unit tests:
`python3 -B tests/platform/test_cms_tool.py -v`).

**One at a time, in the foreground.** The wrapper holds a lock
(`/run/terrace-cms-tool.lock`, root's alone: mode `0600`, so no other account can
open it to hold it) from its checks until the tool ends. The file is never removed
or replaced, so every wrapper locks the same file: it is opened without
truncation, locked, and only then checked through the open descriptor. A wider
mode (an older wrapper's `0644` file) is narrowed to `0600` under the lock; a
symbolic link (never followed), a directory or other non-regular file, or a file
root does not own is refused with exit 72 and left as it is: inspect it, remove it
by hand and run the tool again. It refuses
while any `terrace-cms-tool-*` unit is still active, naming it (exit 73). Run the
tools one at a time, in the foreground of an SSH session (or in `tmux`/`screen`),
and do not start the wrapper as a background job with an open standard input
(no proof exercises that). A dropped session kills the wrapper but not the
tool's unit: it keeps running until it ends or reaches its bound, and while it
runs the next wrapper and every migration refuse. A migration started at the very
moment a wrapper starts can still pass both checks (a window of milliseconds).

**Run-time bound.** Each tool unit has `RuntimeMaxSec` from
`operatorToolMaxSeconds` (60 to 86400, default 3600). The website VM's runs take
seconds; a roster apply handles at most 200 rows and the real WordPress plan
about 245 items, so an hour leaves wide room while still freeing an orphaned or
hung run. Above 3600 the bound is longer than the importer's one-hour takeover
window: an importer run idle for an hour would look stale to `wp-import
rollback --take-over-stale` while its unit could still be running. Keep it at
3600 or below; if you raise it, check that no tool unit is active before a
takeover. In the website VM a tool stopped at the bound made the wrapper exit 1
(the unit's journal says `Failed with result 'timeout'`), and one killed by
`SIGKILL` made it exit 255 (`Failed with result 'signal'`); the tool's own exit
statuses (0, or 1 for a refusal or a failed row) come back unchanged. A tool unit
stopped with `systemctl stop` makes the wrapper exit 0 (systemd counts a death by
its own `SIGTERM` as clean): after a stop, check the run, not the exit status. A tool
stopped at the bound (or by a signal) ends where it was:
- roster `apply`: rows committed before the stop stay, journaled, with their
  link files; the run stays `running` until it has been idle for 30 minutes,
  after which the next apply or a rollback marks it failed. Roll it back with
  `roster-invite rollback --run RUN_ID ... --confirm-production` (a failed run can
  be rolled back), or dry-run the file again and apply what is left;
- importer `apply` or `publish`: the run ledger stays `running`; roll it back
  once it has been idle for an hour with `wp-import rollback --run RUN_ID
  --take-over-stale ...` (or sooner with `--stale-after-minutes N`), then apply
  again.

**Inputs and reports.** The tools read only owner-only files of the account
that runs them, outside any Git checkout, by absolute path. Put them in the
module's `operatorDirectory` (default `/var/lib/terrace-cms-operator`, `0700`,
CMS account), and purge it when the run is over:

```sh
install -o terrace_cms -g terrace_cms -m 0600 roster.csv /var/lib/terrace-cms-operator/roster.csv
find /var/lib/terrace-cms-operator -mindepth 1 -delete      # after the run
```

**Command lines on a host** (`OP` is the operator's administrator email; the
plan hash comes from the dry run's output):

```sh
D=/var/lib/terrace-cms-operator
terrace-cms-tool roster-invite dry-run --file $D/roster.csv --operator OP --report $D/roster-dry-run.json
terrace-cms-tool roster-invite apply --file $D/roster.csv --operator OP --plan-sha256 PLAN_SHA256 --report $D/roster-apply.json --confirm-production
terrace-cms-tool roster-invite rollback --run RUN_ID --operator OP --report $D/roster-rollback.json --confirm-production
terrace-cms-tool wp-import dry-run --plan $D/wp-import-plan.json --report $D/import-dry-run.json --allow-nonlocal-database
terrace-cms-tool wp-import apply --plan $D/wp-import-plan.json --report $D/import-apply.json --allow-nonlocal-database --confirm-production
terrace-cms-tool wp-import rollback --run RUN_ID --report $D/import-rollback.json --allow-nonlocal-database --confirm-production
```

Both flags stay explicit operator choices; the wrapper adds neither. The host's
`DATABASE_URI` (`postgresql://terrace_cms@/terrace_cms?host=...`) names a user
with an empty host, which the tools' local-database check does not parse, so it
counts as not local: the importer refuses every command without
`--allow-nonlocal-database`, and both tools treat the database as production, so
every write also needs `--confirm-production`. Take a verified backup before an
importer apply or a roster apply, as for a migration.

**When a tool is still running.** A migration refuses to start while a
`terrace-cms-tool-*` unit is active, before it stops anything: the CMS and the
website keep running and serving, and the ledger is unchanged. `systemctl start`
only says that the unit failed; the reason is in the apps journal namespace.
Before deploying, check that no tool is running (no output means none):

```sh
systemctl list-units --plain --no-legend --state=active,activating,deactivating 'terrace-cms-tool-*'
```

If a migration was refused (or a tool unit was left behind), read the reason,
stop the tool unit, then run the normal release steps (the website VM proof runs
exactly these commands):

```sh
journalctl --namespace=terrace-apps -u terrace-cms-migrate.service -o cat --no-pager
systemctl list-units --plain --no-legend --state=active,activating,deactivating 'terrace-cms-tool-*'
systemctl stop 'terrace-cms-tool-*'
systemctl reset-failed terrace-cms-migrate.service
systemctl start terrace-cms-migrate.service
systemctl start terrace-cms.service terrace-web.service
```

The site stays up until the second-to-last command, which stops it as any
migration does.

If that stop fails partway (not tested), some of the CMS, website and sweep are
already stopped and the migration does not run: `systemctl start` reports the
migration unit failed and the apps journal has the stop error. Check which of
the three are active (`systemctl is-active terrace-cms.service
terrace-web.service terrace-cms-sweep.service`), then either start the CMS and
website again (`systemctl start terrace-cms.service terrace-web.service`) or fix
the cause, `systemctl reset-failed terrace-cms-migrate.service` and run the
migration again.

Stopping a tool unit stops the tool where it was (see the run-time bound above
for what that leaves and how to roll it back).

**Output, arguments and who sees them.** The tools' output (counts, hashes and
fixed refusal text; reports go to files) and their exit status reach only the
caller's terminal: with `--pipe` nothing of it is written to any journal, and the
unit has a fixed description, so its command line is not in systemd's start and
stop messages either (the website VM proof checks both). While a tool runs, its
command line, which carries the operator's email and file paths, is visible to
every local user through `ps`, `systemctl status`, `systemctl show` and
`systemctl cat` (checked in the website VM as `nobody`). On a production host
the only local accounts are the operators' and the services' own, but do not put
anything confidential in arguments: personal data belongs in the files. Other
places a command line can be recorded on a host, none of them tested here:
sudo's own log of the command it ran (the system journal), audit `execve`
records if auditing is turned on, and root's shell history. Do not run the tools
on a host with unprivileged interactive users until they can take the operator
from a file or an environment variable (backlog).

**What is backed up.** The tools' database rows (the roster journal
`roster_runs` and `roster_run_items`, applications, invitations, outbox rows,
imported documents and the import run ledger) are ordinary CMS rows in the
database backup. The roster tool's invitation link files are written to the
lifecycle outbox (`outboxDirectory`), which is in the backup set
(`extraPaths.website-outbox`) and restored with the database. The operator
directory is in no backup set, by design: it holds personal data the operator
supplies and keeps elsewhere, and a module assertion refuses a configuration that
puts it inside, around or at a backup path.

**Proven where.** In VM proofs only (below and in the production profile
proof); nothing has been run on a real host or against a provider. The website VM
switches the protected `cms.env` to a public-profile variant (same secrets) for
the tools' writes while the running CMS server stays on the proof's loopback
profile; the public profile's server path is proven by the HTTPS gateway check,
and the tools under the real sops-rendered environment only by the production
profile proof's dry run.

## Health, private logs and capacity

Startup waits for actual CMS `/api/events` and website `/events/` HTTP200.
Failures restart after10seconds, at most3starts/10minutes, with150second
startup and30second stop limits. CMS exit143 after a handled SIGTERM is a normal
stop; migration success still requires exit0. A failed health gate leaves the
unit failed. There is no external alert delivery configured yet. Check service state and
the protected namespace locally:

```sh
systemctl status terrace-cms.service terrace-web.service
journalctl --namespace=terrace-apps -u terrace-cms.service -u terrace-web.service
```

Application/import errors may contain protected values. The dedicated volatile
journal is readable only by privileged journal operators, targets8MiB total
with2MiB journal files and one-day retention, rate-limits40messages/30seconds,
and does not forward to the default journal/syslog/console. Active-file journal
overhead can exceed the target; these are journald retention bounds, not an
exact byte quota. Never paste its raw contents into CI or chat. Health and
permission errors are fixed text. Runtime environment values remain outside Git
and the Nix store.

| Process | MemoryHigh / MemoryMax | CPU limit | Other bounds |
| --- | --- | --- | --- |
| CMS, including native images | 1536MiB / 2GiB | 200% | Node old-space1024MiB,256tasks |
| Website | 512MiB / 768MiB | 100% | Node old-space384MiB,256tasks |
| Shared PostgreSQL | existing/default / 1GiB | 200% | default40connections,64MiB shared buffers |

The approved planning candidate remains a16GiB application host under the total
US$80/month ceiling, including relay/backups/tax/overlap. These are workload caps,
not an account quote or a production load measurement. Adding existing identity's
2GiB cap and the existing768MiB backup cap leaves more than4GiB planning reserve
before OS/gateway overhead on16GiB; measure the combined stack and keep at least
25% measured memory/CPU headroom under representative load before activation.
CPU quotas are limits, not reservations. K3s is not enabled by this module. Do not
co-enable additional workloads or infer that Documenso/Windmill fit from this
small synthetic proof. Provider access, exact price/stock/tax and real content
load remain unverified.

## Backup owner and isolated recovery

When `terrace.backup.enable` is true, the module automatically adds its managed
`terrace_cms` database, uploads, the lifecycle outbox and, only when `inputFiles`
is unset, the protected input directory to the existing encrypted restic backup.
With `inputFiles` set (the production profile delivers `cms.env` and `web.env` as
sops templates on ramfs) the input directory is neither used nor backed up: the
secret manager is the source of truth (`platform/website.nix`). Recovery credentials/SSH keys are separately
escrowed, outside that input directory and independently available without SSO.
The infrastructure operator owns backup targets, retention, age/check alerts,
restore drills and credential escrow. The inherited backup retention applies;
no external destination or alert account is created by this module.

This proof keeps the source serving read-only traffic. The existing backup tool
captures PostgreSQL and filesystem inputs sequentially; concurrent upload/delete
or secret rotation consistency is not established. Before real writes and
scheduled backups are enabled, review a quiescent backup window or equivalent
media lifecycle guarantee and test it. Automatic inclusion is not a claim of
an atomic cross-dataset snapshot. Do not rotate runtime inputs during capture.

With identity co-enabled, explicitly include `keycloak` and its protected input
directory as documented in [identity hosting](identity-host.md). Website backup
inclusion does not silently back up every other database. Existing
[identity recovery](backups.md) and this website proof are separate; a combined
whole-host/off-site disaster recovery drill is still required.

The disposable recovery target sets `manageDatabase = false` and points
`databaseSocketDirectory` at the restore tool's private socket. The test alone
runs that cluster under an isolated `/var/lib` directory, outside the services'
private temporary namespaces. It runs CMS as the existing isolated
`terrace-restore` user to access that0700
socket; it keeps the original database role and app package. No loopback TCP
database proxy, schema push, copied production account or replacement of the
source database is needed. Do not apply this disposable-user override to a live
host. The original source continues serving unchanged throughout recovery.

## Reproduce the actual-artifact VM check

On the x86-64 Linux builder, under the shared queue:

```sh
BUILD_LOCK=/path/to/shared-heavy.lock platform/website-check \
  /protected/releases/terrace-web-dd1aef7a06ddfa2a31a7cd215afe0f6f10b23191-linux-x64-gnu.tar.gz \
  /protected/releases/terrace-cms-dd1aef7a06ddfa2a31a7cd215afe0f6f10b23191-linux-x64-gnu.tar.gz
```

This separate check accepts local hash-pinned artifacts so ordinary flake
checks never depend on expiring CI authentication or a developer's absolute
path. It uses the platform's existing locked nixpkgs and two disposable guests.
It also evaluates a website/identity composition toplevel without reading
runtime files or starting a provider; this is configuration compatibility only.
No actual host profile changes. The test exercises real CMS migrations/schema,
repeat ledger, public content/private draft and media denial, native image cache,
service/code/input isolation and resource caps. It backs up database/uploads and
protected settings over test SFTP with restic, performs a complete repository
check, restores into a fresh guest, reconciles table/file hashes and serves the
same content with private routes still denied. It also checks the ledger against
the pinned migration list, refuses a short intake secret, runs the sweep service
as the CMS account (writing a synthetic pending outbox copy `0600`), proves the
real timer unit fires it (a backdated persistent stamp makes it catch up), and
restores the outbox with identical bytes, owner and modes. It measures migration, backup,
check, restore and recovered HTTP verification, plus source cgroup peaks.
Only synthetic, user-free fixtures are used; no real provider/email call.

Through `terrace-cms-tool` it also runs both operator tools as the CMS account, with
the protected file switched to a public-profile variant for the writes (same
secrets; the original is put back byte for byte): the wrapper refuses a non-root
caller, an unknown tool, a closed migration gate and a running migration, and a
migration refuses to start under a running tool; a path with a space and an
option-like argument reach the tool unchanged and exit statuses come back. Roster:
the default dry run writes nothing, a write without `--confirm-production` is
refused, the apply writes the journal (`completed`, two items), two approved
applications with pending invitations and two link files (`0600`, CMS account) with
the public origin, a re-run is a no-op and rollback revokes both. Importer (the
website's synthetic fixture plan): refused without `--allow-nonlocal-database`, the
dry run writes nothing, an apply without `--confirm-production` is refused, the
confirmed apply creates the planned documents, a re-apply skips them all and
rollback deletes the documents and redirects it created. With
`lifecycleLockTimeoutMs = 500` and the address lock held, the public apply endpoint
answers 503 within the bound and a roster row waits about half a second, then stops
with `address-locked` and stores nothing. A frozen run's transient unit has the CMS
unit's account and settings, and no synthetic secret value appears in its
properties (as root or another user), any process's arguments or any journal. The
backup snapshot contains the outbox and not the operator directory, which is then
purged; the roster journal rows come back from the restore.

Since the PR 68 review the proof also covers: dry runs leave the outbox and
upload directories byte-identical and add only their own report to the operator
directory; one wrapper at a time (the second is refused by the lock, and after
the first wrapper is killed, as by a dropped session, its unit keeps running and
the next wrapper names it); a migration started then is refused with the reason
in the apps journal (see the follow-up below for what is true since); and the
recovery commands above bring them back; a tool killed by a signal and one
stopped at a 60-second `operatorToolMaxSeconds` (exit statuses above); the run
time bound is set on the unit; neither the tools' output nor their arguments
appear in any journal entry; and the restore comparison now includes
applications, invitations, the import ledger and journal, pages, posts and
redirects (the imported documents and redirects were rolled back earlier in the
proof, so those last three compare their remaining rows).

Since the PR 68 round 2 review the proof also covers: the lock file is
`root:600` and `nobody` cannot open it; a migration refused by a running tool,
or by the identity guard, leaves the CMS and website running with the same
processes and still serving, and the ledger unchanged; an accepted migration
finds the CMS, website and a running sweep stopped, their processes gone, at the
step standing in for its first statement; a real importer apply stopped mid-run
with `systemctl stop` (its run row and 6 media documents written, its first event
waiting on a held lock; the wrapper exits 0) leaves the run `running`, rollback refuses it (`run-still-running`, then `run-not-stale`), and
`--take-over-stale --stale-after-minutes 1` rolls it back (the 6 documents
deleted) once it has been idle for a minute; a roster run left `running` is refused by rollback while it is
younger than 30 minutes and marked failed and rolled back after (simulated by
setting the run's status and timestamp in `roster_runs`, not produced by stopping
a real roster apply).

Verified again 2026-11-24 at Womb `e7403a5` after the review (fresh result,
04:02 to 04:08 Pacific): 39 CMS tables; roster lock wait 0.50 s at 500 ms,
public apply 503 in 0.55 s; migration 5.32 s, encrypted backup 7.73 s, complete
check 5.00 s, restore 4.11 s, recovered HTTP verification 10.42 s. The Documenso
and Vaultwarden functional proofs were not rerun for this re-pin: their
derivations equal ones that passed before (Womb PR 64), so they carry over.

Verified 2026-11-24 on the shared Linux builder with the kept `dd1aef7` build
(web `1a4e07f7…bd12`, CMS `8929443f…2a4c`) at Womb `3f4bc9e` (fresh result): 39
CMS tables and the twelve-migration ledger; roster lock wait 0.52 s at 500 ms,
public apply 503 in 0.56 s; migration 4.99 s, encrypted backup 9.08 s, complete
repository check 5.41 s, restore 4.85 s, recovered HTTP verification 11.20 s;
source cgroup peaks 82.1 MiB CMS, 47.1 MiB website, 128.3 MiB PostgreSQL.

Verified 2026-11-23 on the shared Linux builder with the `eab4189` bundles (05:26
to 05:30 Pacific, fresh result): the two-guest test passed with 39 actual
non-system CMS tables and the twelve-migration ledger in pinned order, and a
synthetic bulk-roster journal row (`roster_runs`, `roster_run_items`) came back
identical from the restore. Migration 5.84 s, encrypted backup 8.28 s, complete
repository check 5.62 s, restore 4.87 s, recovered HTTP verification 11.01 s;
source cgroup peaks 81.9 MiB CMS, 47.3 MiB website, 125.6 MiB PostgreSQL.

Verified 2026-11-19 on the shared Linux builder at Womb `48ab695` with the
`680ad56` bundles (06:06 to 06:10 Pacific, prebuilt closure): the two-guest test
passed with 37 actual non-system CMS tables and the eleven-migration ledger in
pinned order. Explicit migration took 6.02 s, encrypted backup 8.34 s, complete
repository check 4.95 s, isolated restore 5.43 s and recovered HTTP verification
11.77 s. The sweep service, its schedule and timer catch-up, its wait behind a slow
migration, and the outbox restore (bytes, static owner, modes, and a read and
write by the CMS account) passed. Source cgroup peaks were 81.9 MiB CMS, 47.5 MiB
website and 125.4 MiB PostgreSQL.

Verified2026-11-03 on the shared Linux builder (historical, website `3902e16`): the final two-guest test passed
in83.34seconds with25actual non-system CMS tables and an unchanged six-migration
ledger. Explicit migration took4.48s, encrypted backup7.05s, complete repository
check5.45s, isolated restore3.96s and recovered HTTP verification11.29s. Source
cgroup peaks were101.9MiB CMS,47.3MiB website and121.2MiB PostgreSQL during this
small synthetic workload. These are component measurements, not combined
identity/gateway capacity or a production load test. The composition toplevel
also evaluated without runtime credential files. Incorrect archive hash input
was rejected before package copy.

This evidence does not establish public TLS, public SSO, gateway/CDN rules,
live provider recovery, WordPress parity, full-stack load or actual off-site DR.
