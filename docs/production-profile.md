# Production host profiles

Design and operating notes for the first production-shaped host profiles. They
enable the modules the rehearsals prove, require every deployment-specific value
as a parameter, and take all secrets from [sops-nix](secrets.md). Written
2026-11-19. A profile that evaluates and passes its VM proof is not a
deployment: no provider resource, DNS record, certificate or real secret exists
because of this document.

## Topology

Two roles, the same split the cloud rehearsal and the all-services VM rehearsal
already prove ([access relay](access-relay.md), [cloud rehearsal](cloud-rehearsal.md)):

| Role | Module | Runs | Public ports |
| --- | --- | --- | --- |
| `app` | `platform/production/app.nix` | PostgreSQL 18, Keycloak (identity), CMS and website, the HTTPS gateway (nginx), backup | TCP 22, 443; 80 only with ACME (challenge and redirect, [TLS](tls.md)) |
| `relay` | `platform/production/relay.nix` | Headscale with embedded DERP behind nginx, backup | TCP 22, 80 (HTTPS redirect; with ACME also the challenge), 443; UDP 3478 |

Alternatives considered:

- **One host for everything.** Cheaper, but relay access would die with the
  application host, which defeats the reason for an independent recovery path
  (owner decision 2026-10-28). Rejected.
- **App host split further** (identity on its own host). More cost and more
  secrets to deliver, and no rehearsal proves it. Rejected.
- **Same profile, different hardware.** Accepted: the role modules carry no
  disk, boot loader or network layout; a hardware module supplies them (below).

## Decisions waiting on the owner

These are stated, not decided, by this profile.

