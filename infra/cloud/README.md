# Dedicated Hetzner bootstrap plan

This Pulumi project declares exactly four resources in the **dedicated
Terrace Hetzner Cloud project**: one SSH public key, one inbound-SSH-only
firewall, and two protected EU x86_64 shared-vCPU servers, each with a primary
IPv4 and Hetzner image backups by default: a small Headscale/DERP **relay**
(`cx23`, `hel1`) and a heavier single-node **application** host (`cx43`,
`nbg1`). The reviewed plan, its arithmetic and the sources are in
[`docs/access-relay.md`](../../docs/access-relay.md). It uses a current Debian
image only as a disposable bootstrap OS. NixOS installation is a separate,
gated step. It does not touch the root legacy Pulumi program or any existing
server. The bootstrap firewall opens SSH only; relay HTTPS/STUN rules belong
to the later install step.

The token must be issued inside the dedicated project and passed as
`HCLOUD_TOKEN` at runtime. [Hetzner tokens are project-bound](https://docs.hetzner.com/cloud/api/getting-started/using-api/),
but this program cannot prove the project's human-readable name from the API.
`dedicatedProjectConfirmed=terrace` is an operator attestation, not an API
identity check. Confirm the project in Hetzner Console before preview, then
verify that its API token sees only the expected dedicated-project inventory.
Pin the token's SHA-256 fingerprint in stack config so a different inherited
token fails before provider calls. Do not print the token, place it in Pulumi
config, or persist it in this repo.

## Required plan inputs

Use the ignored `Pulumi.bootstrap.yaml` stack config for these **non-secret**
values. No default SSH source, disk path, or provider image is assumed.

| Config key | Meaning |
| --- | --- |
| `dedicatedProjectConfirmed` | Exactly `terrace`, after checking the console |
| `tokenSha256` | Lowercase SHA-256 of the dedicated project token, computed locally without exposing the token |
| `bootstrapImage` | Current `debian-N` image name from the project API |
| `operatorPublicKeyFile` | Absolute path to a readable `.pub` file; never the private key |
| `sshCidrs` | Comma-separated, narrow source CIDRs for SSH; no open internet |
| `quoteDate` | Date of the account quote, within seven calendar days |
| `relayServerMonthlyUsd` | Relay server recurring monthly USD quote excluding IPv4 and tax |
| `appServerMonthlyUsd` | Application server recurring monthly USD quote excluding IPv4 and tax |
| `ipv4MonthlyUsd` | Primary IPv4 recurring monthly USD price per address (two are counted) |
| `offsiteBackupMonthlyUsd` | Positive allowance for the separate encrypted backup targets |
| `existingServerMonthlyUsd` | Positive monthly charge of the legacy server and its IPv4 that stays billable during migration |
| `otherMonthlyUsd` | Other recurring charges, including egress and one-time snapshot margin |
| `taxPercent` | Applicable account tax rate |

Optional keys and their defaults (the reviewed recommendation; any override
must still pass the budget gate):

| Config key | Default | Meaning |
| --- | --- | --- |
| `relayServerType` | `cx23` | One of `cx23`, `cx33`, `cx43`, `cx53`, `cpx12`, `cpx22`, `cpx32`, `cpx42` |
| `appServerType` | `cx43` | Same allowlist (x86_64 shared vCPU only; Arm is untested for the NixOS profile) |
| `relayLocation` | `hel1` | `fsn1`, `nbg1`, or `hel1`; a different site from the application host |
| `location` | `nbg1` | Application host location; same allowlist |
| `relayProviderBackups` | `true` | Hetzner image backups for the relay |
| `appProviderBackups` | `true` | Hetzner image backups for the application host |

The program adds [Hetzner's 20% image-backup charge](https://docs.hetzner.com/cloud/billing/faq/)
for each backed-up host, two IPv4 addresses, the off-site allowance, other
charges and tax, and fails unless **both** the steady state and the overlap
month (steady state plus the legacy server) are **strictly below $80.00**.
This is a quote guard, not a billing cap: prices, stock, usage and tax must be
rechecked in the actual account before resource creation. Outputs are
`quotedSteadyMonthlyUsd` and `quotedOverlapMonthlyUsd`. List prices checked
2026-10-31 and the VAT/currency assumptions are in the plan document; US
locations are excluded because CX does not exist there and CPX is too costly.

Provider image backups are disk snapshots, not database-consistent backups.
The off-site allowance is reserved for a separately encrypted, tested restic
repository (B2/S3 or another approved target). Google Drive is intended for
human-facing file integration and is **not** assumed to be the backup target.
No application data should be placed on these servers until the DB, media and
K3s token backup/restore gates have passed.

## Existing project evidence and preservation gate

Verified in Hetzner Console on 2026-10-28: the **existing Womb project** has
one running Ashburn CPX11 (2 vCPU, 2 GB RAM, 40 GB disk). Provider backups are
disabled; no project firewalls, volumes or object storage were shown. A
pre-existing write token is present. This is not the selected dedicated
Terrace project, and Console inventory does not establish what workloads or
data are on the server, whether separate backups exist, or whether it can be
restored. Do not repurpose its credential or change the server.

Before any replacement, inventory the existing server's OS, running services,
databases, volumes, application files, DNS dependencies and credential consumers
through approved protected access. Obtain independently recoverable copies of
its data and configuration, then validate an isolated restore and a rollback
path. The original server stays intact until those checks and application
reconciliation pass.

The US$80 guard is for total retained cloud spending. Obtain the actual
billing currency and itemized account charges before filling the quote: put the
existing server and its IPv4 in `existingServerMonthlyUsd`, any other retained
charges in `otherMonthlyUsd`, and the new servers, IPv4, provider backups,
off-site allowance and tax in their own fields. No account-specific amount or
currency has been verified yet. Do not resize the existing server (a rescale
moves it to current, higher prices). Public list prices are not a substitute
for the account quote. If billed in another currency, record the conversion rate, date and a
conservative USD margin outside Git before entering the USD fields. If the
combined amount reaches US$80, defer provisioning until a safe
retirement plan or a revised owner-approved budget exists.

## Review and recovery sequence

1. Obtain the dedicated project token through a protected channel. Verify
   project scope in Console; obtain the operator's `.pub` file and narrow SSH
   source CIDR. Use an ignored local Pulumi backend for **preview only**.
2. Fill the non-secret stack config and run `bun install --frozen-lockfile`,
   `bun test`, and `bun run typecheck`. Select the `bootstrap` stack in this
   project and run `./preview.sh bootstrap`; it targets only the four URNs
   in that script. The script suppresses provider stderr, which may contain
   sensitive request details. Keep the reviewed preview in a protected channel.
3. Before any matching targeted `pulumi up`, move state to a durable backend
   recoverable without the server, verify the quote and project inventory,
   then review the exact target list and replacement/deletion diff. Approval
   for provisioning is separate from this source change.
4. On a fresh disposable server, verify console and Rescue SSH access before
   installing NixOS. Follow [the disk profile](../nixos/README.md) after
   recording the actual disk identity. Do not change an existing server.
5. Recovery must remain possible from Git, Pulumi state, Hetzner Console/Rescue,
   and encrypted off-site backups. [Rescue is network-booted and does not
   modify disks by itself](https://docs.hetzner.com/cloud/servers/getting-started/rescue-system/).
   Rebuild only after verifying the target disk and a restorable copy. Restore
   K3s DB and token together, then PVs and application data, and check health
   before traffic moves. A restore drill is still required.

The public website, Payload, SvelteKit, Vaultwarden, GitOps controller,
Google Drive integration, DNS and application backups are outside this plan.
