# Delivery backlog

Updated 2026-10-28. Delivery is tracked in the [Womb milestone](https://gitlab.com/terrace/tech/womb/-/milestones/1)
and [website milestone](https://gitlab.com/terrace/tech/tfc-website/-/milestones/1).
Each issue carries acceptance criteria; a merged foundation or preview does not
establish a live deployment. Link each small MR to its issue.

| Order | Work item | Acceptance criteria | State |
| --- | --- | --- | --- |
| 1 | [Contributor workflow](https://gitlab.com/terrace/tech/womb/-/issues/1) | Credential-free devenv, local CLI state, installed hooks, shell contract | Merged in Womb !1 |
| 2 | [Single-node platform](https://gitlab.com/terrace/tech/womb/-/issues/2) | Pinned NixOS/K3s, private API, retained storage, VM test | Foundation merged in Womb !2; capacity/recovery follow-up remains |
| 3 | [Website design](https://gitlab.com/terrace/tech/tfc-website/-/issues/1) | SvelteKit preview/hot reload, events/newsletters, keyboard/mobile checks | Preview merged in website !1; [frontend !2](https://gitlab.com/terrace/tech/tfc-website/-/merge_requests/2) merged for published Payload content; design review remains |
| 4 | [CMS spike](https://gitlab.com/terrace/tech/tfc-website/-/issues/2) | Publish/revise/preview; roles; export/import; memory measurement | Payload implementation in progress |
| 5 | [Hetzner bootstrap](https://gitlab.com/terrace/tech/womb/-/issues/3) | Targeted plan, approved cost ceiling, separate relay/application hosts, closure diff/health | [Womb !5](https://gitlab.com/terrace/tech/womb/-/merge_requests/5) merged; install/boot VM passed. Strictly-below-$80 plan with Pulumi defaults (relay CX23 + app CX43, $37.18 steady / $44.77 overlap at 2026-10-31 public prices; CX stock reported unavailable). Access recovery, protected backup/restore, dedicated credential, account quote and stock check pending; see [relay plan](access-relay.md) |
| 6 | [Backup and restore](https://gitlab.com/terrace/tech/womb/-/issues/4) | Encrypted DB/media backup; isolated restore; reconciliation; cost | Module and two-VM restore test in `platform/backup.nix` ([docs](backups.md)); no real Storage Box/B2, secrets delivery or off-site drill yet. Existing server's provider backups are disabled, and no recoverable data copy has been verified |
| 7 | [WordPress preservation](https://gitlab.com/terrace/tech/tfc-website/-/issues/3) | Full protected DB/files backup, plugin/table inventory, restore | Access pending |
| 8 | [Membership hardening](https://gitlab.com/terrace/tech/womb/-/issues/5) | Contract tests, authenticated webhook, idempotency, retry/replay, email outbox | Access pending |
| 9 | [Credential remediation](https://gitlab.com/terrace/tech/womb/-/issues/6) | Map consumers, verified backup, rotation, old credentials rejected | Access pending |
| 10 | [CMS import](https://gitlab.com/terrace/tech/tfc-website/-/issues/4) | Stable IDs, repeatable import, count/hash reconciliation, redirects | Depends on 4, 7 |
| 11 | [Public cutover](https://gitlab.com/terrace/tech/tfc-website/-/issues/5) | Content review, integration parity, accessibility/performance, rollback | Depends on 5, 6, 10 |
| 12 | [CDN](https://gitlab.com/terrace/tech/womb/-/issues/7) | Public cache; preview/admin/member bypass; cookie/invalidation tests | After origin acceptance |
| 13 | [Documenso/Windmill](https://gitlab.com/terrace/tech/womb/-/issues/8) | Per-user access, signing/email/storage, workflow parity, restore | After capacity check |
| 14 | [On premises](https://gitlab.com/terrace/tech/womb/-/issues/9) | Hardware/disks/network inventory; HA/Frigate integration | Hardware/access pending |
| 15 | [Community pilot](https://gitlab.com/terrace/tech/tfc-website/-/issues/6) | Approval, per-user Keycloak, privacy/moderation, opt-in mentorship | Payload/SvelteKit pilot; identity and permission-enforced collections merged; interface validation underway |

## Release gates

- Report unit, integration, build, browser, deployment and restore evidence
  separately. Static checks do not establish live health.
- Public routes: semantic headings, keyboard access, mobile layout, no broken
  internal links, correct timezones, empty/error states, reduced motion.
- Content: drafts stay private; APIs enforce permissions; reject unsafe HTML;
  bound uploaded media; preserve old URLs.
- Membership: duplicate events create no duplicate records/messages; invalid
  authentication fails closed; bounded retries and visible replay status.
- Operations: dependency/image pins, resource bounds, probes, rollback, log
  redaction, backup age alerts, restore evidence and access instructions.
- Performance: use a representative US/mobile scenario and explicit budgets;
  record measurements instead of promising an unmeasured score.

Issue descriptions: problem, intended behavior, acceptance criteria. Labels:
`platform`, `website`, `migration`, `integrations`, `community`. Distinguish ready,
in progress, review, and external dependency. Keep evidence in the linked MR.
