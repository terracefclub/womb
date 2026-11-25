# Restore promotion: putting a recovery into service

Written 2026-11-24 against Womb `trunk` at `1cf1374`. This is owner runbook gap
G-22 (step 7.8): what an operator does after a restore has been reconciled, to
make the replacement pair the live pair.

**Status of this document.** It was written by reading the rehearsal code and
the tools' own scripts. On 2026-11-25 its section 2 was run as written, in
a VM, by the promotion drill (`platform/production/promotion-test.nix`, run with
`platform/production-promotion-check`, section 5): the production app role as a
planned replacement and the production relay role as a lost original, on
synthetic data, with the commands taken from this document. Where that run showed
the text was wrong, the text was corrected; each correction says "the promotion
drill showed". It has never been used with real data or on a production host
(none exists), and nothing in it has run on a real host, at a provider, with real
DNS or with a real escrow retrieval. Every step says
which rehearsal does the same action and at what level of evidence; where the
rehearsal does something different from what a promotion needs, the step says so
and the [traceability table](#3-traceability-rehearsal-evidence-per-step) repeats
it. The section [what nothing has exercised](#4-what-a-production-promotion-needs-that-nothing-has-exercised)
is the honest size of the gap. Do not read "the rehearsal passed" as "this
procedure is safe".

Terms, each defined once:

- **Original pair**: the production `app` host and `relay` host that are lost or
  being replaced. **Replacement pair**: the two new hosts of the same roles.
- **Target**: a configured backup repository (`primary`, optionally `secondary`).
  **Snapshot**: one restic backup run, identified by a hexadecimal ID, in one target.
- **Restore directory** (also "staging directory"): the empty directory
  `terrace-backup-restore` fills with the snapshot's files and a throwaway
  PostgreSQL cluster. Nothing in it is live.
- **Reconciliation**: the restore tool's comparison of the restored database row
  counts, file hashes and SQLite checks against the manifests written at backup
  time. It proves the restore equals what the backup captured; it cannot prove
  the backup was complete or recent.
- **Writer**: any service or timer that changes stored state (PostgreSQL,
  Keycloak, nginx, the CMS, the website, Headscale, backup timers, the sweep
  timer). **Adoption** (`terrace-production adopt`): the one command that starts
  the writers on a production host; until it runs, a boot starts none of them.
- **Secrets store** and **escrow**: the private directory of per-host encrypted
  secrets (`TERRACE_SECRETS_STORE`) and the offline age key that can decrypt it
  ([secrets](secrets.md)).
- **Fencing**: making sure the original cannot serve or write once the replacement
  does.
- **Promotion**: everything from the owner's decision to restore until the
  replacement is declared in service.

## 1. Scope, preconditions and the owner's decisions

### 1.1 When this applies

Use it when the original pair is lost, or is being replaced on purpose, and the
data must come from a backup. It does not apply to a first deployment (that is
[the cold start](production-profile.md#cold-start-of-a-new-deployment), which
uses `terrace-production adopt --new`), to restoring one service while the rest
keeps running (nothing in the repository does that, see section 4), or to
"testing" a restore: never restore over a live host to test
([backups](backups.md#restore-procedure)).

Two cases differ at the start only:

- **Planned replacement**: the original is reachable. Step 2 quiesces it and takes
  a final backup, and only then does step 3 fence it, so the data-loss window is
  small. This is the only case any rehearsal exercises.
- **Original lost**: no final backup is possible, so you skip step 2 and fence
  first (step 3). The newest scheduled snapshot is
  all there is: up to one backup interval of data is lost (the schedule is daily at
  03:15; the proposed 24 hour target is unmeasured) and that snapshot was taken
  without quiescing writers. Its database and media files can disagree
  ([backups](backups.md#what-runs-and-its-resource-bounds), "Consistency"; runbook 7.9 is
  "NOT YET BUILT"). No rehearsal has restored from such a snapshot.

### 1.2 What must exist before starting

| Item | Why | Where it lives |
| --- | --- | --- |
| A reachable target holding a snapshot of each role | The data | The `primary` target of the host profile (a Storage Box SFTP repository; none exists yet). One repository per host, so `latest` is unambiguous ([backups](backups.md#enabling-it-on-a-host)) |
| The repository password and the SFTP key for that target | Without the password the repository is unreadable for ever | In the secrets store as `backup/primary/repository-password` and `backup/primary/sftp-key`, and in no backup ([backups](backups.md#where-secrets-must-come-from), [secrets](secrets.md#inventory)). The Storage Box host key is pinned as the profile parameter `app.backup.primary.knownHosts` |
| The secrets store | Every secret of the replacement | A private directory outside this repository, with its own private remote ([secrets](secrets.md#where-things-live)) |
| The escrow age key (identity file for `SOPS_AGE_KEY_FILE`) and its public recipient | The only way to re-key the store for a new host | Owner's offline storage ([secrets](secrets.md#where-things-live)). If it is lost, see [secrets](secrets.md#escrow-key-lost-or-leaked): every secret must be regenerated, which breaks things this procedure relies on (below) |
| The same secret values, not regenerated ones | Keycloak and PostgreSQL keep their own copies (the database role password hash, the Keycloak client secrets); a regenerated database password does not change an existing role ([secrets](secrets.md#rotation), [owner runbook](owner-runbook.md) 9.9) | `rekey` re-encrypts the same values to the new host ([secrets](secrets.md#re-keying-for-a-replacement-host)). A regenerated store needs the rotation procedures, which no rehearsal has run after a restore |
| TLS inputs | nginx will not serve without a certificate | Manual mode: `tls/certificate` and `tls/key` in the store. ACME mode: the owner's CA choice, accepted terms, a contact address and DNS for every name pointing at the new host before adoption ([TLS](tls.md#enable-acme-on-a-host)) |
| The reviewed release pair and the private host module | The replacement must run the code that wrote the data | `platform/website-release.json` pin, the private host module and hardware module ([production profile](production-profile.md#building-a-host)). The PostgreSQL major version must be the same (the app profile pins PostgreSQL 18) |
| The same host name as the original | Snapshots record the host name and `terrace-backup` tags the series with it (`BACKUP_HOST`, `platform/backup.nix`) | The private host module |
| A way to build and install the replacement pair | The hosts | Not determined from the repository for a real provider. What exists: the production host build ([production profile](production-profile.md#building-a-host)) and `infra/nixos/rescue_install.py` (lease tooling for synthetic hosts, [Rescue installer](cloud-rescue-install.md)); the installer stages only the host key, not the encrypted secrets (gap G-11) |
| The public names and the relay's public address | Clients and members must reach the replacement | The profile parameters `app.*Hostname`, `relay.hostname`, `relay.publicIPv4`; DNS is the owner's ([TLS](tls.md#who-terminates-tls)). Nothing exercises changing them |
| A way to power off or delete the original | Fencing | Not determined from the repository. Provider access for production does not exist, and no tool here fences a production host |
| Operator tools on the operator machine | The commands below | `terrace-secrets` (with `sops`, `age`, `ssh-to-age`) from the dev shell ([secrets](secrets.md#operator-tool)) |

### 1.3 Decisions that are the owner's

The operator does not decide these. Each is a stop point: nothing after it runs
until the owner says go.

| Point | Decision | Needs to be known first |
| --- | --- | --- |
| D1, before step 2 (planned) or step 3 (original lost) | Declare the original dead, or authorise a planned replacement | Why the original cannot be recovered in place; who can power it off |
| D2, after step 7 | Accept the data-loss window | The chosen snapshot's time against now; for the unplanned case, that it was not quiesced; which member erasures were made after the snapshot time (section 4, "Erasure replay") |
| D3, after step 9 | Go or no-go to touch live paths | The reconciliation report (table names and counts, file counts) looks like the real thing |
| D2b, before step 15 | Erasure replay and public exposure (see "Erasure replay and the public-exposure conflict" below the table) | The list of erasures since the snapshot, and which of the options there the owner accepts |
| D4, before step 15 | Go or no-go to start the writers | Steps 10 to 14 done. After step 15 the replacement writes; this document has no tested way back (step 15) |
| D5, step 21 | Declare the replacement in service, cut public traffic over, and arm the sweep timer (`terrace-production arm-timers --in-service`; step 21 has armed the backup timers) | The checks of steps 20 and 21, and the erasure replay of D2b done |
| D6, after D5 | Retire the original's disk and state for good | Never earlier. Only the all-services scenario (`platform/full/scenario.py`, `stopped_guest`) keeps the retired original disks; the production VM check and the TLS VM test delete the original disk (`test.nix`, `tls-test.nix`), so a production promotion's retention of the original is your decision, not a rehearsed practice |

### Erasure replay and the public-exposure conflict (D2b)

A snapshot older than a member's erasure brings that person's data back, and
nothing in the system re-applies the erasure (details in section 4, "Erasure
replay"). Re-applying it needs the CMS administrator interface on the replacement,
after the migration gate is open (step 16). The conflict, from the code:

- Adoption (step 15) starts nginx, and the host firewall already allows TCP 443
  from anywhere (`platform/website-gateway.nix`, `networking.firewall.allowedTCPPorts`;
  the production app role opens 22 and 443, and 80 as well in ACME mode,
  `platform/production/tls.nix`). The gateway has **no maintenance or
  closed state**: its only refusals are 421 for an unknown host name and the
  application's own authorisation. From adoption the restored site can be reached
  by anyone who connects to the replacement's address with the right host name.
  Public DNS is therefore the practical switch for discovery, not a closed state.
- In ACME mode the certificate comes only through HTTP-01, which needs each public
  name to resolve to the replacement already (step 19; DNS-01 is not built,
  [TLS](tls.md#who-terminates-tls)). So in ACME mode the public names must point at
  the replacement, and the restored site is publicly reachable by name, before or
  while the erasures can be applied.
- How the administrator reaches the CMS before public DNS: the CMS listens on
  loopback only (port 13048 by default), the gateway serves its host name on 443,
  and the CMS configuration, its single sign-on issuer and the site origins all use
  the public host names (`platform/website.nix`, `platform/production/app.nix`).
  The only route the repository describes for reaching such a host without public
  DNS is the rehearsal design: an SSH loopback forward to 443 plus a local name
  mapping on the operator machine, trusting a private CA ([cloud rehearsal](cloud-rehearsal.md#keep-the-data-and-checks-private)).
  That is a described design only: lease 5 did not run browser checks, and its
  protocol steps ran on the host itself (`protocol()` in
  `infra/nixos/rehearsal_services.py`). What has run is name-mapped HTTPS from a
  separate client guest on the VM rehearsal's private test network
  (`scenario.py`, the `client` guest). Whether a CMS administrator can sign in
  through a forward with a production certificate has not been tried; a bare
  loopback forward to port 13048 without the names is **not determined from the
  repository** to work.

Options, each with its evidence:

1. **Manual certificate mode, public DNS unchanged until the erasures are
   re-applied.** The operator supplies a certificate valid for all public names
   (`terrace-secrets set app tls/certificate`, `tls/key`; supported, the default
   mode, [TLS](tls.md#two-modes)), reaches the replacement by SSH forward and name
   mapping, re-applies the erasures, and only then moves DNS. The administrator's
   browser must resolve both the CMS host name and the identity host name to the
   replacement (single sign-on goes through the identity host; the website host
   name is needed to see the site); that is part of the same mapping and is not
   exercised. This option assumes a **manual-mode replacement**: built with
   `tls.mode = "manual"` and with `tls/certificate` and `tls/key` in its store. A
   replacement created with the `acme-tls` feature has no such files and no manual
   mode; to use this option it must first be switched back by the sequence in
   [TLS, "Fall back to the manual path"](tls.md#fall-back-to-the-manual-path) (its
   order and checks are there, not restated here), or created without the feature.
   If the host is to use ACME afterwards, follow
   [TLS, "Switching a live manual host to ACME"](tls.md#switching-a-live-manual-host-to-acme)
   in its order: `terrace-tls prepare-acme` first and the store change last, because
   the last command drops the manual certificate and key, and run early it removes
   the only certificate the host can serve. `tls.md` records that live switch as
   proven on the relay only, not on the app role.
   Evidence, piece by piece: an operator-supplied (manual-mode) certificate: VM
   rehearsal (production VM check); name-mapped HTTPS from a client guest: VM
   rehearsal; an SSH loopback forward: a described design, not exercised anywhere;
   administrator sign-in through either: not exercised anywhere; the whole
   sequence on the production profile: not exercised anywhere. Residual exposure:
   port 443 stays open to anyone using the address and host name.
2. **ACME mode, accept a window.** DNS goes to the replacement, the certificate is
   issued, the erasures are re-applied through the public names. Erased people's
   data is served, or can be, from DNS cut-over until the last erasure completes.
   Evidence: ACME against a local test server only, VM rehearsal; the window itself:
   not exercised anywhere.
3. **Hold the public site closed while DNS points at it.** The repository has no
   such state. Stopping `nginx.service` would also stop the ACME challenge, which
   is served by the same nginx. Not supported.

If none of these is acceptable, the repository supports nothing else: until a way
to re-apply erasures before public exposure exists (the backlog records it), a
restore from a snapshot older than an erasure cannot be put in service without
briefly serving, or risking serving, erased data. Accepting that window (option 2,
or the residual exposure of option 1) or blocking the promotion until it is closed
is the owner's decision, D2b. Record it with the snapshot time and the erasure list.

## 2. The procedure

Order follows the rehearsal: quiesce and fence the original (in the order the case
needs, Part A), restore to a staging directory and reconcile, load into live paths
while nothing writes, start the writers once, migrate where a migration gate
exists, check health. Scheduled backups wait for the end: `adopt --restored` (step 15)
leaves the backup, check, freshness and sweep timers off; step 21 arms the backup
timers once the checks pass, and the sweep is armed at D5, after any erasure replay.
All commands run as root on the host named, unless a step says "operator machine".
`<...>` marks a value you supply. Never paste a command's output that could hold a
secret into a ticket or chat; the commands below send secret-bearing output to
files on purpose.

The `app` role is the production profile's app host (PostgreSQL 18, Keycloak, CMS,
website, gateway nginx). The `relay` role is the Headscale host. Documenso, Windmill
and Vaultwarden are **not part of the production profile**: it refuses them
([production profile](production-profile.md#decisions-waiting-on-the-owner)), and
K3s is off too. Step 18 therefore describes only what the rehearsal does with
them; a production procedure for them cannot exist until their modules are
allowed into a profile.

### Part A. Decide, then quiesce and fence the original

The order depends on the case. Do not fence a planned replacement before its final
backup: the final backup needs the original running.

- **Planned replacement** (original reachable): step 1, step 2 (quiesce and final
  backup), step 3 (fence), then step 4.
- **Original lost**: step 1, step 3 (fence), then step 4. Skip step 2.

**Step 1. Owner decision D1.** Record the time, who decided and why.

**Step 2. Planned replacement only: quiesce the original and take a final
backup.** On the original `app` host
([production profile](production-profile.md#host-replacement-rehearsed), as the
production VM check does, `platform/production/test.nix`), first stop the
original's own backup and check timers, then quiesce, back up and stop the
database:

```sh
systemctl stop terrace-backup.timer terrace-backup-check.timer
systemctl stop terrace-cms-sweep.timer terrace-cms-sweep.service terrace-web.service terrace-cms.service keycloak.service nginx.service
systemctl start terrace-backup.service
terrace-backup restic primary snapshots --json
systemctl stop postgresql.target postgresql.service
```

The first line is not in the production VM check or any rehearsal (READ: timers
are `wantedBy` the production target, `platform/production/base.nix`, and stopping
services does not stop them); it is here because the original's own scheduled
backup could otherwise run between your final backup and the fence, and end in a
prune of the repository you are about to restore from. Stopping the timers lasts
until the next boot of the original (see below).

On the original `relay`, first stop its backup and check timers too
(`systemctl stop terrace-backup.timer terrace-backup-check.timer`; the relay has no
sweep timer), then stop `nginx.service` and `headscale.service` and run the same backup (the rehearsal does: `quiesce_relay` in
`infra/nixos/rehearsal_services.py`). This disconnects Headscale clients until the
replacement is up.

**After this step the original is quiesced, not fenced.** Its boot gate is still
open: `terrace-production-boot` starts the production target at every boot of an
adopted host, and that target wants every writer and timer
(`platform/production/base.nix`, the `terrace-production` target and
`terrace-production-boot`). A reboot of the original, by anyone or
by the provider, restarts the writers and re-arms the backup, sweep and freshness
timers: the promotion drill showed it, booting the retired original's disk after
the promotion (item 6 of section 5). Fence it (step 3) before anything else touches it.

Worked: `systemctl show terrace-backup.service -p Result --value` prints `success`
(the backup log line is `target primary: backup succeeded in <n>s`), and
`terrace-backup age` exits 0. Note the snapshot ID of the newest `app` entry.
Failed: the unit's `Result` is not `success` and the log says `target primary:
backup FAILED` (`backup_target` in `platform/backup/backup.sh`); stop and do not
fence or destroy the original.

**Step 3. Fence the original.** For a planned replacement, only after step 2
worked; for a lost original, right after step 1. What the repository can and
cannot do:

- It cannot power off, delete or isolate a production host. No tool here does it,
  and no provider access exists. Do this at the provider (or physically) and write
  down when. If the original cannot be confirmed off, stop here and treat that as
  a no-go: a second live copy would write to the same backup repository with the
  same credentials.
- Re-keying the store to the replacement's new host key (step 4) keeps the old
  host key from reading the new ciphertext (asserted in the production VM check).
  It does not take back what the original already holds: it still has the SFTP
  key and the repository password, because `rekey` keeps the same values
  ([secrets](secrets.md#re-keying-for-a-replacement-host)).
- To cut a returning original off from the repository, rotate the SFTP key and
  the repository password after promotion and remove the old public key at the
  Storage Box ([secrets](secrets.md#revoke-after-a-host-compromise),
  [rotation](secrets.md#rotation)). The promotion drill ran both rotations for
  this purpose once, in a VM: afterwards the returning original's backup failed
  and changed nothing in the repository (step 21 has what it did before them).
- The replacement marker and repository check (step 6) stop the replacement being
  adopted as a new host. They do nothing about the original.
- Public traffic: pointing DNS at the replacement is the practical fence for
  users. It is part of step 21 and nothing exercises it.
- In the promotion drill the fence is the nearest a VM has: a power-off (`shutdown`
  for the planned app, an unclean `crash` for the lost relay) and the disk image
  moved aside (app, kept for the fencing check) or deleted (relay).

### Part B. Prepare the replacement pair (nothing writes yet)

**Step 4. Operator machine: create the replacement's identity and re-key the
store from escrow.** Set `TERRACE_SECRETS_STORE`, `TERRACE_ESCROW_RECIPIENT` and
`SOPS_AGE_KEY_FILE` as in [secrets](secrets.md#operator-tool), then:

```sh
terrace-secrets host create app --role app --replace
terrace-secrets rekey app
terrace-secrets check app
```

Repeat with `relay --role relay` for the relay. Pass the same `--feature` options
the original host had (`acme-tls`, `secondary-backup`; [TLS](tls.md#enable-acme-on-a-host)).
Worked: `host create` prints "Run `rekey` before pushing."; `check` passes. A `push`
before `rekey` is refused (asserted in the VM check). Failed: stop; with the
escrow key lost see [secrets](secrets.md#escrow-key-lost-or-leaked).

**Step 5. Install the replacement with the new host key staged, and let it boot
once.** How to do this on a real provider is **not determined from the
repository** (see the preconditions). Worked: the host runs the staged key, has no
secrets, and `terrace-production status` prints `not adopted: no writer starts at
boot` followed by `inactive` for the target.

**Step 6. Deliver the secrets and check the replacement guards.** Operator machine:

```sh
terrace-secrets push app --address <replacement address> --identity <operator ssh key>
terrace-secrets host unstage app
```

On the replacement:

```sh
terrace-secrets verify
test -e /var/lib/terrace-secrets/replacement
```

Worked: `terrace-secrets verify: PASS <n> secrets, <m> templates, root-only modes,
valid formats, ramfs.` and the second command succeeds (the marker `push`
delivers to a host made with `--replace`). While the marker is present,
`terrace-production adopt --new` refuses, and so it does when the repository
already holds a snapshot ("this is a replacement") or cannot be reached; it never
initialises a database ([production profile](production-profile.md#boot-gate-adopt-before-writers-start)).
Failed: fix the cause (host key pin, store recipients) and push again; do not
adopt. The VM check unstages before it pushes; `secrets.md` says push first. The
order does not matter to the tool, since `push` pins the stored public key.

### Part C. Choose the snapshot and restore to a staging directory

**Step 7. List snapshots and choose.** On the replacement (secrets delivered):

```sh
terrace-backup restic primary snapshots --json
```

Each entry has `id`, `time` and `hostname`. Use the newest entry for the original's
host name, by its ID, as the rehearsals do (`latest` also works, with `--tag
terrace` applied by the restore tool, but an ID is exact). Worked: the list is
non-empty and the newest `time` is what the owner expects. Failed: no snapshot
or an unreachable repository means no restore; try `--target secondary` if one is
configured (step 9). **D2** here: show the owner the snapshot time.

**Step 8. Optional but done by the rehearsal: read every byte of the repository.**

```sh
terrace-backup restic primary check --read-data
```

(`terrace-backup restic TARGET ARGS` passes the arguments to restic.) The
rehearsal runs `restic check --read-data` from the operator's side against the
repository directory, not from the replacement, and also requires the snapshot list
and the repository `config` bytes to be unchanged between backup and restore
(`offhost_verifier` in `infra/nixos/rehearsal_services.py`). The scheduled
`terrace-backup-check` runs only a 5 percent subset on production
(`check.readDataSubset`), while the rehearsals set 100 percent. Time on real data
is unmeasured. Failed (`restic` exits non-zero): do not restore from this
snapshot; try another or the other target.

**Step 9. Restore to the staging directory and reconcile.**

```sh
umask 077
terrace-backup-restore --target primary --snapshot <SNAPSHOT_ID> --dest /var/lib/terrace-restore >/var/tmp/restore.log 2>&1
grep -qx 'RECONCILIATION OK' /var/lib/terrace-restore/reconciliation.txt
```

The destination is `/var/lib/terrace-restore` because `terrace-production adopt
--restored` reads exactly that file (`restoreMarker` in
`platform/production/base.nix`). Do **not** add `--keep-running`: the isolated
database must be stopped before its directory is copied in step 12, and a failed
restore stops it anyway. The tool refuses a destination that exists and is not
empty, is `/`, or overlaps a live path (`check_dest` in `platform/backup/restore.sh`).
It sends no mail and starts no application. Output of role restore goes to root-only
files in the directory, not the terminal.

Worked: exit status 0, the `grep` succeeds, and `reconciliation.txt` ends with
`RECONCILIATION OK` after one `OK database ...`, `OK file set ...` and, on the
relay, `OK sqlite headscale: integrity ok, table row counts match` line per item
(table names and counts only). Failed:
`RECONCILIATION FAILED: <n> mismatch(es)` and exit 1. Stop. Nothing live was
touched. Diagnostics are root-only files in the restore directory
(`restore-<db>.err`, `globals.err`, `diff-*.txt`, `hashfail-*.txt`). Try another
snapshot or `--target secondary`. The tool refuses to restore into a non-empty
directory (`check_dest`), so a retry at the same path needs the failed directory
gone. The repository gives no command for that. Reading `restore.sh`, the
directory is referenced by nothing live: the tool's own PROTECTED_PATHS do not
include it, the only external reader is `terrace-production adopt --restored`,
which reads its `reconciliation.txt`, and a failed run stops its isolated database
itself (`cleanup_db`). So the sequence is: (1) confirm
no isolated cluster is running, with `terrace-backup-restore --stop
/var/lib/terrace-restore` (it errors harmlessly if none runs; only a restore that
succeeded with `--keep-running`, or a crash, can leave one); (2) remove the
directory (`rm -rf /var/lib/terrace-restore`); (3) restore again. The promotion
drill ran this on the app replacement, after a restore of a deliberately
damaged snapshot had failed (`pg_restore failed` in the log, no `RECONCILIATION
OK`, no isolated database left running, host still unadopted): a retry into the
failed directory was refused (`destination exists and is not empty`), (1) exited
1 and removed nothing, and after (2) the chosen snapshot restored and reconciled. Never remove it
after a *successful* restore and before adoption has finished: adoption needs its
`reconciliation.txt`, and removing it deletes the file the copy steps read.

Relay: the same command on the replacement relay.

**D3.** The owner reads `reconciliation.txt` (it holds no row data) and says go or
no-go. Reconciliation is only as good as the backup's own manifests.

### Part D. Load into live paths while nothing writes

**Step 10. Confirm every writer is still off.**

```sh
terrace-production status
```

Worked: `not adopted: no writer starts at boot` then `inactive`. Failed: any
other state means something started; find out what before copying anything.

**Step 11. Copy the restored file sets into place.** For each path, check that the
target is empty or absent and not a symlink, then copy with ownership
(`platform/production/test.nix`, `cp -a`; `COPY_RESTORED` in
`infra/nixos/rehearsal_services.py`):

```sh
test ! -L <path> && { test ! -e <path> || test -z "$(ls -A <path>)"; }
mkdir -p <path> && cp -a /var/lib/terrace-restore/files<path>/. <path>/
```

Paths on the production `app` role: `/var/lib/terrace-cms-media` and
`/var/lib/terrace-cms-outbox`. The website input files are **not** restored from
the backup on production (they are sops templates; the module backs
`website-inputs` up only when `inputFiles` is null, `platform/website.nix`), and
`/var/lib/acme` is deliberately not in any backup ([TLS](tls.md#backup-deliberately-excluded)).
Numeric owners are restored as they were: the CMS and website accounts have static
IDs 44121 and 44122 for this reason, and they are the only accounts whose numeric
IDs the repository fixes (`platform/production/app.nix`; Windmill and Vaultwarden
have a `uid` option, but only on the rehearsal profile). The PostgreSQL directory is
chowned explicitly in step 12; the Headscale files in step 13 are not. Worked: no error, then
`test "$(stat -c %U:%a /var/lib/terrace-cms-outbox)" = terrace_cms:700` (the
rehearsal's `ownership` step also checks the two invitation files are
`terrace_cms:600`). Failed: a non-empty target means the host already holds state;
stop, do not overwrite.

**Step 12. Put the restored PostgreSQL cluster in place (app role).** The
rehearsals do not load dumps into a running cluster. They copy the restored,
stopped cluster directory over an empty data directory:

```sh
pg_dir=/var/lib/postgresql/18
test ! -L "$pg_dir" && { test ! -e "$pg_dir" || test -z "$(ls -A "$pg_dir")"; }
install -d -m 0700 -o postgres -g postgres "$pg_dir" && cp -a /var/lib/terrace-restore/pgdata/. "$pg_dir"/ && chown -R postgres:postgres "$pg_dir"
```

The rehearsals read `pg_dir` from `/etc/rehearsal-pg-directory`, a file only the
rehearsal harness installs; on a production host it is the PostgreSQL module's
data directory, which the production VM check asserts is `/var/lib/postgresql/18`.
The host's own configuration replaces the restored `postgresql.conf` and uses the
declared native access rules when PostgreSQL starts ([full rehearsal](full-rehearsal.md)).
The restored cluster keeps the original's bootstrap role name and every role
password hash, grant and role setting ([PostgreSQL bootstrap](postgresql-bootstrap-restore.md)).
This replaces a whole cluster on an empty host. **Loading one database into a
cluster that already holds data is not done anywhere** (see the partial-restore item in section 4).
Worked: the copy completes. Failed: stop; the data directory was not empty or the
copy was cut short; do not start PostgreSQL on a half-copied directory.

**Step 13. Relay role: restore Headscale's keys and database before its first
start.** Headscale makes new private keys if it starts without them, and clients
then lose their network identity ([secrets](secrets.md#inventory), "Network
identity"). The gate keeps it off until adoption; the copy refuses a directory
that already holds files, so a Headscale that already started would stop this step
(`COPY_RESTORED`). On the replacement relay:

```sh
test ! -L /var/lib/headscale && { test ! -e /var/lib/headscale || test -z "$(ls -A /var/lib/headscale)"; }
mkdir -p /var/lib/headscale && cp -a /var/lib/terrace-restore/files/var/lib/headscale/. /var/lib/headscale/
install -m 0600 -o headscale -g headscale /var/lib/terrace-restore/files/var/lib/terrace-backup/dumps/sqlite/headscale.sqlite /var/lib/headscale/db.sqlite
```

The database file is installed separately because the backup excludes the live
`db.sqlite*` from the file set and stores a consistent copy instead
(`platform/backup.nix`, `snapshot_headscale` in `platform/backup/backup.sh`).
Worked: `sqlite3 /var/lib/headscale/db.sqlite "PRAGMA integrity_check"` prints
`ok`; the rehearsal also compares `sha256sum` of `noise_private.key` and
`derp_server_private.key` with the original's (reconciliation already compared them
to the backup's manifest). Failed: stop. Without the keys or the database, do not
start Headscale; a fresh start would create a new identity.

Ownership: only `db.sqlite` is installed with the Headscale owner; the copied key
files keep the numeric owner `cp -a` gives them from the backup, and the rehearsals
do nothing more about it (they rely on identical closures giving identical
IDs). The repository fixes numeric IDs for no account except the CMS and website
accounts (`platform/production/app.nix`), so for the relay the operator should
check, before adoption: `stat -c %U:%G /var/lib/headscale /var/lib/headscale/noise_private.key /var/lib/headscale/derp_server_private.key`
shows `headscale:headscale`, and if not, `chown -R headscale:headscale
/var/lib/headscale`. The promotion drill ran this check on the relay replacement
(it printed `headscale:headscale` three times, so no `chown` was needed); the
`chown` itself has not been run.

**Step 14. Optional check of the restored database's roles, before any writer
starts.** The rehearsal starts PostgreSQL alone and checks its identity and
isolation (`ADOPT_POSTGRES` in `infra/nixos/rehearsal_services.py`):

```sh
systemctl start postgresql.target
runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d postgres -c "select rolsuper and rolcanlogin from pg_authid where oid=10 and rolname='postgres'"   # t
runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d postgres -c "select count(*) from pg_roles where rolname='terrace-restore'"   # 0
runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d postgres -c "show fsync"   # on
runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d postgres -c "show hba_file"   # a /nix/store path
runuser -u terrace-restore -- psql -X -w -h /run/postgresql -U postgres -d postgres -c 'select 1'   # must FAIL
```

The production VM check does not run these (it lets adoption start PostgreSQL).
The promotion drill ran this block as written on the production app role before
adoption, and each answer was the one in its comment; the following `adopt
--restored` then succeeded with PostgreSQL already running. If you skip this step,
adoption starts it. Failed: any wrong answer means do
not adopt; the cluster is not what the backup captured.

### Part E. Start the writers once, migrate, check

**Step 15. D2b and D4 decided, then adopt.**

```sh
terrace-production adopt --restored
```

It refuses unless `/var/lib/terrace-restore/reconciliation.txt` has the line
`RECONCILIATION OK` and the host's state witness exists (`PG_VERSION` in the
PostgreSQL data directory on the app; `db.sqlite` on the relay). That is all it
checks: it does not check that the media and outbox were copied, that the
reconciled restore is the one you copied, or how old the snapshot is. It then runs
`terrace-secrets verify`, writes the hold markers
`/var/lib/terrace-production/timers-held` (backup timers) and, on the app,
`/var/lib/terrace-production/service-timers-held` (sweep), starts every writer and
every timer that is not held, waits a 15 second stability window, and writes the adopted marker only
if everything is running (the CMS and website may be skipped by their migration
gate). Worked:

```text
Adopted: the writers start now and at every boot.
Held until you have verified the restored services: terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer (they stay off across reboots).
Arm them with: terrace-production arm-timers
Held until the replacement is declared in service, after any erasure replay: terrace-cms-sweep.timer (they stay off across reboots).
Arm it then with: terrace-production arm-timers --in-service
```

preceded by the `terrace-secrets verify: PASS <n> secrets, <m> templates,
root-only modes, valid formats, ramfs.` line, because `adopt` runs `verify` first
(on the relay the verify line and the first three lines of the block). The
promotion drill showed both: an earlier version of this step left out the verify
line and said the relay prints only the first two lines. `terrace-production
status` prints `adopted <time>` and one line per held group. It also removes the replacement marker.
Failed: it names what is not running and exits non-zero; fix the cause and run
**the same command again** (it resumes). Next, from the operator machine, a later
`terrace-secrets push` clears the store's own replacement marker (asserted in the VM
check).

**The backup, check and freshness timers stay off from here to step 21, and the
sweep until D5** (`restoreHeldTimers` and, for the sweep,
`restoreServiceHeldTimers` in `platform/production/app.nix` and `relay.nix`; the
conditions and the `adopt`/`arm-timers` commands are in
`platform/production/base.nix`). The hold markers are written before `adopt` starts
the production target, and each held timer has `ConditionPathExists=!` on its
group's marker, so no start of the target arms them while the marker exists: not this adoption, not
a failed adoption you then resume (it writes the marker again before starting the
target), and not a reboot of the replacement. The adoption's own completeness check
does not wait for the held timers. Until you arm them nothing schedules a backup:
every backup run ends in `forget --prune` against the repository the restore came
from (`platform/backup/backup.sh`; what that can remove is in step 21), and the
sweep (03:30 daily once armed) would act on the restored data as soon as the
migration gate opens. Holding the sweep until D5 keeps it from running on restored
data before the erasure replay of step 16 and before you have checked the data; it
does not itself protect any erasure: the sweep expires stale invitations, withdraws unreviewed applications and completes
pending outbox redactions, the same things the original's sweep would have done,
and it never erases a member by itself (READ: website `docs/member-lifecycle.md`
section 9 at `dd1aef7`). The manual sweep run in step 16 performs the same actions
anyway. Starting a held timer by hand (`systemctl start terrace-backup.timer`) is
skipped by the same condition; do not remove a marker by hand, use step 21 and D5.
Asserted in the production VM check ("replacement" subtest; passed at `9664be1`, 2026-11-24): a first adoption that
fails on nginx and its resumed run both leave the four timers inactive (condition
not met), a second start of the target and of the backup timer leaves them so, the
identity health timer is armed, and `status` reports both held groups. The relay's
held timers are not in that check; the promotion drill (2026-11-25) asserts
them on a restored relay: held after `adopt --restored` and after a reboot, with
`status` naming the group, then armed by `arm-timers` and still armed after a
second reboot.

**There is no documented way back after this step.** The snapshot still exists in
the repository and the original's disk exists if you kept it, so a second
restore onto a fresh replacement is possible. How to stop a running replacement for
good is not described in the repository: **not determined from the repository**.
Reading `base.nix`, the adopted marker `/var/lib/terrace-production/adopted` is the
only condition on the boot start, so removing it would stop writers starting at the
next boot; that is READ only and not tried.

**Step 16. Run the CMS migration gate and start the CMS and website.** The marker
`/var/lib/terrace-cms/migrated-revision` is not in any backup, so a restored host
starts with the gate closed (the CMS and website are skipped, not failed):

Read the ledger first (PostgreSQL is running after adoption), keep the output:

```sh
runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d terrace_cms -c 'select name||batch from payload_migrations order by id'
systemctl start terrace-cms-migrate.service
systemctl start terrace-cms.service terrace-web.service
runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d terrace_cms -c 'select name||batch from payload_migrations order by id'
```

Use the same release pair the data was written by. Worked: both units succeed and
the two ledger readings are identical (only the production VM check compares a
ledger across this step, `test.nix`; see the traceability table). Failed: a changed
ledger means the replacement runs a different release than the original; stop and
decide with the owner.

`terrace-cms-sweep.timer` stays held (step 15) until D5; the production VM check
asserts that it and the three backup timers are inactive with their start condition
not met right after the migration and the start of the CMS and website, just before
it compares state (passed at `9664be1`, 2026-11-24).

**Step 16, continued: erasure replay (D2b), only now.** It can start only after the
gate is open and the CMS is running, and before D5. For each person the owner lists
as erased since the snapshot's time (READ: website `docs/member-lifecycle.md`
sections 5 and 8 at `dd1aef7`, `cms/src/lifecycle/erasure.ts`):

The administrator signs in through single sign-on, so the browser must resolve the
CMS host name and the identity host name to the replacement (the same name mapping
as D2b, option 1, or public DNS); not exercised.

1. In the CMS administrator interface, create a **Member offboardings** record for
   the member. An administrator-initiated erasure is only possible for a member who
   is already offboarded. A member-initiated erasure (the person's own two-step
   request) cannot be replayed as such.
2. Create a **Data requests** record of kind `erasure`. The erasure runs inside that
   save, in one database transaction: it deletes the profile, mentorship offer,
   group memberships and stories, anonymises requests, scrubs applications,
   decision notes and outbox recipients, revokes pending invitations and turns the
   member row into a tombstone.
3. Complete the outbox files, because the administrator save cannot redact files
   itself. The sweep is started by hand while its timer stays held:

```sh
systemctl start terrace-cms-sweep.service
```

4. Close the realm side: the erased person's Keycloak account must be disabled. The
   website's own tool for that is `provision-member.ts offboard` (website
   repository, not in this one). Whether `terrace-identity-admin user disable`
   covers member accounts is **not determined from the repository**
   (`docs/identity-admin.md` describes it for editorial site accounts and says
   member accounts follow the website's provisioning).

Worked: the erasure response or the Data requests record shows counts and no
`mirror: pending`; the sweep service succeeds. Not exercised anywhere: no
rehearsal replays an erasure on a restored host (the rehearsals erase two
synthetic members after their restore, which is a different thing).

**Step 17. Identity (Keycloak).** Nothing is imported over the restored realm:
Keycloak's startup import creates a missing realm and skips an existing one
([identity host](identity-host.md)). Worked: `systemctl is-active keycloak.service`
is `active`; the realm ID, admin-events flags and user list equal the original's
where you have a record of them (the VM check compares them); `systemctl --failed`
shows none of the `terrace-identity-*` units. The independent administrator access
(SSH key, no single sign-on) is the break-glass path in
[identity administration](identity-admin.md#7-break-glass-no-administrator-no-sso-no-proxy-g-40);
it has not been exercised after a restore ("a restore of the Keycloak database after
any of these commands" is listed as not covered there).

**Step 18. Documenso, Windmill and Vaultwarden: rehearsal behaviour only.** The
production profile does not run them. On the rehearsal profile, after the restore
the order is: restore their input and state directories and databases (the same
steps 9, 11 and 12; they are in the backup set through
`extraPaths`, `platform/documenso.nix`, `platform/windmill.nix`,
`platform/vaultwarden.nix`), start PostgreSQL, then the units below
(`BOOTSTRAP_APP`, `START_APPLICATIONS` in `infra/nixos/rehearsal_services.py`):

```sh
# after PostgreSQL, Keycloak, nginx, the CMS migration and the CMS are up (BOOTSTRAP_APP):
systemctl start terrace-documenso-migrate.service
systemctl start terrace-documenso-bootstrap.service
systemctl start terrace-documenso.service
curl --fail --silent --max-time 5 http://127.0.0.1:13050/api/health | jq -e '.checks.database.status == "ok"'
systemctl start terrace-vaultwarden-migrate.service
systemctl start terrace-windmill-migrate.service
# then the Windmill role-boundary check (WIND_ROLES), then START_APPLICATIONS:
systemctl start podman-terrace-windmill-server.service podman-terrace-windmill-worker.service
curl --fail --silent --max-time 5 http://127.0.0.1:18001/api/version
systemctl is-active --quiet podman-terrace-windmill-worker.service
systemctl start vaultwarden.service
systemctl start terrace-vaultwarden-bootstrap.service
curl --fail --silent --max-time 5 http://127.0.0.1:18222/alive
```

Constraints that the code and docs state:

- **Documenso keys.** `secrets.json` holds `authSecret`, `encryptionKey`,
  `encryptionSecondaryKey` and the rest; changing the encryption keys can make
  stored data unreadable, so they come back from the backup (`/var/lib/terrace-documenso-inputs`)
  and are never regenerated ([Documenso host](documenso-host.md)). Bootstrap
  reconciles the approved accounts and revokes sessions of changed ones; review who
  can sign in before restoring an existing database (same document).
- **Windmill encryption key.** The only statement in the repository is that
  changing Windmill's encryption key alone can make stored resources unreadable
  ([migration](migration.md), [owner runbook](owner-runbook.md) 9.9). Where that
  key lives and how it is delivered are **not determined from the repository**
  (looked at: `platform/windmill.nix`, `docs/windmill-host.md`, `docs/migration.md`;
  the module declares no encryption-key option, and its database holds the workspace secrets per `docs/windmill-host.md`).
  The rehearsal restores the database, `/var/lib/terrace-windmill-inputs` and
  `/var/lib/terrace-windmill` unchanged and changes no key.
- **Vaultwarden.** `rsa_key.pem` and attachments live under `/var/lib/vaultwarden`
  and the inputs under `/var/lib/terrace-vaultwarden-inputs`, all restored. Users'
  master passwords are never escrowed; the rehearsal decrypts the vault with the
  password kept on a separate test device.

**Step 19. TLS and certificate authority material.** In the rehearsal the
operator tooling creates a synthetic certificate authority and supplies the
certificate, key, CA file and repository password again, checked by digest
(`install_runtime`, `INSTALL_INPUT`); none of that is restored from the backup. On
production:

- Manual mode: `tls/certificate` and `tls/key` come with the re-keyed store
  (step 6) and nginx loads them as credentials. The certificate must cover every
  public name and not be near expiry. Worked: `terrace-tls check` exits 0
  ([TLS](tls.md#check-status)).
- ACME mode: the replacement registers a new account and orders a new certificate
  after adoption; until the order succeeds nginx serves a placeholder that clients
  reject. HTTP-01 needs each public name to already resolve to the replacement, so
  DNS (step 21) and adoption are tied together (and see D2b above for what that
  does to erasure replay), and the CA's rate limits apply to retries (`tls.md` quotes Let's Encrypt's published
  figure of 5 certificates a week for the same set of names as of its writing; check
  the CA's current page). Worked:
  `terrace-tls status` shows an issued certificate and `terrace-tls check` exits 0.

Apps that trust a private CA (the rehearsal's `identityCaFile`) need none on
production: the production profile refuses extra trust anchors.

**Step 20. Health checks that exist on a production host.**

```sh
systemctl --failed
terrace-tls check
terrace-secrets verify
terrace-backup age
```

Worked: no failed units (`0 loaded units listed.`; the freshness unit is held
and has not run, see below), every public name passes
`terrace-tls check`, `verify` passes. `terrace-backup age` exits 1 until the first
backup of the replacement (step 21 takes it; the timers are held since step 15).
The freshness timer is held too, so `terrace-backup-freshness.service` has not run
and is not failed; started by hand before the first backup it would fail ("no
successful backup recorded").
The rehearsal's functional checks (198 HTTPS protocol assertions, the website
lifecycle steps, the browser checks) use scripts that only the rehearsal harness
installs (`/etc/gateway-protocol.py`); no equivalent is installed on a production
host, so the production health check after a promotion is thinner than the
rehearsal's. Alerting that reaches a person does not exist (G-23): a writer that
fails later shows only in `systemctl --failed`.

### Part F. First backup and timers, then declare

**Step 21. Take the first backup by hand, then arm the backup timers.** Adoption
(step 15) left the backup, check, freshness and (app) sweep timers off. The backup
state files on the replacement start empty, so the freshness unit fails until one
backup succeeds (shown for a newly adopted host, not for a replacement, in
`platform/production/tls-test.nix`). Do **not** run `terrace-backup init primary`:
the repository exists, and `init` is only for a first deployment. When steps 16 to
20 have passed, take a backup and check it, then arm the backup timers:

```sh
systemctl start terrace-backup.service
terrace-backup age
systemctl start terrace-backup-freshness.service
terrace-backup restic primary snapshots --json
terrace-production arm-timers
```

Worked: the service succeeds, `terrace-backup age` exits 0, the freshness unit
passes, a new snapshot appears under the same host name
(`platform/production/tls-test.nix` takes such a backup on the replacement into
the same repository and checks that it succeeds; nothing there checks the series or
the retention), and `arm-timers` prints on the app

```text
Armed: terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer.
Still held until the replacement is declared in service: terrace-cms-sweep.timer. Arm with: terrace-production arm-timers --in-service
```

and on the relay the `Armed:` line and `Every writer timer is active now and at
every boot.` Repeated, it prints `Already armed: nothing to arm.` and the same
second line; it fails, naming the timer, if one does not become active. From now
on the backup timers start at every boot as on any adopted host; the sweep stays
held. The production VM check (passed at `9664be1`, 2026-11-24) takes the backup by hand, refuses
`arm-timers --in-service` before it, runs `arm-timers` (both
outputs), reboots the replacement and checks that the backup timers came back
armed and the sweep still held.

**Nothing alerts on a replacement whose timers are never armed.** The hold markers
never expire, and the freshness timer that would report a missing backup is itself
held, so a replacement adopted with `--restored` on which `arm-timers` is never run
**never takes a scheduled backup and nothing says so**: only `terrace-production
status` shows it (one line per held group). Run `arm-timers` at this step, check
`status` afterwards, and check it again at D5. Alerting that reaches a person does
not exist (G-23).

What a backup run can remove. READ from `platform/backup/backup.sh` and the
retention options in `platform/backup.nix`, not tried across a promotion. The run
does `restic backup --tag terrace --host <hostname>` and then `forget --tag
terrace --group-by host,tags --prune` with keep-last 3, daily 7, weekly 4, monthly
6 (restic's rules, summarised in [backups](backups.md#retention)). Everything in
the repository with the same host name and tag is one group, whoever wrote it.
Per run, the group gains one snapshot and loses the ones outside the keep rules.
Consequences:

- A snapshot that was the newest when you restored from it is among the 3 newest
  after the next two runs, so under the default settings one or two backups cannot
  remove it. The promotion drill showed this on the app replacement: still listed
  after step 21's backup and one more, and removed by the third run, which fell on
  the same day (keep-last 3; it was no longer the newest of its day, week or month).
  On different days keep-daily keeps it longer. If you restored from an older snapshot because the newest was bad, it
  is already not among the newest: one more run can push it out, unless it is the
  kept newest snapshot of its day, week or month. Settings lower than the defaults
  change this; the effect of a changed policy is **not determined from the
  repository**.
- What a backup from a half-restored replacement certainly does is add a snapshot
  that is incomplete (for example an empty media directory) to the group, and
  prune older snapshots outside the windows: the very fallbacks you would use if
  this promotion turns out wrong. That is why the timer stays held until now.
- The same applies to a returning original, which has the same host name and
  credentials. The promotion drill booted the retired original's disk after the
  promotion: its boot gate started its writers and re-armed every timer, a
  `terrace-secrets push` to it was refused (the store pins the replacement's host
  key), and its backup run succeeded into the repository, became the newest
  snapshot of the host name (so `latest` would now restore its stale data) and
  pruned one of the replacement's snapshots. After the repository password and
  the SFTP key were rotated as [secrets](secrets.md#rotation) says (old public key
  removed at the repository), its backup failed and the snapshot list did not
  change. A prune by it while you are restoring could remove a snapshot you
  chose: whether restic refuses to prune while your restore holds a lock on the
  repository is restic behaviour, **not determined from the repository**. Fence
  first (step 3).
- With a different host name the replacement's snapshots are a separate group, and
  nobody ever prunes the original's.
- There is no pin-a-snapshot option in this tooling; whether tagging a snapshot
  differently from `terrace` would protect it from this `forget` (which filters on
  `--tag terrace`) is untested and changes its ID: **not determined from the
  repository**. Record the ID restored from and let the owner decide.

Never let the original run a backup after promotion.

Only now is the replacement ready to be declared in service. **D5**: the owner
declares it when steps 15 to 21 passed, D2b is settled (the erasure replay of step
16 is done) and public traffic has been moved. Then arm the sweep and end the
procedure by checking that every timer is active. `arm-timers --in-service` refuses
while the backup timers are still held (`the backup timers are still held (...); run
terrace-production arm-timers first, after the first backup by hand (restore
promotion step 21); nothing armed`), so step 21 cannot be skipped. App host:

```sh
terrace-production arm-timers --in-service
terrace-production status
systemctl is-active terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer terrace-cms-sweep.timer
systemctl list-timers
```

Relay (no sweep timer; `arm-timers --in-service` says `This role holds no timer
until service (it has no sweep).` and does not fail):

```sh
terrace-production status
systemctl is-active terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer
```

Worked: `arm-timers --in-service` prints `Armed: terrace-cms-sweep.timer.` and
`Every writer timer is active now and at every boot.` (repeated: `Already armed:
nothing to arm.` and the same line), `status` names no held timers, `active` once
per unit named, and `list-timers` shows a next run for each. Timers you forget to
arm stay off for good (across reboots, by design) and raise no alert: no alerting
exists (G-23), so this check is the only safeguard. The production VM check
(passed at `9664be1`, 2026-11-24) asserts both outputs, the four app timers active and `status` naming nothing held on the
replacement, and the relay's `--in-service` output on an adopted relay (not a
restored one). The promotion drill ran both blocks as written, on a restored app
and a restored relay, and asserts the outputs above, including the relay's
`--in-service` answer and a next run for each app timer in `list-timers`.
The rehearsals end with every guest stopped; none has a "declared in service" point,
and DNS cut-over, member communication and the retirement of the original (D6) are
outside every test.

## 3. Traceability: rehearsal evidence per step

Evidence levels are exactly: **VM rehearsal (synthetic data)**; **also on
temporary Hetzner hosts once (synthetic data; ...)** with the lease 5 limits;
**not exercised anywhere**. Lease 5 (run `efc9794a`, 2026-11-20, staged at
`bb0d526`) ran the rehearsal profile on two pairs of temporary Hetzner hosts with
public addresses and operator SSH ingress only; the backup repository was a
directory on the build machine reached through a reverse SSH forward; the
certificate authority, TLS files and repository password were supplied again by
the operator tooling, not restored from backup; Documenso was migrated,
bootstrapped, started and health-checked, Windmill and Vaultwarden were checked for
liveness only; nothing of the production profile, ACME or the identity
administration tool ran on a provider ([cloud rehearsal](cloud-rehearsal.md#lease-5-result-2026-11-20)).
`infra/nixos/rehearsal_services.py`, the backup tools and `platform/production/base.nix`
are unchanged between `bb0d526` and trunk; `platform/website.nix` is not. "Production
VM check" is `platform/production/test.nix` (production profile, instance
`rehearsal`, app role restored, relay role booted only, guests on a private test
network).

The promotion drill (`platform/production/promotion-test.nix`, section 5) adds
one more level, given last in the evidence column: **VM drill of this procedure**
(2026-11-25; production profile for both roles, synthetic data, the document's
commands; the runs and their results are listed in [the backlog](backlog.md#restore-promotion-follow-ups-g-22-recorded-2026-11-24);
a few passing runs are not a failure rate).

| Step | Rehearsal code that does the equivalent | What the rehearsal asserts | Evidence, and how it differs from a promotion |
| --- | --- | --- | --- |
| 2. Quiesce and final backup | `quiesce_writers` and `backup` in `scenario.py`; `quiesce`, `STOP_APP` and `original()` in `rehearsal_services.py`; the "back up, destroy the host" subtest in `platform/production/test.nix` | Every writer inactive (`ActiveState`, `MainPID`, `ControlPID`), K3s runtime gone, PostgreSQL left up only for the dump, backup succeeds, signature equal before and after | VM rehearsal (synthetic data); also on temporary Hetzner hosts once (synthetic data; rehearsal profile, `writersStopped` true; backup repository a directory on the build machine). A final backup while the original's own backup timer is stopped, and the original rebooting after quiesce: not exercised anywhere. A restore from a scheduled snapshot taken during writes: not exercised anywhere. **VM drill of this procedure**: the block as written on the production app role, with the original's backup and check timers stopped first; its reboot after quiesce (writers back, every timer re-armed) shown after the promotion (item 6). Relay: not run (the drill's relay is the lost case) |
| 3. Original absent | `stopped_guest` in `platform/full/scenario.py`; `check_original_absence` and the `original-absence-*.json` receipt in `infra/nixos/rehearsal_services.py` | Each original guest's QEMU PID and start time are absent before its disk is retired; on Hetzner, absence receipts for the original servers and addresses (three reads), baseline preserved, replacement IDs disjoint | Guest absence: VM rehearsal (synthetic data). Provider absence: also on temporary Hetzner hosts once (synthetic data; the controller deleted run-owned servers it created; no production fence of an unreachable host). Fencing a host you cannot reach, or one that comes back: not exercised anywhere. **VM drill of this procedure**: app powered off and its disk moved aside; relay crashed and its disk deleted. No provider, no fencing of an unreachable host |
| 3. Cut a returning original off the repository (rotate SFTP key and password) | none | none | Repository password rotation: VM rehearsal (synthetic data) in the production VM check, for the running host only. SFTP key rotation and use as a fence: not exercised anywhere. **VM drill of this procedure**: both rotations run from the replacement, then the retired original booted: before them its backup succeeded and pruned one replacement snapshot; after them it failed and changed nothing |
| 4. `host create --replace`, `rekey`, `check` | "back up, destroy the host, re-key from escrow" subtest, `platform/production/test.nix` | Values identical after re-key (hash), recipients exactly the new host and escrow, the retired key cannot decrypt, `push` refused before `rekey` | VM rehearsal (synthetic data), app role only. Relay role: not exercised anywhere. Lease 5 did not use this path (operator-supplied inputs on the rehearsal profile). **VM drill of this procedure**: both roles, the commands as written (relay with `relay --role relay`); same values (hash), `push` before `rekey` refused |
| 5. Install and first boot | `first_boot_without_secrets` in `test.nix`; on Hetzner `infra/nixos/rescue_install.py` | Host runs the staged key, no secrets, writers inactive, boot gate closed | First boot gated: VM rehearsal (synthetic data). Installing the production profile on a provider: not exercised anywhere (lease 5 installed the rehearsal profile). **VM drill of this procedure**: both roles: staged key, no secrets, `status` not adopted and `inactive` |
| 6. `push`, `verify`, replacement guards | "replacement: new key, push, guards, restore" subtest, `test.nix` | `verify` PASS; `adopt --new` refused by the marker and, with the marker removed, by existing snapshots; the refused attempts initialise nothing | VM rehearsal (synthetic data), app role only. Relay replacement: not exercised anywhere. **VM drill of this procedure**: both roles: `push`, `unstage`, `verify` PASS line, marker present, `adopt --new` refused |
| 7. Snapshot list | `check_snapshots` in `rehearsal_services.py`; the snapshot choice in `test.nix` | Exactly one snapshot per host, distinct valid IDs | VM rehearsal (synthetic data); also on temporary Hetzner hosts once (synthetic data; builder-directory repository, not a Storage Box or B2 account). A real Storage Box: not exercised anywhere. **VM drill of this procedure**: both roles: newest entry is the final backup (app) or the snapshot taken while it ran (relay) |
| 8. Full repository read | `offhost_verifier` in `rehearsal_services.py`; `fullEncryptedCheck` in `scenario.py` | `restic check --read-data` passes; the repository `config` is not parseable (encrypted); after restore the snapshot list and config hash equal those recorded before. Run by the operator side, not on the replacement host; the VM sets the check to 100 percent where production defaults to 5 percent | VM rehearsal (synthetic data); also on temporary Hetzner hosts once (synthetic data). **VM drill of this procedure**: both roles, run on the replacement host itself |
| 9. Restore and reconcile | `RESTORE` in `rehearsal_services.py`; `fresh_restore` in `scenario.py`; subtest in `test.nix` | `RECONCILIATION OK` in `reconciliation.txt`; database counts, file hashes, SQLite integrity; the failure path (damaged dump, tampered file) is in `platform/backup-test.nix`, not in a promotion | VM rehearsal (synthetic data); also on temporary Hetzner hosts once (synthetic data). Differences: the rehearsal profile restores into `/var/lib/rehearsal-recovery`; the production check uses `/var/lib/terrace-restore`; the data is small (the backup VM test's dataset is a few hundred kilobytes, [backups](backups.md#what-the-vm-test-proves-and-what-it-does-not); the repository does not state the all-services rehearsal's data volume); time and size on real data unmeasured. **VM drill of this procedure**: both roles into `/var/lib/terrace-restore`; app also a damaged snapshot that fails, a refused retry and the documented clean-up |
| 10. Writers off | `inactive` in `rehearsal_services.py`; `assert_blank_writers` in `scenario.py`; `writers_inactive` in `test.nix` | Units inactive with no main or control PID; ports closed; target inactive | VM rehearsal (synthetic data); also on temporary Hetzner hosts once (synthetic data; rehearsal profile). **VM drill of this procedure**: both roles, `status` as written |
| 11. Copy file sets | `COPY_RESTORED` in `rehearsal_services.py`; `fresh_restore` in `scenario.py`; copy loop in `test.nix` | Target empty and not a link (K3s server directory: exactly the one manifest skeleton); outbox bytes, owner and modes equal the original's; media hashes equal | VM rehearsal (synthetic data); also on temporary Hetzner hosts once (synthetic data; rehearsal profile copies more paths than production). Equality is checked against a signature taken from the original after its writers were stopped and while it was still alive (`original()` in `rehearsal_services.py`, `test.nix`), which a lost original cannot give. **VM drill of this procedure**: app, the block as written for media and outbox, owner and mode check |
| 12. PostgreSQL cluster in place | `ADOPT_POSTGRES` in `rehearsal_services.py`; lines after `fresh_restore` in `scenario.py`; copy in `test.nix` | Source bootstrap identity preserved; no `terrace-restore` role; native HBA and `fsync` on; restore OS user cannot connect; app roles log in but cannot assume `postgres`; role, membership and setting fingerprints equal; Windmill role boundary | VM rehearsal (synthetic data); also on temporary Hetzner hosts once (synthetic data; rehearsal profile). The production VM check copies the cluster and lets adoption start it but runs none of these role checks. Loading a database into a running, populated cluster: not exercised anywhere. **VM drill of this procedure**: app, the block as written; afterwards the whole quiesced signature equal |
| 13. Headscale keys and database before first start | `relay-state-adoption` and `sqlite-adoption` in `rehearsal_services.py`; `restore-relay` in `scenario.py` | Key hashes equal the original's; user list equal; `PRAGMA integrity_check` ok; relay health through HTTPS; with `--relay-peers`, all three client identities (node ID, key, address) return without enrolling again | Keys, database and users: VM rehearsal (synthetic data) and also on temporary Hetzner hosts once (synthetic data; rehearsal profile; "identity and private HTTPS unchanged"). Client reconnection after restore: VM rehearsal (synthetic data) only (lease 5 ran no peer checks). The production relay role replaced and adopted with `--restored`: not exercised anywhere. The relay's public name and address changing: not exercised anywhere (the VM guests used `.invalid` or `.test` names on a private test network; lease 5 ran its checks on the hosts themselves). **VM drill of this procedure**: relay replaced under the production profile: the block as written, integrity `ok`, key hashes and users equal, the ownership check; no peers |
| 14. Database role checks | `ADOPT_POSTGRES`, `WIND_ROLES` | as step 12 | as step 12. **VM drill of this procedure**: app, every line as written before adoption, each answer as commented |
| 15. `adopt --restored` | "replacement" subtest in `test.nix`, `platform/production/terrace-production.sh` (settings from `base.nix`) | Refused with no reconciled restore; refused for `--new` on a restored replacement; a first restored adoption that fails on nginx (443 taken) names nginx and no timer, and it and its resumed run leave the three backup timers and the sweep timer inactive with their start condition not met, also after a further `systemctl start` of the target and of `terrace-backup.timer`; the identity health timer is active; `status` names both held groups; the resumed adoption succeeds and prints both held groups and their commands; replacement marker gone; the next `push` clears the store's marker | VM rehearsal (synthetic data), app role only, passed at `9664be1` (2026-11-24); unit tests (`tests/platform/test_terrace_production.py`, stub systemctl); relay and lease 5: not exercised anywhere (the rehearsal profile has no adoption gate; `bootstrap_app` starts services directly). **VM drill of this procedure**: both roles: output as documented after the `verify` line; three refusals (no line, FAILED report, no witness); held timers on both roles, the relay's also across a reboot; markers cleared |
| 16. CMS migration gate | `BOOTSTRAP_APP` in `rehearsal_services.py` (`terrace-cms-migrate.service`); `bootstrap_app` in `scenario.py`; `test.nix` | Rehearsals: the restored database's signature equals the original's, compared before the migration runs (`replacement()` in `rehearsal_services.py`, the signature check before `recoveredBootstrapApplication` in `scenario.py`); afterwards only the protocol assertions run. Production VM check: the whole signature (realm, users, migrations, outbox, applications, roles, media) compared before and after the migration and start, and the replacement runs with the delivered secret | Restored-database signature equals the original's: VM rehearsal (synthetic data) and also on temporary Hetzner hosts once (synthetic data; rehearsal profile). Ledger and signature unchanged across the migration gate: VM rehearsal (synthetic data), production VM check only. A production release pair differing from the original's: not exercised anywhere. **VM drill of this procedure**: app: ledger unchanged, signature equal, sweep by hand succeeds while its timer is held. No erasure replay (none to replay) |
| 17. Identity | signature in `test.nix` and `app_signature`; `documenso_recovery_access` in `scenario.py`; `platform/identity-restore-test.nix` | Realm ID, admin-events flags, user list equal; 198 protocol assertions with real single sign-on after restore; recovery administrators of the apps authenticate while Keycloak is stopped; issuer and public signing keys continue (separate drill) | VM rehearsal (synthetic data); the 198 assertions after restore also on temporary Hetzner hosts once (synthetic data; rehearsal profile). Real accounts, sessions and tokens after the move, and break-glass after a restore: not exercised anywhere. **VM drill of this procedure**: app: `keycloak.service` active, realm ID, issuer and public signing keys equal, no identity unit failed; no tokens or sessions |
| 18. Documenso | `BOOTSTRAP_APP`; `documenso_browser('verify')`, `app_signature` in `scenario.py` | Same unsigned and signed PDF, decrypted 2FA data, input hashes, account hash, one completed signing request | VM rehearsal (synthetic data) for the full checks. On temporary Hetzner hosts once: migrated, bootstrapped, started, health-checked only. Production profile: refused, not exercised anywhere |
| 18. Windmill | `WIND_ROLES`, `START_APPLICATIONS`; `windmill_browser('verify')` in `scenario.py` | Restored private variable decrypts, original script, job and result read, a new job runs, role boundaries | VM rehearsal (synthetic data). Hetzner once: role boundary checked, server and worker started, `/api/version` answers and the worker is active; liveness only. The key constraint: nothing changes any key, and no test changes one; location of the key not determined. Production profile: not exercised anywhere |
| 18. Vaultwarden | `START_APPLICATIONS`; `vault_browser('restore')`, `vault_attachments('verify')` | Same two vault items and attachment bytes decrypt for a retained user device; the server-side copy of master passwords is absent | VM rehearsal (synthetic data). Hetzner once: migrated, started, bootstrap unit run, `/alive` answers; liveness only. Production profile: not exercised anywhere |
| 19. TLS and CA | `install_runtime` and `INSTALL_INPUT` in `rehearsal_services.py`; CA by store path in `scenario.py`; `platform/production/tls-test.nix`; manual mode in `test.nix` | Rehearsal: operator supplies CA, certificate, key and repository password again, each checked by digest; served chain verifies. Production VMs: manual `tls/*` re-delivered by `push`; ACME state not in the backup and a restored host registers a new account and gets a new certificate from a local test ACME server | Re-supplied by operator tooling: VM rehearsal (synthetic data) and also on temporary Hetzner hosts once (synthetic data; **not restored from backup**). Manual and local-ACME replacement: VM rehearsal (synthetic data). A public CA, real DNS, a real first issuance: not exercised anywhere. **VM drill of this procedure**: both roles, manual mode, `terrace-tls check` exits 0. No ACME |
| 20. Health checks | `protocol` (198 assertions) and `lifecycle` in `rehearsal_services.py`; `check_app` in `scenario.py`; `terrace-tls check` in `tls-test.nix` | Native HTTPS SSO, roles, publication, privacy; member lifecycle before and after | VM rehearsal (synthetic data); the 198 assertions and lifecycle also on temporary Hetzner hosts once (synthetic data). These scripts exist only in the rehearsal harness; the production host has `systemctl --failed`, `terrace-tls check` and `terrace-secrets verify`. **VM drill of this procedure**: both roles: the block as written; no failed unit, `terrace-backup age` exits 1 |
| 21. First backup from the replacement | `first_backup(app, "replacement", init=False)` in `platform/production/tls-test.nix` (about line 738); the freshness assertions for a first adopted app host at about lines 434-436 | Replacement: the backup service succeeds into the same repository under the same host name and the freshness unit then passes. First adopted app host (not the replacement): the freshness unit fails before its first backup and passes after. "The restore did not bring the old host's backup status" is a code comment there, not an assertion. Nothing asserts the snapshot series or retention across the promotion; on the restored app replacement ("replacement" subtest in `test.nix`): `arm-timers --in-service` first is refused with `the backup timers are still held (...); run terrace-production arm-timers first ...; nothing armed` and both markers kept; a backup by hand succeeds; `arm-timers` prints `Armed: terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer.` and the `Still held ... terrace-cms-sweep.timer ...` line, the three are active and the sweep held, a repeat prints `Already armed: nothing to arm.` and the same line; after a reboot the three are active, the sweep held and `status` names it; `arm-timers --in-service` prints `Armed: terrace-cms-sweep.timer.` and `Every writer timer is active now and at every boot.`, all four are active, `status` names nothing held, and a repeat prints `Already armed: nothing to arm.` and the same line. On an adopted (not restored) relay `arm-timers --in-service` prints `Already armed: nothing to arm.`, `This role holds no timer until service (it has no sweep).` and the same line | VM rehearsal (synthetic data), local repository, for the backup itself; the `arm-timers` assertions passed at `9664be1` (2026-11-24). The rehearsal harnesses (`scenario.py`, `rehearsal_services.py`) take no backup after restoring, and the backup timers stay disabled in them. Retention and pruning across a promotion, the freshness behaviour on a replacement, a restored relay's held timers, and an original that comes back: not exercised anywhere. **VM drill of this procedure**: both roles: the block as written, outputs as documented, repeat idempotent; app retention after two and three more runs, whole-repository `check --read-data` |
| 21. Declared in service, DNS cut-over | none (every rehearsal ends by stopping all guests) | none | not exercised anywhere. **VM drill of this procedure**: both roles: the D5 blocks as written and their outputs, timers active after a reboot (relay). Not DNS, not an owner's declaration |

## 4. What a production promotion needs that nothing has exercised

- **DNS and public-name cut-over.** Every rehearsal used `.invalid` or `.test` names (the VM
  rehearsals with name mapping on a private test network; lease 5 ran its checks
  on the hosts themselves). Nothing changes a public record, waits
  for a TTL, or orders the cut-over against ACME's HTTP-01 challenge. The relay's
  `server_url` is its hostname and its configuration takes its public IPv4 as a
  parameter; a replacement relay with a new address, and clients reconnecting to it,
  has not been tried.
- **Public-CA certificates.** No public CA account, order, terms, rate limit or
  CAA record has been used; only a local test ACME server.
- **Real secrets and escrow retrieval.** Only synthetic values exist. No drill has
  retrieved the escrow key and the private store with the owner after losing the
  operator machine ([service lifecycle](service-lifecycle.md)).
- **Real data volumes and timings.** Recovery time is unmeasured. The restore
  needs space for the restored files and a second copy of the cluster in the
  restore directory before they are copied into place (reading the scripts;
  nobody has measured the disk needed). The restore directory also keeps plaintext
  dumps and role password hashes on the root disk; removing a failed one is covered in step 9 (run in the promotion drill), but when to
  remove a successful one is **not determined from the repository**. In the drill
  (tiny synthetic data) the restore directory held 84 MB, free space on
  `/var/lib` fell by about 168 MB from before the restore to after step 12, and
  every step took seconds (adoption about 34 s on the app, 16 s on the relay); this
  says nothing about real data.
- **Member sessions and tokens after the move.** Sessions live in the restored
  database and the signing keys come back with it, but no rehearsal has a populated
  realm, an issued session or a token that must survive. Documenso revokes sessions
  of changed managed accounts at bootstrap.
- **Mail.** No mail transport is built (G-20). The restored outbox holds live
  invitation links; both a promoted replacement and a returning original would
  hold them. Nothing tests a duplicate or lost message.
- **Erasure replay.** A snapshot older than a member's erasure brings that
  member's personal data back: their member, application and profile rows, their
  Keycloak account (the realm side is a separate manual step at erasure time) and
  any invitation file in the outbox. Nothing re-applies the erasure. READ in the
  website source at `dd1aef7` (`docs/member-lifecycle.md` sections 5, 8 and 9,
  `cms/src/lifecycle/erasure.ts`): an erasure is a database transaction run by the
  CMS (member-initiated, or an administrator **Data request** for an already
  offboarded member) that tombstones the member row; the sweep only completes the
  outbox-file redaction afterwards and never erases anyone itself. So the only
  record of an erasure is in the restored database (the tombstone) and the outbox;
  No erasure ledger outside the restored data was found, in this repository or in
  the website source, so where one would live is **not determined from the
  repository**. Retention keeps erased data in older snapshots for months
  (keep-daily 7, weekly 4, monthly 6), which AGENTS.md's member-privacy rules make
  a question for the owner. What the operator does, in order: D2b decided before
  adoption; the list of erasures made since the snapshot's time obtained from the
  owner, from records kept outside this system; replay after the migration gate
  opens (step 16, continued) and before D5. The ordering conflict with ACME and
  DNS is set out under D2b. No rehearsal tests it.
- **The original's own timers.** After step 2 the original is quiesced, not fenced;
  its backup, sweep and freshness timers stay armed and return on any reboot, and
  its backup timer could prune the repository you restore from (step 2 stops them
  until the next boot; unrehearsed).
- **A returning original pruning the chosen snapshot mid-restore**: see step 21;
  whether restic's repository locks stop it is **not determined from the repository**.
- **Public names changing and Keycloak.** Keycloak's issuer follows the host name.
  The restored realm keeps its old client redirect addresses (startup import skips
  an existing realm), and the app's configured issuer and origins derive from the
  new profile hostnames. If public names change, tokens bound to the old issuer stop
  validating and the realm needs `terrace-identity-admin realm plan` and `realm
  apply` ([identity administration](identity-admin.md)); neither is exercised after
  a restore, or with changed names.
- **The original coming back after promotion.** Same repository, same credentials,
  possibly the same host name, the same Headscale keys and the same invitation
  tokens. The promotion drill boots the retired original app (VM) once before and
  once after the credential rotations (step 21 has what it did); its CMS, outbox
  and website on a public address, and a returning relay, are not tested.
- **A restore from the second backup target.** `platform/backup-test.nix` restores
  `latest` from a second target, but that target is a second SFTP repository; the
  S3/B2 code path has no test, and no promotion has used `--target secondary`.
- **A partial restore of one service.** `terrace-backup-restore` restores a whole
  snapshot; there is no per-service selection, and the rehearsals replace the whole
  cluster directory on an empty host. Loading one database dump into a populated
  production cluster (what runbook 7.8 literally says) is done nowhere.
- **A restore from a snapshot taken during writes** (no quiesce): the cross-dataset
  consistency gap is open (runbook 7.9).
- **The relay role replaced under the production profile**, and its reboot after
  a restore: done by the promotion drill (VM, synthetic data, no Headscale clients).
  Clients reconnecting to a replaced production relay remain untried.
- **Installing the production profile on a provider**, and the first-boot window
  in which the encrypted secrets are not yet staged (G-11).
- **Numeric owners.** Restored files keep their numeric user IDs. Static IDs are set
  for the CMS and website accounts, and Windmill and Vaultwarden have a `uid`
  option; whether the IDs of the PostgreSQL, Keycloak and Headscale accounts are
  fixed is **not determined from the repository**. The rehearsals use identical
  closures, so a mismatch has never been possible to observe. A replacement built from a different pin than the original is
  untested.
- **A rollback after `adopt --restored`**, and a snapshot that proves wrong after
  the writers have started.
- **Shallow adoption witnesses.** `adopt --restored` checks one report line and one
  file; a promotion that skipped the media copy, or used the wrong snapshot, would
  be adopted (read in `terrace-production.sh`; the promotion drill asserts the
  three refusals, not this acceptance, which would start writers on a
  half-restored host).
- **Alerting.** Nothing reaches a person when a promoted writer fails later (G-23).
- **Windmill's encryption key**: where it lives and how it is delivered, as above.
- **Identity-independent access after a restore** (break-glass) and whether
  real clients' tokens stay valid.

## 5. The promotion drill

`platform/production/promotion-test.nix` is a VM drill of **this written
procedure** on the production profile, both roles. Run it on the Linux builder:

```text
BUILD_LOCK=/path/to/shared-heavy.lock platform/production-promotion-check WEB_ARCHIVE CMS_ARCHIVE TFC_SOURCE
```

(the same pinned website pair and website source as `platform/production-check`).
It prints `PROMOTION-PROOF` with the assertion count, the seconds of each step,
records (reconciliation lines, the restore directory's listing, free space) and
the decision points, and `PROMOTION-CHECKS` with every assertion's label.

**How it keeps to the document.** Every command it runs is in
`platform/production/promotion-commands.json`. Evaluation fails unless each
shell block there is a fenced block of section 2, each expected output a fenced
block, and each inline command a phrase of this document, character for
character; `tests/platform/test_promotion_drill.py` (CI) checks the same and also
that every shell block of section 2 is either run or listed as excluded with a
reason (only step 18, which the production profile refuses). Substituted are
only the documented placeholders (`<SNAPSHOT_ID>`, `<path>`, `<replacement
address>`, `<operator ssh key>`) and, for the relay, `relay --role relay`.

**What it does.** An operator guest holds the store, the escrow key and the SFTP
repositories (the Storage Box's stand-in). An original app (adopted as new, a
first backup, synthetic content, two media files, three outbox files, a separately
tagged damaged snapshot) and an original relay (two Headscale users, a backup)
are set up first. Then, in the document's order: the app as a **planned
replacement** (steps 1 to 21 and D5, with step 2), and the relay as a **lost
original** (steps 1, 3 to 10, 13, 15, 19 to 21 and D5; step 2 skipped, its
snapshot taken while Headscale ran). Last, the retired original app is booted again
twice, before and after the credential rotations (item 6).

**What it does differently from a real promotion.** No provider: fencing is a VM
power-off and moving or deleting the disk image. No real DNS: names are `.invalid`
on a private test network, and nothing is moved. No real escrow retrieval: the
escrow key is made at build time and sits on the operator guest. Synthetic data
(a handful of rows and files; the restore directory held 84 MB, mostly the
isolated cluster), a synthetic CA and manual TLS mode. Each original and
its replacement are the same VM definition with a new disk, so they have the same
address, closure and numeric account IDs. The harness (`appHarness`,
`relayHarness` in the drill) adds the installer stand-in, trust in the synthetic
CA, the content fixture and client tools; it adds no `rehearsal/*` secret, so
`check` and `push` run without `--rehearsal`. The owner's decisions (section
1.3) are not taken: the drill goes on at each and records the option it used, as
**the drill's choice, not the owner's**: D1 a planned app replacement and a lost
relay; D2 the newest snapshot; D2b option 1 (manual certificate, no DNS change)
with no erasure to replay; D3, D4, D5 go; D6 the original app's disk deleted
after the fencing check, the relay's at once.

**The items this section listed before the drill existed, and where each stands.**

| Item | Status | Where |
| --- | --- | --- |
| 1. Both roles | Asserted, without peers: `host create --replace`, `rekey`, `push`, step 13 before the first start, `adopt --restored`, key hashes and users equal. Left out: Headscale clients (the `--relay-peers` rehearsal has them, on the rehearsal profile) | relay subtests |
| 2. Commands verbatim | Asserted: section 2's blocks as written (above), with their documented outputs and `status` before and after. Left out: step 18 (refused by the production profile) and the relay's step 2 (the relay is the lost case) | every subtest; the evaluation check and CI test |
| 3. The guards | Refusals asserted: no `RECONCILIATION OK` line, a `RECONCILIATION FAILED` report, no state witness. Left out: acceptance with the media or outbox copy skipped (it would start writers on a half-restored host; the code reads only the line and the witness, section 4) | "app, steps 10 to 14" |
| 4. A failed restore | Asserted: a damaged snapshot (its own tag) fails step 9, the host stays unadopted, a retry into the directory is refused, the documented clean-up, the chosen snapshot then reconciles. Left out: a retry from `--target secondary` (item 5) | "app, step 9" |
| 5. Second target | Left out: the production profile requires an `s3:https://` secondary; no S3 endpoint with trusted TLS is built in a VM. `platform/backup-test.nix` restores from a second SFTP target | none |
| 6. Fencing | Asserted: the retired original boots with its old key, its boot gate restarts its writers and re-arms every timer, `push` to it is refused, its backup succeeds and becomes the newest snapshot of the host name (one replacement snapshot pruned, recorded); after the repository password and SFTP key rotations its backup fails and the snapshot list is unchanged | the three "item 6" subtests |
| 7. Backups after promotion | Asserted: step 21, the series continues under the same host name, the restored-from snapshot still listed after two runs and pruned or kept after a third as restic's keep rules predict (pruned in the runs so far: same day), a snapshot of another tag untouched, `check --read-data` of the whole repository, `age` and freshness pass | "item 7" |
| 8. Same values | Asserted: the decrypted store is equal before and after `rekey` (both roles); the delivered database password opens the restored Keycloak role and a regenerated value is refused. Left out: a whole regenerated store pushed to a replacement | "app, step 4", "app, steps 16 and 17" |
| 9. No stale state | Asserted: no `/var/lib/acme` (manual mode), no CMS migration marker until step 16, the replacement marker gone after step 15 and the store's cleared by the next `push`. ACME mode: `platform/production/tls-test.nix` | "app, steps 10 to 14", "app, step 15" |
| 10. Identity continuity | Asserted: realm ID, issuer and public signing keys equal. Left out: an old token and session (no realm client that the drill can get a token from without the rehearsal protocol harness) | "app, steps 16 and 17" |
| 11. Non-quiesced snapshot | Relay only: its snapshot was taken with Headscale and nginx running and reconciled. Left out: the app with writes in flight (runbook 7.9 is open) | relay subtests |
| 12. Timings | Recorded, not asserted: seconds per step, restore directory size, free space before the restore and after step 12 (section 4) | `PROMOTION-PROOF` |
| 13. Restore directory | Recorded and asserted: after adoption it is `root:711`, its files `0600` and `files/` `0700` root, the isolated `pgdata` and `pgsock` `0700` owned by `terrace-restore`; an unprivileged account reads neither the report nor the files. Left out: a clean-up after success (this document settles none) | "app, step 15" |
| 14. Public-name realism | Left out: real names and addresses would be real public domains, and a VM has no DNS to change; the replacement relay keeps the test-network address | none |
