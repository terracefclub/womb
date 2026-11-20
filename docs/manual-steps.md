# External prerequisites

Updated 2026-11-18 (rows below marked historical were last checked 2026-10-28). Record completion evidence; never credentials or exports. The [owner runbook](owner-runbook.md) lists each owner input with its default and what it unblocks.

| Prerequisite | Completion criterion | Resume |
| --- | --- | --- |
| GitLab API (historical) | Owner login verified 2026-10-28; use the authenticated credential without printing it or changing user config | GitLab is now a historical audit trail and one-way mirror; GitHub is primary since 2026-11-06 ([migration](github-migration.md)) |
| Hetzner project access | Owner observed the existing Womb project in Console on 2026-10-28. On 2026-11-07 the owner confirmed use of the project behind a protected project-scoped Read & Write token and preservation of the existing server; the read-only inventory passed (one running CPX11 in Ashburn, two primary IPs, one SSH key). Disposable, lease-bound probes and known-full leases 1 to 4 have used it since, each with a fresh explicit approval; install, boot and networking on Hetzner were proven by lease 3 (2026-11-13), and lease 4 (2026-11-18) was stopped by builder-lock contention before the plan step. | Still open: Console access to the dedicated project is not recorded as provided, and the old server's access is not recovered (its SSH key is unknown), inventory and a protected copy of the existing server, and the production sizing decision. Never paste the token anywhere. See [Womb #11](https://github.com/terracefclub/womb/issues/11) and the [guarded preview](../infra/cloud/README.md). |
| CMS choice | Payload + SvelteKit selected 2026-10-28 | In progress: CMS implementation and integration |
| Off-site backup targets | Storage Box sub-account (SFTP key, verified host key) and optional B2 bucket/application key created by the owner; repository passwords escrowed in the password manager; secrets delivered to hosts as 0400 files; `terrace-backup init` done and an off-site restore drill recorded | Follow [backups](backups.md); never paste credentials into chat, issues or MRs |
| WordPress access | Protected complete DB/files backup and restore; plugin inventory | Preservation/import. The WXR export and Gravity Forms definitions were supplied 2026-11-03; the Kinsta ZIP and Kinsta-held settings are still outstanding |
| Automation access | Windmill/Documenso endpoints and operator access verified | Consumer map, rotation, tests |
| Identity ownership and public origins | Owner names the primary/backup identity administrator, enrollment approver and exact public HTTPS origins, and approves data retention | Resume the [identity host](identity-host.md) release gates: production artifact, secret delivery, per-user enrollment/recovery and an isolated database restore; no live activation is configured |
| TLS certificates by ACME | Owner chose the CA and accepted its subscriber agreement, named a contact address, and published DNS records for every public name pointing at its host before adoption; first issuance seen with `terrace-tls status` | [TLS](tls.md); until then the manual mode (`tls/*` delivered with `terrace-secrets`) applies |
| On-premises access | Hardware/disks/network and camera/device inventory | Host and HA/Frigate integration |

Use project-local CLI state and protected secret-manager references. Never paste
tokens into chat, issues, MRs, command arguments or committed examples.

Before real community invitations: identify the authoritative current-member/alumni roster, access approver, and a moderator with a backup. Synthetic prototype work does not depend on these names.
