# Optional Windmill runtime

The prototype consumes the official unchanged Community Edition image. Its
OAuth authentication includes non-public proprietary code, unlike the pure OSS
Nixpkgs package whose OAuth implementation is a stub. The upstream free CE
limits SSO to ten users; this is not an Enterprise trial or a paid license.
See the [exact upstream license](https://github.com/windmill-labs/windmill/blob/v1.861.0/LICENSE).
No image is rebuilt, patched, rebranded or redistributed by this repository.

Inspect a proposed official image pin from a clean checkout (public metadata
only, bounded network requests, registry bearer tokens never printed):

```sh
direnv exec . bun platform/oci-image.ts windmill-labs/windmill 1.861.0
```

The Linux/amd64 manifest digest is verified against its bytes before reporting.
This metadata lookup is not runtime or functional acceptance.

Fetch/verify the pinned archive on the Linux builder, without starting a service:

```sh
direnv exec . env BUILD_LOCK=/absolute/private/shared-build.lock CHECK_TIMEOUT=900 CHECK_BUILD_TIMEOUT=900 ./platform/run-check --impure --file platform/windmill-image-check.nix
```

For reviewed pin updates that expression also accepts `--argstr version`,
`--argstr imageDigest` and `--argstr archiveHash`. These override only a private
preflight evaluation, leaving the runtime pin untouched. An intentional dummy
archive hash can identify the actual fixed-output hash; its expected mismatch
is never a passing verification. Commit the measured hash and repeat the
default check, then run the full runtime/SSO acceptance before claiming support.

## Native PKCE release prerequisite

Upstream [1.861.0](https://github.com/windmill-labs/windmill/releases/tag/v1.861.0)
(November 4, 2026) introduces per-provider SSO PKCE. The bootstrap explicitly
sets `pkce: true` beside the Keycloak client credentials, matching the
[upstream implementation contract](https://github.com/windmill-labs/windmill/pull/12189).
The flag is opt-in for existing providers: merely changing the image does not
enable it. Upstream describes a fresh verifier per login, S256 challenge and
HTTP-only cookie, and callback verifier exchange. Our strict Keycloak client
and browser acceptance remain required; static authorization parameters are
not substituted for native verifier binding.

The previous 1.839 functional trial observed an authorization request failing
S256/challenge validation. No per-user SSO acceptance was established. The new
image requires its own runtime and browser proof before enabling any profile.
Source review found 16 added forward migrations, including older timestamps;
the new latest version is 20261102180907 and the three background migration
identifiers are unchanged. Image-bound migration markers force explicit stopped
writer migration on this pin change. No live database upgrade is performed by
these disposable checks.

Checked November 5, 2026: the five published upstream
[security advisories](https://github.com/windmill-labs/windmill/security/advisories)
list patched versions no later than 1.782.0. This is a dated advisory check,
not a claim of vulnerability absence. The 1.861 license adds explicit terms for
internal unchanged CE/API use and commercial redistribution/embedding; this
prototype uses the unchanged official image internally, with no license bypass,
image modification or commercial embedding. Actual rollout remains separate.

## Runtime and bootstrap boundary

`nixosModules.windmill` is optional and disabled in every actual profile. It
runs the original hash-checked image as UID44123, with no capabilities, no
privileged mode, no Docker socket, a read-only image filesystem and bounded
writable `/tmp`. Server/worker bind loopback; nginx adds a distinct non-default
HTTPS vhost. External TLS files are systemd credentials. Host/container UID
matching and a read-only PostgreSQL socket mount provide peer authentication as
`terrace_windmill`; no database password or bridge listener is introduced.
The mandatory upstream `windmill_user`/`windmill_admin` group roles have no login.

Supply a root-owned0700 `inputsDirectory` (default
`/var/lib/terrace-windmill-inputs`) containing root-owned0400 `bootstrap.json`:

```text
adminPassword: independently generated32–4096UTF8bytes, trimmed/no controls
origin: exact configured https://hostname, no trailing slash
issuer: exact canonical https://identity-host/realms/terrace
clientId: terrace-windmill
clientSecret: independently generated32–4096UTF8bytes, distinct from adminPassword
approvedUsers: [{email, name, role: admin | operator}]
```

Unknown keys, duplicate accounts, the factory account in the approval list,
control characters, weak/equal secrets and more than ten SSO users fail closed.
At least one individually named administrator is required. This is operator
approval input, not a seeded/shared human administrator. The upstream factory
account is retained solely for protected local bootstrap/break-glass; its
factory password is rotated and explicitly rejected. Public password login is
blocked. Approved users use their own Keycloak accounts; unlisted existing
Windmill accounts are disabled without deleting their data.

Bootstrap writes/read-checks the closed OAuth signup policy, disables telemetry,
Hub synchronization and SMTP, and configures the named Keycloak provider. Every
bootstrap begins by removing the public readiness marker, including invalid
inputs and failed reconciliation. nginx returns503 until successful rotation
and approval/provider reconciliation. Server restart and stop also remove that
marker; nginx may start concurrently without exposing a first-run window.

Initial user-free realm composition must call the pure
`platform/windmill/policy.ts` `windmillClient(origin, clientSecret)` helper:
confidential authorization code with S256 PKCE, exact
`/user/login_callback/keycloak`, no password/implicit/service-account grant,
profile/email scopes and optional offline_access. Append it to the existing TFC
production realm; do not duplicate the four website/CMS clients or seed users.
Keycloak startup import skips an existing realm: this helper is initial input,
not ongoing client reconciliation. Complete real SSO/denied-unapproved browser
proof is a separate release gate; a registered provider is not that proof.

## Explicit operator sequence and rerun

Enable the module only in a disposable configuration with runtime inputs/TLS
already delivered. Run `terrace-windmill-migrate.service` explicitly before
starting `podman-terrace-windmill-server.service` and
`podman-terrace-windmill-worker.service`. The migration unit conflicts with and
orders after both writers, runs the original server privately, waits for the
pinned SQLx ledger **and three resumable background migrations**, then stops its
exact container. Normal runtime has `SKIP_MIGRATION=true` and rejects a missing
or different-image migration marker. Upgrades/downgrades require separate schema
compatibility review; HTTP readiness alone does not mark a release migrated.

`terrace-windmill-bootstrap.service` repeats the same bootstrap used in server
startup. Both paths use the same root-private serialized ownership lock; wait
time is bounded at30seconds (exit75 if busy). Cancellation/failure removes the
readiness marker. Before success the owning run re-reads the protected input
and requires identical bytes and secure ownership/mode, so a long older run
cannot publish stale approval after an input update, even if a newer caller's
lock wait expires. A busy caller with unchanged input does not supersede an
identical successful reconciliation. It never needs a browser wizard. All diagnostics are fixed stage labels;
raw account/provider/token responses stay private. App/container logs use the
bounded16MiB volatile privileged `terrace-apps` journal. Server and worker have
explicit memory/CPU/PID bounds and PostgreSQL pools6/4 (`DATABASE_CONNECTIONS`).

Run the disposable real-image VM check on the Linux builder under the shared lock:

```sh
BUILD_LOCK=/path/to/shared-heavy.lock platform/windmill-check /path/to/accepted/tfc-source
```

The first cold image archive can be verified independently, with bounded
queue/build time, before VM execution:

```sh
cd platform
BUILD_LOCK=/path/to/shared-heavy.lock ./run-check --impure --file windmill-image-check.nix
```

To measure host systemd's completed-failure/stop/reset behavior without another
guest, create a root-owned0700 evidence directory and run on the Linux builder:

```sh
sudo -n python3 platform/windmill-unit-probe /private/root-owned-directory/probe.json
```

It creates one unique test-owned `false` unit (5s runtime bound), records the
original failure and post-stop state, then stops/resets only that unit in
`finally`. Main/control PIDs and the recursive cgroup must be empty, and the
final unit inactive, before success. The receipt is0600 and never overwritten;
controller commands have3s deadlines and the diagnostic phase has a30s bound.
On November 5, 2026, systemd261.3 returned stop status0 while retaining the
original failed state/status 1; resetting the exact stopped unit then made it
inactive/collected. The VM helper therefore preserves its original result,
requires zero main/control PIDs and an empty cgroup, resets only a known failed
test unit, and requires an inactive final state.

The check hash-verifies the same three secret-free TFC producer files as the
gateway check. It composes the reviewed `windmillClient` into a user-free realm,
boots actual native Keycloak, and checks trusted HTTPS discovery both as root
and as the restricted Windmill UID. No master administrator or identity users
are needed for this runtime proof. Per-user sign-in remains a separate check.
The wrapper is the acceptance entry point because this test requires the external
reviewed producer input; it is not exported as an input-free flake check.

The planned VM acceptance checks explicit real-schema migration, repeated
bootstrap, delayed/failed bootstrap503, rejected factory credentials, per-user
account records, one real bounded Bash worker job, unprivileged/read-only
container state, UID-scoped egress and retained data after exact service stop.
Record final passing source/result before considering these checks verified.
A generic job proves runtime only; it does not migrate membership flows.

### Individual sign-in and worker acceptance

The separate functional check uses the pinned image, explicit migration and
reviewed initial client, with the approved-state bootstrap described below. Its
shared identity
fixture enrolls distinct synthetic administrator and operator accounts, an
initially approved operator for revocation, and an unapproved IdP account. No
account, password or generated realm enters the Nix store. It uses native
Keycloak login through the Windmill button and requires authorization code with
S256 PKCE and the exact callback; it never substitutes a shared admin session.
The official [Keycloak setup](https://www.windmill.dev/docs/advanced/setup_oauth#keycloak)
requires the `offline_access` scope already present in the reviewed client.
Community license limits and lack of automatic role/group reconciliation above
still apply; the fixture uses three individually approved app users.

From a clean checkout, allow its credential-free shell once with `direnv allow .`.
Run only on the configured x86-64 Linux builder with its shared heavy-job lock:

```sh
direnv exec . env BUILD_LOCK=/absolute/shared-heavy.lock \
  BUILD_LOCK_TIMEOUT=1200 CHECK_TIMEOUT=1500 CHECK_BUILD_TIMEOUT=1200 \
  CHECK_STOP_GRACE=30 ./platform/windmill-functional-check /absolute/accepted/tfc-source
```

`platform/test-identity-contract.nix` verifies the three accepted TFC producer
source hashes. The check requires no provider credentials or license bypass.
Its initial scope is native browser sign-in, exact per-user identity and role
readback, operator administration denial, private variable isolation between
operators, and an approved operator running a persisted Bash script on the real
worker. Public password login and anonymous identity reads must fail. Removing
approval must preserve the account but remove tokens and reject both its stored
session and a fresh IdP login. Token-cache invalidation is measured with a
130-second bound; it is not claimed to be instantaneous.

The private browser helper's `create` and `verify` modes retain a stable
workspace/script path, completed job identity/result digest, per-user identities
and private variable digest. The receipt contains no cookies or passwords.
`verify` starts fresh individual sign-ins, checks retained data and runs a NEW
worker job. A separate protected old-session file exists solely for the
`disabled` phase. These modes can bracket a later originals-off restore. The
standalone check covers restart continuity; it does not establish fresh-host
recovery, actual membership-flow execution, capacity under production load or
live Sheets/mail/payment integration. The source-matched native result is
recorded below; pure tests alone do not prove SSO support.
Measured November 5, 2026 at source `88842ca` on the old 1.839 image: the native browser flow emitted a
provider authorization request, but that request failed both the exact S256
method and challenge-format checks. The test stopped before individual login,
permission, worker and restart acceptance; all 15 recorded host identities were
gone afterward. This differs from the earlier loading-screen failure, whose
unobserved defaults established no PKCE result. Raw queries were not retained,
so these facts do not distinguish absent from malformed parameters.
The merged [PKCE prerequisite](https://gitlab.com/terrace/tech/womb/-/merge_requests/36)
now pins official 1.861 and enables its native per-provider setting. Its fresh
runtime check passed; the browser proof below establishes individual SSO against
the same strict S256 client policy.
Static extra parameters are not a substitute for per-login verifier binding.
Evaluating the driver requires retained native kernel/initrd, shared-filesystem
and test-network options on both guests. Ownership tags append a distinct key;
they never replace the framework's QEMU option list.
Pinned 1.861 returns exact 401 for superadmin and private-variable denials via
[`NotAuthorized`](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/windmill-common/src/error.rs),
the [superadmin gate](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/windmill-api-auth/src/lib.rs)
and [variable gate](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/windmill-store/src/variables.rs).
Its [script creation](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/windmill-api-scripts/src/scripts.rs)
now calls [check_role_can_code](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/windmill-common/src/workspaces.rs),
whose `PermissionDenied` maps to exact 403 for an operator. The public password endpoint still requires nginx 403;
successful per-user identity checks precede the role denials.

The first supported-version trial (`69c904d`, November 5) reached the worker
submission after native S256/callback-200, individual role checks and private
value isolation, then was denied access to the newly created script. Its
protected native journal showed the operator's cached folder list was empty.
Pinned [authentication caches folder ACLs for 120 seconds](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/windmill-api-auth/src/auth.rs),
and [folder creation invalidates only the creator's session](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/windmill-api-groups/src/folders.rs).
The check therefore opens another independent native operator session after
creating the shared folder and requires the same identity/role before job
submission. It changes no grant or cache setting. Failure facts distinguish
submission status, UUID validation, bounded polling and boolean result/owner
checks without recording job payloads, identifiers or credentials. The complete
native check exercises worker, restart and revocation together.

Unchanged bootstrap preserves existing sessions: it reads the native global
account inventory and sends only changed privilege/disabled flags. The native
[`superadmin` or `devops` update](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/windmill-api-users/src/users.rs#L1553-L1599)
invalidates sessions even when the supplied flag already matches. Real privilege
changes and reactivation still make the explicit native update and require
matching readback. Missing/duplicate approved identities or malformed fields
close readiness. Inventory uses one explicit page of 100 accounts and rejects a
full page as potentially incomplete, so a larger instance needs deliberate inventory
review rather than partial revocation. No unbounded paging is introduced.

Revocation positively resolves the retained alumni cookie at the end of restart
verification, immediately before approval removal. The native
[`disabled` update](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/windmill-api-users/src/users.rs)
deletes token rows; the public authentication implementation gives cached
session permissions an expiry up to 120 seconds. The check measures exact 200-to-401
convergence with a 130-second deadline, rejecting other statuses and late
responses, then requires global 401, fresh login denial and fresh approved
administrator/operator positives. It reports the measured delay without
claiming instantaneous revocation. The stable job/data receipt is unchanged.

The wrapper retains failed-build private state. Fixed stage/status facts and a
masked actual-page screenshot are copied only to owner-private failed-build
shared directories, never `$out`; runtime failure capture reuses the bounded
private module evidence path. All guests/writers must stop at completion or
failure through the existing bounded supervisor. Preserve retained data until
investigated; no automatic synthetic purge is added by this check.
Frontend failure facts cover only the latest sign-in page: 64 allowlisted
same-origin static/API path/status/MIME rows, 32 fixed network failures,
16 fixed page-error classes/kinds, pending-route counts and document readiness.
Queries/fragments, bodies, raw headers, cookies, callback codes and raw error or
console messages are omitted. Explicit truncation flags prevent an incomplete
capture being treated as proof of absence. Positive/failing metadata and private
marker suppression have focused tests; all authentication assertions stay active.
Auth facts reset for every person: `null` means a request/check has not occurred,
while observed PKCE and callback facts describe only that current login. Facts
are saved before optional DOM/screenshot calls so a failed renderer cannot erase
the network/error evidence.
The functional check trusts a separate synthetic CA and serves a signed
CA:false/serverAuth leaf for both exact fixture hostnames; OpenSSL verifies
purpose and hostnames before the fixture can build. System and browser trust
only the CA. This corrects the CA-as-leaf test pattern without asserting that
it caused any earlier frontend loading failure. The baseline runtime check's
default certificate fixture remains unchanged.

The check wrapper uses Nix `--keep-failed`. Before a failed guest is torn down,
it exports protected raw launcher/journal data, exact selected systemd state
(including completion code as well as status), and PID/start-tick observations
into a0700 directory under the failed build's shared directory, with0600 files.
It never uses `copy_from_vm` for these records: that would target substitutable
Nix-store output. Keep the failed build private; inspect only the named failure
artifact and do not publish it. A failed-build directory is retained by the
wrapper, not by an ordinary `nix flake check` invocation. The public summary
uses tested exact message allowlists; an absent message proves no stage or
cause. The fixed provider readback follows the pinned
[CE OpenAPI](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/windmill-api/openapi.yaml):
`oauth` contains provider objects with `type` and optional `display_name`.
Only Keycloak and no alternate provider is accepted.
The provider input also follows the pinned upstream
[Keycloak settings producer](https://github.com/windmill-labs/windmill/blob/v1.861.0/frontend/src/lib/components/KeycloakSetting.svelte):
the canonical realm URL is supplied as `org`, with an explicit fixed display
name and the exact login endpoints. Failure diagnostics expose only bounded
counts, HTTP status and fixed shape categories, never provider response values.
Strict provider readiness has a30s deadline, including per-request deadlines.
The pinned server polls settings invalidations every10s by default
([upstream main.rs](https://github.com/windmill-labs/windmill/blob/v1.861.0/backend/src/main.rs));
a5s readback loop could finish before one refresh. Ingress stays closed until
the exact provider check succeeds; an expired or late result cannot open it.

## Observed 1.861 runtime proof

On November 5, 2026, exact source `1f6b698` passed all nine native subtests
in 332.43 seconds. The default fixed-output archive hash verified, followed by
actual foreground/background migrations, persisted `pkce: true`, repeated
approved-user bootstrap, factory/public password denial, real Bash worker job,
UID/network boundaries, changed-input/concurrent and cancelled-owner failures,
closed ingress after failed rebootstrap, and final service stop/data retention.
All sixteen recorded host identities were gone; the isolated build namespace,
source-process and runtime-tag scans were empty. The immutable result was:

```text
/nix/store/rsskcbi5h8lz5aa5sgdznxb8576p48a3-vm-test-run-terrace-windmill-runtime
```

This is fresh-state runtime acceptance for the earlier bootstrap. The session
preservation follow-up is checked separately below. Existing 1.839 database
upgrade remains unproved. No profile was enabled.

## Observed individual SSO and session preservation

On November 5, 2026, native source `fa9d243` passed all four functional subtests
in **714.30 seconds**. It proved individual native code/S256 logins and exact
roles, unknown-user denial without account creation, operator administration and
authoring denial, private-variable isolation, persisted real-worker execution
with exact caller/result, and a new job after database/identity/app restart.
The original pre-restart session remained valid after unchanged bootstrap.
Removing approval then denied that warmed session after **5,015 milliseconds**,
with global 401, fresh login denial and fresh approved-user positives afterward.
Every writer/listener stopped and all 17 recorded host identities were gone;
source, workload tag and former isolated-namespace scans were empty. Result:

```text
/nix/store/wd5kjppb0wj2rlgv5ihy3lm0r8c703wb-vm-test-run-terrace-windmill-functional
```

Exact source `660035d` also passed the nine-subtest native startup/concurrency/
failure regression in **310.29 seconds**, with actual migration/bootstrap,
factory rejection, worker execution, changed-input/concurrent and cancelled
owners, failure closure and stop/retention checks. All 19 recorded host identities
were gone and owned source/tag/isolated-namespace scans were empty. Its result:

```text
/nix/store/w1zj2kh592f801iirga0m3ds1n27b7m6-vm-test-run-terrace-windmill-runtime
```

The same final snapshot evaluates to the exact successful functional artifact
above. The functional check uses a 1,500-second wall/1,200-second per-build bound;
the baseline regression uses 900 seconds for both. Each uses the existing shared
queue and supervisor, and all test services stop before the next lease.
Actual Python membership-flow
execution, encrypted originals-off recovery, production load and live provider
integration remain separate acceptance gates.

## Persistence and remaining gates

The database contains Windmill workspaces, queue/history, scripts, variables and
application secrets. Retain `terrace_windmill`, `/var/lib/terrace-windmill`
(migration marker) and the protected input directory. They are included when
`terrace.backup` is enabled. TLS delivery/independent escrow remains the operator's
responsibility if files live elsewhere. No backup target is enabled here;
actual encrypted restore and old-node-off combined recovery are follow-ups.
Stop the worker then server before taking a consistent database/input snapshot.
Worker `/tmp` and dependency caches are disposable and intentionally excluded.

### Managed Python runtime

The worker exposes the pinned official image's managed interpreter directory at
`/tmp/windmill/cache/py_runtime` through an absolute, read-only image subpath.
The [pinned Dockerfile](https://github.com/windmill-labs/windmill/blob/v1.861.0/Dockerfile)
installs Python 3.11/3.12/3.13 there. Native `uv python find` selects the interpreter;
its path cache and job/log directories remain disposable and writable.
No runtime package download or additional network access is enabled.

Worker explicit `/tmp` limits total 512MiB: 128MiB `/tmp`, 128MiB `/tmp/windmill`,
256MiB `/tmp/windmill/cache`. Both nested parents use mode 0700 and Podman's
[`U=true` tmpfs ownership option](https://raw.githubusercontent.com/containers/podman/v6.1.4/docs/source/markdown/options/mount.md),
which derives UID/GID from the container user without recursively changing the
separate interpreter mount. Server and migrator retain their original 512MiB
layout. Numeric `uid=`/`gid=` tmpfs CLI options are rejected by this Podman.

The full startup regression submits real Bash and Python 3.12 jobs. Python has an
explicit empty dependency lock and must return the exact result, run from the
managed directory and observe `ST_RDONLY` on its interpreter filesystem. The
container readback separately requires the configured user, read-only root and
exactly one `Type=image` mount at the runtime destination with `RW=false`.
Podman also reports a bind representation at that destination; selecting its
canonical image entry follows [GetMounts](https://github.com/containers/podman/blob/v6.1.4/libpod/container_inspect.go#L266-L274).
Failed synthetic results go only to the existing bounded root-private capture;
tokens, credentials and job payloads never enter probe stdout.

On November 5, 2026 exact source `4c0ea87` passed the small mount proof in
**112.70 seconds** and all **nine** native startup/worker/concurrency/failure/stop
subtests in **337.32 seconds**. The image remained unchanged; managed Python 3.12
and actual Bash jobs passed alongside factory rejection, account isolation,
UID-scoped egress, changed/cancelled bootstrap closure and retained state.
All eleven mount-proof and fifteen full-regression recorded host identities
were absent afterward; owned source/tag/former isolated-namespace scans were
empty. Results:

```text
/nix/store/fp549w2mv9pjmg38qg7z47mkxj0d4v43-vm-test-run-terrace-windmill-mounts
/nix/store/vhjj425yilmgdw9lxyzx4693b7bg0bz8-vm-test-run-terrace-windmill-runtime
```

For a small mount-only reproduction from a clean checkout on the Linux builder
with locked tools available:

```sh
BUILD_LOCK=/private/shared-heavy.lock BUILD_LOCK_TIMEOUT=1200 CHECK_TIMEOUT=300 CHECK_BUILD_TIMEOUT=300 CHECK_STOP_GRACE=30 platform/windmill-mount-check
```

It uses one 2GiB disposable guest without PostgreSQL, Keycloak, credentials or the
application. It reproduces the exact unsupported numeric-ownership error, then
checks shared production mount options, positive log/cache writes, native
managed Python, kernel read-only status and selected container readbacks.
Cold image import has a 180s bound; the whole check remains 300s. Exact containers
are removed in `finally`, and only inspection status 1 proves absence. The full
baseline command documented above retains its 900s wall/per-build limits.

This prototype permits only loopback egress for its dedicated UID, including
before telemetry settings are reconciled. Live Sheets, mail and payment access
remain disabled. Actual original roster/SDK execution and encrypted originals-off
Windmill recovery are separate acceptance gates. General worker code requires
trusted approved authors; privileged namespace/nested-container jobs,
Docker-in-Docker, LSP/multiplayer and hostile multi-tenant code isolation are not
provided. Other languages' hidden caches are unproved. Per-user login does not
make trusted-code execution a sandbox.

Upstream 1.861.0 exceeds the 1.782.0 fix boundary of
[GHSA-2jfj-x8j7-7mfh](https://github.com/windmill-labs/windmill/security/advisories/GHSA-2jfj-x8j7-7mfh);
this is version provenance, not a general security certification. Production
profiles, DNS/provider actions, complete membership/payment-email integration,
real-user enrollment, fresh-host restore, pressure/alert delivery and cloud
deployment remain disabled or unverified until their own acceptance passes.
