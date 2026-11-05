# Optional Windmill runtime

The prototype consumes the official unchanged Community Edition image. Its
OAuth authentication includes non-public proprietary code, unlike the pure OSS
Nixpkgs package whose OAuth implementation is a stub. The upstream free CE
limits SSO to ten users; this is not an Enterprise trial or a paid license.
See the [exact upstream license](https://github.com/windmill-labs/windmill/blob/v1.839.0/LICENSE).
No image is rebuilt, patched, rebranded or redistributed by this repository.

Inspect a proposed official image pin from a clean checkout (public metadata
only, bounded network requests, registry bearer tokens never printed):

```sh
direnv exec . bun platform/oci-image.ts windmill-labs/windmill 1.839.0
```

The Linux/amd64 manifest digest is verified against its bytes before reporting.
This metadata lookup is not runtime or functional acceptance.

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
On November5,2026, systemd261.3 returned stop status0 while retaining the
original failed state/status1; resetting the exact stopped unit then made it
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
[CE OpenAPI](https://github.com/windmill-labs/windmill/blob/v1.839.0/backend/windmill-api/openapi.yaml):
`oauth` contains provider objects with `type` and optional `display_name`.
Only Keycloak and no alternate provider is accepted.
The provider input also follows the pinned upstream
[Keycloak settings producer](https://github.com/windmill-labs/windmill/blob/v1.839.0/frontend/src/lib/components/KeycloakSetting.svelte):
the canonical realm URL is supplied as `org`, with an explicit fixed display
name and the exact login endpoints. Failure diagnostics expose only bounded
counts, HTTP status and fixed shape categories, never provider response values.
Strict provider readiness has a30s deadline, including per-request deadlines.
The pinned server polls settings invalidations every10s by default
([upstream main.rs](https://github.com/windmill-labs/windmill/blob/v1.839.0/backend/src/main.rs));
a5s readback loop could finish before one refresh. Ingress stays closed until
the exact provider check succeeds; an expired or late result cannot open it.

## Persistence and remaining gates

The database contains Windmill workspaces, queue/history, scripts, variables and
application secrets. Retain `terrace_windmill`, `/var/lib/terrace-windmill`
(migration marker) and the protected input directory. They are included when
`terrace.backup` is enabled. TLS delivery/independent escrow remains the operator's
responsibility if files live elsewhere. No backup target is enabled here;
actual encrypted restore and old-node-off combined recovery are follow-ups.
Stop the worker then server before taking a consistent database/input snapshot.
Worker `/tmp` and dependency caches are disposable and intentionally excluded.

This prototype permits only loopback egress for its dedicated UID, including
before telemetry settings are reconciled. It cannot call live Sheets, mail,
payment or other external services. Expanding egress/provider access needs a
separate reviewed activation; synthetic membership-contract execution is also
separate from a live provider cutover. General worker code is for trusted
approved authors: privileged namespace/nested-container jobs, Docker-in-Docker,
LSP/multiplayer and multi-tenant hostile-code isolation are not provided. The
read-only image/tmp boundary must be proved for each newly enabled language;
preinstalled caches hidden by `/tmp` are not assumed available. Per-user login
does not turn trusted-code execution into a sandbox.

Upstream1.839.0 exceeds the1.782.0 fix boundary of
[GHSA-2jfj-x8j7-7mfh](https://github.com/windmill-labs/windmill/security/advisories/GHSA-2jfj-x8j7-7mfh);
this is version provenance, not a general security certification. Production
profiles, DNS/provider actions, complete membership/signing/email integration,
real SSO, restore, pressure/alert delivery and cloud deployment remain disabled
or unverified until their own acceptance passes.
