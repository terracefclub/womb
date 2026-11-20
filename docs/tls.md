# TLS certificates for the production hosts

How the production roles ([production profile](production-profile.md)) get,
renew and check their certificates. Written 2026-11-19. Module
`platform/production/tls.nix`; VM proof `platform/production-tls-check`.
Nothing here has touched a public certificate authority, real DNS or a provider:
no account, certificate or DNS record exists because of this document.

## Who terminates TLS

Each production host terminates TLS for its own public names, so each host runs
its own ACME client:

| Host | Names on its certificate | Why there |
| --- | --- | --- |
| `app` | website, CMS and identity hostnames (one certificate, the website name first) | The gateway nginx on this host is the only HTTPS listener for all three. Keycloak, the CMS and the website listen on loopback behind it. |
| `relay` | the relay hostname | The relay is an independent host with its own public address and direct DNS (no CDN), so recovery access survives an app-host outage. |

Neither host proxies the other's names, and every public name points straight
at the host that serves it. That is exactly what the HTTP-01 challenge needs:
the CA connects to `http://<name>/.well-known/acme-challenge/<token>` and finds
the host that asked for the certificate.

**Challenge method: HTTP-01**, by the NixOS ACME module (lego) writing the token
to `/var/lib/acme/acme-challenge`, served by nginx on port 80.

- **DNS-01 (alternative, not built).** It needs an API credential for the DNS
  provider, which the owner has not chosen (registrar access is G-31). It would
  allow wildcard names and issuance before the name points at the host. If it is
  chosen later: one more sops secret (the provider token, delivered through
  `credentialFiles`), `dnsProvider` instead of `webroot`, port 80 closed again
  on the app host, and a VM proof against pebble's DNS mode.
- **TLS-ALPN-01** is not used: nginx owns port 443.
- **A CDN in front of the app names** (G-32, not chosen): HTTP-01 keeps working
  only if the CDN passes `/.well-known/acme-challenge/` over plain HTTP to the
  origin. Otherwise use DNS-01, or the manual mode with an origin certificate.

## Two modes

`terrace.production.tls.mode` selects how the role gets its certificate:

