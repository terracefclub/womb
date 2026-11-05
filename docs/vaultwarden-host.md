# Vaultwarden native artifact preparation

This increment prepares a narrowly pinned native PostgreSQL binary and the
matching official web vault. No runtime module or actual profile is enabled.
The source pin follows the official Nixpkgs1.37.5 recipe at
[`224e5a3d`](https://github.com/NixOS/nixpkgs/blob/224e5a3d70c79442a94d16352f55fb28f0025744/pkgs/by-name/va/vaultwarden/package.nix),
with explicit source/vendor hashes and official web archive hash. No dependency
or image build runs on a developer's Mac.

Run the Linux artifact check under the shared builder lock:

```sh
BUILD_LOCK=/path/to/shared-heavy.lock platform/vaultwarden-package-check
```

The first Rust build has a40-minute total/build bound; use the documented
`run-check` bounds to override it explicitly. The check runs only `--version`
inside the build sandbox with an empty environment, disabled dotenv and an
absent saved-config path. It does not start a service, create accounts, connect
to PostgreSQL or deliver mail. The output must identify the pinned server and
web-vault versions.

The November5 Linux proof at source `66f5df8` passed with `Vaultwarden 1.37.5`
and `Web-Vault 2026.10.1`; the native build also passed all38 upstream tests.
The version-only output is
`/nix/store/zgsmaw0l7g66gyiqcz87168cm8m53nml-terrace-vaultwarden-release-proof-1.37.5`.
All86 frozen platform files matched their recorded hashes. The three outer
job identities and two observed compiler identities were absent after completion;
the isolated build PID namespace was empty on the final check. No application
service, database or account was started by this proof.

The pure initial client helper is
`platform/vaultwarden/policy.ts:vaultwardenClient(origin, secret)`: append its
user-free confidential code/S256 client to the existing reviewed production
realm. It allows only the exact `/identity/connect/oidc-signin` callback,
profile/email scopes and no password, implicit or service-account grant.
Keycloak startup import skips existing realms; this is not client reconciliation.
Native runtime, approved-email bootstrap, actual per-user SSO, denied-user and
master-password enrollment, encrypted data/attachment/key restore and combined
teardown remain unverified until their own VM proof.

## Optional native runtime (not enabled in actual profiles)

Import `platform/vaultwarden.nix` and set `terrace.vaultwarden.enable`, exact
`hostname`/`identityHostname` and external certificate/key paths. No DNS,
certificate issuance or provider change occurs. This dedicated instance uses
native PostgreSQL role/database `terrace_vaultwarden`, pool maximum6 and SQL
connection limit6; peer authentication is bound to UID44124. Backend18222 and
explicit migrator18224 bind loopback. UID egress is restricted to loopback;
a co-located identity hostname must resolve there. Optional `identityCaFile`
is a readable runtime trust bundle, with TLS verification enabled.

Provision root-owned0700 `inputsDirectory` (default
`/var/lib/terrace-vaultwarden-inputs`) with root-owned0400 `input.json`:

```json
{
  "origin": "https://vault.example.org",
  "issuer": "https://identity.example.org/realms/terrace",
  "clientId": "terrace-vaultwarden",
  "clientSecret": "<separate externally generated secret, 32–4096 UTF-8 bytes>",
  "adminToken": "<separate externally escrowed recovery secret, 32–4096 UTF-8 bytes>",
  "approvedEmails": ["approved@example.org"]
}
```

These placeholders are documentation only. The operator supplies actual values
at runtime; no secret file enters Nix evaluation or the store. The native
administrator token is hashed before entering the service environment; its raw
value remains only in protected input and local bootstrap memory. It is a
machine setup/recovery credential, not a shared human session. Public `/admin`
is always denied. Operator recovery uses only the local backend, independently
of identity, and must retain protected credential escrow.

`approvedEmails` is the COMPLETE enabled-account allowlist for this dedicated
instance. Bootstrap uses native admin cookies to create local invitation records
with email delivery disabled, enables approved records and disables/deauthorizes
all other native users. It never deletes users, encrypted vault records or
attachments. Do not adopt an unrelated multi-tenant instance without reviewing
this complete-list policy. SSO-only, verified email association, confidential
code/S256 and closed signup are fixed configuration; ordinary organization
invitations, mail, push and icon downloads are disabled. SSO authenticates an
individual; their master password still encrypts/unlocks their vault.

Initial operations, with inputs/certificates already installed:

```sh
systemctl start terrace-vaultwarden-migrate.service
systemctl start vaultwarden.service
systemctl start terrace-vaultwarden-bootstrap.service
```

The native binary [runs embedded Diesel migrations before HTTP startup](https://github.com/dani-garcia/vaultwarden/blob/1.37.5/src/db/mod.rs).
There is no invented migration-only or skip flag: the explicit unit runs the
original binary on its separate loopback port, checks every expected migration
version and generated RSA key, stops its exact systemd-owned private migration-server unit and verifies zero
main/control PIDs and an empty cgroup, then records the exact immutable
release. Normal startup first checks that marker and full ledger; the binary's
native migration call should then be a no-op. A release change requires the
explicit migration unit with application writers stopped. Startup, migration,
reconciliation, process counts, memory/CPU and private volatile logs are bounded.
The root input preparation, ledger guard and account bootstrap run as separate
bounded oneshots, outside the long-lived Rust application's native hardening.
The app retains its fixed unprivileged UID, no capabilities, private user/devices/
tmp isolation, executable-memory prohibition, privileged-syscall exclusion and
read-only host filesystem. Its Wants dependency schedules the root bootstrap
AFTER backend readiness; public readiness remains closed until that bootstrap
completes. Operators can synchronously wait on the explicit bootstrap unit.

Input rotation: stop `vaultwarden.service`, install the new protected input,
restart `terrace-vaultwarden-inputs.service`, then start the application. That
unit refuses to snapshot while application/control PIDs exist. Repeated account
reconciliation uses the installed `terrace-vaultwarden-bootstrap` under one
private ownership lock. Readiness is cleared before owning reconciliation and
published only after exact source/snapshot bytes, release and complete ledger
checks. Changed inputs and failed/partial/cancelled setup keep ingress closed;
Nginx uses private/no-store responses and fixed trusted proxy headers.

Rerun the synthetic native check on x86-64 Linux:

```sh
BUILD_LOCK=/path/to/shared-heavy.lock platform/vaultwarden-check
```

The check creates no real account or mail delivery and uses a synthetic test
certificate. It covers actual native schema/bootstrap, private admin/ports,
repeat setup, changed-policy closure, disable with record retention, RSA/ledger
continuity and stop/restart. On November5,2026, exact source
`e4f613383d671fbfce5bb07aa3a7ceeeebd5031b` passed all six native subtests in
37.14seconds (exit0), producing
`/nix/store/xn2d4c4b03lmwa0z1zhzkgyxm9nj3ns5-vm-test-run-terrace-vaultwarden-runtime`.
The failed-start sentinel, full48 migration ledger, native app hardening,
repeated bootstrap, changed-policy closure, exact account disable/re-enable
identity, RSA retention, FIFO/broad-mode rejection and final service stop passed.
All106 frozen platform files matched their recorded hashes. Live host
fingerprints for the wrapper, driver and both uniquely tagged QEMUs were absent
at final inspection; the exact guest tag and source-process scan both returned0.
This establishes native runtime/setup/retention, with the SSO/crypto limits below.

Failed builds are retained by
`--keep-failed`; before teardown the fixture captures bounded private unit/PID
metadata and logs into a0700/0600 bundle outside the published result. Only a
fixed capture notice is printed; raw evidence must be inspected privately.
Per-user native SSO (approved/unknown/disabled/old-session denial), master-password
enrollment, actual encrypted item/attachment decryption and original-node-off
fresh recovery remain separate acceptance gates. A placeholder issuer in this
first runtime fixture does not establish SSO integration.

Backup ownership: this module includes its PostgreSQL DB plus
`/var/lib/vaultwarden`, protected inputs and `/var/lib/terrace-vaultwarden`
release metadata in `backup.nix`. Operators must quiesce `vaultwarden.service`
and migration/bootstrap writers before a consistent DB/files snapshot. Removal
retains these paths by default; no live deletion command is provided. Encrypted
backup inclusion alone is not restore evidence; fresh-host decryption and actual
off-site/operator escrow recovery remain unverified.

The private migration server is a separate native systemd unit with the same
application hardening and a150-second independent runtime bound. Its parent
oneshot uses exact-unit stop (never a recycled numeric PID), preserves the first
completed/failed unit result, requires zero main/control PIDs plus an empty
recursive cgroup before resetting only that owned failed unit, and withholds or
removes the release marker on uncertainty. Parent cleanup repeats exact-unit
stop; a native failed-start test must leave an unrelated bounded sentinel alive.

Successful cleanup of a failed migration server is not successful migration.
The positive marker additionally requires a live/running server immediately
before stop and a clean exit0 or expected SIGTERM15 with successful unit result
after stop. A server that fails after HTTP/ledger readiness is cleaned up but
leaves the release unmarked.


## Per-user functional acceptance

The optional companion check composes the same reviewed TFC initial user-free
realm with `vaultwardenClient`, then separately enrolls synthetic people through
the private identity API and removes its temporary master administrator. No
production realm import seeds a user or changes the four website/CMS clients.
Run on the Linux builder with the reviewed TFC source available:

```sh
BUILD_LOCK=/path/to/shared-heavy.lock platform/vaultwarden-functional-check /path/to/reviewed/tfc-source
```

On November5,2026, native source `b66b7f8` passed all five subtests in197.54s,
with output `/nix/store/7c4p99h50qra2avyrg9k9mq4jkadmfxk-vm-test-run-terrace-vaultwarden-functional`.
All15 recorded host process fingerprints were absent afterward; the test also
requires exact application/identity/PostgreSQL units and listeners stopped.
The fixture serves a separate signed SAN/server-auth leaf and trusts only its
synthetic CA; certificate verification stays enabled.
It exercises real trusted HTTPS/Keycloak code/S256 browser sign-in, individual
master-password creation and fresh-browser unlock, actual web-vault encryption
and clipboard decryption of two separate users' items, reciprocal private-item
API denial, an unapproved identity, a previously valid/unexpired revoked session,
fresh disabled-account denial, and re-enabled-user decryption of retained data.
It preserves all native runtime policy, checks signatures through the actual
server/API, and uses JWT expiry parsing only to exclude expiration as a false
revocation result. The pinned upstream [SSO browser tests](https://github.com/dani-garcia/vaultwarden/blob/1.37.5/playwright/tests/setups/sso.ts)
provide the UI entry contract; identity credentials and vault master passwords
are separate. Browser traffic to other hosts is denied; no real provider is used.
The synthetic enrollment explicitly deselects the optional external password-breach
lookup, while retaining the strong-password and third-party-request denial checks.
It dismisses only the native optional extension-install introduction and its
explicit skip confirmation, then still
requires the authenticated vault, actual profile and decrypted-item checks.
Fresh synthetic contexts also explicitly skip the exact native first-use welcome
tour. Item creation selects the native Login type through the pinned client picker.

The reproducible Vaultwarden client sets `access.token.lifespan=600`; the shared
realm's website/CMS lifetime remains60 seconds. With native IdP session coupling
enabled, [Vaultwarden1.37.5](https://github.com/dani-garcia/vaultwarden/blob/1.37.5/src/sso.rs)
carries the IdP expiry into its own token, while its web client refresh window is
[five minutes](https://github.com/dani-garcia/vaultwarden/blob/1.37.5/src/auth.rs).
The browser requires a real refresh token and more than300 seconds remaining,
native profile200, expected email/UUID and token-subject equality. Failure
evidence retains only bounded lifetime/status numbers and equality booleans.
Changing this initial client artifact requires deliberate reconciliation for an
existing realm; startup import still skips existing realms. Approval, PKCE,
IdP session coupling and both existing/fresh revoked-session denials remain
mandatory. Before its deliberate fourth rapid operator start, the test clears
only the exact stopped unit's restart counter after proving inactive state,
zero main/control PIDs and no control group. The native three-starts-per-five-
minutes guard remains unchanged.

Each phase prints a fixed result. Failed checks retain bounded private labels
plus fixed numeric network/status counters (never URL, token or error text)
and screenshots in the kept build's0700/0600 shared directory, outside the
published result; cookies/tokens/passwords/response bodies are never printed.
The synthetic browser receipt is reusable by a later create/verify recovery
companion. Actual attachment decryption and original-node-off encrypted fresh
restore remain follow-ups; a same-node restart is not that restore evidence.
