# Established-service bootstrap and recovery rehearsal

This optional builder-only command composes the existing K3s foundation with
native website/CMS, Keycloak and PostgreSQL, a separate synthetic Home Assistant/
Frigate guest, an independent Headscale guest, and a repository/browser guest.
The extra guests isolate incompatible test contracts and recovery escrow. They
are not required production topology. Actual host profiles remain disabled.

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
  --webArchive /protected/releases/terrace-web-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz \
  --cmsArchive /protected/releases/terrace-cms-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz \
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

The build queue is bounded at 1200 seconds, warming at 1800 seconds (each Nix
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
PostgreSQL requires its bootstrap catalog owner to remain SUPERUSER; see
[PG18 ALTER ROLE](https://www.postgresql.org/docs/18/sql-alterrole.html).
This internal role is made NOLOGIN, has no password, no direct or inherited
membership path, and no client sessions. Actual fresh peer-login and app-role
assumption attempts must fail; normal managed `postgres` remains the login
administrator. The recovered PVC must
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
and unrelated paths. Failed runs are rejected: investigate/capture them before
a separately reviewed deletion. Pure rejection/sentinel tests run with
`direnv exec . python platform/rehearsal/test-full.py -v`.

This is prebuilt-closure VM bootstrap/recovery, not a blank-provider install,
public DNS/certificate issuance, CDN behavior, off-site operator recovery,
real invitations or final simultaneous capacity approval. Windmill and
Vaultwarden are not part of this scenario. Documenso is included only with
`--documenso`; the original baseline acceptance below is separate from the
optional-extension results.
A passing source-matched native receipt is required before claiming this
scenario accepted.


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
peers and adds the accepted Windmill and Vaultwarden runtimes. Its own
source-matched combined native receipt is still required; component restart
proofs do not establish replacement-host recovery.

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

With the accepted archives already present on Falcon, the complete invocation
from a clean repository checkout is:

```sh
install -d -m 0700 /home/tristan/terrace-work/full-rehearsal-output
direnv exec . env BUILD_LOCK=/home/tristan/.falcon-heavy.lock \
  ./platform/full-rehearsal run --all-services \
  --output /home/tristan/terrace-work/full-rehearsal-output \
  --webArchive /home/tristan/terrace-work/release-390-output/terrace-web-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz \
  --cmsArchive /home/tristan/terrace-work/release-390-output/terrace-cms-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz \
  --tfcSource /home/tristan/terrace-work/release-a2
```

Successful `run` stops all owned compute and preserves its disks, encrypted
repository and separately protected escrow by default. Only an explicit
completed synthetic purge removes that run's retained payloads:

```sh
direnv exec . ./platform/full-rehearsal purge \
  /home/tristan/terrace-work/full-rehearsal-output/run-REPLACE_WITH_RECEIPT_RUN_ID \
  --synthetic-only --yes
```

Receipts/evidence and unrelated or failed runs remain. No provider apply, blank
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