- **`manual`** (the default; the path proven by PR 50): the operator delivers
  `tls/certificate` and `tls/key` with `terrace-secrets` ([secrets](secrets.md)),
  nginx loads them as systemd credentials. Port 80 stays closed on the app host
  (the relay keeps its module's redirect). Renewal is the operator's job.
- **`acme`**: the host obtains and renews its certificate itself. The store
  holds no `tls/*` for that host (host feature `acme-tls`).

| Option | Default | Meaning |
| --- | --- | --- |
| `tls.mode` | `manual` | `manual` or `acme` |
| `tls.acme.directory` | required with `acme` | the CA's ACME directory URL |
| `tls.acme.acceptTerms` | `false` | must be `true`: the owner accepted the CA's subscriber agreement |
| `tls.acme.contact` | required with `acme` | the address registered with the CA account |
| `tls.acme.otherAuthority` | `false` | owner decision: a CA other than Let's Encrypt (see Owner decisions) |
| `tls.acme.retrySeconds` | `3600` | wait before the order unit retries a failed order itself (production: at least 900) |
| `tls.acme.retries` | `3` | attempts a day before a failed order stays a failed unit (1 to 5) |
| `tls.minDays` | `14` | the expiry check fails below this many days left (1 to 60) |
| `tls.trustAnchorFile` | `null` | rehearsal only (the VM proof's CA); production refuses it |

A production host does not evaluate with: a directory other than Let's
Encrypt's production directory (`https://acme-v02.api.letsencrypt.org/directory`)
unless `otherAuthority` is set, and then any directory whose host or path has a
token `staging`, `stg`, `test`, `testing`, `sandbox`, `pebble`, `localhost`,
`boulder` or `dev`, a port, plain `http://` or a reserved host name;
`acceptTerms` false; a missing or reserved contact address; a missing
directory; extra lego flags, an environment file, credential files, a CSR or a
challenge other than HTTP-01; anything on port 80 besides the challenge and the
redirect; a public name served over plain HTTP; a certificate loaded from
anywhere but the ACME directory, not refreshed on reload, or readable by the
nginx user; an order unit without its bounded retry (or retrying more often
than every 15 minutes or more than 5 times a day); ACME units or timers that
start before adoption; a trust anchor override; or test-only ACME client trust
(`LEGO_CA_CERTIFICATES`, `LEGO_TLS_SKIP_VERIFY`).
`infra/nixos/production-eval-check.nix` has a rejected variant for each. The
module also refuses to evaluate when nixpkgs moves lego off major version 5
(see the account workaround below).

## What runs, and when

Nothing of ACME runs before `terrace-production adopt` (the [boot
gate](production-profile.md#boot-gate-adopt-before-writers-start)): a blank or
replacement host makes no request to the CA until its operator adopts it.

1. Adoption starts nginx. nginx first pulls in `acme-<name>.service`, which
   writes a **placeholder**: a certificate from a local self-signed CA
   (`/var/lib/acme/.minica`), so nginx always has something to start with.
2. `acme-order-renew-<name>.service` runs after nginx is up, so the challenge
   location answers. It registers the account (first run), orders the
   certificate by HTTP-01, installs it in `/var/lib/acme/<name>/` and then runs
   `systemctl try-reload-or-restart nginx.service` and waits for it.
3. nginx never reads the key file: systemd (as root) hands nginx the
   certificate and key as credentials (`LoadCredential`, the same mechanism as
   the manual mode) and, with `RefreshOnReload=credentials`, copies the new pair
   in on every `systemctl reload`. The reload is a SIGHUP: same master process,
   listeners kept; new connections get the new certificate, requests in
   progress finish on the old workers, idle keep-alive connections are closed
   (clients reconnect). A failed reload fails the order unit and keeps the
   `renewed` marker, so the unit's retry reloads again (the VM proof makes a
   reload fail once and sees the retry serve the new chain); until then the
   expiry check still sees the previous, valid certificate.
4. `acme-renew-<name>.timer` (daily, with up to 24 h of random delay) runs the
   same unit again. lego renews when a third of the lifetime is left (half for
   certificates shorter than ten days), so a 90-day certificate renews at about
   30 days left. A run that is not due changes nothing.
5. At every boot of an adopted host the same unit runs once (not due: no
   change). While no certificate has been issued yet, every boot runs a full
   order.

**A failed order retries on its own, then stays a failed unit.** The unit
retries `retries` times a day, `retrySeconds` apart (default 3 attempts, an hour
apart: well inside Let's Encrypt's 5 failed validations per name per hour).
While it waits it shows as `activating (auto-restart)`; once the attempts are
used up it is `failed` (`start-limit-hit`) and `systemctl --failed` lists it
until the next timer run (after the day's start limit) or `terrace-tls renew`
(which resets it).

The start budget (`retries` starts a day) counts every start of the order
unit: boots, configuration switches, the renewal timer, manual starts and the
unit's own retries. A further start that day is refused with
`start-limit-hit` even when nothing else is wrong, until the day is over,
`systemctl reset-failed` or `terrace-tls renew`; `terrace-tls status` says so
in words. The certificate already installed stays in place and nginx
keeps serving it. On the first order there is no previous certificate: the
placeholder stays, and the expiry check fails as well.

**A first order that cannot reach the CA does not leave the host stuck.**
lego 5 (5.5.1) writes the account file (`"registration": null`) and key before
registering; when that registration fails, its later runs try to recover the
never-registered account and fail for good. Before every run, the order unit
removes exactly such an account (its `account.json` and key) so the next run
registers afresh; an account file of any other shape is left alone and logged.
The VM proof reproduces the failure on the relay. Re-check this step and re-run
the VM proof on every lego or nixpkgs update.

**The placeholder is never presented as valid**: browsers and the expiry check
reject it (self-signed, local CA). It is replaced by the first successful
order and regenerated only until then. A live switch from the manual mode
stages the served certificate first, so it never appears there (below).

## Port 80

With ACME, port 80 (both roles) is exactly two nginx server blocks:

- `terrace-http`, for the role's own names: `/.well-known/acme-challenge/`
  served from the challenge directory as `text/plain`, everything else a 301 to
  `https://<same name><same path>`. No proxying, no application content, access
  log off.
- `terrace-http-reject`, the default for any other `Host`: 421.

The app role's HTTPS server blocks are unchanged (443 only; unknown names and a
mismatched SNI get 421).

## Expiry check (both modes)

`terrace-tls-check.service` connects to the host's own port 443 once per public
name, with that name as SNI, and fails when the served chain does not verify
against the system trust store ("no verified TLS session"), does not name the
host ("does not name"), or has fewer than `tls.minDays` days left. It is the
safety net for everything above, including a reload that did not take. It prints one line per name (expiry date and issuer,
no key material). `terrace-tls-check.timer` runs it an hour after adoption or
boot and daily after that. A failure shows in `systemctl --failed`. Delivering
that to a person is G-23 (alerting), not built.

## Files and permissions

| Path | Owner, mode | Content |
| --- | --- | --- |
| `/var/lib/acme/<name>/` | `acme:acme` 0750 | `fullchain.pem`, `key.pem`, `chain.pem`, `full.pem` 0640; `cert.pem` link |
| `/var/lib/acme/.lego/<name>/<hash>/` | `acme:acme`, files 0640 | lego's own copy |
| `/var/lib/acme/.lego/accounts/` | `acme`, directories writable only by `acme`; key files 0600 | the ACME account key |
| `/var/lib/acme/acme-challenge/` | `acme` 0755 | challenge tokens while an order runs |
| `/run/credentials/nginx.service/` | systemd, readable by the nginx service | the copy nginx serves (refreshed on reload) |

Keys are generated on the host by lego; they never enter the Nix store, the
repository or the secrets store. The nginx user and the service accounts (CMS,
website, Keycloak) cannot read the files in `/var/lib/acme`. That protects
the files on disk only: nginx still holds its credential copy and the key in
memory, so a compromised nginx can read the key, exactly as in the manual
mode. Unlike the manual mode (key on ramfs, from the encrypted store), the ACME
key sits unencrypted on the persistent disk, like any ACME client's.

## Backup: deliberately excluded

`/var/lib/acme` is **not** in the backup set (the backup module lists its paths
explicitly and none is there; the VM proof checks the snapshot). A restored or
replacement host registers a new account and orders a new certificate as soon
as it is adopted.

- Why: the backup would otherwise carry a TLS private key and an account key
  off-site, for state that is cheap to recreate in about a minute once DNS
  points at the host.
- Rate limits (Let's Encrypt's published limits at the time of writing; check
  the CA's current page): 5 certificates per week for the exact same set of
  names, 50 per week per registered domain, 10 new accounts per IP address per
  3 hours, 5 failed validations per name per account per hour. One replacement
  costs one account and one certificate, so up to about four replacements or
  forced renewals of the same host in one week are safe. Beyond that, use the
  manual mode for the rest of the week.
- Failed validations: a replacement whose DNS does not yet point at it fails
  and retries on its own `retries` times a day, `retrySeconds` apart (default 3,
  an hour apart), then stays a failed unit: well inside the hourly limit.

## Owner decisions and inputs (before enabling ACME)

1. **Choose the CA and accept its terms.** Let's Encrypt's production directory
   is `https://acme-v02.api.letsencrypt.org/directory`; its subscriber
   agreement is linked from that directory. Setting `acceptTerms = true` records
   that the owner accepted it. This is an owner decision.
2. **A contact address** the club reads (account and policy notices). Not a
   personal address that may lapse.
3. **Optional: another CA.** `tls.acme.otherAuthority = true` lets production
   use a CA other than Let's Encrypt (any public `https://` directory that does
   not look like a staging or test endpoint). Choosing it, and accepting that
   CA's agreement, is an owner decision.
4. **The hostnames**, the same as `app.webHostname`, `cmsHostname`,
   `identityHostname` and `relay.hostname` (runbook 1.19).
5. **DNS**: A (and AAAA if the host has IPv6) records for every name pointing
   at the host that serves it, published **before** the host is adopted (or
   before switching it to ACME). Port 80 and 443 reachable from the internet.

## Enable ACME on a host

Before anything: every name resolves to the host that serves it, and port 80
answers there. Check from outside the host, for each name:

```sh
dig +short A www.example.org        # the host's address, and AAAA if it has IPv6
curl -sI http://www.example.org/    # once the host runs: 301 to https (manual mode on the app: no answer on 80)
```

For a new host (the mode is part of its first deployment):

```nix
# in the private host module (docs/production-profile.md#building-a-host)
terrace.production.tls = {
  mode = "acme";
  acme = {
    directory = "https://acme-v02.api.letsencrypt.org/directory";
    acceptTerms = true; # owner decision, see above
    contact = "REPLACE_WITH_CLUB_ADDRESS";
  };
};
```

```sh
terrace-secrets host create app --role app --feature acme-tls   # instead of the plain create
terrace-secrets generate app        # "Supply with `set`: none."
terrace-secrets check app
```

Then install, push and adopt as in the [cold start](production-profile.md#cold-start-of-a-new-deployment).
Adoption starts the first order. Check it within a few minutes (below). A
replacement host is created with the same `--feature acme-tls` and `--replace`.

### Switching a live manual host to ACME

**The switch restarts nginx (a few seconds without HTTPS).** Without the first
step below it would also serve the self-signed placeholder until the first
order succeeds. Run the DNS and port 80 checks above first.

1. On the host: `sudo terrace-tls prepare-acme`. It copies the certificate the
   host serves now into `/var/lib/acme/<name>/` as the first ACME certificate.
2. Deploy the ACME configuration (`tls.mode = "acme"`). nginx restarts on the
   same certificate; the first order replaces it as soon as it succeeds. If it
   fails, the manual certificate stays served and the unit retries (above).
3. When `sudo terrace-tls status` no longer says `NOT YET ISSUED BY ACME`:
   `terrace-secrets host tls app acme --yes` (it refuses without `--yes`, because
   it drops the manual certificate and key from the store; keep your own copy if
   you may fall back), then push. Until then the host accepts the old `tls/*` in
   its ciphertext and `terrace-secrets verify` prints a warning naming them.

## Check status

```sh
sudo terrace-tls status       # mode, names, issuer, expiry, PLACEHOLDER or NOT YET ISSUED BY ACME,
                              # order unit state and retries, next renewal and check runs (root only)
sudo terrace-tls check        # the expiry check, printed to the terminal
systemctl --failed            # acme-order-renew-<name> or terrace-tls-check if anything failed
journalctl -u acme-order-renew-<name>.service
```

## Force a renewal

```sh
sudo terrace-tls renew                 # runs the order unit; renews only if due
sudo terrace-tls renew --force --yes   # orders a new certificate now (counts against the rate limits above)
```

Both reset the unit's retry budget first. `--force` removes lego's stored
copy so the unit orders afresh; the served certificate stays until the new one
is installed, and nginx is reloaded, not restarted. If the order fails, the
command says so, the previous certificate is still served and the unit retries
on its own.

## Fall back to the manual path

When ACME cannot work (DNS not ready, CA unreachable, rate limit, CDN in the
way), with a certificate for all the role's names obtained elsewhere:

```sh
terrace-secrets host tls app manual
terrace-secrets set app tls/certificate < fullchain.pem
terrace-secrets set app tls/key < key.pem
terrace-secrets check app
terrace-secrets push app          # the ACME host accepts and ignores the pair
```

Then set `terrace.production.tls.mode = "manual"` (or remove the block) and
deploy. nginx restarts onto the delivered pair (a few seconds without HTTPS); on
the app host port 80 closes. `/var/lib/acme` stays on disk, unused, until the
host goes back to ACME: it still holds the ACME key and account key (and, after
an earlier `prepare-acme`, a copy of a manual key), unencrypted. Remove it with
`sudo rm -rf /var/lib/acme` once the host stays on the manual mode. Going back
to ACME later: if it still holds a valid ACME certificate, deploying ACME serves
that one and `prepare-acme` refuses to overwrite it unless given
`--overwrite`. The VM proof does this switch and back on the relay.

## Verification

- Evaluation check: `infra/nixos/production-eval-check.nix` (production app and
  relay with ACME, the ACME rehearsal instance, and the rejected variants
  above), same command as in the [production profile](production-profile.md#verification).
- VM proof, on the Linux builder (the wrapper takes the shared lock itself):
  `BUILD_LOCK=/path/to/shared-heavy.lock platform/production-tls-check WEB_ARCHIVE CMS_ARCHIVE TFC_SOURCE`
- Tool unit tests: `python3 platform/production/test_terrace_secrets.py -v`.

The VM proof boots the same app and relay role modules (`instance =
"rehearsal"`, `.test` names) next to a local ACME server (`pebble` from
nixpkgs) on the test network. Pebble's HTTPS certificate is made at boot inside
its guest; its roots are fetched from its management API at test time. The
rehearsal harness (`platform/production/tls-rehearsal.nix`, refused in
production) lets the ACME client trust pebble (`LEGO_CA_CERTIFICATES`) and the
expiry check use pebble's roots. No guest has a route off the test network.

It proves: nothing ACME and no port 80 before adoption; an ACME host needs and
accepts no `tls/*`; placeholder, then issuance by HTTP-01 for the three names
on adoption, served and verified, nginx reloaded not restarted; port 80 only
redirects (also POSTs) and serves challenge files, 421 for other hosts; owners
and modes, and that the nginx user cannot read the key (ACME directory and
lego copy) while nginx serves it as a credential; the expiry check passing on a
fresh certificate and failing (a failed unit), with its own message, on a
3-day and on a wrong-name certificate; a not-due renewal changes nothing; a
forced renewal needs `--yes`, keeps the same nginx process and answers 80 new
HTTPS connections spread across the reload; with the CA unreachable the
previous certificate keeps being served and the unit recovers by its own retry
once the CA is back; a reboot keeping the certificate and the account key; the
relay's first boot with the CA unreachable (placeholder served, failing check,
a failed unit after its retries), then issuance with a fresh account; backup
snapshots of both roles without ACME state, and an app replacement restored
from one that registers a new account and gets a new certificate; a live switch
of the relay to the manual mode (expiry check failing on a 3-day delivered
certificate, passing on a good one) and back to ACME on a host without ACME
state, with clients seeing only the manual and then the ACME certificate,
never the placeholder, and `prepare-acme` refusing to overwrite a valid ACME
certificate; a renewal whose nginx reload fails is a failed run that keeps the
previous certificate served, and the unit's own retry reloads and serves the
new one.

Evidence: see the pull request and the owner runbook (step 6.16) for the
recorded runs, commit and receipts.

## Not proven

- Any public CA: no Let's Encrypt (or other) account, order, terms or rate
  limit has been exercised; the rate-limit figures above are the CA's published
  numbers, not measured.
- Real DNS, CAA records, a real public address, port 80 reachability through a
  provider firewall, validation over IPv6 from a public CA, a CDN in front of
  the names.
- Clock skew against a real CA (the local CA does not backdate certificates;
  the proof tolerates a guest a moment behind it and counts those retries).
- A timer actually firing (the proof starts every unit itself; the daily timer,
  its jitter and the start-limit window are configuration).
- A change of the hostname list on a running host (a new order is needed; the
  expiry check should then fail for the new name until it succeeds), a
  replacement of the relay, and the live switch on the app role (proven on the
  relay only).
- lego or nixpkgs updates: the account workaround depends on lego 5's account
  file; re-run the proof after any update.
- Alert delivery for a failing check or order (G-23).
- DNS-01 and wildcard certificates (not built).
