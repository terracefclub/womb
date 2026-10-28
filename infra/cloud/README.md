# Dedicated Hetzner bootstrap plan

This Pulumi project declares exactly three resources in the **dedicated
Terrace Hetzner Cloud project**: one SSH public key, one inbound-SSH-only
firewall, and one protected EU `cx33` x86_64 server with a primary IPv4 and
Hetzner image backups. It uses a current Debian image only as a disposable
bootstrap OS. NixOS installation is a separate, gated step. It does not touch
the root legacy Pulumi program or any existing server.

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
| `location` | `fsn1`, `nbg1`, or `hel1`; verify current CX33 stock |
| `bootstrapImage` | Current `debian-N` image name from the project API |
| `operatorPublicKeyFile` | Absolute path to a readable `.pub` file; never the private key |
| `sshCidrs` | Comma-separated, narrow source CIDRs for SSH; no open internet |
| `quoteDate` | Date of the account quote, within seven calendar days |
| `serverMonthlyUsd` | CX33 recurring monthly USD price excluding IPv4 and tax |
| `ipv4MonthlyUsd` | Primary IPv4 recurring monthly USD price |
| `offsiteBackupMonthlyUsd` | Positive allowance for a future separate encrypted backup target |
| `otherMonthlyUsd` | Expected other recurring charges, including egress margin |
| `taxPercent` | Applicable account tax rate |

The program adds [Hetzner's 20% image-backup charge](https://docs.hetzner.com/cloud/billing/faq/)
to the server quote, adds IPv4, off-site backup allowance, other charges and
tax, then fails above **$20/month**. This is a quote guard, not a billing cap:
prices, stock, usage and tax must be rechecked in the actual account before
resource creation. The [published 2026 price table](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)
listed EU CX33 at $9.99/month excluding IPv4 and tax when checked on
2026-10-28; [Primary IPv4 pricing](https://docs.hetzner.com/general/infrastructure-and-availability/ipv4-pricing/)
listed $0.60/month. The account quote and stock remain unverified.

Provider image backups are disk snapshots, not database-consistent backups.
The off-site allowance is reserved for a separately encrypted, tested restic
repository (B2/S3 or another approved target). Google Drive is intended for
human-facing file integration and is **not** assumed to be the backup target.
No application data should be placed on this server until the DB, media and
K3s token backup/restore gates have passed.

## Review and recovery sequence

1. Obtain the dedicated project token through a protected channel. Verify
   project scope in Console; obtain the operator's `.pub` file and narrow SSH
   source CIDR. Use an ignored local Pulumi backend for **preview only**.
2. Fill the non-secret stack config and run `bun install --frozen-lockfile`,
   `bun test`, and `bun run typecheck`. Select the `bootstrap` stack in this
   project and run `./preview.sh bootstrap`; it targets only the three URNs
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
