# Optional HTTPS origin gateway

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
HTTP80 is closed; HTTP-to-HTTPS redirection and certificate renewal are separate
activation work.
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

This first origin gateway uses `proxy_cache off` and sends `private, no-store`
for **every** web/CMS response, including public pages/assets, error responses,
authentication and image optimization. It does not cache a published response
after withdrawal. Cookies and authorization headers pass to the application;
the proxy never treats them as a signal to grant access. nginx access logs are
disabled; errors use the existing privileged, bounded, volatile app journal.
nginx is capped at256MiB and128tasks. App/identity/database limits remain those
of their native modules.

## Disposable actual-bundle proof

The check consumes the already accepted3902e16 web/CMS archives and **only three
hash-pinned, secret-free source files** from that TFC revision: the realm
generator, shared client/role contract and pure production-input validator.
Their hashes are checked before Nix-store copying; the source checkout and its
private state are never copied. The test does not duplicate client callbacks,
audiences or role mappings in Womb.

On the x86-64 Linux builder, under the shared heavy-job queue:

```sh
BUILD_LOCK=/path/to/shared-heavy.lock platform/website-gateway-check \
  /protected/releases/terrace-web-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz \
  /protected/releases/terrace-cms-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz \
  /source/tfc-at-3902e16c8c681638282e5983869d53efa8bb424c
```

Two guests contain the actual immutable app pair and native PostgreSQL/Keycloak.
The server generates fresh synthetic secrets and a user-free production realm
at runtime. Test-only temporary master bootstrap credentials seed the shared
contract's synthetic users through the private loopback admin API, then remove
that administrator. This fixture setup is absent from module defaults and
production enrollment. No registration, external mail or real account is used.
The disposable public test CA is trusted explicitly; TLS checks never use an
insecure override. All real deployment certificates/keys must be delivered
separately.

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

The remote guest separately checks certificate/hostname trust, spoofed forwarding,
SNI/Host confusion, blocked identity administration, direct backend denial even
with the VM firewall pierced, and cache headers on private/error routes. The
test explicitly stops its application/identity/database/gateway services before
guest teardown. Every test-owned job/guest/process must be verified stopped
before the task is idle; protected retained archives and evidence can remain.
The check uses the shared [bounded supervisor](../platform/README.md#check-supervision)
for lock ownership, whole-run/build deadlines and cancellation cleanup.

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
