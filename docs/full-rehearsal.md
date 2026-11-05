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
BUILD_LOCK=/absolute/shared-heavy.lock platform/full-rehearsal run \
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
code data. The private browser receipt models a retained user device in the
repository/browser guest; it is separate from server backup contents. Signing
and SMTP remain disabled, and actual profiles remain unchanged. This optional
scenario requires its own source-matched native acceptance receipt.

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
runs an actual PVC-mounted container; Headscale's first composition check is
control-plane/key/database recovery only. Real tailnet clients/ACL/DERP and
human relay enrollment remain separate follow-ups.

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
`--documenso`; the baseline observed acceptance below does not cover that
optional extension.
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
