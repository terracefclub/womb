# Delivery backlog

Updated 2026-11-24. GitHub is primary (2026-11-06): current work is tracked in the linked GitHub issues. The
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
| 4 | [CMS spike](https://gitlab.com/terrace/tech/tfc-website/-/issues/2) (historical; GitHub [tfc-website #5](https://github.com/terracefclub/tfc-website/issues/5)) | Publish/revise/preview; roles; export/import; memory measurement | Linux bundles of website `dev` `dd1aef7` (twelve-migration ledger including the bulk-roster journal, packaged `bin/sweep`, `bin/roster-invite` and `bin/wp-import`, realm admin events) are pinned and pass the [native host/recovery check](website-host.md) (now including the operator tools through `terrace-cms-tool`) and the [HTTPS gateway check](website-gateway.md) with the member lifecycle (2026-11-24). The [all-services rehearsal](full-rehearsal.md) and the four other driver variants each passed once at this pin. Actual public activation and combined capacity remain gated |
| 5 | [Hetzner bootstrap](https://gitlab.com/terrace/tech/womb/-/issues/3) (historical; GitHub [womb #11](https://github.com/terracefclub/womb/issues/11)) | Dedicated Terrace credential, protected existing data, targeted plan, tested bootstrap/health/teardown | [Womb !5](https://gitlab.com/terrace/tech/womb/-/merge_requests/5) (historical) merged; install/boot VM passed. Earlier October31 price estimates are historical; November4 owner direction removes unanswered budget questions as a code/rehearsal gate. The owner authorised a dedicated Terrace project token on 2026-11-07 (read-only inventory first; the existing server is outside every write target). Disposable, lease-bound Hetzner probes and known-full leases 1 to 5 have run since. Proven on Hetzner by lease 3 (run `90e965da`, 2026-11-13): host-key pinning, RAM installer, install plan, FORMAT and install, reboot from the installed disk and networking. Lease 4 (run `7d217921`, 2026-11-18) was stopped by builder-lock contention before the plan step and proved nothing beyond lease 3. Lease 5 (run `efc9794a`, 2026-11-20, staged at trunk `bb0d526`) passed end to end and was torn down: services launch from installed disks, a backup to a restic repository that was a directory on the build machine, reached through a reverse SSH forward (not a storage provider account, not off-site), retiring the original pair and restore onto a replacement pair, for the rehearsal profile on synthetic data on private temporary hosts, one run on one day ([cloud rehearsal](cloud-rehearsal.md#lease-5-result-2026-11-20)); not proven by it: the production profile, ACME, the Keycloak administration tool, real data migration, cutover, alerting. Lease 6 has not been requested. No production host exists and no production provider apply was made; access recovery for and a protected copy of the existing server remain open. See [relay plan](access-relay.md), [cloud rescue install](cloud-rescue-install.md) and the [owner runbook](owner-runbook.md) Part 6 |
| 6 | [Backup and restore](https://gitlab.com/terrace/tech/womb/-/issues/4) (historical; GitHub [womb #11](https://github.com/terracefclub/womb/issues/11)) | Encrypted DB/media backup; quiescent capture; isolated restore; reconciliation; teardown | Module and two-VM restore test in `platform/backup.nix` ([docs](backups.md)); no real Storage Box/B2 or off-site drill yet; secrets delivery (sops-nix, offline escrow key, rotation and re-keying) is proven in the production VM check on synthetic data, with no real secret and no escrow retrieval drill. Current existing-server backup state and real recoverable copies remain unverified by this builder work |
| 7 | [WordPress preservation](https://gitlab.com/terrace/tech/tfc-website/-/issues/3) (historical; GitHub [tfc-website #3](https://github.com/terracefclub/tfc-website/issues/3)) | Full protected DB/files backup, plugin/table inventory, restore | Full protected WXR export and Gravity Forms definitions supplied 2026-11-03; the Kinsta full backup ZIP and Kinsta settings are still outstanding (access pending) |
| 8 | [Membership hardening](https://gitlab.com/terrace/tech/womb/-/issues/5) (historical; GitHub [womb #12](https://github.com/terracefclub/womb/issues/12)) | Contract tests, authenticated webhook, idempotency, retry/replay, email outbox | Opt-in [roster replay step](membership-roster.md) passed the original 11 cases through a real Windmill worker with pinned offline SDKs and fake transports; live webhook mapping/authentication, email ledger and cutover still need operator access |
| 9 | [Credential remediation](https://gitlab.com/terrace/tech/womb/-/issues/6) (historical; GitHub [womb #12](https://github.com/terracefclub/womb/issues/12)) | Map consumers, verified backup, rotation, old credentials rejected | Access pending |
| 10 | [CMS import](https://gitlab.com/terrace/tech/tfc-website/-/issues/4) (historical; GitHub [tfc-website #3](https://github.com/terracefclub/tfc-website/issues/3)) | Stable IDs, repeatable import, count/hash reconciliation, redirects | Planner and applier merged (website PR 8, `dev` `ffbe3ff`; `dev` now `9fa1341`): real-export dry run reproduces 245 items and 87 publish candidates; local rehearsals on the owner's real export (2026-11-18, local database only, aggregate counts recorded, nothing private published) imported, rolled back and re-applied cleanly. Website PR 14 added content quality gates (36 documents are now eligible for bulk publish, down from 56; 24 Divi, 11 empty-body and 5 leftover-shortcode documents wait for editor review), a `publish` command for imported drafts and redirects that follow publication. No import has been applied to any real target; since website `dd1aef7` the applier is packaged (`bin/wp-import`, run on a host through `terrace-cms-tool`, its writes needing `--confirm-production`; proven in the VM only), and there is still no draft preview on the site; media bytes, menus and plugin settings wait on the Kinsta ZIP. Depends on 4, 7 |
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

## Credential rotation follow-ups (G-37), recorded 2026-11-24

From the written [credential rotation](credential-rotation.md) document. None was done
in that pull request; the production safeguards follow-up (2026-11-24) did the items
marked done.

- Done (production safeguards): the inventory's `identity/database-password` text now
  says push, then `apply`; `docs/identity-host.md` names the production rotation
  (`terrace-secrets apply`). The owner runbook's 9.9 has named the database and
  repository password rotations since its fifteenth pass.
- Code: Windmill `adminPassword` cannot be changed after the first bootstrap
  (`platform/windmill/bootstrap.ts` logs in with it, falls back only to the factory
  password); add a rotation path and a VM assertion.
- Done (production safeguards): `apply --finish` sends the store value and the host
  refuses it unless its delivered file holds exactly that value, before restic is
  asked (unit tests; the production VM check asserts it is refused before `push`
  with both keys kept, passed at `9664be1`).
- Code: no `client-secret sync` for the Documenso, Windmill and Vaultwarden OIDC
  clients (only the four production clients), no rotation of Headscale keys, host SSH
  key (in place), Keycloak realm keys, the Vaultwarden RSA key, or the ACME account key.
- Done (production safeguards): the first certificate's public key must be the
  key's (`tls_pair_problem`, OpenSSL on stdin). `set` warns while the stored pair
  does not match (one half is set at a time, so it cannot refuse); `push` refuses
  it before sending when `SOPS_AGE_KEY_FILE` lets it decrypt the store (otherwise
  it prints a note); the host's `install` refuses a mismatched pair before anything
  is installed or restarted (unit tests with stand-in and real OpenSSL, RSA, EC and
  Ed25519; the production VM check, passed at `9664be1`, asserts: push refused
  before sending, and by the host's install
  when pushed without an identity, nginx and the served certificate unchanged, then
  the matching pair delivered). Left: a host still running an older
  `terrace-secrets` has no install check; `check` does not decrypt and so does not
  check the pair; possible false refusals of a valid pair for an RSASSA-PSS
  certificate or an EC key with explicit curve parameters (not tested;
  [secrets](secrets.md)).
- Done (production safeguards): `--finish --only-current` without `--remove` lists
  every key and removes nothing; `--remove KEYID` must name exactly each key it
  removes (unit tests; asserted in the production VM check, passed at `9664be1`). `--finish --keep PREFIX` refuses, with the
  key list and before removing anything, a prefix that matches no key or more than
  one (a typo would otherwise have removed the key meant to be kept; unit tests).
  It still removes every other non-current key without listing it first.
- Decide and document: where Windmill's encryption key lives and how it is delivered;
  the exact way to deploy a changed private host module (escrow recipient, operator
  keys); how to compute the Hetzner `tokenSha256` pin.
- Tests: old-credential-rejected assertions for the SFTP key rotation, `escrow
  replace`, and the compromise drills listed in the document. Done (production
  safeguards): the database password (`password authentication failed`) and the
  repository password (`wrong password`) are refused after their procedures, each
  with the old value shown to work first as a control.

## Restore promotion follow-ups (G-22), recorded 2026-11-24

From the review of the written [restore promotion](restore-promotion.md) procedure.
None of these was done in that pull request; the production safeguards follow-up
(2026-11-24) did the items marked done.

- Drill: run the procedure's commands verbatim in a VM on the production profile
  for both roles with the original disks destroyed, and add the assertions in its
  "Drill to add" section. Until then it is written from the rehearsals and has not
  been run as written.
- Done (production safeguards; decision: leave them off): `adopt --restored` holds
  the backup, check and freshness timers (`restoreHeldTimers`) and on the app the
  sweep (`restoreServiceHeldTimers`), each group by a `ConditionPathExists=!` on its
  own marker under `/var/lib/terrace-production/`, written before the target
  starts, so a resumed adoption and a reboot do not arm them. `terrace-production
  arm-timers` arms the backup timers (procedure step 21, after a backup by hand);
  `arm-timers --in-service` arms the sweep (D5, after the erasure replay); both say
  what they armed and what is still held, and are idempotent; `status` prints one
  line per held group. `--in-service` refuses while the backup timers are held, so
  the order is enforced. A plain adoption is unchanged. The production VM check
  asserts (passed at `9664be1`): both paths, the refused order, a
  failed and resumed restored adoption, both stages with a reboot between them, and
  the relay's `--in-service` answer. Not exercised: a restored relay's held timers
  (no relay replacement in the VM). The script is now
  `platform/production/terrace-production.sh` with unit tests
  (`tests/platform/test_terrace_production.py`, stub systemctl).
- Proofs of the production safeguards (2026-11-24, Womb `9664be1`, on the build
  machine): production VM proof 263 assertions, identity administration 347, TLS 148,
  run fresh; the website and website gateway proofs passed at `fff40ce` with the same
  derivations; the five rehearsal driver variants (default, `--documenso`,
  `--documenso-signing`, `--relay-peers`, `--all-services`) each passed once and were
  purged. One pass per variant shows no failure rate. An earlier head (`fff40ce`)
  failed the production proof at build: `writeShellApplication`'s shellcheck reported
  SC2016 on a message with backquotes in `terrace-production`; fixed before these runs.
- `tests/platform/test_terrace_production.py` builds the script's settings prelude by
  hand, parallel to the one `platform/production/base.nix` generates; a drift between
  the two is caught only by the production VM proof. `terrace-production.sh` keeps
  the indentation it had inside `base.nix` (cosmetic, left as is).
- A restored host whose timers are never armed takes no scheduled backup and
  nothing says so: the hold markers never expire, the freshness timer that would
  report a missing backup is itself held, and only `terrace-production status`
  shows the hold. Belongs to the alerting gap (G-23); not built here.
- Decide and document: the production website backup set is the `terrace_cms`
  database, the media directory (`/var/lib/terrace-cms-media`) and the outbox
  (`/var/lib/terrace-cms-outbox`), plus the `keycloak` database. It does not
  include the website input files (`cms.env`, `web.env`: sops templates, the
  inputs directory is backed up only when `inputFiles` is null,
  `platform/website.nix`), the CMS migration marker
  (`/var/lib/terrace-cms/migrated-revision`), ACME state (`/var/lib/acme`) or the
  operator directory. Decide whether that is intended and document the re-supply
  path for each.
- Erasure replay: no erasure ledger exists outside the restored database and
  outbox (website `dd1aef7`, `cms/src/lifecycle/erasure.ts`), and the production
  gateway has no closed or maintenance state, so a restore from a snapshot older
  than an erasure cannot re-apply it before public exposure unless the operator
  uses a manual certificate and a forwarded, name-mapped route that has never been
  tried on the production profile. Needed: an erasure record kept outside the
  backed-up data, a way to re-apply erasures before the site is reachable (a closed
  gateway state or an operator-only listener for the CMS administrator interface),
  and the realm-side disable for members in the production identity tool.
- Docs corrections, done (production safeguards): `docs/website-host.md` (the input
  directory is backed up only when `inputFiles` is null; the outbox is backed up),
  `docs/production-profile.md` "Host replacement" (the cluster copy is separate from
  `fresh_restore`; `/etc/rehearsal-pg-directory` is harness-only), `docs/secrets.md`
  (the push pins the store's public key, so the order of push and `host unstage`
  does not matter), `docs/full-rehearsal.md` (its "full" read check is right for the
  rehearsal, which sets `100%`; it now says production keeps `5%`). Left:
  `docs/backups.md` now says the two recovery-time proposals (four hours, same day)
  differ; which applies is an owner decision.

## Follow-ups recorded 2026-11-20

Small review findings that did not block a merge. Each is open work, not a defect
known to affect a deployed host (none exists).

- `terrace-tls prepare-acme` inspects only the first stored certificate
  directory entry (`platform/production/tls.nix`; [TLS](tls.md)). Done since
  (verification sweep): a mistyped `--overwrite` (any argument other than
  `--overwrite`) is a usage error before any change, asserted in the TLS VM
  proof on the relay. Left as is (harmless, review of PR 66): an empty second
  argument is accepted as none, a third argument is ignored, and the root check
  runs first, so a non-root typo gets "run as root" rather than the usage line.
- `terrace-identity-admin` ([identity administration](identity-admin.md)): a
  failure of the hardening step aborts the command that triggered it (no
  lockout, because `break-glass begin` needs no login); the timer's
  hardening-off branch has a unit test but no VM assertion. Done since
  (verification sweep): a `--dry-run` no longer turns master-realm hardening
  on, it says what the real run would turn on (unit test; it fails against the
  earlier code); the unit test of `first-admin`'s success path now asserts that
  it turns hardening on (removing that call left the earlier suite green).
- The production VM proof's early trust guard (`trust_consistent`) runs on the
  app only, not the relay, and its fingerprint comparison is nearly
  tautological. The proof also no longer exercises the expiry check's default
  system trust store.
- Most likely cause found, not shown; mitigated in the test client: the
  all-services rehearsal's concurrent Documenso browser check (`verify
  --signing`) failed intermittently at "retained document: encryption keys
  (recovery codes)": twice on 2026-11-20 (PR 58's first commit, before
  diagnostics; 2 of 8 runs that day) and once on 2026-11-23 in the website
  `eab4189` re-pin's first all-services rehearsal (run `9d18e784`, restored phase;
  evidence on the build machine in
  `website-repin-evidence-eab4189/rehearsal-run-9d18e784/evidence/concurrent-failure-restored/documents.stderr`).
  What the pinned Documenso v2.21.1 source shows: the server accepts a TOTP code
  only in its own current 30-second step (`verify-2fa-token.ts`, `window = 1`, no
  look-back, no replay store), and a rejected code is an `AppError` without a
  status, which the auth error handler returns as HTTP 500. What the evidence
  shows: the 2026-11-23 failure was a 500 on `two-factor view-recovery-codes`
  whose request ended at second 29 of the client's step, 69 ms after it started;
  the same restored data had passed the same call in the run's sequential verify
  minutes earlier (so not a key or data fault); and a reused code or the
  concurrent browsers cannot make a code wrong (no replay store; they use other
  services). A code made at the end of a step and checked by a server already in
  the next step fits all of this, and is the most likely cause, but it is not
  shown: `totpStepSecond` is read when the request ends, not when the code is
  made; the skew figure is whole seconds from the `Date` header, so 0 only means
  the clocks were within about a second and cannot show that client and server
  were in different steps; and the server's step at that instant was not
  recorded (the client now records it as `serverStepSecond`). Guest clock
  differences of about a second were measured in a different VM test (the TLS
  proof, before its clock sync), not in this rehearsal. Mitigation: the browser
  helper never generates a code within 3 seconds of either edge of a step
  (`platform/documenso/functional-browser.py`, `totp`), with unit tests that drive
  both edges on a fake clock. It is the only TOTP generator in Womb (two-factor
  `enable` and both `view-recovery-codes` calls use it). With it the next
  all-services rehearsal (run `798369c9`, 2026-11-23) passed, but that run kept
  no per-request record of whether the guard fired, and one pass does not show
  that the failure rate (3 failures in about 10 runs) has dropped.
  Done since (proof follow-ups PR): `totp()` checks the clock again after every
  wait (at most 3 waits, then a fixed "clock was still within 3 s of a step
  edge" message) and makes the code from the same clock reading it checked; each
  two-factor request gets only the time left in the step less 1 s as its
  timeout, and a request that outlasts it fails with "request outlasted the
  TOTP margin" instead of the server's 500. Every TOTP request now prints one
  `Documenso TOTP request: {...}` line (fixed keys and numbers only: second in
  the step when the code was made, guard waits and milliseconds slept, the
  deadline, elapsed time, status, second in the step at the end, whether the
  request ended in the code's step, the server's step second); the sequential
  calls show it in the driver log through the guest console, and the
  all-services rehearsal copies the concurrent call's lines into its driver log
  on a pass. Unit tests on a patched clock (`tests/platform/test_documenso_functional.py`).
  In the two all-services rehearsals at the proof follow-ups (runs `2a1b0d0c`
  and `1f051756`, 2026-11-23, both passed) the guard fired once in each (the
  original phase's concurrent call waited 2.6 s and 0.4 s, and made its code at
  second 3.0); all ten TOTP requests ended in their code's step (evidence on the
  build machine in `proof-followups-evidence-ce03a7c/` and
  `proof-followups-evidence-f0c35bd/`).
  Still not shown: that the guard removes the failure (it needs many runs), and
  the server's step at the instant it checks a code.
- Done: the rehearsal's `purge` has an explicit `--failed` mode for a failed
  or incomplete run (the disk budget requires purging pass or fail; run
  `9d18e784` was removed by hand on 2026-11-23). It keeps the logs, `evidence/`
  and receipt and removes the disks, repository and escrow, with the existing
  checks plus: the receipt must not be complete, and the run's controller
  (`controller.json` or `control.json`, process id and start time) must have
  exited ([full rehearsal](full-rehearsal.md)). Unit tests for the success and
  each refusal in `platform/rehearsal/test-full.py` (evidence in `proof-followups-evidence-f0c35bd/`).
  Used on real incomplete runs since (verification sweep, 2026-11-23, default
  variant at Womb `ede1f07`, output on the scratch dataset): runs `e5d6ad2a`
  and `c512e778` were left incomplete by killing their controller (SIGKILL, by
  its recorded process id) while their guests ran, the second in the restored
  phase with retired original disks, the exported repository and escrow and a
  `driver-root` link present. On both, during the driver build `--failed`
  refused with "the controller in controller.json is still running", and with
  guests up with "the run unit or a tagged guest is still running"; the
  ordinary form refused throughout. The guardian stopped the guests within
  a second of the controller's death, and `--failed` then removed
  `state/`, `retained/`, `escrow/`, the runtime directory and the `driver-root`
  link, kept the logs, `evidence/` and the receipt (marked
  `purgedIncomplete`); no unit, tagged guest or controller remained. For
  `c512e778` (851 MiB of allocated state) free space on the scratch
  dataset was 1.08 GB higher just after the purge than while it ran, and 1.18 GB
  higher 60 s later. Not seen on a real
  run: a refusal while the unit is still stopping after the controller died
  (the shutdown was faster than a second purge could start). Also since:
  explicit refusals, with a reason, for an unparsable `/proc` stat line and a
  controller record nested too deeply to read (both were safe, with the
  generic message). A run that failed before either record existed (older runs
  that died before their guests started; today the controller record is
  written right after the run directory, so only a death in between leaves
  none) is still removed by hand; that case was not produced.
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
  The proof now sets each guest to the host clock at boot and, before every
  verifying check, asserts that the client's clock has reached the served
  certificate's notBefore ([TLS](tls.md#not-proven)). Not observed: a public CA's
  backdating. Test-only, given that inference.
- Done (product): `terrace-tls check` and `status` print OpenSSL's verification
  reason after "no verified TLS session" (for example `verify error 9:
  certificate is not yet valid`), so a clock problem is not misread as an
  untrusted chain. Unit tests `tests/platform/test_tls_check.py`; the TLS VM
  proof asserts the reason for a certificate whose notBefore is a day ahead
  (check and status) and for the relay's placeholder ([TLS](tls.md)); evidence
  in `proof-followups-evidence-f0c35bd/`.
- Done (test): the TLS proof fails, instead of waiting up to 30 s, when a
  verifying client's clock is still before the served certificate's notBefore
  after the boot sync (only fetching the served certificate is retried). No
  earlier run needed a wait (`clockWaits` was empty in all ten recorded passes
  since the sync was added); the failure path is asserted with a certificate
  whose notBefore is a day ahead (evidence in `proof-followups-evidence-f0c35bd/`).
- Fixed (test, wall clock): the TLS proof's "reboot keeps the certificate and
  the account key" subtest failed when the app guest's reboot crossed a full
  hour (seen once on 2026-11-23, reboot at 13:59:58 UTC, during the website
  `eab4189` re-pin): `terrace-backup-freshness.timer` is hourly and
  `Persistent`, so it fired at boot on a host with no backup yet, the unit failed
  ("target primary: no successful backup recorded") and the no-failed-units
  check tripped. The same exposure existed without a reboot, whenever the hour
  passed between a host's adoption and its first backup (the app until the last
  subtest, the relay until after its issuance check, the restored replacement
  until the end). Production behaviour is unchanged (a host with no recent
  backup fails that unit, and the proof asserts it). The proof now takes each
  adopted host's backup at once and runs the freshness unit, which must pass,
  and makes the timer fire at boot across the reboot by moving its stamp two
  hours back; the no-failed-units checks are unchanged. Reproduced on purpose
  before the fix (same stamp move, no early backup) and passing after it:
  evidence in `proof-followups-evidence-ce03a7c/repro-before-fix/`, and the TLS
  proof in `proof-followups-evidence-f0c35bd/`. Other VM proofs with a reboot are not exposed:
  the production proof reboots once before adoption (timers not armed) and once
  after its first backup, and has no no-failed-units check; the identity
  administration proof does not reboot; the all-services rehearsal turns the
  backup timers off. Still possible in the TLS proof, not seen: the daily
  `terrace-backup.timer` (03:15 UTC) or the weekly `terrace-backup-check.timer`
  (Sunday 05:30 UTC) firing in the minutes between an adoption and that first
  backup (the backup's own rerun clears the first, nothing clears the second).
- The all-services rehearsal sets Vaultwarden's and Windmill's `identityCaFile`
  to the synthetic CA itself, so only the standalone Vaultwarden functional and
  Windmill checks exercise those modules' default (the system trust bundle). They
  keep doing so with a bundle assembled at boot inside the guest from the public
  roots and the CA's store path (`platform/test-system-trust.nix`), not a cached
  `security.pki` copy.
- Done: the identity proof's "no copy" scan (every output built from its
  certificate must reference it at run time). It never completed in PR 61's
  evidence: the first attempt passed a stale `.drv` path and an empty
  certificate path, and at the final head the certificate output had been
  garbage-collected before the scan, so there was nothing to scan. The scan is
  now a committed tool, `platform/test-trust-scan.py`, which can first build
  the certificate and every output built from it (not the VM run), so nothing
  is left unchecked ([identity host](identity-host.md#test-certificate-every-dependent-output-keeps-its-reference)).
  At Womb `ede1f07` (2026-11-23, identity check derivation unchanged from
  trunk): 13 derivations are built from the certificate; all 12 of their
  outputs other than the test result reference it, none without a reference,
  none unchecked (evidence `verification-sweep-evidence-ede1f07/head-ede1f07/identity-scan.*`).
  What this establishes: no output built from the certificate drops its store
  reference, so none can be a cached copy split from the key in that way. What it
  does not: that an output which references the certificate holds no copy of its bytes as
  well (that rests on reading the sources), and it was not rerun with the
  certificate rebuilt under a new key (PR 61 ran the identity proof that way).
- The pinned website release (`dd1aef7`, realm producer unchanged since `eab4189`) declares admin events for the
  `terrace` realm (`adminEventsEnabled: true`, details off); first import and
  reconciliation of an older realm are proven in the VM. Remaining on a real
  host: run `realm plan` and `realm apply` once after deploying the re-pinned
  build. The identity administration proof rebuilds the previous release's
  realm producer from the current one and checks it against the old pinned
  hash; the next re-pin that changes `identity/tools/realm-config.ts` must
  replace that reconstruction (the build fails until it does).
- Proof derivations read ignored files: the Windmill checks (and the all-services
  rehearsal driver, which composes Windmill) copy `platform/windmill` as a path,
  so an ignored `__pycache__` left there by Python unit tests changes their
  derivation without any commit. Found at PR 68: the same commit evaluated to two
  Windmill derivations, one with and one without the cache; on a clean tree they
  match the proven ones. The PR 68 rehearsals were built after the unit tests had
  created such caches (no functional effect). Fixed after the PR 68 round 2
  review: the checks copy `platform/windmill-evidence-source.nix`, a filtered
  copy without what `.gitignore` ignores there (same name and content as a clean
  tree's copy, so the clean-tree derivations do not change);
  `tests/platform/test_clean_sources.py` evaluates it with and without a planted
  cache and fails on any other unfiltered directory copy under `platform/` or
  `infra/` (none exists). Still check `git status --ignored` before a proof.
- Sweep and migration (pre-existing, also on trunk before PR 68), closed after
  the PR 68 round 2 review: the migration stopped `terrace-cms-sweep.service`
  unconditionally, so a sweep start requested between the migration becoming
  `activating` and that stop (a window of milliseconds) was cancelled instead of
  queued. The migration's preflight now stops the sweep only when it is running
  (a sweep start requested during the migration leaves it `inactive` with a start
  job waiting, which is kept); only a sweep already running is interrupted, as
  intended. The exact window is not tested; the website VM proof's queued-sweep
  and running-sweep checks cover the two cases on either side. After a restore
  the sweep timer stays held until `terrace-production arm-timers` (see "Restore
  promotion follow-ups"); a sweep started during a migration waits for it and runs
  right after it.
- Migrations start with a plain `systemctl start terrace-cms-migrate.service`
  (by hand in the production steps, or from the rehearsal launcher). Since the
  PR 68 round 2 review the unit has no `Conflicts=` on the CMS and website: its
  last start-up step refuses while an operator tool runs and only then stops the
  running CMS, website and sweep, so a refused migration (by that check or any
  guard) leaves the site up ([website host](website-host.md), "When a tool is
  still running"). The CMS, website and sweep are ordered after it, so a start
  of any of them requested meanwhile waits; before, a CMS start would have
  stopped a running migration through `Conflicts=`. A separate deployment
  command was considered and not built: the refusal it would add before the
  stop is now in the unit, where every caller (operator, rehearsal launcher,
  proofs) gets it.
- Operator tools (G-45). Website `dd1aef7` packages the bulk roster tool and the
  WordPress import applier as `bin/roster-invite` and `bin/wp-import`; Womb runs
  them as the CMS account through the root-only `terrace-cms-tool`
  ([website host](website-host.md), "Operator tools"), and owns
  `LIFECYCLE_LOCK_TIMEOUT_MS` as `lifecycleLockTimeoutMs`. Proven in the website VM
  proof (roster dry run, apply, no-op and rollback; importer dry run, apply, no-op
  and rollback; refusals; a 500 ms lock wait against a held lock; no secret in unit
  properties, arguments or journals; journal rows restored from backup) and once
  in the production-profile proof (a roster dry run with the sops-rendered
  environment). Left: a run on a real host; the importer needs
  `--allow-nonlocal-database` on a Womb host because the module's `DATABASE_URI`
  names a user with an empty host, which the tools' local-database check does not
  parse (a different URI form is a website or Womb decision, not made here); the
  operator directory is purged by hand; a migration started in the same
  milliseconds as a wrapper can pass both checks; while a tool runs, its command
  line (operator email, file paths) is visible to every local user, and it may be
  recorded by sudo's log, audit `execve` records or root's shell history (none
  tested); the wrapper has not been run as a background job with an open
  standard input.
- Done (production safeguards): the operator tool lock file is never removed or
  replaced. A link or non-regular file is refused before it is opened; the regular
  file is opened without truncation, locked, then checked through the descriptor
  (same inode as the path, owned by root); a wider mode is narrowed to `0600` under
  the lock; anything else is refused with exit 72. Unit tests (Linux only, they need
  GNU `stat` and `/dev/fd`; a stub `stat` drives the foreign-owner and swapped-inode
  branches) fail against the earlier script; the website VM proof asserts the
  narrowing on the same inode and the refusal of a foreign owner and of a link
  (passed on 2026-11-24 with the derivation of Womb `9664be1`).
- Done (production safeguards; passed on 2026-11-24 with the derivation of Womb
  `9664be1`): the website VM proof requests sweep, CMS and website
  starts while a slowed migration runs, asserts that each stays inactive with its
  start job waiting, and that each runs after the migration ended. Still not tested:
  a migration preflight whose stop of the CMS, website and sweep fails partway
  ([website host](website-host.md), "When a tool is still running").
- Found and fixed (production safeguards): `docs/website-host.md` still said a sweep
  requested in the first milliseconds of a migration is cancelled; the preflight
  has stopped only a running sweep since the PR 68 round 2 review.
- Operator from a file (website repo, tfc-website): `bin/roster-invite` takes
  the operator's administrator email only as `--operator` on the command line.
  A host with unprivileged interactive users should not run the tools until the
  website tools can take the operator (and any other identifying argument) from
  a file or an environment variable; Womb's production hosts have no such users
  today. Tool runs are serialised and bounded (`operatorToolMaxSeconds`,
  default 3600 s), proven in the website VM.

## Follow-ups from the proof follow-ups review (PR 64), done in the verification sweep

- `terrace-tls check` took its 30 s handshake bound from `TLS_CHECK_TIMEOUT` in
  the caller's environment (an exported value could shorten or break it; it
  failed closed). The bound now comes from the script only: 30 s, or 5 s with
  `--status`, which only `terrace-tls status` passes; an unknown argument is a
  usage error. Unit tests (`tests/platform/test_tls_check.py`) fail against
  the earlier script; the TLS VM proof passed at `ede1f07` (148 assertions).
- `purge --failed`: an unexpected `/proc` stat line (IndexError) and a
  controller record nested too deeply to read (RecursionError on Pythons whose
  JSON reader counts recursion) are now refusals with a reason; both were safe
  before (nothing deleted, generic message). Unit tests fail against the
  earlier code.
- [TLS](tls.md) states the `status` delay per public name, with today's counts
  (three names on the app host, one on the relay) and where the names are set.
- Driver variants: PR 64 changed every variant's derivation and only
  `--all-services` had run. The default, `--documenso`, `--documenso-signing`
  and `--relay-peers` variants each passed once at `ede1f07`
  ([full rehearsal](full-rehearsal.md)); their derivations equal PR 64's
  reviewed code. The `--all-services` driver is unchanged from run `1f051756`
  and was not rerun. One run per variant does not show any failure rate.

## Host update follow-ups (G-37 / 9.7), recorded 2026-11-24

From the written [host updates](host-updates.md) procedure. None is done in that
pull request; the deploy path pull request (2026-11-24) closes or narrows the items
marked so.

- Decide (owner, before any production install): how a new system closure reaches
  an installed production host. **Narrowed 2026-11-24**: option 1 (a signing key
  trusted by the host) now exists as an opt-in, `terrace.production.deploy.trustedKeys`
  ([host updates, section 1.5](host-updates.md#15-the-deploy-path-option-1-of-u0-opt-in)),
  VM-proven on the app role; the owner has not chosen it, and the decision stays
  open. SSH accepts only `operator` (`AllowUsers operator`,
  `PermitRootLogin no`, `platform/production/base.nix`), `operator` is not a trusted
  Nix user and signatures are required (nixpkgs defaults, unchanged by the
  production modules), and nothing in the repository copies or activates a closure
  on such a host. The choice (a signing key trusted by the host, a trusted
  `operator`, or something else) must be in the module a host is installed with,
  or its first update has no path.
- Done (deploy path, 2026-11-24): `terrace-production activate` and `rollback`
  record the generations, activate with `switch` or `boot` in the `terrace-switch`
  unit, and refuse while a switch, a migration or a backup runs; VM drill
  `platform/production/deploy-test.nix`. Left: no `test` mode (deliberately: the
  boot default must follow the migrated database), and a running
  `terrace-cms-tool-*` unit is not checked by `activate` (the migration's preflight
  refuses it).
- Hazard, decide and fix: after a migration, the previous generation's
  `terrace-cms-migrate.service` would run the old bundle's `bin/migrate` against
  the newer schema and, if it succeeds, write the old revision to the marker and
  reopen the CMS gate. The gate compares revisions, not schemas. Make the marker or
  the migration unit refuse a downgrade (for example compare the ledger with the
  bundle's migration list), or test and document what Payload does. **Narrowed
  2026-11-24**: `terrace-production rollback` and `activate` refuse to move to a
  release whose manifest lacks a migration the ledger holds (unless
  `--cms-stays-down`, which warns against starting the old migration unit); the
  old unit itself still has no such check.
- Hazard, decide: the pre-update snapshot is protected only by retention. If a
  second snapshot of the host is taken the same day, keep-last 3 is all that keeps
  it (about two days with the daily timer, reading restic's rules; not tried).
  Decide whether pre-update snapshots need a pin, and test the retention reading.
- Docs correction: the website service has no revision gate (`terrace-web.service`
  runs only the identity guard; `requireMigration` is on the CMS, its sweep and the
  operator tools, `platform/website.nix`), but `docs/production-profile.md` ("a
  revision mismatch still fails through the existing gate"), the comment in
  `platform/production/app.nix` and `docs/website-host.md` ("Startup rejects a
  missing/different revision marker") read as if both were gated. Decide whether
  the website should be gated too, then correct the text. **Observed 2026-11-24**
  (deploy VM rehearsal): on a re-pin the activation's switch starts the new website
  against the stopped CMS; it fails its health check and restarts until its start
  limit, three to four minutes later. **Proposed follow-up change, not decided**:
  add the module's `requireMigration` to `terrace-web.service`'s `ExecStartPre`
  (one line in `platform/website.nix`), so the website fails at once like the CMS;
  it changes the website module, so the website and gateway checks and the
  rehearsal drivers would have to run again.
- Decide: the order between activating a revision that adds an inventory secret
  and pushing it (the host refuses unknown names before activation; after it, a
  reboot before the push leaves the writers gated), and how a removed inventory
  name leaves the store (`terrace-secrets` has no general delete command; every
  push is refused while the store holds it).
- Decide: how the operator reaches a production host's console when a new
  generation does not boot (no emergency shell; no provider access exists), and a
  policy for garbage collection and boot-entry limits (none is set; generations
  accumulate on the EFI system partition).
- Not determined, needs a test: Keycloak and Headscale schema migrations on a
  version change in `platform/flake.lock`, and whether the previous generation
  starts on the migrated schema; what `bin/migrate` leaves after a failure part-way;
  a PostgreSQL major upgrade (no tool exists).
- Hazard, decide and fix: a migration that fails inside `bin/migrate`, or is killed
  by the unit's `TimeoutStartSec = 180` (`platform/website.nix`; the preflight and
  `bin/migrate` share it), leaves the marker at the **old** revision
  (the marker is written only after `bin/migrate` exits 0). The new generation's
  gate stays closed, but a generation rollback would start the old CMS on a
  possibly partly migrated database. Decide whether the migration unit should mark
  a failed attempt (so no generation's gate opens), and whether 180 s is right for
  real data (measured only on empty or small synthetic databases). **Narrowed
  2026-11-24**: `terrace-production rollback` refuses, with no flag, when the ledger
  holds a migration the target lacks while the marker holds the target's revision
  (unit test); a migration killed before it recorded anything is not detected.
- Not determined, needs a test: what sops-nix does at activation when the new
  manifest declares a name the delivered ciphertext lacks (expected failure of
  `sops-install-secrets`; whether the previous secrets stay), and whether a push's
  `restartUnits` starts a stopped `terrace-cms.service` or `terrace-web.service`
  (the website would then run ungated on a stopped CMS).
- Drill: the 16 items in the document's "Drills to add". **Narrowed 2026-11-24**:
  the deploy VM rehearsal covers drills 2, 3 (one release pair, small synthetic
  data), 4 (without the sweep and tool refusals), 5, 6 (without H1) and 15, and drill
  1 for the app role without backup, realm and secrets steps. Open: the rest.
- Drill: a reboot into a new generation and `activate --boot` (kernel change). A
  NixOS test VM boots its first system directly, not through the boot loader, so
  the deploy proof cannot show it; it needs a VM with `useBootLoader` or a lease.
  The kernel guard is unit-tested only.
- Drill: the relay's update through the deploy path, with Headscale peers
  reconnecting (the deploy proof only builds the relay's closure to show `activate`
  refuses it on the app).
- Drill: the bootstrap route for a host installed without a deploy key (export,
  `sudo nix-store --import`, the raw step 7 commands), and key rotation with two
  trusted keys ([host updates, section 1.5](host-updates.md#15-the-deploy-path-option-1-of-u0-opt-in)):
  written, not run.
- Decide (reviewer, then owner): whether `terrace-production activate` should set the
  system profile back to the previous generation when `switch-to-configuration`
  fails (today it leaves the failed generation as the boot default, as
  `nixos-rebuild` does; `status` warns, and the host updates document gives the
  manual commands, not run).
- Decide: `terrace-production rollback` goes to the newest generation older than the
  current one; after an attempt that failed and was rolled back, then a new
  activation, that can be the failed attempt's generation (as with `nixos-rebuild
  --rollback`). The docs tell the operator to read `status` first; a rollback to a
  named generation is not offered.
- Decide (owner): whether to keep an offline spare deploy key trusted from the
  first install ([secrets](secrets.md#deploy-signing-key)); without it, a lost key
  means the manual bootstrap route.
- Docs: the owner runbook's 9.7 is labelled TESTED IN CODE ONLY while G-37 calls
  9.7 NOT YET BUILT; 6.15 ("Deploy a release") is labelled PROVEN IN VM REHEARSAL
  but its evidence migrates and starts a host built with the release, not a release
  deployed to a running host. Settle both labels at the next runbook refresh.
