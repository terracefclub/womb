# Optional native website host

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
archives from TFC source `680ad567f553449280664cafad2d200011b6abac` (website `dev`, PR #15).
`website-release.nix` checks the raw archive SHA256, source/runtime manifest and
every packaged file hash before producing immutable Nix packages. The archive hash
must match before that archive is copied into the Nix store. It uses local retained
archives, not expiring CI links. Node24.21.0 is required; Bun,
package installation, source and builds are absent from runtime services.

Retain the web/CMS pair, manifests and checksum index independently of expiring
CI artifacts before any deployment. The infrastructure operator owns that
release archive and its off-site preservation; this module does not configure
an artifact registry or claim that an external retained copy already exists.
Never put environment files, credentials, uploads or a database into a bundle.

Import `platform/website.nix` or `nixosModules.website`. A reviewed host can
construct `release = import ./platform/website-release.nix { inherit pkgs;
webArchive = "/protected/releases/terrace-web-680ad567f553449280664cafad2d200011b6abac-linux-x64-gnu.tar.gz";
cmsArchive = "/protected/releases/terrace-cms-680ad567f553449280664cafad2d200011b6abac-linux-x64-gnu.tar.gz"; };`
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
secret manager. Files must be regular files, not symlinks. Nix only carries
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

The migration action stops both application services, applies the bundle's
explicit migrations and records the successful revision under
`/var/lib/terrace-cms`. It is never enabled at boot or called by normal startup.
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
their defaults apply otherwise. Everything else in the website's production
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

A sweep requested while an explicit migration runs waits for it (and is skipped
if one is still active when it starts); starting a migration stops a running sweep,
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
`terrace_cms` database, uploads and the entire protected input directory to the
existing encrypted restic backup. Recovery credentials/SSH keys are separately
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
  /protected/releases/terrace-web-680ad567f553449280664cafad2d200011b6abac-linux-x64-gnu.tar.gz \
  /protected/releases/terrace-cms-680ad567f553449280664cafad2d200011b6abac-linux-x64-gnu.tar.gz
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
