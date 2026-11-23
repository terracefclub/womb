# Delivery backlog

Updated 2026-11-20. GitHub is primary (2026-11-06): current work is tracked in the linked GitHub issues. The
GitLab issue, merge-request and milestone links below are the historical audit trail ([Womb milestone](https://gitlab.com/terrace/tech/womb/-/milestones/1),
[website milestone](https://gitlab.com/terrace/tech/tfc-website/-/milestones/1), both historical). GitLab `!N` numbers are not
GitHub PR numbers; see [migration and mirroring](github-migration.md). The step-by-step owner view, with proof status and open gaps,
is the [owner runbook](owner-runbook.md).
Each issue carries acceptance criteria; a merged foundation or preview does not
establish a live deployment. Link each small MR to its issue.

| Order | Work item | Acceptance criteria | State |
| --- | --- | --- | --- |
| 1 | [Contributor workflow](https://gitlab.com/terrace/tech/womb/-/issues/1) (historical GitLab issue; no GitHub equivalent) | Credential-free devenv, local CLI state, installed hooks, shell contract | Merged in Womb !1 |
| 2 | [Single-node platform](https://gitlab.com/terrace/tech/womb/-/issues/2) (historical; GitHub [womb #11](https://github.com/terracefclub/womb/issues/11)) | Pinned NixOS/K3s, private API, retained storage, VM test | Foundation merged in historical Womb !2; optional [identity substrate](identity-host.md), disabled by default; production realm, enrollment, capacity and recovery follow-up remain. Production host profiles (`app`, `relay`) with sops-nix secrets delivery are proven in a VM check on synthetic data (Womb PR 50, [production profile](production-profile.md), [secrets](secrets.md)); certificate issuance, renewal, the port-80 redirect and an expiry check by ACME are proven in a VM check against a local ACME server, not against a public CA or real DNS ([TLS](tls.md)); nothing is deployed and the Hetzner leases have not installed them. Keycloak production administration (first administrator from the escrowed bootstrap password, administrators, editor accounts and role grants, listing, client-secret rotation, realm reconciliation and break-glass) is a root-only host tool proven in its own VM check ([identity administration](identity-admin.md)). These VM proofs with a build-time random test CA trust it by store path or through a bundle assembled at run time, never a cached `security.pki` copy that a partial garbage collection could split from the signing key: the production proof (PR 55), the identity-administration proof (PR 53), the website gateway check, the relay check and the all-services rehearsal (PR 58), and the identity, identity-restore, Vaultwarden runtime and functional, Documenso runtime and functional, and Windmill runtime, functional and roster checks (PR 61). The TLS proof makes its CA at run time; the backup check's certificate is served by store path and trusted by no client |
| 3 | [Website design](https://gitlab.com/terrace/tech/tfc-website/-/issues/1) (historical; GitHub [tfc-website #5](https://github.com/terracefclub/tfc-website/issues/5)) | SvelteKit preview/hot reload, events/newsletters, keyboard/mobile checks | Preview merged in historical website !1; [frontend !2](https://gitlab.com/terrace/tech/tfc-website/-/merge_requests/2) (historical) merged for published Payload content. Website `dev` `dad0a20` (2026-11-18) also serves imported news and pages (`/news/`, `/pages/<slug>/`) and the active-redirect consumer (website PR 10); proven with the synthetic fixture and in a local rehearsal on the owner's real export (local database only); nothing is imported to any real target. Design review remains |
| 4 | [CMS spike](https://gitlab.com/terrace/tech/tfc-website/-/issues/2) (historical; GitHub [tfc-website #5](https://github.com/terracefclub/tfc-website/issues/5)) | Publish/revise/preview; roles; export/import; memory measurement | Linux bundles of website `dev` `eab4189` (twelve-migration ledger including the bulk-roster journal, packaged `bin/sweep`, realm admin events) are pinned and pass the [native host/recovery check](website-host.md) and the [HTTPS gateway check](website-gateway.md) with the member lifecycle (2026-11-23). The [all-services rehearsal](full-rehearsal.md) passed on its second run at this pin; the first failed in the concurrent Documenso check, since explained and fixed (see below). Actual public activation and combined capacity remain gated |
| 5 | [Hetzner bootstrap](https://gitlab.com/terrace/tech/womb/-/issues/3) (historical; GitHub [womb #11](https://github.com/terracefclub/womb/issues/11)) | Dedicated Terrace credential, protected existing data, targeted plan, tested bootstrap/health/teardown | [Womb !5](https://gitlab.com/terrace/tech/womb/-/merge_requests/5) (historical) merged; install/boot VM passed. Earlier October31 price estimates are historical; November4 owner direction removes unanswered budget questions as a code/rehearsal gate. The owner authorised a dedicated Terrace project token on 2026-11-07 (read-only inventory first; the existing server is outside every write target). Disposable, lease-bound Hetzner probes and known-full leases 1 to 5 have run since. Proven on Hetzner by lease 3 (run `90e965da`, 2026-11-13): host-key pinning, RAM installer, install plan, FORMAT and install, reboot from the installed disk and networking. Lease 4 (run `7d217921`, 2026-11-18) was stopped by builder-lock contention before the plan step and proved nothing beyond lease 3. Lease 5 (run `efc9794a`, 2026-11-20, staged at trunk `bb0d526`) passed end to end and was torn down: services launch from installed disks, a backup to a restic repository that was a directory on the build machine, reached through a reverse SSH forward (not a storage provider account, not off-site), retiring the original pair and restore onto a replacement pair, for the rehearsal profile on synthetic data on private temporary hosts, one run on one day ([cloud rehearsal](cloud-rehearsal.md#lease-5-result-2026-11-20)); not proven by it: the production profile, ACME, the Keycloak administration tool, real data migration, cutover, alerting. Lease 6 has not been requested. No production host exists and no production provider apply was made; access recovery for and a protected copy of the existing server remain open. See [relay plan](access-relay.md), [cloud rescue install](cloud-rescue-install.md) and the [owner runbook](owner-runbook.md) Part 6 |
| 6 | [Backup and restore](https://gitlab.com/terrace/tech/womb/-/issues/4) (historical; GitHub [womb #11](https://github.com/terracefclub/womb/issues/11)) | Encrypted DB/media backup; quiescent capture; isolated restore; reconciliation; teardown | Module and two-VM restore test in `platform/backup.nix` ([docs](backups.md)); no real Storage Box/B2 or off-site drill yet; secrets delivery (sops-nix, offline escrow key, rotation and re-keying) is proven in the production VM check on synthetic data, with no real secret and no escrow retrieval drill. Current existing-server backup state and real recoverable copies remain unverified by this builder work |
| 7 | [WordPress preservation](https://gitlab.com/terrace/tech/tfc-website/-/issues/3) (historical; GitHub [tfc-website #3](https://github.com/terracefclub/tfc-website/issues/3)) | Full protected DB/files backup, plugin/table inventory, restore | Full protected WXR export and Gravity Forms definitions supplied 2026-11-03; the Kinsta full backup ZIP and Kinsta settings are still outstanding (access pending) |
| 8 | [Membership hardening](https://gitlab.com/terrace/tech/womb/-/issues/5) (historical; GitHub [womb #12](https://github.com/terracefclub/womb/issues/12)) | Contract tests, authenticated webhook, idempotency, retry/replay, email outbox | Opt-in [roster replay step](membership-roster.md) passed the original 11 cases through a real Windmill worker with pinned offline SDKs and fake transports; live webhook mapping/authentication, email ledger and cutover still need operator access |
| 9 | [Credential remediation](https://gitlab.com/terrace/tech/womb/-/issues/6) (historical; GitHub [womb #12](https://github.com/terracefclub/womb/issues/12)) | Map consumers, verified backup, rotation, old credentials rejected | Access pending |
| 10 | [CMS import](https://gitlab.com/terrace/tech/tfc-website/-/issues/4) (historical; GitHub [tfc-website #3](https://github.com/terracefclub/tfc-website/issues/3)) | Stable IDs, repeatable import, count/hash reconciliation, redirects | Planner and applier merged (website PR 8, `dev` `ffbe3ff`; `dev` now `9fa1341`): real-export dry run reproduces 245 items and 87 publish candidates; local rehearsals on the owner's real export (2026-11-18, local database only, aggregate counts recorded, nothing private published) imported, rolled back and re-applied cleanly. Website PR 14 added content quality gates (36 documents are now eligible for bulk publish, down from 56; 24 Divi, 11 empty-body and 5 leftover-shortcode documents wait for editor review), a `publish` command for imported drafts and redirects that follow publication. No import has been applied to any real target, the importer is deliberately not packaged in the release bundle, and there is still no draft preview on the site; media bytes, menus and plugin settings wait on the Kinsta ZIP. Depends on 4, 7 |
| 11 | [Public cutover](https://gitlab.com/terrace/tech/tfc-website/-/issues/5) (historical; GitHub [tfc-website #6](https://github.com/terracefclub/tfc-website/issues/6)) | Content review, integration parity, accessibility/performance, rollback | Depends on 5, 6, 10 |
| 12 | [CDN](https://gitlab.com/terrace/tech/womb/-/issues/7) (historical; GitHub [womb #14](https://github.com/terracefclub/womb/issues/14)) | Public cache; preview/admin/member bypass; cookie/invalidation tests | After origin acceptance |
| 13 | [Documenso/Windmill](https://gitlab.com/terrace/tech/womb/-/issues/8) (historical; GitHub [womb #11](https://github.com/terracefclub/womb/issues/11), which lists GitLab #8 among its sources; live integrations are tracked in [womb #12](https://github.com/terracefclub/womb/issues/12)) | Per-user access, signing/email/storage, workflow parity, restore | Optional [signed Documenso fresh-node recovery](full-rehearsal.md), Windmill per-user SSO/worker/restart and Vaultwarden encrypted item/attachment/restart proofs passed; the complete all-services originals-off replacement recovery and teardown passed with retained data/identity proofs; production capacity and cutover remain gated |
| 14 | [On premises](https://gitlab.com/terrace/tech/womb/-/issues/9) (historical; GitHub [womb #13](https://github.com/terracefclub/womb/issues/13)) | Hardware/disks/network inventory; HA/Frigate integration | Hardware/access pending |
| 15 | [Community pilot](https://gitlab.com/terrace/tech/tfc-website/-/issues/6) (historical; GitHub [tfc-website #4](https://github.com/terracefclub/tfc-website/issues/4)) | Approval, per-user Keycloak, privacy/moderation, opt-in mentorship | Payload/SvelteKit pilot; identity and permission-enforced collections merged. Member lifecycle merged (website PR 9; `dev` now `9fa1341`): applications and approval, single-use invitations, outbox without a mail transport, offboarding, moderation reports, export, erasure and the sweep, plus loopback-only realm provisioning. Website PR 14 fixed the defects the 2026-11-18 real-browser rehearsal found (admin member picker and offboarding in the UI, automatic first-sign-in linking by verified email, erasure of unlinked applications) and re-verified the whole member chain locally; the sweep is packaged as `bin/sweep` (PR 15) and Womb runs it on a daily timer with the outbox in backup and restore, proven in the VM rehearsal (2026-11-19, synthetic data). An opt-in bulk roster onboarding tool (website PR 21) exists, unused and tested in code only; the default stays that everyone applies. Mail delivery, production provisioning and real hosts are not done. Not accepted or deployed |

## Release gates

The owner authorized functional ephemeral builder rehearsals on November4,
superseding earlier cost-ceiling prerequisites. On 2026-11-07 the owner authorised
a dedicated Terrace Hetzner project token; provider validation now runs as
disposable, lease-bound rehearsals that each need a fresh explicit approval, and
it is not a production deployment (see item 5 for what is proven). The optional
[combined rehearsal](full-rehearsal.md) uses extra guests for test isolation;
it does not prescribe a production host count or authorize a provider apply.

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

## Follow-ups recorded 2026-11-20

Small review findings that did not block a merge. Each is open work, not a defect
known to affect a deployed host (none exists).

- `terrace-tls prepare-acme` inspects only the first stored certificate
  directory entry, and silently ignores a mistyped `--overwrite` when no valid
  certificate exists (`platform/production/tls.nix`; [TLS](tls.md)).
- `terrace-identity-admin` ([identity administration](identity-admin.md)): a
  `--dry-run` can turn on master-realm hardening; a failure of the hardening
  step aborts the command that triggered it (no lockout, because
  `break-glass begin` needs no login); the `first-admin` success path's
  hardening call and the timer's hardening-off branch have unit or VM coverage
  gaps.
- The production VM proof's early trust guard (`trust_consistent`) runs on the
  app only, not the relay, and its fingerprint comparison is nearly
  tautological. The proof also no longer exercises the expiry check's default
  system trust store.
- Explained and fixed (test client): the all-services rehearsal's concurrent
  Documenso browser check (`verify --signing`) failed intermittently at
  "retained document: encryption keys (recovery codes)": twice on 2026-11-20
  (PR 58's first commit, before diagnostics; 2 of 8 runs that day) and once on
  2026-11-23 in the website `eab4189` re-pin's first all-services rehearsal (run
  `9d18e784`, restored phase; evidence on the build machine in
  `website-repin-evidence-eab4189/rehearsal-run-9d18e784/evidence/concurrent-failure-restored/documents.stderr`).
  Cause, from the pinned Documenso v2.21.1 source: the server accepts a TOTP code
  only in its own current 30-second step (`verify-2fa-token.ts`, `window = 1`, no
  look-back, no replay store), and a rejected code is an `AppError` without a
  status, which the auth error handler returns as HTTP 500. The 2026-11-23 failure
  was exactly that 500 on `two-factor view-recovery-codes`, 69 ms after a code made
  at second 29 of its step; the same restored data had passed the same call in the
  run's sequential verify minutes earlier (so not a key or data fault), and a reused
  code or the concurrent browsers cannot make a code wrong (no replay store; they
  use other services). Guest clocks in these VM runs differ by up to about a second
  (the TLS proof measures it), enough to put the server in the next step. Not
  directly observed: the server's own step at that instant (the `Date` header has
  whole-second resolution); the client now records it as `serverStepSecond`. Fix:
  the browser helper never generates a code within 3 seconds of either edge of a
  step (`platform/documenso/functional-browser.py`, `totp`), with unit tests that
  drive both edges on a fake clock. It is the only TOTP generator in Womb (two-factor
  `enable` and both `view-recovery-codes` calls use it). With the fix the next
  all-services rehearsal (run `798369c9`, 2026-11-23) passed; one pass does not
  measure the old failure rate (3 failures in about 10 runs).
- The rehearsal's `purge` refuses a failed run by design, but the disk budget
  requires purging a run's disks pass or fail; a failed run's directory currently
  has to be removed by hand after copying its small evidence (done for run
  `9d18e784` on 2026-11-23). Give `purge` an explicit failed-run mode that keeps the
  logs and receipt and removes the disks, repository and escrow.
- Fixed (cause most likely, not directly observed): the TLS VM proof
  (`platform/production/tls-test.nix`) failed once on 2026-11-20 at "relay:
  first boot with the CA unreachable is safe and visible; issuance once it is
  back": the expiry check 0.8 s after `terrace-tls renew` found no verified TLS
  session. nginx already served the new certificate (the proof had waited for
  its fingerprint, and the operator's verified handshake passed). Most likely
  the relay guest's clock trailed the local CA's guest, and the local CA sets
  notBefore to the issue time, so the certificate was not yet valid on the relay.
  Observed: the relay check's own verify error was never captured (the check
  prints only "no verified TLS session"); the skew of about 1.5 s is derived from
  log timestamps of two guests, not measured directly; corroborating, an earlier
  run counted a "certificate is not yet valid" retry of the operator's handshake
  in the same subtest, and later runs measured boot offsets of -1.44 to +0.45 s.
  The proof now sets each guest to the host clock at boot and waits, at most
  30 s, for the served certificate's notBefore before every verifying check
  ([TLS](tls.md#not-proven)). Not observed: that wait's timeout path, and a public
  CA's backdating. Test-only, given that inference.
- Follow-up (product): `terrace-tls check` and `status` should print the verify
  result or reason (for example "certificate is not yet valid") instead of the
  generic "no verified TLS session", so a clock problem is not misread as an
  untrusted chain.
- Follow-up (test): the TLS proof should fail, or assert a small bound, if any
  notBefore wait is needed after the boot sync, so a broken clock sync cannot
  hide behind 30 s waits.
- Known flake (test, wall clock): the TLS proof's "reboot keeps the certificate
  and the account key" subtest fails when the app guest's reboot crosses a full
  hour. `terrace-backup-freshness.timer` is hourly and `Persistent`, so it fires
  at boot; the proof's host has no backup yet, the unit fails ("target primary:
  no successful backup recorded") and the proof's no-failed-units check trips.
  Seen once on 2026-11-23 (reboot at 13:59:58 UTC) during the website `eab4189`
  re-pin; the rerun passed. Not a product defect: a host with a recorded backup
  passes the check. Fix: stop or mask the freshness timer before the reboot in
  that subtest, or exclude it from that one check.
- The all-services rehearsal sets Vaultwarden's and Windmill's `identityCaFile`
  to the synthetic CA itself, so only the standalone Vaultwarden functional and
  Windmill checks exercise those modules' default (the system trust bundle). They
  keep doing so with a bundle assembled at boot inside the guest from the public
  roots and the CA's store path (`platform/test-system-trust.nix`), not a cached
  `security.pki` copy.
- The identity proof's "no copy" scan (every output built from its certificate
  must reference it at run time) never completed in PR 61's evidence: the first
  attempt passed a stale `.drv` path, and at the final head the certificate output
  had already been garbage-collected before the scan. The identity proof itself
  passed, including once with the certificate rebuilt under a new key. Redo the
  scan right after an identity run while its outputs are still in the store.
- The pinned website release (`eab4189`) declares admin events for the
  `terrace` realm (`adminEventsEnabled: true`, details off); first import and
  reconciliation of an older realm are proven in the VM. Remaining on a real
  host: run `realm plan` and `realm apply` once after deploying the re-pinned
  build. The identity administration proof rebuilds the previous release's
  realm producer from the current one and checks it against the old pinned
  hash; the next re-pin that changes `identity/tools/realm-config.ts` must
  replace that reconstruction (the build fails until it does).
- The website's bulk roster tool (`roster:invite`, website `docs/member-lifecycle.md`)
  is not in the release bundle and needs a source checkout with Bun on the host;
  Womb has no packaged way to run it (G-45). Its journal tables are ordinary CMS
  rows and are covered by the database backup (the website check restores a
  synthetic journal row). The optional `LIFECYCLE_LOCK_TIMEOUT_MS` (per-address
  lock wait, default 3000 ms) is not set by Womb and is untested at other values.
