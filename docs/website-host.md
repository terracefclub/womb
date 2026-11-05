# Optional native website host

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
archives from TFC source `3902e16c8c681638282e5983869d53efa8bb424c` (TFC !42).
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
webArchive = "/protected/releases/terrace-web-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz";
cmsArchive = "/protected/releases/terrace-cms-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz"; };`
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

Follow the [TFC runtime contract](https://gitlab.com/terrace/tech/tfc-website/-/blob/dev/tools/release/README.md)
and [public policy](https://gitlab.com/terrace/tech/tfc-website/-/blob/dev/cms/README.md).
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
  /protected/releases/terrace-web-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz \
  /protected/releases/terrace-cms-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz
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
same content with private routes still denied. It measures migration, backup,
check, restore and recovered HTTP verification, plus source cgroup peaks.
Only synthetic, user-free fixtures are used; no real provider/email call.

Verified2026-11-03 on the shared Linux builder: the final two-guest test passed
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
