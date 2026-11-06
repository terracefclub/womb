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

## Optional local signing certificate

`terrace.documenso.enableSigning` defaults to false. Enable it only with explicit
runtime files in the existing root-owned mode 0700 inputs directory:

| File | Owner/mode | Contents |
| --- | --- | --- |
| `signing.p12` | root/0400 | PKCS#12 signing certificate and private key, 256–65536 bytes |
| `signing-passphrase` | root/0400 | 32–256 ASCII letters, digits, `_+/=-`, with an optional final newline |

Keep both files in the protected component backup. The module does not generate,
replace or rotate them. Stop the writer before changing them, restart
`terrace-documenso-inputs.service`, then start `terrace-documenso.service`.
Preparation validates the certificate and passphrase with the pinned image's
native `P12Signer` in a resource-bounded container without network access. An
invalid, expired or not-yet-valid certificate prevents writer startup. The
application's native certificate health check must also pass. A source or
prepared-file change invalidates startup until preparation succeeds again.

The passphrase stays in private runtime files and the container environment;
neither it nor the private key enters Nix, command arguments or diagnostics.
Only the prepared certificate is bound read-only into the application as its
non-root UID. Signing uses the native local transport, with no timestamp
authority configured. Native sealing can resolve certificate intermediates, so
the isolated test uses a generated self-signed certificate without AIA URLs.
That certificate is a test fixture, not a publicly trusted identity or a claim
about a signature's legal status.

The runtime VM check covers valid certificate health, wrong passphrase,
malformed PKCS#12, changed source files and restored valid files before the
writer starts. Completing a recipient workflow and recovering signed PDFs on a
replacement host remain separate acceptance checks.

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


## Functional acceptance companion

The disposable functional check composes the reviewed TFC identity producer with
`documensoClient()` using the shared, user-free realm fixture. Its private seed
then enrolls synthetic people through Keycloak's private bootstrap interface and
removes that temporary administrator. It never enrolls the reserved recovery
address. A real browser uses the canonical HTTPS origins and trusted test CA;
certificate verification, PKCE and the ordinary app authorization code flow stay
enabled.

Disposable multi-service checks can import `writeDocumensoInputs()` from
`platform/documenso/write-inputs.ts` with the result of the shared identity
fixture and its `documenso` extra secret. This writes app inputs and the limited
browser credentials without creating another realm. Its default signing
preparation preserves the standalone check; `{ signing: false }` omits the
synthetic signing files for unsigned-only fixtures. This test helper does not
provision real accounts or reconcile an existing production realm.

```sh
# x86_64 Linux builder only; TFC source must match test-identity-contract.nix.
BUILD_LOCK=/absolute/shared/build.lock platform/documenso-functional-check /source/tfc
# Small protocol fixtures, also in CI:
direnv exec . python -m unittest discover -s tests/platform -p test_documenso_functional.py -v
```

The test creates an organisation, team and synthetic PDF through the
native interface. It tests approved member/admin login, closed signup for unknown
and unapproved editorial identities, ordinary-member admin denial, anonymous and
non-team document denial, and retrieval of the same document after database/app
restart. It uses the native Download PDF → Original action in the v2 editor,
checks downloaded bytes against the session-authenticated no-store endpoint and
requires its native 401 refusal for anonymous requests and 403 refusal for
users outside the team. It adds the synthetic member as a signing recipient,
places a native signature field and distributes through a local Mailpit capture
sink. The sink accepts only that `.invalid` recipient, binds SMTP to the private
Podman bridge and exposes its API only on loopback. Relay/forwarding is absent,
reverse DNS and version checks are disabled, and the client must be unable to
reach either listener. The sink's database stays in its private retained state
directory until the disposable guest is retired.

