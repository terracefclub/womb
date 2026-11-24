# Established-service bootstrap and recovery rehearsal

This optional builder-only command composes the existing K3s foundation with
native website/CMS, Keycloak and PostgreSQL, a separate synthetic Home Assistant/
Frigate guest, an independent Headscale guest, and a repository/browser guest.
The extra guests isolate incompatible test contracts and recovery escrow. They
are not required production topology. Actual host profiles remain disabled.

The complete `--all-services` variant also includes signed Documenso, individual
Windmill worker/roster execution and encrypted Vaultwarden items/attachments.
Its originals-off replacement recovery result and exact invocation are recorded
in the [all-services section](#all-services-composition). Earlier baseline
results below retain their narrower scopes.

## Run from a clean checkout

Use the x86-64 Linux builder with its existing passwordless transient-unit
support and shared build lock. Supply the accepted website/CMS archives and
reviewed TFC source. Inputs are verified before copying their public code into
the store; runtime secrets/accounts are generated only in disposable guests.
Create a private output parent outside the checkout and Nix store, using a
normalized absolute path containing only letters, digits, slash, underscore,
dot and hyphen:

```sh
install -d -m 0700 /absolute/private/terrace-rehearsals
direnv exec . env BUILD_LOCK=/absolute/shared-heavy.lock ./platform/full-rehearsal run \
  --output /absolute/private/terrace-rehearsals \
  --webArchive /protected/releases/terrace-web-dd1aef7a06ddfa2a31a7cd215afe0f6f10b23191-linux-x64-gnu.tar.gz \
  --cmsArchive /protected/releases/terrace-cms-dd1aef7a06ddfa2a31a7cd215afe0f6f10b23191-linux-x64-gnu.tar.gz \
  --tfcSource /path/to/reviewed/tfc-checkout
```

Add `--documenso` to the same `run` command to include the optional Documenso
recovery scenario. It reuses the reviewed user-free realm/client fixture and
closed account approval policy. The first service guest creates an unsigned PDF
through the native interface and enables encrypted 2FA. Its automatic database,
protected runtime inputs and migration markers join the quiescent backup. After
the original service QEMU is absent, the replacement imports the pinned image
into a fresh container runtime, restores those inputs and database, then uses
fresh SSO to download the identical PDF and decrypt the retained 2FA/recovery
code data. After that fresh SSO check, the replacement stops Keycloak and its
health timer, verifies its HTTP/management listeners are absent, and authenticates
the restored native loopback recovery administrator. Public password login must
remain denied. Keycloak then restarts without the removed temporary bootstrap
administrator. Earlier results below did not exercise this recovery-access
check; the final dated result records its separate source-matched acceptance.
The private browser receipt models a retained user device in the
repository/browser guest; it is separate from server backup contents. Signing
and SMTP remain disabled in this plain mode, and actual profiles remain unchanged. This optional
scenario requires its own source-matched native acceptance receipt.

The explicit `--documenso-signing` variant implies `--documenso` and reuses its
same approved people and encrypted backup scope. It additionally enables the
accepted runtime signer and private local Mailpit sink. Before any sender UI,
the native container must pass the existing SMTP/image-mailer probe while
remote SMTP/UI access stays denied. A synthetic `.invalid` recipient completes
the native signing interface; independent OpenSSL CMS verification requires the
exact fixture certificate and rejects changed bytes. Before capture, the app
and sink stop. The disposable test mailbox is excluded from application backup;
its original disk remains protected until explicit synthetic purge.

After original service guests are absent, the fresh application must start with
the same restored P12/passphrase hashes. Fresh SSO retrieves the exact signed PDF
and repeats certificate/ByteRange verification alongside original PDF and
encrypted-account checks. The signing-request ledger must still contain exactly one completed request;
the replacement sink must remain inactive with no main/control process. The browser receipt and public fixture
certificate stay on the retained test-user device, separate from server escrow.
Bounded private failure artifacts stay outside the store. This variant requires
its own passing source-matched native receipt; signing restart acceptance alone
does not prove this replacement-host recovery. No production transport,
certificate authority, signature trust or actual profile is enabled.

The synthetic gateway CA and relay certificate are built with random keys, so
no cached derivation holds a copy of them (no `security.pki` system trust
bundle). Guest clients reference the CA's store path through the
`/etc/terrace-test-ca.pem` and `/etc/terrace-relay-ca.pem` symlinks: `curl
--cacert`, `SSL_CERT_FILE` (protocol client, lifecycle steps, `tailscaled`),
`NODE_EXTRA_CA_CERTS` and Vaultwarden's and Windmill's `identityCaFile`. Two
consumers use run-time copies instead: the test script imports the CA into the
browser's NSS database at start, and Documenso's `identityCaFile` is a copy that the
gateway's input service makes at boot from the CA's store path into
`/var/lib/terrace-gateway-test` (restored from backup only within the same
run). Those copies are not the hazardous kind: they are made in the same run
from the current store path on a disposable disk and never cached across builds.
Each gateway and relay start first checks that the served chain verifies against
the symlinked anchor, failing with "test CA and trust anchor disagree" otherwise.
Because the rehearsal sets Vaultwarden's and Windmill's `identityCaFile` to that
anchor, their default trust path (the system trust bundle) is exercised only by
their standalone checks, not by this rehearsal.

The separate optional `--relay-peers` switch adds three disposable Tailscale
clients to the existing application, automation and browser guests. Private
single-use tagged enrollment keys are expired before capture. The allow rule
permits only the application client to reach one private sensor proxy port;
the denied client must fail TCP connection, and another peer's live SSH port
must remain unreachable. The approved client can read a native synthetic Home
Assistant sensor, while other paths and writes are rejected by the separate
test-only proxy. Its listener is allowed only on the tailnet interface; a LAN
connection must fail. Production relay defaults remain deny-all.

Client cryptographic state and protected fixture inputs join the application
and automation encrypted backups. The browser guest represents a retained user
device. After original service guests stop, replacements reconnect without an
enrollment command; node IDs, public keys and addresses must match, and the
same private-access/denial checks repeat against the recovered Headscale.
This does not prove human OIDC enrollment, production HA user permissions or
forced DERP/NAT traversal: transport remains selected by the native clients.
No production or host network settings are changed. The key-file mechanism
follows the [Tailscale CLI contract](https://tailscale.com/docs/reference/tailscale-cli/up)
and [Headscale fixture enrollment](https://headscale.net/stable/ref/registration/).
This extension remains unaccepted until its source-matched native run passes.

The driver runs after its build, outside the build's temporary store roots, so
the runner roots its output in the private run directory (`driver-root`, through
`run-check`'s `CHECK_OUT_LINK`) until the guests are off: a host garbage collection
during a run (as on 2026-11-19, which deleted a guest closure mid-run) can no longer
remove the guests' closures. The build queue is bounded at 1200 seconds, warming at 1800 seconds (each Nix
build at 1200 seconds), and controller cleanup at 70 seconds. VM execution uses
the independently validated transient system service/cgroup: queue 1200 seconds,
active 1800 seconds, then 15 seconds of host stop grace. The build phase retains run-check's documented
boundary; it is not claimed universally cgroup-contained. No cloud apply,
purchase, real provider, mail, payment, signature or device is used.

## Acceptance and retained state

The scenario starts with empty writable guest disks and asserts all application
writer services, targets and sockets are inactive before initial bootstrap and
before replacement restore. Home Assistant's upstream target is disabled as
well as its service/socket, so it cannot generate fresh identities or a database
before restore. Nonempty mutable restore targets still reject. The scenario runs
explicit bootstrap twice and checks stable identities/inputs/migration ledger. It uses the accepted
immutable application archives, real HTTPS native code/PKCE SSO, Chromium's
native denied-account switch, public/private content and image-cache checks.
Application requests and synthetic automation checks run concurrently. K3s
runs an actual PVC-mounted container. Without `--relay-peers`, Headscale covers
control-plane/key/database recovery only. Human relay enrollment and forced
DERP/NAT traversal remain separate follow-ups.

Before capture it scales K3s controllers down, terminates pods, stops its
server/runtime and verifies no pod/runtime processes remain. Every cgroup read
must succeed: empty output after a failed find or file reader refuses capture. It stops all app,
identity, automation and relay writers, then keeps them stopped throughout all
three encrypted snapshots and a full repository read check. PostgreSQL remains
up only for logical dumps. This quiescent window closes the known sequential
DB/files snapshot consistency gap for this test; it does not implement a live
production backup scheduler or promise zero downtime.

Every original guest QEMU must be absent before its disk is retired and an
empty replacement boots. Declared host/node identity is retained, while the
container runtime and image imports are fresh. Restore reconciles DB counts,
file hashes and SQLite integrity, then adopts the stopped isolated PostgreSQL
copy under the declared native HBA/config (not isolated trust authentication).
The isolated restore preserves the recorded source bootstrap name at OID10.
PG18 attaches role-grant dependencies to that identity; importing its grants
under a differently named bootstrap account fails even when both accounts are
superusers. See the [upstream PG18 report](https://www.postgresql.org/message-id/CA%2BC_kKWHMP4c56jx1BPvP1jmjp2pmBu0Cw07fPVECUmkJSnT4w%40mail.gmail.com)
and [pg_dumpall bootstrap notes](https://www.postgresql.org/docs/18/app-pg-dumpall.html#APP-PG-DUMPALL-NOTES).
The operating-system restore owner remains separate and gains no SQL role.
This declared native source uses `postgres` as the login administrator; its
flags, password, settings and every membership/grantor/option are compared
before resumed writers. The operating-system restore user cannot connect as
itself or impersonate `postgres`, and app credentials cannot assume that role.
Native peer HBA and fsync remain mandatory. The narrow restore contract requires
hash-bound bootstrap metadata and a source bootstrap with LOGIN and SUPERUSER.
Embedded double quotes in that bootstrap name are rejected before initdb; other
role names and their SQL remain intact. Legacy metadata-free or NOLOGIN-bootstrap
snapshots fail closed and retain their
original artifacts/pinned tooling. The recovered PVC must
read its original marker and persist a new write. Native SSO/private-content,
automation auth/sensors, relay keys/users and unrelated sentinels are checked.
All replacement guests and the repository/browser guest stop before success.

The private `receipt.json` records versions/revisions, check counts, timings,
peak memory, original process identities, containment/teardown and artifact
hashes. `retained/encrypted-repository.tar` contains the encrypted restic
repository. `escrow/recovery-escrow.tar` separately contains its recovery
password and per-host SSH inputs: keep it private and independently protected;
it is not encrypted by the repository password it recovers. Both are outside
the Nix store and mode0600. Do not publish the run directory or logs.
Writable and retired original disks live in the private `state/` directory;
only sockets/runtime scratch use the recorded short `/tmp` directory. Encrypted
repository and recovery escrow copies are exported immediately after the backup
check, before original-node shutdown, so a later failure retains recoverable
state outside the socket runtime. Failure does not delete needed evidence.

Explicitly purge only a completed, verified disposable run:

```sh
platform/full-rehearsal purge /absolute/private/terrace-rehearsals/run-UUID \
  --synthetic-only --yes
```

Purge requires the matching owner/scope markers, completed receipt, exact unit
and cgroup off, and no tagged guest. It removes that run's recorded runtime,
state, retained encrypted repository and escrow. It retains receipts/evidence
and unrelated paths. A failed or incomplete run is refused by this form. The
disk budget still requires purging it, pass or fail: copy its small evidence
first (`driver.log`, `receipt.json`, `evidence/`), then

```sh
platform/full-rehearsal purge /absolute/private/terrace-rehearsals/run-UUID \
  --synthetic-only --yes --failed
```

`--failed` accepts only a run directory the tool created (same owner, scope,
run id and mode checks) whose receipt is not complete, whose unit and cgroup are
off with no tagged guest, and whose controller has exited: the run records the
controller's process id and start time in `controller.json` (and, once guests
start, `control.json`); it checks every record present and refuses, printing
a fixed reason, when there is none, when one is malformed (a link, not private,
not a JSON object, nested too deeply to read, a wrong run id), when the named
process's state cannot be read or parsed, or when one names a process that is
still running. A process that has exited counts as gone, also when it exits while
being checked. It removes the same things (`state/` with the disks, `retained/`,
`escrow/`, the recorded runtime directory when one was recorded, a leftover
`driver-root` link, never following a link out of the run) and keeps
`driver.log`, `evidence/` and the receipt, marked `syntheticStatePurged` and
`purgedIncomplete`. A run that failed before this record existed and before its
guests started has neither file and is still removed by hand. Pure
rejection/sentinel tests run with
`direnv exec . python platform/rehearsal/test-full.py -v`.

This is prebuilt-closure VM bootstrap/recovery, not a blank-provider install,
public DNS/certificate issuance, CDN behavior, off-site operator recovery,
real invitations or production capacity approval. The default baseline command
excludes Windmill and Vaultwarden; the explicit `--all-services` variant below
includes both. Documenso is included with `--documenso` or its implied variants.
Each dated result below applies to its stated flags and source; it does not
extend the coverage of another variant.


## Observed acceptance — November 5, 2026

Source `28fabd811f2b78ebf8010f228d17a34988806ff5` completed the whole scenario in
521.27 seconds. Both functional phases passed 198 HTTPS protocol assertions and
the real Chromium account-switch check. Fresh service guests recovered native
SSO, PostgreSQL authorization, the original K3s node/PVC with a new volume write,
Home Assistant identity/sensors, synthetic camera frames and Headscale keys/users.
The final writer stop and guest shutdown passed; the independent host guard and
all seven observed QEMU processes were verified off.

The explicit synthetic purge then removed that completed run's seven disks,
encrypted repository copy, separate recovery escrow and socket runtime. Its
unrelated host sentinel and an earlier failed-run receipt were unchanged.
Private receipts, source/log hashes and cleanup/purge evidence were retained.
These observations cover the established services above; the deployment and
additional-application limits remain unchanged.

The optional `--documenso` scenario at source
`a8cdb0761c28acffd4b0999f63b95b0769057822` also passed on November 5 in
791.74 seconds. It retained both 198-assertion HTTPS/browser phases and the
existing automation, K3s/PVC and relay recovery checks. Native Documenso create
took 78.93 seconds; fresh-container verification took 40.94 seconds and required
the same unsigned PDF digest and decrypted 2FA/recovery codes. PostgreSQL had
19 connections after concurrent checks, not a measured peak. Documenso's unit
MemoryPeak was 1,692,995,584 bytes. All 33 recorded controller/guard/driver/guest
and helper process identities were subsequently absent, with the exact guard
unit and cgroup off. Explicit synthetic purge removed seven disks, repository
and escrow while preserving its unrelated sentinel, an earlier failed receipt
and all evidence. This does not establish signing recovery or real relay peers.

Source `870c5672e43f5fb925bf22e4a9b1f4ed32f8a971` passed the combined
`--documenso --relay-peers` scenario on November 5 in 825.39 seconds. Native
Tailscale 1.104.2 and Headscale 0.29.6 preserved the three client identities;
the gateway and automation clients were restored onto new service guests,
while the browser client retained its original disk. Initial and restored
private-access/denial checks took 15.54 and 15.49 seconds. All existing
198-assertion/browser, PDF/encrypted-2FA, PVC and automation checks also passed.
The driver asserted every final QEMU PID absent; independent host checks
confirmed the exact recursive cgroup/unit off and all 26 captured process
identities absent. Those live snapshots covered the originals and fresh
gateway, not independent fingerprints for both later replacements. Explicit
synthetic purge preserved its unrelated sentinel and earlier failed receipt.
DERP transport, human enrollment and final production capacity remain unproved.


Source `fcbc136e6e4387721f8264a2dbbaf36883aa760c` passed the same combined
`--documenso --relay-peers` rehearsal on November 5 in 847.26 seconds. After
fresh SSO downloaded the original PDF and decrypted the retained 2FA data,
the replacement stopped Keycloak and its health timer and verified empty
HTTP/management listeners. The restored native recovery administrator then
authenticated through loopback while public password login remained denied.
Keycloak restarted with the trusted issuer and no temporary administrator;
the test-only bootstrap environment had been emptied before backup. This
additional recovery-access phase took 18.85 seconds.

Both 198-assertion HTTPS/browser phases, K3s retained-PVC new write, automation
and real tailnet access/identity recovery also passed. All seven original and
replacement QEMU identities and 35 total recorded host identities were verified
absent, with the exact unit collected and recursive cgroup empty. Explicit
completed synthetic purge removed seven disks, the encrypted repository and
separate escrow while preserving its unrelated sentinel, earlier failed-run
receipt and evidence. The deployment, transport and remaining-application
limits above still apply; no actual profile is enabled.


The explicit signed-document variant at source
`378691a03dd664281a8af124577c39f3fc05c589` passed on November 5 in
927.93 seconds. Use `--documenso-signing --relay-peers` on the clean-checkout
command above; the signing switch includes Documenso automatically. Native
SMTP/image-mailer preflight took 9.93 seconds. Document/account creation took
83.60 seconds; recipient completion and certificate/tamper checks took 30.75
seconds. After
the original service QEMUs were absent, fresh SSO retrieved the identical
original and signed PDFs and decrypted the retained account data in 39.85 seconds.
The P12/passphrase hashes matched; the single signing-request job remained
COMPLETED and the replacement mail sink had no active/main/control process.
Identity-independent recovery after replacement also passed in 21.23 seconds.

The two phases each passed 198 HTTPS assertions and the browser check. Retained
PVC writes, automation and private-tailnet identity/access recovery also passed.
All 36 recorded host identities,
including all seven original/replacement QEMUs, were independently absent or
reused; the exact unit was collected and recursive cgroup empty. The explicit
synthetic purge removed seven disks, repository and escrow, retaining evidence,
its unrelated sentinel and earlier failed state. These observations establish
synthetic signed-envelope recovery, not production certificate trust, real mail
or legal signing, remaining-app composition or actual provider installation.


### All-services composition

The explicit `--all-services` mode implies signed Documenso and real tailnet
peers and adds the accepted Windmill and Vaultwarden runtimes. The dated
source-matched combined result below establishes replacement-host recovery for
this mode, separately from the earlier component restart proofs.

One shared synthetic identity fixture supplies the three reviewed application
clients. Windmill uses individual native code/S256 login, decrypts its retained
private variable, reads its original script/job/result and executes a new real
operator job. Explicit `--roster` additionally runs the original eleven-case
roster contract with real pinned SDKs and fake Sheets transport in that worker;
its separate token-free receipt retains source/lock/script/job/result digests.
Vaultwarden uses fresh individual SSO and the retained user's master password
to decrypt the same two vault items and uploaded attachment bytes. Its explicit
`restore` mode requires no old access or refresh token. Signed PDF, encrypted
Documenso 2FA and private-network identity/ACL checks remain enabled.

All app and worker writers stop before capture and stay stopped across every
configured snapshot. After the original service guests are absent, the blank
replacement adopts the declared native PostgreSQL/HBA configuration, with the
same app role limits and restricted group memberships, before app startup.
Windmill receives new container IDs with the same pinned image; its immutable
SDK closure is reinstalled rather than recovered from mutable worker caches.
All three apps exercise their private native administrator recovery while
Keycloak and its health service/timer are stopped. Public password/admin paths
remain denied. User-side vault decryption is a separate check from administrator
readback.

Four real browser helpers run together inside a bounded client systemd cgroup,
while separate guest consoles perform the website/CMS protocol and automation
checks. This load repeats after replacement recovery. Each phase has exclusive
private witnesses. App PostgreSQL connection limits total 42 within a
64-connection server, with maintenance and superuser reserve. The receipt reports
sampled connections (including the sampler), actual container kernel memory
counters, bootstrap timings, service versions and seven original/final guest
PID/start identities with verified absence. Sampled peaks and this synthetic
load do not establish production sizing.

The existing four disposable guests isolate test contracts. The optional
application guest uses 12GiB/four vCPUs and the concurrent browser/repository
guest 6GiB/four vCPUs; this is test capacity, not a production host requirement.
This explicit mode has a 2700-second independent driver deadline; existing
variants retain their 1800-second deadline. Build/queue and stop bounds above
remain unchanged.

The fixture's browser credentials, including synthetic vault master passwords,
are retained on the disposable client. After verifying the copied bytes and
real client encryption/decryption, the server-side vault fixture copy is removed
before backup; absence is checked again on the replacement. Production
Vaultwarden inputs do not escrow each person's master password. This test's
private administrator readback does not prove it can decrypt real users' vaults.

With the pinned archives and the matching website checkout already present on the
builder, the complete invocation from a clean repository checkout is:

```sh
install -d -m 0700 $HOME/terrace-work/full-rehearsal-output
direnv exec . env BUILD_LOCK=$HOME/.falcon-heavy.lock \
  ./platform/full-rehearsal run --all-services \
  --output $HOME/terrace-work/full-rehearsal-output \
  --webArchive $HOME/terrace-work/website-bundle-dd1aef7-output/terrace-web-dd1aef7a06ddfa2a31a7cd215afe0f6f10b23191-linux-x64-gnu.tar.gz \
  --cmsArchive $HOME/terrace-work/website-bundle-dd1aef7-output/terrace-cms-dd1aef7a06ddfa2a31a7cd215afe0f6f10b23191-linux-x64-gnu.tar.gz \
  --tfcSource $HOME/terrace-work/website-bundle-dd1aef7
```

Successful `run` stops all owned compute and preserves its disks, encrypted
repository and separately protected escrow by default. Only an explicit
completed synthetic purge removes that run's retained payloads:

```sh
direnv exec . ./platform/full-rehearsal purge \
  $HOME/terrace-work/full-rehearsal-output/run-REPLACE_WITH_RECEIPT_RUN_ID \
  --synthetic-only --yes
```

Receipts/evidence and unrelated or failed runs remain; a failed run is purged
with the same command plus `--failed` (above). No provider apply, blank
provider-machine install, live-data migration, public DNS/cutover, human relay
OIDC enrollment, forced DERP/NAT path or live Sheets/mail/payment integration is
claimed by this mode. Website/CMS, identity, Vaultwarden, automation and relay
use native services; Documenso and Windmill use managed Podman with native
PostgreSQL. K3s recovery covers its actual persistent-volume probe, not placement
of every application in Kubernetes. Actual profiles remain disabled.

A failed restore exports only fixed restore/globals/per-database error files and
reconciliation output, each bounded to 1MiB, into its private evidence directory
before teardown. Export failures preserve the original restore failure; failed
disks and the early encrypted repository/escrow remain retained. No diagnostic
contents are printed by this export.


### Website member lifecycle in the rehearsal

Since the re-pin to website `680ad56`, every variant also runs the website
lifecycle through the real gateway, using the same step scripts the cloud
service proof runs (`platform/website-gateway/lifecycle_steps.py`, prepended to
the scenario). Before backup it renders `/news/` and a synthetic imported-style
post and follows an old URL's 301 (VM only), then: `apply` (six posts from the
gateway guest with forged client headers: five accepted, the sixth limited, at
the gateway and on the loopback port, with and without script), `approve` (two
applicants, two invitation files `0600` in the CMS outbox), and `redeem`,
`sign-in` and `link` for the first applicant only; the second invitation stays
pending. A second client address then gets its own bucket (VM only). The outbox
and application rows join the app signature, so they must be identical after the
quiescent backup and after the originals-off restore. The sweep timer is armed
only by the explicit bootstrap and stopped before capture.

On the replacement: `ownership` (static CMS ID, restored outbox and invitations
owned by the CMS account and readable by it), `redeem` of the restored pending
token (accepted once, refused again), `sign-in` and automatic `link` by verified
email, `erase` of both members (the running CMS writes two offboarding notices
into the restored outbox; the invitation files still name the applicants),
`sweep` by the real timer, and `redaction` (both invitations redacted, two
erasure confirmations, every file `0600` and owned by the CMS account). The
receipt records each step's seconds.

### Observed rehearsals after the PR 68 review — November 24, 2026 (Pacific)

Womb `e7403a5` (operator tools serialised and bounded, backup-path check
normalised, fixed unit description; same kept `dd1aef7` build). The website module
changed, so every driver derivation changed again and all five ran once more, one
at a time, each purged with the ordinary command at once. As before, they do not
run the operator tools.

| Flags | Run | Time | Scenario | Result |
| --- | --- | --- | --- | --- |
| `--all-services` | `b72d148d` | 05:48 to 06:18 | 1706.80 s | passed: 198 HTTPS protocol assertions in each of the three phases |
| (default) | `03fefb94` | 06:18 to 06:28 | 551.29 s | passed |
| `--documenso` | `dd0ed6e0` | 06:28 to 06:43 | 836.96 s | passed |
| `--documenso-signing` | `24dd7242` | 06:43 to 06:59 | 889.23 s | passed |
| `--relay-peers` | `43e45186` | 06:59 to 07:10 | 599.94 s | passed |

Every receipt is complete with all owned compute off and no check false; each
purge left `syntheticStatePurged` true and the scratch directory empty. Evidence on
the build machine in `cms-tools-evidence-e7403a5/`.

### Observed rehearsals with website `dd1aef7` and the operator tools — November 24, 2026 (Pacific)

Womb `3f4bc9e` (re-pin to the kept `dd1aef7` build: web `1a4e07f7…bd12`, CMS
`8929443f…2a4c`; `terrace-cms-tool`), output on the build machine's
non-snapshotted scratch dataset, one run at a time, each purged with the ordinary
command as soon as it ended. Every variant composes the website with the pinned
release, so the re-pin changed all five driver derivations and all five ran once.
The rehearsals do not run the operator tools; the website and production VM
proofs do ([website host](website-host.md#operator-tools-terrace-cms-tool)).

| Flags | Run | Time | Scenario | Result |
| --- | --- | --- | --- | --- |
| `--all-services` | `9c0164ad` | 01:41 to 02:11 | 1735.81 s | passed: 198 HTTPS protocol assertions in each of the three phases, every receipt check true |
| (default) | `f2f9e504` | 02:11 to 02:22 | 553.57 s | passed |
| `--documenso` | `4621a7f4` | 02:22 to 02:36 | 826.16 s | passed |
| `--documenso-signing` | `b3c35eb0` | 02:36 to 02:52 | 891.50 s | passed |
| `--relay-peers` | `444bfb5e` | 02:52 to 03:03 | 603.36 s | passed |

Every receipt is complete with all owned compute off, no check false, and 198
HTTPS protocol assertions in each phase; each purge left `syntheticStatePurged`
true and the scratch directory empty. One invocation before these was rejected
before it created a run (its output directory did not exist yet); nothing was
started or written. Evidence on the build machine in
`cms-tools-evidence-3f4bc9e/`.

### Observed driver variants and failed-run purges at the verification sweep — November 23, 2026 (Pacific)

Womb `ede1f07`, website `eab4189`, output on the build machine's
non-snapshotted scratch dataset, one run at a time, each purged before the
next. PR 64 changed every driver variant's derivation and only `--all-services`
had run since; these four runs cover the others. Their driver derivations at
`ede1f07` are identical to trunk `427b464` and to PR 64's reviewed code; the
`--all-services` driver is identical to the one run `1f051756` passed, so it
was not run again.

| Flags | Run | Time | Scenario | Result |
| --- | --- | --- | --- | --- |
| (default) | `aa53c6c1` | 17:28 to 17:38 | 552.36 s | passed |
| `--documenso` | `cef2ad64` | 17:38 to 17:53 | 813.78 s | passed |
| `--documenso-signing` | `a7104cbb` | 17:53 to 18:09 | 861.66 s | passed |
| `--relay-peers` | `23beaa26` | 18:09 to 18:20 | 593.09 s | passed |

Each passed run had 198 HTTPS protocol assertions in the original and the
restored phase and every receipt check true, and was purged with the ordinary
command. An earlier default run in the failed-run exercise below (`15489aa2`)
finished before it was due to be stopped and also passed (scenario 559.4 s).

`purge --failed` was used on two real incomplete default runs (`e5d6ad2a`,
`c512e778`), left so by killing the controller by its recorded process id
while the guests ran; `c512e778` was in its restored phase, with retired
original disks, the exported repository and escrow and a `driver-root` link.
While the driver was building, `--failed` refused with "the controller in
controller.json is still running"; with guests up, with "the run unit or a
tagged guest is still running"; the ordinary form refused throughout. The
guardian stopped every guest within a second of the controller's death
(the unit exited 143 then); `--failed` then removed `state/`, `retained/`,
`escrow/`, the runtime directory and the `driver-root` link, kept the logs,
`evidence/` and the receipt (`syntheticStatePurged`, `purgedIncomplete`), and no
unit, tagged guest or controller of the run remained. For `c512e778` (about
851 MiB of allocated state) free space on the scratch dataset was 1.08 GB higher
just after the purge than while it ran, and 1.18 GB higher 60 s later. Evidence
on the build machine in `verification-sweep-evidence-ede1f07/`.

Limits: each variant ran once, so this shows no failure rate; and a `--failed`
refusal during the unit's stop window after the controller dies was never
observed (the guardian's shutdown finished before a second purge could start).

### Observed all-services rehearsals at the proof follow-ups — November 23, 2026 (Pacific)

Website `eab4189`, output on the build machine's non-snapshotted scratch
dataset, both passed: Womb `ce03a7c` (TLS check reasons, the TLS proof's clock
and backup-freshness changes, the Documenso TOTP guard, `purge --failed`), run
`2a1b0d0c`, 10:52 to 11:21 (scenario 1734.96 seconds); and after review fixes,
Womb `f0c35bd`, run `1f051756`, 12:26 to 12:54 (scenario 1702.27 seconds). Each
had 198 HTTPS protocol assertions in each of the three phases, all four
concurrent native browser helpers, and every receipt check true. The driver log
now carries one `Documenso TOTP request` line per two-factor request,
sequential and concurrent: five per run, all ended in their code's step, and
the guard fired once in each run (the original phase's concurrent call, which
waited 2.6 and 0.4 seconds and made its code at second 3.0). Both were purged
with the ordinary command (they passed, so `--failed` was not needed); evidence
on the build machine in `proof-followups-evidence-ce03a7c/` and
`proof-followups-evidence-f0c35bd/`.

### Observed all-services rehearsal with website `eab4189` — November 23, 2026 (Pacific)

The first `--all-services` run at the re-pin (Womb `1356603`, run `9d18e784`, 07:23 to
07:51) failed in the restored phase's concurrent Documenso browser check:
`two-factor view-recovery-codes` answered 500 to a request that ended at second 29
of the client's TOTP step. Documenso accepts only the server's current step and
returns 500 for a rejected code, so a code checked in the next step is the most
likely cause; it is not shown ([backlog](backlog.md) says why). As a mitigation the
browser helper now never makes a code within 3 seconds of either edge of a step. Its disks were removed by hand
(the purge command refuses failed runs). The second run, Womb `78188ce` with that
fix, run `798369c9`, passed from 08:19 to 08:49 (scenario 1722.43 seconds) with its
output on the build machine's non-snapshotted scratch dataset: 198 HTTPS protocol
assertions, all four concurrent native browser helpers, and every receipt check true.
Lifecycle steps before backup: apply 14.97 s, approve 1.56 s, redeem 0.47 s, sign-in
0.59 s, link 0.66 s; on the replacement: ownership 0.08 s, redeem 0.52 s, sign-in
0.74 s, link 0.63 s, erase 2.55 s, sweep 6.44 s, redaction 0.10 s. Its synthetic
state was purged with the documented command (receipt kept).

### Observed review-nit closure with restored lifecycle checks — November 19, 2026 (Pacific)

Womb `4348d0b` (PR 49, code-identical to the later head) re-ran the `--all-services`
rehearsal with website `680ad56` from 07:20 to 08:05 (scenario 1793.29 seconds); the
protocol assertions stayed at 198 in the original, restored and concurrent-restored
phases. The shared lifecycle steps now also assert, before the action, that no
offboarding notice exists in the restored outbox before `erase`, that no invitation
exists before `approve`, and that both invitation files still hold the applicants'
addresses with no erasure confirmation before `sweep`. They took, before backup:
apply 15.5 s, approve 1.56 s, redeem 0.63 s, sign-in 0.81 s, link 0.74 s; on the
replacement: ownership 0.09 s, redeem 0.71 s, sign-in 0.76 s, link 0.78 s, erase 3.39 s,
sweep 6.51 s, redaction 0.10 s. The run's synthetic state was purged afterwards
(receipt kept). At the same commit the infrastructure, run-check and runner unit tests,
the cloud rehearsal configuration check (now also failing closed on boot-enabled
sockets and paths), and the website and gateway checks passed. These steps are proven
in the VM rehearsal; they later ran on Hetzner in lease 5, for the two-host cloud rehearsal profile only ([cloud rehearsal](cloud-rehearsal.md#lease-5-result-2026-11-20)).

### Observed all-services acceptance with the website lifecycle — November 19, 2026 (Pacific)

Womb `48ab695` with the website `680ad56` bundles completed the `--all-services`
rehearsal from 06:13 to 06:58 (scenario 1840.51 seconds). Both phases passed 198
HTTPS protocol assertions and the four concurrent native browser helpers. The
shared lifecycle steps took, before backup: apply 15.48 s, approve 1.80 s, redeem
0.66 s, sign-in 0.82 s, link 0.91 s (25.69 s with content and the second address);
on the replacement: ownership 0.11 s, redeem of the restored pending token 0.63 s,
sign-in 0.88 s, link 0.81 s, erase of both members 3.23 s, sweep by the timer
6.39 s and redaction 0.10 s (13.10 s with content). PostgreSQL sampled peaks were
25 and 29 clients. Signed Documenso, Windmill (roster), Vaultwarden, K3s PVC,
automation and tailnet recovery checks all passed; all seven guest lifetimes were
absent at the end, the guard unit inactive and the driver's GC root released. The
run preserved its disks, repository and escrow. At the same commit the cloud
rehearsal configuration check and the infrastructure unit tests passed.

An earlier attempt at `ed08c4c` failed when the replacement automation guest
could not start: the builder's scheduled garbage collection (05:59 to 06:01)
deleted its unrooted closure mid-run. `48ab695` roots the driver for the run.

### Observed all-services acceptance — November 5, 2026 (Pacific)

Source `71b45be7f1ae2253b788bff69598beae8b642d9c` completed the
`--all-services` rehearsal in 1733.06 seconds (scenario timing 1733.03 seconds).
Both original and replacement phases passed 198 HTTPS protocol assertions and
four concurrent native browser helpers alongside the automation checks.

The original service guests were absent before blank replacements restored the
encrypted repository with separately retained escrow. Actual root/systemd backup,
restic restore and native PostgreSQL adoption preserved the complete role/password,
grantor/options and role-setting fingerprints before application writers resumed.
All five native app logins passed before their bootstrap-role and cross-app
privilege denials. The native Vault probe reads its generated unit User and
validates the expected account rather than assuming a Nix attribute key is an
operating-system username.

Fresh containers recovered the same signed/original PDF, certificate and
encrypted 2FA data; Windmill recovered individual identities, private variable,
script and old job/result, then ran new Bash and original eleven-case roster
jobs with the pinned offline SDK closure. Native Vaultwarden decrypted the same two
items and both attachments from the retained user device's token-free receipt.
Its server-side synthetic master-password fixture was absent before backup and
on the replacement. All three apps passed private administrator readback while
Keycloak was stopped, public password/admin denials and clean issuer return.
Administrator readback does not establish decryption of real users' vaults.

The fresh automation guest retained Home Assistant identity/sensors, synthetic
camera/relay behavior and its protected token. Headscale keys/users and all
three client identities/addresses survived; the two approved service peers were
replaced, while the denied test client was retained. The same allowed/denied
access checks and simultaneous application load passed again. K3s retained its
actual node/PVC identity and accepted a fresh volume write.

PostgreSQL sampled peaks were 27 and 28 clients over 205 and 214 samples, within
the configured 64 maximum; application pool limits totaled 42 with three reserved
superuser slots and 19 connections of non-app headroom. Kernel-recorded container
peaks stayed below their declared limits. These samples and test-VM resources
do not establish production sizing.

All seven distinct QEMU lifetimes were captured live and matched the original/
final guest receipts. All 38 recorded host PID/start identities were absent after
shutdown; the exact guardian was collected/inactive with zero MainPID/ControlPID,
empty recursive cgroup and no owned source/tag processes. The successful command
preserved seven disks, the encrypted repository and escrow by default.

After independent proof review, the explicit completed-run synthetic purge
removed only that run's seven disks, encrypted repository, escrow and validated
socket runtime. A missing-confirmation attempt was first rejected without
mutation. The receipt retained `complete=true`, `computeOff=true` and
`syntheticStatePurged=true`; all other receipt fields, 37 earlier protected
receipt/archive hashes and two unrelated/earlier-failure sentinels were unchanged.
Receipts and evidence remain private and retained.

No hosted-CI or merge success is implied by this native result. The current
GitLab jobs were rejected before execution because the namespace's instance-runner
compute quota was exhausted. Actual profiles remain disabled and the provider/
live-integration limits above remain unchanged.
