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

The disposable cloud rehearsal uses a separate, expiring run lease. Its
baseline and receipts live in a private directory outside Git. The lease
binds a project credential and a random 32-hex run ID to the complete
pre-existing inventory. Cleanup requires the expected name and run label,
excludes all baseline IDs and checks attached IPs and firewalls before
deleting any run server. It waits for provider actions and verifies absence
in three reads, with the baseline resources and associations preserved.

`rehearsal_lease.py prepare` is read-only. `retire-original` requires a
verified off-host backup receipt and fails if the replacement cohort already
exists. `cleanup` closes the lease to future creates before removing its
resources. A launcher must hold the same lease lock across each create and
arm independent, time-bounded cleanup before making the first write. The
lease helper alone does not start a timer or provision resources. It never
deletes unrecognized resources, even if they appear after the baseline.

On Falcon, `rehearsal_timer.py arm` schedules a transient systemd timer at
the lease expiration. It runs the private `rehearsal_lease.py` copy as the
operator, with bounded cleanup attempts and control-group termination. The
creation launcher must check the armed timer while holding the lease lock.
`disarm` requires the same run's verified final absence receipt and checks
that the exact timer and service are inactive. A transient timer survives
SSH disconnects. The protected lease remains the recovery reference if
the builder restarts or loses API access.

The separate `rehearsal-create` project has three isolated stacks. The
foundation creates only the operator key and SSH firewall. Each cohort
creates an application server and relay with explicitly labelled IPv4s.
`rehearsal_apply.py` requires a bounded Falcon service for each preview and
apply. It checks both the preview and the saved plan for exact create-only
scope. Apply consumes the reviewed digest under the lease lock, and binds
it to the reviewed Git source, input, stack config and plan bytes. Stage
only the reviewed creator and its pinned dependency files in the private
project directory. Compute `--source-sha` with `git_source_sha` from the
reviewed commit, then install the locked dependencies there before loading
the project token. Each phase has one dispatch marker. Cleanup reconciles a
failed or interrupted write, records any unresolved provider outcome, and
keeps disarm closed while uncertainty remains. Replacement requires the original
cohort's verified absence. `binding` independently reads each created host
and writes a short-lived private ID/address receipt for the installer.

The operator helper uses the same lease and bounded systemd writer unit
for Rescue boot, an authenticated provider console receipt, and the installed
boot. Each action rechecks the exact provider server and run-owned key. The
provider console passwords are held only in private, short-lived receipts.
The installed boot requires the reviewed disk-install receipt for that role.

For a standalone Rescue console check, add `"probeOnly": true` to the
reviewed `original` input. The creator and plan gate require one relay and
its IPv4, alongside the existing foundation key and firewall. The probe
requests Hetzner's Rescue password mode without passing an SSH key. First
compare the SSH host key through the authenticated provider console, then
use the confirmed pin for a read-only SSH check. Keep password entry private.
This mode cannot format a disk, boot an installation, or authorize
replacement. Clean up and verify the project baseline as with a full lease.

For the known-host bootstrap, the reviewed cohort input carries
`bootstrapHostKeys` for its exact roles. Each entry binds the public
Ed25519 key and the hash of its private sibling file. The controller
derives and checks the public key before preview or apply. It also binds
the root operator key to the previously verified foundation key. The provider
receives the one-use host key as secret cloud-init user data. Keep the
private file and backend outside Git and use a different installed key.
Treat guest metadata as accessible to the temporary host and run no
application services there.

Replacement preserves the original bootstrap mode and requires a different
bootstrap key for every host. The saved plan and final checkpoint must
contain encrypted user data.
The controller uses Pulumi's text renderer for secret-bearing updates.
The pinned CLI can include raw secret values in JSON update events even
with `--show-secrets=false`. The controller also checks bounded private
artifacts and output for plaintext before accepting the phase.

The escrow helper creates per-run TLS material and an independent restic
password. Its bounded Falcon repository service uses only an owner-private
Unix socket, without modifying the personal SSH server. The cloud hosts
reach it only through the pinned reverse SSH connection. Keep the encrypted
repository off both cohorts and verify its data before retiring the original
hosts. Stop the exact repository service after the final verification.

Run the offline cleanup tests from the repository shell with
`python3 -B infra/cloud/test_rehearsal_lease.py -v` and
`python3 -B infra/cloud/test_rehearsal_timer.py -v`. Test the plan gate with
`python3 -B infra/cloud/test_rehearsal_apply.py -v`.

### Persistent bootstrap plan

1. Obtain the dedicated project token through a protected channel. Verify
   project scope in Console, then use read-only inventory commands. A
   project-scoped Read & Write token can support the inventory and later
   authorized creation and teardown. Obtain the operator's `.pub` file and
   narrow SSH source CIDR. Use an ignored local Pulumi backend for **preview only**.
2. Fill the non-secret stack config and run `bun install --frozen-lockfile`,
   `bun test`, and `bun run typecheck`. Select the `bootstrap` stack in this
   project and run the guarded preview from the repository root:

   ```sh
   direnv exec . ./infra/cloud/preview.sh bootstrap --review-file /protected/terrace/preview.json
   ```

   The wrapper requires the four bootstrap resources and accepts only create or
   unchanged steps, including Pulumi's expected stack and provider bookkeeping.
   It rejects replacements, updates, deletions, unexpected URNs and incomplete
   summaries. The optional review file is written once as mode 0600 in a private
   directory outside Git. It contains the four target inputs and operations,
   with the public key hashed. The terminal receives fixed resource names and
   operations. Provider diagnostics and configuration stay out of the report.
   The command has a two-minute deadline.

   Run the offline regressions with
   `direnv exec . python3 -B infra/cloud/test_preview.py -v`.
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
