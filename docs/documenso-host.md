# Optional Documenso host

`platform/documenso.nix` is default off. It runs the unmodified official
Documenso v2.21.1 Linux amd64 image with native PostgreSQL and an exact HTTPS
nginx virtual host. No host profile enables it and no deployment is performed.
The image manifest digest and Nix archive hash are pinned in
`platform/documenso/image.json`; use the [OCI metadata tool](oci-images.md) to
review a version change. The earlier Nixpkgs 2.14 candidate is not used:
[GHSA-3494-9j87-fj64](https://github.com/documenso/documenso/security/advisories/GHSA-3494-9j87-fj64)
requires a patched release after 2.18.0.

## Runtime inputs and initialization

Supply a root-owned mode 0700 `inputsDirectory` (default
`/var/lib/terrace-documenso-inputs`) containing root-owned mode 0400
`secrets.json`. Values must never be placed in Nix, command arguments, Git,
logs or a public report. Its exact keys are:

| Key | Contract |
| --- | --- |
| `authSecret`, `encryptionKey`, `encryptionSecondaryKey`, `oidcClientSecret`, `databasePassword` | Five different generated 32–256 character strings; letters, digits, `_+/=-` only |
| `recoveryUser` | Object with `email`, `name`, and a generated 32–72 character `password` using the same alphabet (upstream bcrypt/login limit) |
| `approvedUsers` | At most 32 objects with `email`, `name`, and `role` (`USER` or `ADMIN`); no password |

Addresses must be unique, names bounded and free of control characters. Reserve
the recovery identity outside the identity provider's enrollable addresses.
Keep original encryption keys with the independently protected recovery inputs:
changing them can make existing encrypted application data unreadable. The
validator does not generate replacement secrets or rotate them on restart.

Set `hostname`, `identityHostname`, `certificateFile` and
`certificateKeyFile` to reviewed exact names/runtime paths. `identityCaFile`
optionally mounts a private CA for the issuer; certificate and email verification
remain enabled. The module does not obtain certificates or configure DNS.

Initialize explicitly with writers stopped:

```sh
sudo systemctl stop terrace-documenso.service
sudo systemctl restart terrace-documenso-inputs.service
sudo systemctl start terrace-documenso-database.service terrace-documenso-image.service
sudo systemctl start terrace-documenso-migrate.service
sudo systemctl start terrace-documenso-bootstrap.service
sudo systemctl start terrace-documenso.service
```

Migrations and bootstrap are separate bounded oneshots, not container startup
side effects. Account or secret changes require the same stopped-writer sequence;
restart the database input unit when changing its password. The application
checks both the current validated input fingerprint and image digest against the
successful bootstrap receipt. Preparing new inputs or a failed bootstrap cannot
reuse an earlier receipt. Do not run these operations concurrently.

The bootstrap tags only its own accounts. A repeated unchanged bootstrap retains
their IDs, password hashes and timestamps. Changed managed accounts have their
sessions revoked; removed approvals are disabled and lose sessions while their
user/document records remain. A matching unmanaged address fails the transaction
instead of being adopted. Other unmanaged users are deliberately untouched;
review/reconcile effective access before restoring an existing database. This
file is not a universal allowlist for arbitrary preexisting accounts.

Upstream also seeds two internal service identities for legacy/deleted document
ownership. Preserve those records; they are not approved human accounts.
The VM requires the exact two native identities plus the exact managed subset,
rejects unexpected users, and exercises native sign-in denial for both reserved
identities. See the upstream
[service-user migration](https://github.com/documenso/documenso/blob/v2.21.1/packages/prisma/migrations/20230830053354_add_service_user/migration.sql)
and [deleted-account migration](https://github.com/documenso/documenso/blob/v2.21.1/packages/prisma/migrations/20240205120648_create_delete_account/migration.sql).

## Per-user identity and recovery boundary

Compose `documensoClient(origin, secret)` from
`platform/documenso/client.ts` into the reviewed **user-free initial** Terrace
realm artifact. It emits an exact confidential authorization-code client with
PKCE S256, callback `/api/auth/callback/oidc`, and profile/email scopes. It enables
no password, implicit or service-account grant. Initial realm import is
create-only; client changes on an existing realm need separate reviewed
reconciliation. No real accounts, invitations or enrollment are created here.

This uses upstream instance-wide generic OIDC, not the enterprise organisation
SSO feature. Upstream links an existing verified email to an OIDC subject on
first sign-in; subsequently it uses that provider/subject association. It does
not enforce a Keycloak role/group claim. Closed signup and explicit application
approval provide the fresh-host gate. Do not promise immediate identity-provider
role revocation: revoke application sessions/disable the managed account with
the stopped-writer bootstrap, and handle existing provider sessions separately.

Public password signup, password login, forgotten/reset password routes and their
Remix data/action paths are blocked by nginx. Native password recovery remains
available only at the host's loopback app port to an authorized operator; it is
not a public fallback login. No shared administrator session is provisioned.
The application still uses upstream session lifetimes. Public passkey/2FA and
document recipient paths keep upstream authorization; they are not signup
exceptions. Recovery usability and actual OIDC/browser flows require the
functional companion check before activation.

Source contracts checked 2026-11-04:
[OIDC configuration](https://github.com/documenso/documenso/blob/v2.21.1/packages/auth/server/config.ts),
[email linking and signup gate](https://github.com/documenso/documenso/blob/v2.21.1/packages/auth/server/lib/utils/handle-oauth-callback-url.ts),
[disabled-account session guard](https://github.com/documenso/documenso/blob/v2.21.1/packages/auth/server/lib/utils/authorizer.ts),
[organisation SSO licensing](https://docs.documenso.com/docs/users/organisations/single-sign-on/configuration).

## Service and retention contract

| Boundary | Value |
| --- | --- |
| Writer | `terrace-documenso.service`; stop it before migrations/backup capture |
| Explicit preparation | `terrace-documenso-{inputs,database,image,migrate,bootstrap}.service` |
| Native database/role | `terrace_documenso`; PostgreSQL Unix socket, SCRAM, role connection limit 6 |
| Prisma URLs | Equal runtime database/direct URLs, `connection_limit=6&pool_timeout=10&connect_timeout=5`; offline migrations/bootstrap use 1 |
| Loopback HTTP | `127.0.0.1:13050` by default; only exact HTTPS nginx host is public |
| Resources | App container 2 GiB/2 CPUs/256 PIDs, service 2304 MiB/320 tasks; offline helper 1 GiB/1 CPU/128 PIDs |
| Retained inputs | `/var/lib/terrace-documenso-inputs` by default; includes all recovery/encryption inputs |
| Retained receipts | `/var/lib/terrace-documenso` |
| Ephemeral prepared inputs | `/run/terrace-documenso-inputs`; root-only environment files, mounted read-only account file |
| Document bytes | Upstream database upload transport; included in the dedicated PostgreSQL backup |
| Email | Local SMTP sink/relay only, default port 1025; no hosted credentials/provider configured |

The container is UID/GID 1001, read-only, without capabilities, with no-new-
privileges, a signal-forwarding init and bounded tmpfs. Migrations/bootstrap have no network, only the
database socket. Private app/helper output is suppressed. Stopping/removing the
module does not delete PostgreSQL or input directories. When `terrace.backup`
is enabled, its existing PostgreSQL dump and extra-path machinery includes this
database and both retained directories. Quiesce the app and finish/stop both
offline writer units before a consistent capture; retain runtime certificates
and issuer configuration separately. Restoring an archive is not acceptance
until login, encryption and document retrieval have been exercised on replacement
guests with originals stopped.

Upstream `/api/health` tests a real database query. A successful HTTP response
can still report a signing-certificate warning; runtime readiness does not prove
signing readiness. No signing certificate is supplied by this module increment.

## Checks and remaining gates

From a clean checkout:

```sh
direnv allow .
direnv exec . bun test tests/platform/documenso-inputs.test.ts
# Configured x86_64 Linux builder only, with the shared lock path:
BUILD_LOCK=/absolute/shared/build.lock platform/check documenso
```

The VM check uses generated synthetic runtime inputs and a test certificate,
explicit migrations/bootstrap, idempotence, retained database restart, failed
and stale approval gates, managed offboarding/session removal, loopback binding,
public password-route denial and the unauthenticated admin loader regression.
That regression uses the pinned image's React Router decoder: a `.data` response
is accepted only as a 202 with an exact redirect-only result and no account data,
not as an arbitrary successful HTTP status. Normal app stops must avoid timeout
KILL; final cleanup requires no containers or listeners on its three service ports.
It never enrolls real users or sends email. Actual per-user OIDC, native recovery,
encrypted data, unsigned PDF creation/retrieval and replacement-host restore
remain functional companion gates; signing additionally requires a generated
test-only key/certificate and local mail capture. None of these gates authorizes
real signing, live email, publishing or deployment.
