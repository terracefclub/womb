# Optional HTTPS origin gateway

> **Note, 2026-11-23.** Re-pinned to website `dev` `eab4189` (website PRs #19 to #22). The gateway's runtime realm now comes from a producer that declares admin events on with details off, which the hosting policy accepts. No gateway rule changed.

> **Note, 2026-11-19.** Re-pinned to website `dev` `680ad56` (website PRs #14 to #18: lifecycle linking, packaged `bin/sweep`, release-proof fixes and no-script form status). The gateway now supplies the lifecycle origins and the client-address pairing below, and the check exercises the member lifecycle end to end.

`platform/website-gateway.nix` adds an opt-in nginx gateway to the accepted native
website/CMS bundles. Actual host profiles remain disabled. Importing the module
alone starts nothing. This adds no DNS record, cloud resource, ACME account,
external identity provider or CDN configuration.

Set distinct canonical `webHostname` and `cmsHostname`, plus external absolute
`certificateFile` and `certificateKeyFile` covering both names. The native
website module must be enabled explicitly. TLS inputs are systemd credentials;
their contents never enter Nix evaluation or the store. When native identity is
co-enabled, its hostname must differ from both application names.

Only HTTPS443 is opened. The immutable app launchers keep binding loopback.
HTTP80 is closed in this module; the production app role adds, with ACME, the
challenge, the HTTP-to-HTTPS redirect and renewal ([TLS](tls.md)).
Unknown hosts and mismatched application SNI/Host receive421. The gateway fixes
upstream Host, forwarded host/protocol/port and client address, and removes
untrusted Forwarded/prefix fields. It preserves Origin for application CSRF
checks. Identity retains its existing restricted public realm/resources routes;
its admin, master realm, registration and management paths remain denied.

The runtime guard requires exact configured HTTPS origins, CMS public activation
and, when co-enabled, the exact native identity issuer. TFC's public CMS policy
still validates complete identity settings, distinct strong secrets, omitted
bootstrap keys and external media storage. Follow the [native runtime contract](website-host.md)
and deliver protected environment files externally; never use local bootstrap
credentials or put input contents in a Nix expression.

The gateway overwrites `X-Forwarded-For` (and `X-Real-IP`) with the connecting
address; it never appends. It therefore sets the website's `ADDRESS_HEADER=X-Forwarded-For`
and `XFF_DEPTH=1` (an evaluation assertion rejects any other pairing, and the web
guard rejects `LIFECYCLE_DIRECT_CLIENTS=1`), plus `LIFECYCLE_PUBLIC_ORIGIN=https://<webHostname>`
for invitation links and `CMS_PUBLIC_ORIGIN=https://<cmsHostname>` for imported body
images. See [native hosting](website-host.md#member-lifecycle-settings-outbox-and-sweep).

This first origin gateway uses `proxy_cache off` and sends `private, no-store`
for **every** web/CMS response, including public pages/assets, error responses,
authentication and image optimization. It does not cache a published response
after withdrawal. Cookies and authorization headers pass to the application;
the proxy never treats them as a signal to grant access. nginx access logs are
disabled; errors use the existing privileged, bounded, volatile app journal.
nginx is capped at256MiB and128tasks. App/identity/database limits remain those
of their native modules.

## Disposable actual-bundle proof

The check consumes the pinned `eab4189` web/CMS archives and **only three
hash-pinned, secret-free source files** from that TFC revision: the realm
generator, shared client/role contract and pure production-input validator.
Their hashes are checked before Nix-store copying; the source checkout and its
private state are never copied. The test does not duplicate client callbacks,
audiences or role mappings in Womb.

On the x86-64 Linux builder, under the shared heavy-job queue:

```sh
BUILD_LOCK=/path/to/shared-heavy.lock platform/website-gateway-check \
  /protected/releases/terrace-web-eab41891f6d68ea000f86148d086daf737f1216e-linux-x64-gnu.tar.gz \
  /protected/releases/terrace-cms-eab41891f6d68ea000f86148d086daf737f1216e-linux-x64-gnu.tar.gz \
  /source/tfc-at-eab41891f6d68ea000f86148d086daf737f1216e
```

Two guests contain the actual immutable app pair and native PostgreSQL/Keycloak.
The server generates fresh synthetic secrets and a user-free production realm
at runtime. Test-only temporary master bootstrap credentials seed the shared
contract's synthetic users through the private loopback admin API, then remove
that administrator. This fixture setup is absent from module defaults and
production enrollment. No registration, external mail or real account is used.
The disposable public test CA is trusted explicitly and never through a
cached copy such as a `security.pki` system trust bundle: clients reference its
store path (`curl --cacert`, `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`), and the
browser's NSS database imports it at test run time. An early guard checks that
the served chain verifies against that anchor (`platform/test-trust.nix`). TLS
checks never use an insecure override. All real deployment certificates/keys
must be delivered separately.

The guest HTTP protocol client follows real authorization-code/PKCE login forms
through HTTPS nginx to Keycloak and back to the applications. It checks approved
editor/member access, wrong-role/unapproved denial, tampered state, Origin CSRF,
Secure/HttpOnly/SameSite cookies, logout, publication and withdrawal, and an
authenticated private media/optimizer request followed by anonymous requests to
the exact same URLs. It bounds requests, response bytes and redirect hops and
prints fixed stage labels and counts only. Credentials, provider responses,
tokens, cookies and callback queries stay in private guest memory/files.

A real Chromium regression also signs in as an unapproved account, clicks the
native switch-account form and then signs in as an approved member. It verifies
that the browser supplies the correct POST Origin. The gateway preserves the
application's Referrer-Policy, including the denied-account page's strict-origin
policy; replacing it with no-referrer can suppress that Origin. Browser fixture
credentials cross only a mode0700 host temporary directory outside test output
and a root-only guest file, then are removed. JavaScript is disabled for this
native-form regression; it is not a dashboard hydration test.

A lifecycle stage then runs through the real gateway with synthetic data only:
`/news/` and an imported-style post (published, with WordPress provenance) render,
a body image on the public CMS origin is kept and a foreign one dropped, and the
old URL answers 301 to the new path. Six `/apply/` submissions from the gateway
guest, posted as a browser with script does (SvelteKit's enhanced form protocol),
each forging a different `X-Forwarded-For` and `X-Real-IP`, give five accepts and
a 429 action result (five applications stored). The same exhausted bucket gives the
same 429 on the private loopback port, and a no-script post into it gets HTTP 429
and the limit page both through the gateway and on the loopback port, so the
gateway passes the application's status through. From the second guest, six
no-script posts (the first forging the gateway guest's exhausted address) give
HTTP 200 and the received page five times, then 429: a separate address has its
own bucket and a forged header cannot move a client into another one. An editor approval writes the
invitation mirror (`0600`, CMS account) with a `https://web.terrace.invalid/join/#…`
link; redemption works once (a repeat is refused, as a no-script post with HTTP 404); the applicant's first sign-in links the application
by verified email; an administrator offboards and erases the member, which
withdraws and scrubs the application and closes member access. The mirror still
names the applicant until the real sweep timer fires (a backdated persistent
stamp makes it catch up), after which it is redacted. The packaged `bin/sweep`, run as the CMS
account with the public `cms.env` but no `LIFECYCLE_OUTBOX_DIR`, must exit non-zero
with its fixed failure line, while the same invocation with the outbox succeeds. The fixture enrollment also
checks the imported realm: `verifyEmail`, no duplicate emails or username edits,
email editable by administrators only, and the `email` default scope on
`terrace-web` but not `terrace-cms`. Synthetic users are created through the
private loopback admin API with `<username>@terrace.invalid` verified addresses;
the website's `provision-member.ts` (loopback realm tool reading the application
through `PG*`) is not used by Womb.

The remote guest separately checks certificate/hostname trust, spoofed forwarding,
SNI/Host confusion, blocked identity administration, direct backend denial even
with the VM firewall pierced, and cache headers on private/error routes. The
test explicitly stops its application/identity/database/gateway services before
guest teardown. Every test-owned job/guest/process must be verified stopped
before the task is idle; protected retained archives and evidence can remain.
The check uses the shared [bounded supervisor](../platform/README.md#check-supervision)
for lock ownership, whole-run/build deadlines and cancellation cleanup.

Verified 2026-11-23 on the shared Linux builder with the `eab4189` bundles
(05:52 to 05:55 Pacific, fresh result): 198 HTTPS protocol assertions and the same
lifecycle stage counts as below (18, 83, 49, 40, 15, 24, 28 and 63) passed.

Verified 2026-11-19 on the shared Linux builder at Womb `48ab695` with the
`680ad56` bundles (06:10 to 06:13 Pacific, prebuilt closure): 198 HTTPS protocol
assertions, the Chromium account switch, and lifecycle stages of 18 (content),
83 (application limit), 49 (second address), 40 (approval), 15 (redemption),
24 (sign-in), 28 (link) and 63 (erasure) bounded assertions, the sweep-timer
redaction and the packaged-sweep negative case all passed.

## Remaining activation and cost gates

This proof has no additional billed resource: it uses the existing shared Linux
builder for one bounded supervised job. It is not a provider quote. The entire
eventual deployment must still remain strictly below US$80/month including tax,
backups and existing-server overlap. Confirm stock/account prices and measure
the combined workload with reserve before any actual profile is enabled.

No real CDN is exercised. Edge cache-key behavior, cookie/authorization/query
bypass, edge rules, purge/withdrawal timing, stale/error serving, origin isolation,
TLS termination and external access require a separate actual-provider proof
before caching is enabled. Globally reachable HTTPS is not a privacy policy:
member/editorial authorization continues to be enforced by the applications.
The protocol proof is not browser hydration/UI coverage or an external sign-in
availability check. Live-write backup consistency, whole-host/off-site recovery,
production enrollment, load, public DNS, certificate renewal and cloud deployment
remain gates documented in [native hosting](website-host.md).

## Shared disposable identity fixture

`platform/test-identity-contract.nix { pkgs; tfcSource; }` verifies the three
accepted TFC producer-file hashes before copying them into its small test
package. It exports `fixture.ts` with `createIdentityFixture(options)`. No
private checkout state enters the store. Running the default fixture inside a
disposable guest preserves the accepted four-client realm and existing paths.

A companion service may pass `extraSecretNames = ["documenso"]` and
`extraClientFactories = [secrets => documensoClient(origin, secrets.documenso)]`
using its reviewed pure client factory. In TypeScript these are array-valued
object properties. The helper returns private `extraSecrets`, `users`, `issuer`,
`directory` and `inputs`; never print or serialize these into a build output.
Names/factories are bounded to eight, identities must be unique, and extension
clients require enabled OIDC, distinct generated confidential secrets, bounded
HTTPS callbacks/exact origins, code flow, S256 PKCE and scoped
roles with password/implicit/service-account grants disabled. This test-only
composition does not reconcile clients in an existing realm.

The initial realm remains user-free. The separate private fixture enrollment
uses the accepted five synthetic role identities, with verified addresses
`<username>@terrace.invalid`; it deletes its temporary master administrator.
Use `local-member@terrace.invalid` as an approved account and
`local-unapproved@terrace.invalid` as unknown. App-specific approval or denial
is the app's own contract, not an assumed role mapping. Reserve
`recovery@terrace.invalid` outside all fixture IdP enrollment. Production
account enrollment and ongoing client reconciliation remain separate gates.

Pure regression tests: `direnv exec . bun test tests/platform/fixture-clients.test.ts`.
The existing native gateway check exercises the default fixture against actual
Keycloak and both accepted application bundles.
