# Delivery backlog

Updated 2026-10-27. These are issue-ready work items; planned items are not
delivered. Create GitLab issues once authenticated API access is available and
link each small MR to its issue.

| Order | Work item | Acceptance criteria | State |
| --- | --- | --- | --- |
| 1 | Contributor workflow | Credential-free devenv, local CLI state, installed hooks, clean-shell test | In progress |
| 2 | Single-node platform | Pinned NixOS/K3s, private API, retained storage, VM test | In progress |
| 3 | Website design | SvelteKit preview/hot reload, events/newsletters, keyboard/mobile checks | In progress in TFC Website |
| 4 | CMS spike | Publish/revise/preview; roles; export/import; memory measurement | Selection pending |
| 5 | Hetzner bootstrap | Targeted plan, within $20 total, isolated resources, closure diff/health | Account scope pending |
| 6 | Backup and restore | Encrypted DB/media backup; isolated restore; reconciliation; cost | Planned |
| 7 | WordPress preservation | Full protected DB/files backup, plugin/table inventory, restore | Access pending |
| 8 | Membership hardening | Contract tests, authenticated webhook, idempotency, retry/replay, email outbox | Planned |
| 9 | Credential remediation | Map consumers, verified backup, rotation, old credentials rejected | Access pending |
| 10 | CMS import | Stable IDs, repeatable import, count/hash reconciliation, redirects | Depends on 4, 7 |
| 11 | Public cutover | Content review, integration parity, accessibility/performance, rollback | Depends on 5, 6, 10 |
| 12 | CDN | Public cache; preview/admin/member bypass; cookie/invalidation tests | After origin acceptance |
| 13 | Documenso/Windmill | Per-user access, signing/email/storage, workflow parity, restore | After capacity check |
| 14 | On premises | Hardware/disks/network inventory; HA/Frigate integration | Hardware/access pending |
| 15 | Community pilot | Platform, invitation/privacy/moderation, opt-in mentorship | Decision pending |

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