Before any sender UI cycle, a seven-second probe runs inside the actual app
container with its configured environment. It requires the native host alias to
resolve to the bridge host, then completes SMTP greeting/EHLO/QUIT without sending
a message. It also verifies the official image's installed Nodemailer transport
with the same local configuration. The captured signing email must correspond to exactly one completed
native `send.signing.requested.email` background job. The functional VM alone
enables a capped 4 MiB root-private container log; SMTP or mail-capture failures
export a bounded 0600 copy outside the Nix output and emit only fixed job-state
counts. Raw errors, payloads and tokens remain private. Production logging stays
disabled by this change.
Configuration is checked against the pinned
[SMTP transport](https://github.com/documenso/documenso/blob/8707e33c124e4b1a68dde92d5e05178c9aabd0be/packages/email/mailer.ts)
and [local job provider](https://github.com/documenso/documenso/blob/8707e33c124e4b1a68dde92d5e05178c9aabd0be/packages/lib/jobs/client/local.ts):
no SMTP username, no TLS for the local sink, and the container's own loopback job
endpoint. The probe also requires that exact local provider and endpoint.
The configured local SMTP destination is the default bridge's literal
`10.88.0.1`, avoiding the SMTP library's
[DNS-first hostname resolution](https://nodemailer.com/smtp#general-options)
before its hosts-file fallback. The probe separately verifies the native host
alias still identifies that bridge. This removes an unnecessary resolver
dependency; it does not establish the historical cause of a processing job.
The module permits the configured SMTP port only on the default `podman0`
private bridge. It does not add that port to the host's public allowlist; a
custom Podman network requires deliberate matching interface/bind review.

On November 5, 2026, the first synthetic sender cycle reached Send successfully,
but the copied retained Mailpit database contained zero messages. The read-only
[disk evidence helper](documenso-mail-evidence.md) verified the original disk's
unchanged hash. This establishes a delivery/job gap, not its SMTP cause; the
subsequent complete check below supplies the signing acceptance evidence.
Evaluation of that exact VM also confirmed an enabled input firewall with only
public 443 and Podman DNS 53 allowed. The private SMTP rule closes this
configuration gap; the actual protocol probe must still prove connectivity.

Only a bounded, exact-origin `/sign/` link is extracted privately from captured
HTML; links, images and scripts are never fetched by the capture helper. A fresh
browser context completes the native typed signature, field and recipient
confirmation. The v2 recipient heading includes its field-count/sidebar controls,
and fields are rendered on Konva. The check uses the native Next Field tooltip's
measured rectangle for its physical click, then requires the native Complete
action before confirmation; it does not invoke internal signing functions.
The sender downloads the native `signed` PDF version. Independent
OpenSSL CMS verification must use the exact generated fixture certificate and
reject altered signed bytes. The bounded PDF check requires one signature's
ByteRange to cover the complete file outside its signature placeholder; it does
not assert a public trust chain or timestamp authority. No test secret or
signing token enters diagnostics.

Identity is stopped before the shared database restart
and started again with the temporary bootstrap credentials removed; its deleted
master administrator must stay absent. It sets up native two-factor encryption, enables it with an actual TOTP,
and decrypts the same recovery codes after restart. This tests encryption key
continuity; generic OIDC relies on the identity provider for MFA and does not
thereby enforce Documenso's native password-login second factor.

Removing the source-managed alumni approval must revoke its existing browser
session and deny a fresh OIDC login while retaining its account. Finally the
check stops Keycloak and exercises the local recovery administrator through the
loopback interface; the same password endpoint remains forbidden publicly.
The browser fixes the locale and uses the pinned rendered English labels.
Failure diagnostics contain only counts for a fixed allowlist of accessible
labels, script failures and HTTP status; arbitrary DOM/text, exceptions and HTTP
bodies are suppressed. A failed browser phase also captures a private masked
viewport screenshot and bounded button/heading names. Input values, scripts,
links, PDF canvas and synthetic identities/content are excluded. The wrapper
also retains a failed signature check's synthetic PDF and generated public
certificate as private artifacts, so its exact bytes can be inspected without
another guessed runtime attempt. These files are never printed. The wrapper
uses Nix `--keep-failed`: artifacts remain mode0600 under the failed build's
mode0700 shared subdirectory, outside the Nix store, after guests stop. Inspect
and copy them to protected evidence storage on the builder, then remove only
that run's disposable synthetic build state when it is no longer needed. Private fixture credentials,
TOTP/receipt data and one temporary session-state file remain in the disposable
guest's root-only directory, outside test outputs and the Nix store. The latter
is removed after the revocation test. No database/encryption/client secret is
copied to the browser guest. All app/identity/database units and containers must
stop before the VM driver finishes.

`functional-browser.py create` and `verify` retain their unsigned-only contract
and do not require SMTP, a signing key or a signed receipt. The signing wrapper
explicitly opts into `create --signing` and `verify --signing`; `sign` is the
separate recipient step. The plain modes remain usable by the fresh-host restore
check independently of the signing acceptance gate. Both variants use a protected receipt containing a
relative document path, its download digest, a synthetic TOTP secret and a digest
of recovery codes. `signed-receipt.json` additionally retains the private signed
download path/digest; the generated public `signing.crt` permits the same
independent cryptographic check after recovery. This permits the same behavioral checks on a replacement
host without retaining raw recovery codes or depending on old login cookies.
The standalone check currently tests restart, **not replacement-host disaster
recovery**. That requires a verified encrypted backup, originals stopped and a
fresh replacement serving the same issuer/app origins. On November 5, 2026, all
four native functional subtests passed in 438.90 seconds: actual per-user SSO,
local-only email delivery and one completed native email job, recipient typed
signature/field/completion, signed PDF retrieval, exact-certificate CMS
verification and altered-byte rejection, anonymous/non-team denials, retained
signature/encryption checks after restart and offboarding, IdP-independent
loopback recovery, and final service/port stop. All 14 recorded host controller,
driver and guest processes were subsequently absent. This is synthetic
standalone acceptance; replacement-host signing recovery and production use
remain separate gates. Pure parser tests and certificate health alone do not
establish the workflow. No check publishes
documents, sends real mail, or signs legal documents.


Login failure diagnostics
-------------------------

The functional browser preserves the existing login timeouts and exact OIDC,
closed-signup, disabled-account and session assertions. Before the first provider
click it attaches per-attempt counters (saturated at 255) for page errors, failed
requests/scripts and same-origin authentication responses/errors. Failure output
records only the login ordinal, fixed substep/exception labels, numeric HTTP
statuses and expected-error/session-empty booleans. Each login resets those facts;
a later failure after a completed login is marked `not-login`. No exception text,
usernames, URLs, callback tokens, response bodies or credentials are emitted.
The combined CREATE failure on 2026-11-05 lacked these witnesses, so its signin
screen and unrelated license-client errors do not establish a login cause.

Run the focused privacy/reset checks from a clean checkout with
`direnv exec . python3 -B -m unittest discover -s tests/platform -p test_documenso_functional.py`.
The existing Linux `platform/documenso-functional-check TFC_SOURCE` remains the
native rerun entrypoint; this diagnostic change alone is not native acceptance.
