# Encrypted off-site backups and isolated restore

Updated 2026-11-02. Implements the backup module for [issue #4](https://gitlab.com/terrace/tech/womb/-/issues/4)
declaratively in `platform/backup.nix` (NixOS module `terrace.backup`, default
off) with a two-VM test in `platform/backup-test.nix`. Nothing here is deployed:
there is no Storage Box, no B2 bucket and no production secret yet (see
[what is proven](#what-the-vm-test-proves-and-what-it-does-not)). A successful
backup job is not a successful restore; the restore gate stays open until a
drill passes against a real off-site repository.

**Verified on 2026-11-02:** the two-VM backup and restore drill passed in 133
seconds. The first synthetic backup took 13.5 seconds and peaked at 47.8 MiB.
Restoring the selected earlier snapshot took 3.6 seconds. The separate single-node
check also passed. Production recovery time still requires an off-site drill
with representative data.

## Decision: restic

The hosting plan ([access-relay.md](access-relay.md)) already prices a Hetzner
Storage Box (restic/Borg/SFTP) as the primary repository and an optional
Backblaze B2 copy at a second provider, both with restic. restic gives
client-side encryption, deduplication, compression, SFTP and S3 backends, one
static binary, and a repository that is unreadable without its password. No
other tool was evaluated further.

## Datasets, owners and retention

Owner names are deliberately unset: **every row needs a named, accountable owner
before real data is loaded.** Until then "TBD" means nobody is responsible.

| Dataset | Where | Method | Owner | Off-site | Restore check |
| --- | --- | --- | --- | --- | --- |
| CMS database (`cms`, Payload) | PostgreSQL on the application host | `pg_dump -Fc` in a `REPEATABLE READ` snapshot | TBD | Primary and optional secondary | Row counts per table vs counts taken in the same snapshot |
| Community database (`community`) | same cluster | same | TBD (also moderator ownership, see [community pilot](community-pilot.md)) | same | same |
| Keycloak database (`keycloak`) | same cluster | same | TBD | same | Table counts plus a separate [actual schema/issuer/signing-key recovery check](identity-recovery.md), with no real accounts |
| PostgreSQL roles | same cluster | `pg_dumpall --globals-only` (contains password hashes; protected by repository encryption) | TBD | same | Roles restored first; database owners checked in the VM test |
| CMS and community media | `terrace.backup.media` paths (default budget: 5 GB, see community pilot) | restic file backup, taken after the database dump; sha256 manifest | TBD | same | sha256 and file count per directory |
| Headscale SQLite | `/var/lib/headscale/db.sqlite` on the relay | `sqlite3 .backup` (consistent), `integrity_check`, table counts | TBD | same | integrity check plus table counts |
| Headscale Noise and DERP private keys | `/var/lib/headscale/*.key` | restic file backup; sha256 manifest | TBD | same | sha256 of every restored file |
| Extra configuration (`terrace.backup.extraPaths`) | per host | restic file backup; sha256 manifest | TBD | same | sha256 |
| NixOS and Kubernetes configuration | Git | Not backed up here: Git is the source of truth | Repository maintainers | Git remote | Rebuild from a clean checkout |

Explicit exclusions and gaps (not covered by this module):

- **K3s datastore and server token** (`/var/lib/rancher/k3s/server/db`,
  `server/token`) and local PV data under `/var/lib/rancher/k3s/storage` are not
  backed up. Application data that lives in PVs must be listed in `media` or
  `extraPaths` once the application's storage layout exists. The platform
  README still lists the K3s datastore restore as an open gate.
- **Repository passwords, the SFTP key and B2 credentials are not in any backup**
  (that would be circular). Losing every copy of a repository password makes that
  repository permanently unreadable. Keep an escrow copy in the team password
  manager, outside the servers. Owner: TBD.
- Documenso, Windmill, Vaultwarden, Home Assistant and Frigate are not deployed
  yet; each needs a row here, an owner and a restore check before it holds real
  data. Removing an application never deletes its backed-up data.
- WordPress backups are tracked in the TFC website repository, not here.

### Retention

Per target, applied after every successful run with `restic forget --prune`
(grouped by host and tag): the last 3 snapshots plus 7 daily, 4 weekly and
6 monthly (`terrace.backup.retention.*`). Restic additionally keeps the oldest
snapshot when there are fewer calendar periods than requested; see its
[retention rules](https://restic.readthedocs.io/en/stable/060_forget.html).
These overlapping rules bound the retained snapshot count to at most 21.
`restic check` runs weekly (Sunday 05:30,
randomised by up to 30 minutes) with a 5% data subset
(`terrace.backup.check.readDataSubset`), so each week re-reads a different slice.
Proposed recovery targets from the [community pilot](community-pilot.md) are at
most 24 hours of data loss (daily schedule) and four hours to recover; they are
unmeasured against a real repository.

### Cost

The allowance in [access-relay.md](access-relay.md) is US$5.00/month: a Storage
Box BX11 (1 TB, EUR 3.20) for the primary and up to 100 GB at B2 ($6.95 per TB-month)
for the secondary. Prices are public-page figures from 2026-10-31, not an account
quote. The module exports `terrace_backup_repository_bytes` per target so storage
growth is visible before the allowance is exceeded. The issue's original US$20
total is superseded by the strictly-below-US$80 ceiling.

## Where secrets must come from

Every secret is a root-owned file outside the Nix store and Git with mode `0400`;
the tooling refuses group- or other-accessible files and anything under
`/nix/store`.

| Option | Contents |
| --- | --- |
| `targets.<name>.passwordFile` | restic repository password (one per target, so one leak does not open both) |
| `targets.<name>.sftp.identityFile` | SSH private key for the Storage Box sub-account |
| `targets.<name>.sftp.knownHostsFile` | pinned host key; checking is always strict (not secret, but must be verified out of band) |
| `targets.<name>.environmentFile` | optional `NAME=value` lines, e.g. `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for B2's S3 API |

The legacy root flake uses sops-nix, but the `platform` flake does not yet, and
no encrypted secrets file exists for these hosts. When sops-nix (or agenix) is
adopted for the platform hosts, point the options at
`config.sops.secrets.<name>.path`. **Production secret delivery is still a release
gate.** Keep production jobs disabled until declarative secret delivery and file
permissions are verified. Escrow the provider-issued credentials and repository
passwords independently; never commit real values in this repository. The VM
test generates throwaway keys inside the test VM only.

## Enabling it on a host

```nix
{
  imports = [ platform.nixosModules.backup ];   # also imported by single-node.nix
  terrace.backup = {
    enable = true;
    postgresql.databases = [ "cms" "community" "keycloak" ];
    media.cms-uploads = "/var/lib/payload/media";          # TBD real path
    targets.primary = {
      enable = true;
      repository = "sftp:uNNNNNN@uNNNNNN.your-storagebox.de:terrace-restic";
      passwordFile = "/run/secrets/terrace-backup/primary-password";
      sftp.identityFile = "/run/secrets/terrace-backup/storagebox-key";
      sftp.knownHostsFile = "/etc/terrace/backup/storagebox-known-hosts";
    };
    targets.secondary = {            # optional second provider
      enable = true;
      repository = "s3:https://s3.us-west-004.backblazeb2.com/terrace-restic";
      passwordFile = "/run/secrets/terrace-backup/secondary-password";
      environmentFile = "/run/secrets/terrace-backup/b2-env";
    };
  };
}
```

On the relay, `terrace.backup.headscale.enable` defaults to `terrace.relay.enable`.
Targets are off until enabled, and the module asserts at least one is.
One repository per host: each host (relay, application) gets its own repository
path so `latest` is unambiguous. The Storage Box SFTP port and the B2 endpoint/region
are provider details to confirm on the account (`sftp.port` defaults to 22).

First use, as root, once per target (initialisation is never automatic, so a
wrong path cannot silently create an empty repository):

```sh
terrace-backup init primary
systemctl start terrace-backup.service   # first run now, instead of waiting for 03:15
terrace-backup restic primary snapshots
```

## What runs, and its resource bounds

| Unit | Schedule | Purpose |
| --- | --- | --- |
| `terrace-backup.service` / `.timer` | daily 03:15, 15 min random delay, persistent | stage dumps and manifests, back up, forget and prune, per target |
| `terrace-backup-check.service` / `.timer` | Sundays 05:30 | `restic check --read-data-subset=5%` per target |
| `terrace-backup-freshness.service` / `.timer` | hourly | fails while any target is older than `staleAfterHours` (26) or has never succeeded |

Backup and check services run with `MemoryMax=768M`, `MemoryHigh=640M`,
`CPUQuota=50%`, `IOWeight=20`, `TasksMax=256`, nice 10, `GOMEMLIMIT=512MiB`, a
6-hour timeout, `ProtectSystem=strict`, private `/tmp`, and no new privileges.
They run as root because they must read media owned by several users; PostgreSQL
is dumped through `runuser` as the `postgres` user. The planning allowance in
[access-relay.md](access-relay.md) assumes 0.3-0.5 GB restic peak; the limits are
bounds, not measurements of the real stack. Dumps are staged in
`/var/lib/terrace-backup/dumps` (mode 0711 root, PostgreSQL files 0700 postgres)
and deleted when the run ends, so the staging space is the size of the largest
uncompressed dump set plus manifests.

**Consistency.** Each database is dumped by `pg_dump --snapshot` inside the same
`REPEATABLE READ` transaction that counts its rows, so the dump and the manifest
describe exactly one instant. Files are read after the databases, so a media file
referenced by a dump is present unless it was deleted in between; files uploaded
or changed while the run is in progress can differ from their manifest hash, and
the next run corrects that. Headscale's SQLite file is captured with SQLite's online
backup API, not by copying live WAL files.
There is no shared transaction across databases or between a database and media;
applications with cross-database/file invariants need a quiesce or storage-snapshot
strategy before production use. A successful synthetic restore does not establish
that live application consistency.

### Backup-age signal

After each run the tool rewrites `/var/lib/terrace-backup/metrics/terrace_backup.prom`
(Prometheus textfile format): `terrace_backup_last_success_timestamp_seconds`,
`terrace_backup_last_run_ok`, `terrace_backup_last_duration_seconds`,
`terrace_backup_repository_bytes`, `terrace_backup_check_last_success_timestamp_seconds`
and `terrace_backup_check_last_ok`, each labelled by target. The success timestamp
moves only on full success of that target. Alert on `time() - last_success > 93600`
when monitoring exists (the foundation has none yet; point a node-exporter textfile
collector at that directory). Without monitoring, `terrace-backup age` prints each
target's age and exits 1 when stale or never run, and the hourly freshness unit
shows up in `systemctl --failed`.

## Restore procedure

The restore script never touches live data. It restores a snapshot into a new
directory, starts a throwaway PostgreSQL cluster inside that directory (Unix
socket only, no TCP listener, `fsync` off), loads roles and databases, then
reconciles everything against the manifests that the backup embedded in the
snapshot. It sends no mail, makes no payment calls and starts no application.

On the recovery host (any host importing the module with the same target
definitions and the secrets files in place), as root:

```sh
terrace-backup restic primary snapshots                      # choose a snapshot ID
terrace-backup-restore --target primary --snapshot <ID|latest> \
  --dest /var/tmp/terrace-restore-$(date +%Y%m%d) --keep-running
```

- It refuses a destination that exists and is non-empty, is `/`, or overlaps a
  live path (state directory, PostgreSQL, Headscale, media, extra paths).
- Output ends with `RECONCILIATION OK` and exit status 0, or `RECONCILIATION FAILED`
  and status 1. The report (table names and counts only, never row data) is
  `<dest>/reconciliation.txt`.
- Files are under `<dest>/files/<original path>`; the isolated database socket is
  `<dest>/pgsock`:
  `runuser -u terrace-restore -- psql -h <dest>/pgsock -U "$SQL_BOOTSTRAP" -d cms`,
  where `SQL_BOOTSTRAP` is the validated source name retained in
  `<dest>/bootstrap-role.json`; the OS owner stays `terrace-restore`.
- Re-check later with `terrace-backup-restore --verify-only <dest>`; stop the
  cluster with `terrace-backup-restore --stop <dest>`. Without `--keep-running`
  it stops itself. Failed restores stop their isolated database even when
  `--keep-running` was requested. Restored payloads are inside a root-only
  `files` directory (0700), regardless of their original permissions. Diagnostic
  files are root-only (0600); PostgreSQL data and its socket are restricted to the
  restore OS user (0700).
- `--target secondary` restores from the other provider; use it to test that
  copy independently.

Reconciliation checks, per database, that every user table's row count equals the
count taken at backup time, and, per file set (media, extra paths, Headscale
keys), that the file count and every sha256 match. Headscale's snapshot must pass
`PRAGMA integrity_check` with matching table counts. Foreign-key relationships and
ownership come from the restored schema; the VM test additionally asserts them.

The script does not put recovered data into service. Promoting a recovery
(copying media back, loading the databases into the production cluster, restoring
Headscale's keys before its first start so clients keep their network identity) is
a manual, owner-approved step to be written and drilled once real hosts exist.
Never restore over a live host to "test".

## What the VM test proves, and what it does not

Run from the repository root on the x86_64 Linux builder with KVM. Set
`BUILD_LOCK` to the lock used by every heavy job on that host:

```sh
BUILD_LOCK="$HOME/.falcon-heavy.lock" platform/check backup single-node
```

The [platform instructions](../platform/README.md) also cover detached runs.
The check boots a `source` VM (PostgreSQL with
three databases seeded with synthetic rows including foreign keys, two media
directories with random files, the real Headscale relay module) and a `target`
VM that offers SFTP only (no shell), then:

1. Refuses group-readable secrets; a run against an uninitialised repository fails
   and leaves the age signal at zero, with no plaintext staging left behind.
2. Backs up to two independent SFTP repositories (primary and secondary), updates
   the age signal on success only, flags staleness, and proves one failing target
   neither advances its own signal nor blocks the other.
3. Applies the default retention policy to five same-day primary snapshots:
   the oldest is retained as Restic's safety fallback, an intermediate snapshot
   is pruned, and the three most recent recovery points survive. Both targets
   pass `restic check`.
4. Confirms the repository is encrypted: none of the plaintext markers or row text
   occur in any repository file, `config` is not parseable, and both a wrong and a
   missing password are rejected.
5. Asserts declared `MemoryMax`, `MemoryHigh`, `CPUQuota`, `IOWeight` and
   `TasksMax`, and the timers.
6. Destroys the source databases, media and Headscale files, then restores a chosen
   older snapshot by ID into an isolated directory and database: row counts, content
   hashes of every table, foreign keys, database ownership, media hashes, Headscale
   key hashes and users all equal the state captured before destruction (and differ
   from the later state), with no TCP listener. It then tampers with a restored
   file and a restored row and requires reconciliation to fail, and restores
   `latest` from the secondary target.
7. Restores a deliberately damaged dump and requires failure, private diagnostics,
   no remaining restore database process (with and without `--keep-running`),
   and unchanged source data. Unprivileged users cannot read restored payloads.
8. Requires a successful restore without `--keep-running` to stop its database.

Still unproven: a real Hetzner Storage Box (port, quotas, host key, sub-account
restrictions), a real B2 bucket and the S3 code path (no S3 server was available
in the pinned Nixpkgs, so the secondary target in the test is a second SFTP
repository), production secret delivery, an off-site restore drill from a different
host and network, the K3s datastore and PV data, large-data runtime/memory (the VM
dataset is a few hundred kilobytes), the real CMS schema, populated Keycloak accounts/sessions and whether a
restored Headscale lets real clients reconnect. Recovery time and data loss are
therefore still unmeasured; the issue's measured-RTO criterion is open.

The separate [identity recovery VM drill](identity-recovery.md) restores the
actual, user-free Keycloak schema and protected input files on a different guest,
then starts Keycloak and verifies issuer, realm ID and public signing-key continuity
while the source remains running. It uses the same backup/restore tooling and does
not enable production backups or establish a real off-site recovery guarantee.