1. **Documenso, Windmill and Vaultwarden: deferred pending owner sign-off.**
   They are accepted scope (kept services). The production profile refuses them
   today because the runbook requires measuring the combined stack before adding
   each one (6.22) and because their inputs are not yet rendered from secrets.
   Prerequisites per service: render its input file (`secrets.json`,
   `bootstrap.json`, `input.json`) as a sops template from the inventoried
   secrets ([secrets](secrets.md#inventory)); add its OIDC client to the realm
   template; add its database to the backup; a VM proof. Memory ceilings each
   adds, from the existing modules' `MemoryMax`: Documenso 2304 MiB, Windmill
   4096 MiB (server and worker, 2048 MiB each), Vaultwarden 512 MiB; under the
   25 percent rule each MiB of ceiling needs 1.33 MiB of host memory (all three
   together: 6912 MiB more ceiling, 9216 MiB more host memory).
2. **K3s.** Production does **not import** the single-node K3s module at all
   (`single-node.nix`/`hetzner.nix` are bypassed, not disabled with an option),
   and the profile asserts `services.k3s.enable` is false. Reasons: no workload
   uses it, its datastore and PV data are not in the backup (G-21), and an idle
   control plane costs memory. It comes back when a workload needs it, together
   with backup coverage of `/var/lib/rancher/k3s/server` and the PVs, its memory
   ceiling in the budget and a VM proof.
3. **Operator accounts.** The profile creates one shared `operator` account in
   `wheel` with passwordless sudo, reachable only by the listed SSH keys;
   actions are attributable only by key fingerprint in the SSH log. The
   alternative is one named account per person (each with its own key and
   sudo), which gives per-person attribution at the cost of more accounts to
   manage. The choice is open.
4. **Instance size.** The app writers' memory ceilings are PostgreSQL 1024 MiB,
   Keycloak 2048 MiB, CMS 2048 MiB, website 768 MiB, nginx 256 MiB and the
   backup job 768 MiB: 6912 MiB. With at least 25 percent free (runbook 6.22),
   the host needs 6912 / 0.75 = 9216 MiB (9 GiB). The relay's ceilings are
   Headscale 512 MiB, nginx 256 MiB and backup 768 MiB: 1536 MiB, so 2048 MiB.
   No size is recommended here (G-9).

## What the profile requires

Every value that differs between deployments is a required option with no
default. Evaluation stops with a clear message when one is missing or unsafe.

| Option (`terrace.production.*`) | Role | Meaning |
| --- | --- | --- |
| `instance` | both | `"production"` or `"rehearsal"`; no default |
| `operatorKeys` | both | individual operator SSH public keys (ed25519) |
| `memoryMiB` | both | host memory; the writers' `MemoryMax` ceilings must fit in 75 percent of it |
| `escrowRecipient` | both | the offline escrow key's public age recipient; the host accepts only ciphertext for exactly itself and this recipient |
| `app.webHostname`, `app.cmsHostname`, `app.identityHostname` | app | public DNS names; HTTPS origins derive from them |
| `app.identityContract` | app | hash-pinned TFC realm producer (`platform/identity-contract.nix`) |
| `app.backup.primary.repository`, `.knownHosts` (`.port`) | app | Storage Box SFTP repository and its pinned host key |
| `terrace.website.webPackage`, `cmsPackage`, `revision` | app | the reviewed release pair (`platform/website-release.nix`) |
| `relay.hostname`, `relay.publicIPv4`, `relay.backup.primary.*` | relay | as above for the relay |

Optional, with defaults:

| Option (`terrace.production.*`) | Role | Meaning |
| --- | --- | --- |
| `deploy.trustedKeys` | both | the build machine's **public** Nix signing keys for the opt-in deploy path ([host updates](host-updates.md#15-the-deploy-path-option-1-of-u0-opt-in)); default `[]`, which leaves the host's Nix settings unchanged (only root trusted, signatures required): a copy as `operator` of an unsigned or foreign-signed closure is refused (paths signed by the nixpkgs cache key, which Nix trusts by default, are accepted; root, and so `operator` through `sudo`, can still import anything), and `terrace-production activate` refuses everything. A deploy key may not be one of the keys Nix trusts by default. Set it in the module a host is **installed** with, or its first update has no path. It is one way to settle owner decision U0, which the owner has not taken |
| `wombRevision` | both | the Womb commit built, recorded in the system manifest; `production-config.nix` sets it from its `wombRevision` argument; default `null` |
| `app.releaseMigrations` | app | the release's CMS migration list, recorded in the manifest; default the pin's list when the host runs the pinned revision, otherwise `null` (`activate` and `rollback` then refuse any release change on a migrated database) |

Every production system carries a manifest, `/etc/terrace/system.json`: role,
host name, instance, Womb revision, the app's release revision and migration list,
and the Keycloak, PostgreSQL and Headscale versions. `terrace-production activate`
recognises a closure built for this host by it.

Origins, the realm's callbacks, `LIFECYCLE_PUBLIC_ORIGIN`, `CMS_PUBLIC_ORIGIN`
and the client-address header all derive from the hostnames.

### Hardware module interface

`infra/nixos/production-config.nix` takes `role`, `localModule` (the private
parameters above) and `hardwareModule`. The hardware module must provide, and
nothing else:

- `fileSystems."/"` (and any other mounts), the disk layout;
- an installable boot loader (`boot.loader.systemd-boot.enable`, or GRUB with a
  real `boot.loader.grub.device`);
- the network: DHCP (default) or static addressing
  (`networking.useDHCP = false; networking.interfaces.<if>.ipv4.addresses`,
  `networking.defaultGateway`, nameservers); the evaluation check proves a
  static-network host passes;
- kernel modules or firmware the machine needs; no services and no ports.

Hetzner Cloud uses the existing `infra/nixos` cloud disk module (GPT, ESP, ext4,
systemd-boot fallback path), which leases 3 and 5 installed and booted (lease 4 stopped before the install plan; see [cloud rehearsal](cloud-rehearsal.md#lease-5-result-2026-11-20)). The
on-prem server's hardware is unknown, so no layout is written for it; the
profile refuses to evaluate as production without a root filesystem and a boot
loader.

## Invariants enforced at evaluation

The role modules assert, and `infra/nixos/production-eval-check.nix` re-checks
on evaluated configurations, with a negative variant for each that must fail
for the stated reason:

- **Production mode only**: no hostname under a reserved name (`.invalid`,
  `.test`, `.example`, `.localhost`, `.local`, `.internal`, `.lan`,
  `home.arpa`, `example.com/net/org`); no relay address in a private, shared,
  loopback, link-local, benchmarking or documentation range; no rehearsal
  harness or known fixture unit; no extra trust anchors; no Keycloak
  `EnvironmentFile`; a root filesystem and an installable boot loader.
- **Secrets**: every consumer reads a sops-nix path; every secret and template is
  `root:root 0400` from the one host ciphertext (no per-secret `sopsFile`); no
  unit `environment` or `serviceConfig.Environment` carries a secret-named
  variable or a placeholder; no `EnvironmentFile` or `LoadCredential` points
  into the Nix store on any unit; no core dumps (systemd-coredump off,
  `kernel.core_pattern` piped to a no-op, `fs.suid_dumpable = 0`,
  `LimitCORE=0` on the writers and every other unit that holds secrets); no
  unencrypted swap; no `SetCredential` or `ImportCredential` (except NixOS's
  own tmpfiles re-setup); the escrow recipient is pinned.
- **Network**: the firewall opens exactly the role's ports and every port
  definition must come from an allowed module (an extra port is refused, not
  overridden); free-form firewall rules, nftables tables, sshd, sudo or polkit
  configuration only from the pinned nixpkgs' own modules (identified by its
  store path, not a path substring); only listed services may be enabled at
  all and only listed socket units may listen;
  PostgreSQL, Keycloak (HTTP and management), Headscale (API, metrics, gRPC),
  the CMS and the website listen on loopback; nginx has exactly the role's
  virtual hosts, TLS only on the app and HTTPS with the module's redirect on the
  relay, no extra listeners; with ACME, port 80 is only the challenge and the
  redirect, and no staging or test CA, unaccepted terms or missing contact
  ([TLS](tls.md)).
- **Accounts and access**: immutable users; no password on any account (only
  locked markers); `wheel` (users and group members) is exactly `operator`;
  sudo rules only for root and `wheel`, and no `sudo-rs` or `doas`;
  SSH key-only, `AllowUsers operator`, no `extraConfig`, one ed25519 host key;
  no emergency shell and no debug or rescue kernel parameters.
- **Units**: services, timers, sockets, paths and targets wanted at boot are a
  subset of an explicit allowlist (operating-system plumbing plus
  `sops-install-secrets` and `terrace-production-boot`); no boot target pulls a
  unit through `wants`/`requires`; the units wanted by
  `terrace-production.target` are exactly the writers and their timers.
- **Scope and capacity**: K3s, Documenso, Windmill, Vaultwarden and the demos
  off; static CMS and website ids (44121, 44122); the writers' ceilings within
  75 percent of `memoryMiB`.

## Boot gate: adopt before writers start

A brand-new host and a blank replacement boot the same closure. If the writers
started at boot, a replacement would initialise an empty PostgreSQL cluster and
import a fresh realm before the backup is restored. So every writer and its
timers are wanted only by `terrace-production.target`, and
`terrace-production-boot.service` starts it at boot only when
`/var/lib/terrace-production/adopted` exists (after `terrace-secrets verify`
passes). An unadopted host stays gated across reboots. The operator adopts a
host once, explicitly:

- `terrace-production adopt --new` refuses a host that already holds state (a
  PostgreSQL data directory on the app, a Headscale database on the relay) or a
  restore under `/var/lib/terrace-restore`. It also refuses a **replacement**,
  two independent ways: `terrace-secrets push` delivers a replacement marker to
  any host made with `host create --replace` (it arrives over the channel
  pinned to that host's key), and before adopting, the host lists the snapshots
  in each of its backup repositories with its own delivered credentials (within
  120 seconds) and refuses if the repository holds **any** snapshot: the
  repository is per role, so this also catches a replacement given a different
  host name. A repository that is not initialised yet (restic exit 10) is what
  a genuinely new deployment has. An unreachable or silent repository refuses,
  naming the restic exit code, because that is exactly when a replacement could
  be mistaken for a new host. The last resort, `adopt --new
  --first-deployment-without-repository HOSTNAME` (the host's own name, typed
  out), is only for a first deployment whose repository does not exist yet,
  after confirming that no earlier host of this role ever existed; it is
  refused while the replacement marker is present and prints a fixed warning.
  `adopt --new` on an adopted host just answers `Already adopted.`
- The operator's store keeps a host's replacement marker until the first `push`
  after the host was adopted (the host reports `Host state: adopted`), then
  clears it. To redeploy a wiped host as a genuinely new deployment under the
  same name, clear it explicitly: `terrace-secrets host reset-replacement HOST
  --confirm HOST`.
- `terrace-production adopt --restored` refuses unless
  `/var/lib/terrace-restore/reconciliation.txt` says `RECONCILIATION OK` and the
  restored state is in place; it then clears the replacement marker. It starts
  the writers but **leaves the backup, backup check and backup freshness timers
  off** (`restoreHeldTimers`), and on the app **the sweep timer too**
  (`restoreServiceHeldTimers`). Before anything starts it writes
  `/var/lib/terrace-production/timers-held` and, on the app,
  `/var/lib/terrace-production/service-timers-held`; each held timer carries
  `ConditionPathExists=!` on its group's file, so every start of the target (this
  adoption, a resumed one, every boot) skips it, and the adoption's completeness
  check does not wait for it. A scheduled backup ends in `forget --prune` against
  the repository the restore came from, so the backups wait until the operator has
  verified the restored services; the sweep acts on restored data, so it waits until
  the replacement is declared in service, after any erasure replay. The output names
  each group and its command. `terrace-production arm-timers` needs an adopted host
  with the target active, removes the backup hold, starts every writer timer that
  is neither active nor still held, prints `Armed: <timers>.` (or `Already armed:
  nothing to arm.`, so it can be repeated), then either names the timers still held
  until service or says `Every writer timer is active now and at every boot.`, and
  fails naming any timer that did not become active. `terrace-production
  arm-timers --in-service` removes the sweep hold and arms the sweep, and refuses
  (`the backup timers are still held ...; run terrace-production arm-timers first
  ...; nothing armed`) while the backup hold exists, so the order is enforced; on the
  relay, which has no sweep, it says so and does not fail. `terrace-production
  status` prints one line per held group. **Nothing else reports a held timer**:
  the markers never expire and the freshness timer that would report a missing
  backup is held too, so a restored host on which `arm-timers` is never run takes
  no scheduled backup and nothing alerts ([restore promotion](restore-promotion.md),
  step 21). The other writer timers (`writerTimers`: the identity health check in
  `app.nix`, the TLS expiry check and, in ACME mode, renewal added by `tls.nix`, the
  temporary-administrator check added by `identity-admin.nix`) are armed by the
  adoption as before.
  A plain `adopt --new` holds nothing back.

Each host is adopted once. Both commands verify the secrets, record
`/var/lib/terrace-production/adopting`, start the target and write the adopted
marker only when every writer is active (the CMS and website may instead be
skipped by their migration condition) and still runs the same process after a
15-second stability window (a writer can report started and fail seconds later,
as nginx does while it retries a port it cannot bind), every helper (Keycloak's
database init, the website input check) succeeded and every timer runs (except
the ones a restored adoption holds back). If anything is not
running, the command names it and stops; fix the cause and **run the same adopt
command again**: while `adopting` says `new`, `adopt --new` resumes instead of
refusing the database its first attempt created. `terrace-production status`
shows an unfinished adoption.

## Cold start of a new deployment

1. Operator: `terrace-secrets host create`, `generate`, `set` each external
   value, `check` ([secrets](secrets.md#bootstrap-of-a-brand-new-host)).
2. Install the host with the operator-generated host key staged
   ([Rescue installer](cloud-rescue-install.md)). The installer does **not**
   stage the encrypted secrets file (see the gap below), so the first boot has
   no secrets and nothing writes.
3. `terrace-secrets push` over SSH pinned to that host key; it installs and
   verifies the secrets. `terrace-secrets host unstage`.
4. `terrace-production adopt --new`.
5. Explicit release steps (runbook 6.15): `systemctl start
   terrace-cms-migrate.service`, then `systemctl start terrace-cms.service
   terrace-web.service`. Until the first migration the CMS and website are
   skipped, not failed (`ConditionPathExists` on the migration marker; a
   revision mismatch still fails through the module's gate, `requireMigration`,
   which both services carry: the website's was added on 2026-11-25, see
   [host updates](host-updates.md), H7).
6. `terrace-backup init primary`. The first scheduled backup before this step
   fails visibly (`systemctl --failed`); after it, backups succeed.

**Gap, not implemented here:** the Rescue installer (`infra/nixos/rescue_install.py`,
lease tooling in use) copies only the host key. Staging the ciphertext at
install time would remove step 3's first-boot window without secrets; it needs
an installer change to copy `secrets.sops.json` to
`/mnt/var/lib/terrace-secrets/` (`0600`) and a lease rehearsal. The VM proof
follows the steps above (its installer stand-in stages the host key only).

## Host replacement (rehearsed)

1. On the old host: stop the sweep timer, website, CMS, Keycloak and nginx;
   `systemctl start terrace-backup.service`; note the snapshot id; stop
   PostgreSQL. Then the host is destroyed.
2. Operator: `terrace-secrets host create app --replace`, `terrace-secrets
   rekey app` with the escrow key; install the replacement with the new host
   key; `push` (which marks the host as a replacement).
3. On the replacement (not adopted, so nothing writes):
   `terrace-backup-restore --target primary --snapshot <id> --dest /var/lib/terrace-restore`
   must print `RECONCILIATION OK`. Copy the restored media and outbox
   directories (`<dest>/files/<path>`) into place, and the PostgreSQL data
   directory (`<dest>/pgdata`) into the cluster's data directory
   (`services.postgresql.dataDir`, `/var/lib/postgresql/18` today), owned by
   `postgres`. The all-services rehearsal's `fresh_restore`
   (`platform/full/scenario.py`) copies only the file sets; its cluster copy is a
   separate step, and the rehearsals read the data directory from
   `/etc/rehearsal-pg-directory`, a file only the test harness provides (the
   production VM check's exact commands are in its "replacement" subtest). Then
   `terrace-production adopt --restored` (the backup and sweep timers stay off),
   the migration action (the ledger must stay unchanged), the two services, once
   the restored services are verified a backup by hand and `terrace-production
   arm-timers`, and once the replacement is declared in service
   `terrace-production arm-timers --in-service`.

Promoting a restore into service on real data still needs the owner-approved
procedure (G-22); this rehearsal is its technical basis.

## What the evaluation check does not catch

- A host module that deliberately spoofs its `_file`, or a hostile private module
  inside the trust base (the pinned nixpkgs and this repository), can define
  free-form configuration that looks like NixOS's own.
- Secret values written into a unit's `script` text, a file in the Nix store or
  an innocuously named variable: the checks look at names, placeholders and the
  secrets module's paths, not at every string (the VM scans catch values that
  actually appear, for the rehearsal's values).
- Units a package brings in that are not wanted at boot or by the production
  target, and anything started by hand.
- Services enabled outside `services.<name>.enable`: daemons switched on through
  `programs.*`, `networking.*` or `security.*` options are not enumerated
  (only the unit, socket, listener and firewall checks would notice them).
- A replacement adopted as new in two corner cases: when its backup repository
  is unreachable and the operator uses `--first-deployment-without-repository`,
  or when `adopt --new` runs on a replacement that never went through `rekey`
  (so carries no replacement marker) and whose repository shows no snapshot
  (for example one that was never initialised).
- Runtime behaviour after adoption: the 15-second stability window runs at
  adopt time only. A writer that fails after a later reboot shows in
  `systemctl --failed` and nowhere else until alerting exists (G-23).
- The relay role's replacement and its reboot are not exercised in the VM (only
  its first boot, delivery, adoption, listeners and a backup); the app role's
  are.

## Building a host

The private deployment directory holds the host module and the encrypted
secrets store; neither belongs in this public repository.

```nix
# host-app.nix (private)
{
  networking.hostName = "app";
  terrace.production = {
    instance = "production";
    memoryMiB = 15360;
    operatorKeys = ["ssh-ed25519 AAAA... operator"];
    escrowRecipient = "age1...";
    app = {
      webHostname = "www.terracefclub.org";
      cmsHostname = "cms.terracefclub.org";
      identityHostname = "id.terracefclub.org";
      backup.primary = {
        repository = "sftp:uNNNNNN@uNNNNNN.your-storagebox.de:terrace-app";
        port = 23;
        knownHosts = "[uNNNNNN.your-storagebox.de]:23 ssh-ed25519 AAAA...";
      };
    };
  };
}
```

```sh
nix build --no-link --print-out-paths --impure --expr '
  let infra = builtins.getFlake "path:/checkout/infra/nixos"; in
  (import /checkout/infra/nixos/production-config.nix {
     role = "app"; localModule = /private/deployment/host-app.nix;
     hardwareModule = { imports = [infra.nixosModules.cloud-disk];
                        terrace.cloudDisk.device = "/dev/disk/by-id/REPLACE_WITH_VERIFIED_DISK"; };
     webArchive = /protected/terrace-web-<rev>-linux-x64-gnu.tar.gz;
     cmsArchive = /protected/terrace-cms-<rev>-linux-x64-gnu.tar.gz;
     tfcSource = /source/tfc-at-<rev>;
     wombRevision = "<git rev-parse HEAD of /checkout>"; }).config.system.build.toplevel'
```

Build on the Linux builder under the shared lock, never on a Mac. `wombRevision` is
optional and informational (`terrace-production status` shows it). With the deploy
path, the private module also sets `terrace.production.deploy.trustedKeys`, and
the built toplevel reaches an installed host by
`platform/production/terrace-deploy.sh` ([host updates](host-updates.md#15-the-deploy-path-option-1-of-u0-opt-in)).

## Verification

- Evaluation check (no builds; builder, shared lock):
  `nix eval --raw --impure --option builders "" --file infra/nixos/production-eval-check.nix`
- VM proof (builder; the wrapper takes the shared lock itself):
  `BUILD_LOCK=/path/to/shared-heavy.lock platform/production-check WEB_ARCHIVE CMS_ARCHIVE TFC_SOURCE`
- Tool unit tests (CI): `python3 platform/production/test_terrace_secrets.py -v`
- Certificates by ACME against a local CA (builder): `platform/production-tls-check`, see [TLS](tls.md#verification)
- Deploy path (builder): `BUILD_LOCK=/path/to/shared-heavy.lock platform/production-deploy-check WEB_ARCHIVE CMS_ARCHIVE TFC_SOURCE PREVIOUS_WEB_ARCHIVE PREVIOUS_CMS_ARCHIVE`,
  the pinned pair plus the previous kept pair (`680ad56`, pinned in
  `platform/production/deploy-previous-release.json`); unit tests (CI):
  `python3 -B tests/platform/test_terrace_production.py -v` and
  `python3 -B tests/platform/test_terrace_deploy.py -v`; mutation check (any
  machine, several minutes): `python3 -B tests/platform/deploy_mutants.py`. What it
  proves: [host updates](host-updates.md#15-the-deploy-path-option-1-of-u0-opt-in)
- Restore promotion drill (builder): `BUILD_LOCK=/path/to/shared-heavy.lock platform/production-promotion-check WEB_ARCHIVE CMS_ARCHIVE TFC_SOURCE`,
  the written [restore promotion](restore-promotion.md#5-the-promotion-drill)
  procedure run with its own commands on both roles; unit test (CI):
  `python3 -B tests/platform/test_promotion_drill.py -v`

The VM proof boots the **same** app role and, briefly, the **same** relay role
(secrets pushed, adopted, Headscale and HTTPS answering, redirect on port 80,
a backup) with `instance = "rehearsal"`,
synthetic `.invalid` hostnames, a throwaway escrow key made at build time (so
its public recipient can be pinned like the owner's), a host key generated at
test time and secrets generated by `terrace-secrets` inside the operator guest.
A rehearsal-only harness (`platform/production/rehearsal.nix`) adds only what
the shared protocol needs: trust in a synthetic CA (by store-path reference, never a copied bundle), the protocol scripts, a
temporary Keycloak bootstrap administrator supplied through sops and removed by
the protocol, a closure-scan control string, and the stand-in for the installer
that copies the staged host key. The production check rejects that harness.

What the secret scans cover and exclude is in [secrets](secrets.md#verification).
Verified 2026-11-19 on the Linux builder at commit `cbf129f` with the pinned
`680ad56` bundles: evaluation check PASS (production app, static-network app,
relay and rehearsal instance; 71 rejected variants); VM proof PASS in 637 s
with 199 assertions, including the 198 shared protocol assertions, the
unchanged original and replacement lifecycle steps and one boot of the
production relay role; the cloud rehearsal check still passes. The
all-services rehearsal passed at `2ce39bb`, after which no shared module
changed. The relay role has been booted only in that VM proof (secrets, an
unreachable-repository refusal and the named override, adoption, Headscale,
HTTPS, redirect, backup), never on a provider. Not proven: a real provider
host, real DNS and certificates, real secret values or an escrow retrieval with
the owner. Since `bb0d526` (a test-only fix, PR 55) the production VM proof has
200 assertions; the figure above is the dated run at `cbf129f`. The VM proofs
give the expiry check an explicit trust anchor file, so the default system
trust store path of `terrace-tls check` is not exercised by any VM proof
([TLS](tls.md#verification)).

Re-verified 2026-11-24 at Womb `e7403a5` after the PR 68 review (fresh results):
205 assertions (198 protocol) in 626 s; the evaluation check passes with 139
rejected variants, now including the operator directory at, inside and around a
backup path in every spelling (trailing or repeated slashes, `.`, `..`, relative,
`/`), an operator directory overlapping the outbox, and out-of-range values of
`lifecycleLockTimeoutMs` and `operatorToolMaxSeconds` (both options' bounds
evaluate cleanly).

Re-verified 2026-11-24 with the kept `dd1aef7` build at Womb `3f4bc9e` (fresh
results): VM proof PASS in 634 s with 205 assertions (198 shared protocol
assertions). Three are new: as root, `terrace-cms-tool roster-invite dry-run`
runs with the CMS environment sops renders on ramfs and returns the plan with the
database identity, the dry run writes nothing but its owner-only report, and a
non-root caller is refused. The evaluation check passes with 123 rejected
variants and the cloud rehearsal check passes.

Re-verified 2026-11-23 with the pinned `eab4189` bundles (fresh results): VM
proof PASS in 610 s with 202 assertions (198 shared protocol assertions). Two
are new: the first import of the release's realm artifact through the hosting
policy reads back admin events on and details off from Keycloak's database, and
the restored host's signature now includes those two flags. The evaluation check
passes with 123 rejected variants and the cloud rehearsal check passes.

Verified for the deploy path at Womb `c3a9090` (fresh results, builder; runs started
2026-11-24 and ended 2026-11-25, builder local time): the deploy VM proof PASS in
945 s with 124 assertions, each a real condition (an upgrade of a populated app host
from the kept `680ad56` release to the pinned `dd1aef7`, its migration, the refusals,
rollbacks, the cancel of a pending `--boot` activation and a gated switch that also
broke another unit, as in [host updates, section 1.5](host-updates.md#15-the-deploy-path-option-1-of-u0-opt-in));
this VM proof 263 assertions (198 protocol), the identity administration proof 347
and the TLS proof 148, all PASS (their derivations changed: every production system
now carries the manifest and the extended `terrace-production`). The evaluation
check passes with 143 rejected variants. The other checks and the five rehearsal
drivers have derivations equal to trunk's and were not rerun.

Verified for the deploy path follow-ups and the website migration gate at Womb
`c8f6f29` (fresh results, builder, 2026-11-25): the deploy VM proof PASS in 697 s
with 133 assertions ([host updates, section 1.5](host-updates.md#15-the-deploy-path-option-1-of-u0-opt-in));
this VM proof 263 assertions (198 protocol), the identity administration proof 347,
the TLS proof 148, the website and website gateway checks, and the five rehearsal
variants once each, all PASS (every derivation changed with the website module).
The evaluation check passes with 145 rejected variants (two new: the migration gate
removed from the CMS or from the website). After review round 1 of that change, at Womb `5f1a3bb`
(2026-11-25): the deploy VM proof PASS with 133 assertions in 704 s, this VM proof 263
(198 protocol), identity administration 347 and TLS 148, all fresh; the website and
gateway checks and the rehearsal drivers have derivations identical to `c8f6f29`'s and
carry over from it.

## Keycloak administration

The app role also installs `terrace-identity-admin` (root-only wrapper) and the
never-started-at-boot `terrace-identity-bootstrap-admin.service`
(`platform/production/identity-admin.nix`). Procedures, the break-glass path and
their VM proof: [identity administration](identity-admin.md) (G-15, G-39, G-40).

## Out of scope, with what each needs

| Follow-up | Gap | What it needs |
| --- | --- | --- |
| TLS against a public CA | G-12 | Built and VM-proven against a local ACME server ([TLS](tls.md), `tls.mode = "acme"`; manual stays the default). Needs the owner's CA choice and terms acceptance, a contact address, real DNS, and a first real issuance; DNS-01 is not built |
| Mail transport | G-20 | Owner's provider choice; SMTP credentials become sops secrets; outbox delivery job; a test against a local sink |
| Alerting and log retention | G-23 | Delivery channel, external probe, certificate and backup-age alerts that reach a person, a tested failing check |
| Production sizing | G-9 | Owner decision (above) |
| Installer stages the ciphertext | G-11 | `rescue_install.py` change and a lease rehearsal (above) |
| Documenso, Windmill, Vaultwarden in production | 6.18-6.20, 6.22 | Owner sign-off, then the prerequisites above, one at a time |
| Restore promotion on real data | G-22 | Owner-approved procedure based on the rehearsed sequence |
| K3s datastore backup | G-21 | Only if a workload is added |
