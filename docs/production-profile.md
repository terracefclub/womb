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
| `app` | `platform/production/app.nix` | PostgreSQL 18, Keycloak (identity), CMS and website, the HTTPS gateway (nginx), backup | TCP 22, 443 |
| `relay` | `platform/production/relay.nix` | Headscale with embedded DERP behind nginx, backup | TCP 22, 443; UDP 3478 |

Alternatives considered:

- **One host for everything.** Cheaper, but relay access would die with the
  application host, which defeats the reason for an independent recovery path
  (owner decision 2026-10-28). Rejected.
- **App host split further** (identity on its own host, as some deployments do).
  More cost and more secrets to deliver, and no rehearsal proves it. Rejected.
- **Same profile, different hardware.** Accepted: the role modules carry no
  disk, boot loader or network layout. A small per-host module supplies them.
  For Hetzner it is the existing `infra/nixos` cloud disk module (GPT, ESP,
  ext4, systemd-boot fallback path), which leases 3 and 4 installed and booted.
  The on-prem server's hardware is unknown, so no layout is written for it: the
  owner's host module will provide `fileSystems."/"` and a boot loader, and the
  profile refuses to evaluate as production without them.

K3s stays **off** in both production roles. No workload uses it yet, its
datastore and PV data are not in the backup (G-21), and an idle control plane
costs memory the measured stack needs. The module is unchanged and can be
enabled once a workload and its backup coverage exist. Documenso, Windmill and
Vaultwarden are kept (owner decision) but the production profile **refuses**
them for now: the runbook requires measuring the combined stack before adding
each one (6.22), and their inputs (`secrets.json`, `bootstrap.json`,
`input.json`, extra realm clients) are not yet rendered from secrets. Their
secrets are inventoried in [secrets](secrets.md) so that work is mechanical.
Home Assistant and Frigate remain on-prem demonstrations only.

## What the profile requires

Every value that differs between deployments is a required option with no
default. Evaluation stops with a clear message when one is missing or unsafe.

| Option (`terrace.production.*`) | Role | Meaning |
| --- | --- | --- |
| `instance` | both | `"production"` or `"rehearsal"`; no default |
| `operatorKeys` | both | individual operator SSH public keys (ed25519) |
| `memoryMiB` | both | host memory; the sum of every writer's `MemoryMax` must fit |
| `app.webHostname`, `app.cmsHostname`, `app.identityHostname` | app | public DNS names; HTTPS origins derive from them |
| `app.identityContract` | app | hash-pinned TFC realm producer (`platform/identity-contract.nix`) |
| `app.backup.primary.repository`, `.knownHosts` | app | Storage Box SFTP repository and its pinned host key |
| `terrace.website.webPackage`, `cmsPackage`, `revision` | app | the reviewed release pair (`platform/website-release.nix`) |
| `relay.hostname`, `relay.publicIPv4`, `relay.backup.primary.*` | relay | as above for the relay |

Origins (`https://<host>`), the realm's callbacks, `LIFECYCLE_PUBLIC_ORIGIN`,
`CMS_PUBLIC_ORIGIN` and the client-address header all derive from the
hostnames, so they cannot disagree.

## Invariants enforced at evaluation

The role modules assert, and `infra/nixos/production-eval-check.nix` re-checks on
fully evaluated configurations (with negative variants that must fail):

- **Production mode only**: no hostname under a reserved name (`.invalid`,
  `.test`, `.example`, `.localhost`, `.local`, `.internal`, `.lan`,
  `home.arpa`, `example.com/net/org`); no rehearsal harness or known test
  fixture unit (`gateway-test-inputs`, `cloud-rehearsal-inputs`,
  `website-test-inputs`, Mailpit); no extra trust anchors
  (`security.pki.certificateFiles`); no Keycloak `EnvironmentFile` (that is
  where a bootstrap administrator would come from); a root filesystem and an
  installable boot loader from the host module.
- **Both modes**: Keycloak has no `initialAdminPassword`; the env templates
  contain no `CMS_LOCAL_BOOTSTRAP` or `CMS_BOOTSTRAP_*` key; the realm template
  build refuses registration, users or a realm without TLS-only policy;
  PostgreSQL, Keycloak (HTTP and management), Headscale, the CMS and the website
  listen on loopback only; the firewall opens exactly the ports in the table
  above; K3s, Documenso, Windmill and Vaultwarden are off; the CMS and website
  accounts have the static ids 44121 and 44122; every secret consumer (env
  files, realm, database password, TLS, backup password, SFTP key, B2
  environment) points at a sops-nix path; every declared secret and template is
  `root:root 0400`; ciphertext is outside the Nix store; no unit
  `Environment=` carries a secret-named variable or a sops placeholder; swap,
  if any, is randomly encrypted.
- **Boot units**: the evaluation check compares the units wanted at boot with
  an explicit allowlist (operating-system units plus `sops-install-secrets`
  and `terrace-production-boot`), and the units wanted by
  `terrace-production.target` with the writer list (PostgreSQL, Keycloak,
  nginx, CMS, website; the sweep, backup, backup-check, backup-freshness and
  identity-health timers on the app; Headscale, nginx and the backup timers on
  the relay). A new boot unit fails closed until it is reviewed.

## Boot gate: adopt before writers start

A brand-new host and a blank replacement boot the same closure. If the
writers started at boot, a replacement would initialise an empty PostgreSQL
cluster and import a fresh realm before the backup is restored. So every
writer and its timers are wanted only by `terrace-production.target`, and
`terrace-production-boot.service` starts that target at boot only when
`/var/lib/terrace-production/adopted` exists. The operator creates the marker
once, with `terrace-production adopt` (it first runs `terrace-secrets verify`):

