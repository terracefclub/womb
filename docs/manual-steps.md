# External prerequisites

Updated 2026-10-28. Record completion evidence; never credentials or exports.

| Prerequisite | Completion criterion | Resume |
| --- | --- | --- |
| GitLab API | Owner login verified 2026-10-28; use the authenticated credential without printing it or changing user config | Available: issues/labels and MR management |
| Hetzner project access | Owner completed Console two-factor sign-in 2026-10-28 and observed the existing Womb project; dedicated Terrace project identity, its project-scoped credential, stock and account quote still need verification | Owner deferred sign-in 2026-10-29. Then: read-only project token outside chat, authenticated CX/CPX stock and currency/VAT check, existing-server inspection (see [hosting plan](access-relay.md)). Targeted preview only after protected credential and quote |
| CMS choice | Payload + SvelteKit selected 2026-10-28 | In progress: CMS implementation and integration |
| Off-site backup targets | Storage Box sub-account (SFTP key, verified host key) and optional B2 bucket/application key created by the owner; repository passwords escrowed in the password manager; secrets delivered to hosts as 0400 files; `terrace-backup init` done and an off-site restore drill recorded | Follow [backups](backups.md); never paste credentials into chat, issues or MRs |
| WordPress access | Protected complete DB/files backup and restore; plugin inventory | Preservation/import |
| Automation access | Windmill/Documenso endpoints and operator access verified | Consumer map, rotation, tests |
| Identity ownership and public origins | Owner names the primary/backup identity administrator, enrollment approver and exact public HTTPS origins, and approves data retention | Resume the [identity host](identity-host.md) release gates: production artifact, secret delivery, per-user enrollment/recovery and an isolated database restore; no live activation is configured |
| On-premises access | Hardware/disks/network and camera/device inventory | Host and HA/Frigate integration |

Use project-local CLI state and protected secret-manager references. Never paste
tokens into chat, issues, MRs, command arguments or committed examples.

Before real community invitations: identify the authoritative current-member/alumni roster, access approver, and a moderator with a backup. Synthetic prototype work does not depend on these names.