- **New deployment**: `adopt`, then the explicit release steps (runbook 6.15):
  `systemctl start terrace-cms-migrate.service`, then
  `systemctl start terrace-cms.service terrace-web.service`, then
  `terrace-backup init primary`. Until the first migration the CMS and website
  are skipped, not failed (`ConditionPathExists` on the migration marker; a
  revision mismatch still fails loudly through the existing gate).
- **Replacement**: restore first (below), then `adopt`, then the migration
  action (the ledger must stay unchanged) and the two services.

After adoption every reboot starts the writers on its own. The sweep timer and
the backup timers are therefore enabled in production (unlike the cloud
rehearsal, whose bootstrap arms them explicitly).

## Host replacement (rehearsed)

The VM proof runs this exact sequence (see [secrets](secrets.md) for the key
commands):

1. On the old host: stop the sweep timer, website, CMS, Keycloak and nginx;
   `systemctl start terrace-backup.service`; note the snapshot id; stop
   PostgreSQL. Then the host is destroyed.
2. Operator: `terrace-secrets host create app --replace`, `terrace-secrets
   rekey app` with the escrow key, and stage the new host key and ciphertext
   for the installer.
3. On the replacement (not adopted, so nothing writes):
   `terrace-secrets verify`, then
   `terrace-backup-restore --target primary --snapshot <id> --dest /var/lib/terrace-restore`
   (it must print `RECONCILIATION OK`). Copy the restored media and outbox
   directories and the stopped PostgreSQL data directory into place exactly as
   the all-services rehearsal does (`platform/full/scenario.py`,
   `fresh_restore`), then `terrace-production adopt`, the migration action and
   the two services.

Promoting a restore into service on real data still needs the owner-approved
procedure (G-22); this rehearsal is its technical basis.

## Building a host

`infra/nixos/production-config.nix` composes the Hetzner cloud disk module, one
role and a private host module (parameters only, no secrets), like the existing
`rehearsal-config.nix`. The private deployment directory holds the host module
and the encrypted secrets store; neither belongs in this public repository.

```nix
# host-app.nix (private)
{
  networking.hostName = "app";
  terrace.cloudDisk.device = "/dev/disk/by-id/REPLACE_WITH_VERIFIED_DISK";
  terrace.production = {
    instance = "production";
    memoryMiB = 15360;
    operatorKeys = ["ssh-ed25519 AAAA... operator"];
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
nix build --no-link --print-out-paths --impure --expr \
  'import ./infra/nixos/production-config.nix {
     role = "app"; localModule = /private/deployment/host-app.nix;
     webArchive = /protected/terrace-web-<rev>-linux-x64-gnu.tar.gz;
     cmsArchive = /protected/terrace-cms-<rev>-linux-x64-gnu.tar.gz;
     tfcSource = /source/tfc-at-<rev>; }' -A config.system.build.toplevel
```

Build on the Linux builder under the shared lock, never on a Mac. The install
itself follows [cloud-rescue-install](cloud-rescue-install.md); the only
addition is that the installer stages the encrypted secrets file next to the
operator-generated host key (see [secrets](secrets.md#bootstrap-of-a-brand-new-host)).

## Verification

- Evaluation check (no builds; builder, shared lock):
  `nix eval --raw --impure --option builders "" --file infra/nixos/production-eval-check.nix`
- VM proof (builder; the wrapper takes the shared lock itself):
  `BUILD_LOCK=$HOME/.falcon-heavy.lock platform/production-check WEB_ARCHIVE CMS_ARCHIVE TFC_SOURCE`
- Tool unit tests (CI): `python3 platform/production/test_terrace_secrets.py -v`

The VM proof instantiates the **same** app role with `instance = "rehearsal"`,
synthetic `.invalid` hostnames, a throwaway escrow key, a host key generated at
test time and secrets generated by `terrace-secrets` inside the operator guest.
A rehearsal-only harness (`platform/production/rehearsal.nix`) adds only what
the shared protocol needs: trust in a synthetic CA, the protocol scripts, a
temporary Keycloak bootstrap administrator supplied through sops and removed by
the protocol, and the stand-in for the installer that copies the staged host key
and ciphertext. The production check rejects that harness.

## Out of scope, with what each needs

| Follow-up | Gap | What it needs |
| --- | --- | --- |
| TLS issuance and renewal (ACME) | G-12 | Certificates are a static secret pair here. ACME needs port 80 (HTTP-01) or DNS-01 credentials, a renewal path that bypasses sops, expiry alerting, and a test against a local ACME server (pebble) |
| Mail transport | G-20 | Owner's provider choice; SMTP credentials become sops secrets; outbox delivery job; a test against a local sink |
| Alerting and log retention | G-23 | Delivery channel, external probe, certificate and backup-age alerts that reach a person, a tested failing check |
| Keycloak production administration | G-15, G-39 | First administrator via `kc.sh bootstrap-admin` with the escrowed `identity/bootstrap-admin-password`, its removal, role grants, OIDC client-secret rotation through the admin API, realm reconciliation |
| Production sizing | G-9 | Owner decision; `memoryMiB` and the host type follow it. The core app writers' ceilings sum to 6912 MiB |
| Documenso, Windmill, Vaultwarden in production | 6.18-6.20, 6.22 | Measure, then render their inputs as sops templates and add their realm clients to the realm template, one at a time |
| Restore promotion on real data | G-22 | Owner-approved procedure based on the rehearsed sequence above |
| K3s datastore backup | G-21 | Only if a workload is added |
