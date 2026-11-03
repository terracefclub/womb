# Optional Terrace identity host

`platform/identity.nix` exports `nixosModules.identity`, disabled by default. It
runs the platform-pinned Keycloak 26.8.1 on PostgreSQL outside K3s. This is a
single-host substrate, with no deployed host, DNS record, enrollment workflow or
production realm artifact. It creates no bootstrap administrator or user account.

The application host may import this module alongside `single-node.nix`. Do not
put application identity on the recovery relay: relay access must survive an
application-host outage. The module is not imported into any install profile.

## Runtime inputs

An eventual host supplies protected files through its secret manager:

```nix
{
  imports = [ womb.nixosModules.identity ];
  terrace.identity = {
    enable = true;
    hostname = "identity.example.org";
    realmFile = "/run/secrets/terrace-realm.json";
    databasePasswordFile = "/run/secrets/identity-db-password";
    certificateFile = "/run/secrets/identity-fullchain.pem";
    certificateKeyFile = "/run/secrets/identity-key.pem";
  };
}
```

These are string paths, not Nix paths or secret values. Nothing reads their
contents during evaluation. Systemd passes the realm and database password as
credentials; Nginx also receives protected copies of the TLS files through
systemd credentials. Deliver them before service startup and
keep the source directory root-only. The database password initializes a new
local database role; replacing its file does **not** rotate an existing role's
password. Rotation needs a separately reviewed database/consumer transaction.
TLS file replacement requires restarting Nginx to refresh its credential snapshot.

## Realm contract boundary

The source of application clients, audiences and role separation remains the
[TFC identity contract](https://gitlab.com/terrace/tech/tfc-website/-/blob/dev/identity/tools/realm-config.ts)
and its tests. Supply a reviewed, user-free `terrace-realm.json` derived from
that contract with exact production HTTPS callbacks and distinct protected client
credentials. Record its source revision and checksum in protected operations
records. This module deliberately contains no copy of application role mappings.
Producing and reviewing that production artifact is still a release gate; the
TFC local bootstrap uses HTTP origins, local settings and synthetic identities
and must never be applied to production or exported as this input.

Before Keycloak starts, a bounded runtime check rejects seeded users, public
registration, non-TLS realm policy, HTTP/wildcard callbacks, wildcard web origins,
password/implicit/service-account grants, and external identity providers or
LDAP/user-storage federation. Inputs must be at most 1 MiB. This is a hosting
guard, not a replacement for the TFC client/role contract tests or an untrusted
JSON sandbox. Federation, extra realms and SMTP/provider integration are outside
this increment. Administrators must use individual accounts with a reviewed
approval/recovery procedure before any production activation.

Keycloak's [startup import](https://www.keycloak.org/server/importExport)
creates a missing realm and **skips an existing realm**. Changing the input is
not realm reconciliation, client-secret rotation, account provisioning or a
database restore. Do not delete a realm to force an update. Future changes need
an authenticated, tested reconciliation procedure and a recoverable database
copy. The VM check verifies existing realm state is preserved on restart.

## Network, resource and health bounds

Nginx terminates TLS on TCP/443 and forwards only the Terrace realm, public
resources and discovery paths. The root, administration and master-realm paths
return 404, as does dynamic client registration under the Terrace realm.
Management/metrics are never proxied. The proxy overwrites forwarded
host, scheme, port and client address. Keycloak's backend (8088), management
interface (9000) and PostgreSQL bind to loopback. Port 80 is not opened by this
module. Local root remains trusted; no remote admin route is provided yet.

The Keycloak service has a 768 MiB Java heap, 1.5 GiB memory high threshold,
2 GiB hard limit, two CPU quota, 256 tasks, 32 HTTP workers, ten database
connections, five-minute startup bound and three-starts-per-ten-minutes limit.
After process notification, startup also waits for database-aware readiness
within that deadline; notification alone can precede the initial schema migration.
The shared PostgreSQL service defaults to a 1 GiB hard limit, two CPU quota,
40 connections and 64 MiB shared buffers; review these defaults when sharing it
with other applications. These are caps, not measured production capacity.

`terrace-identity-health.timer` checks local database-aware readiness every minute
with a ten-second service deadline. Failure leaves a visible failed unit; it
does not reboot/restart the host or repair the database. Run
`systemctl start terrace-identity-health.service` for an immediate check.
Systemd startup output and proxy access logs are suppressed because they may
contain credentials or authorization codes. Keycloak warning logs remain under
the mode0700, ephemeral `/run/keycloak/` directory, with mode0600 files and size
rotation at 5 MiB with two backup files (no time-based suffix). This bounds
retained files to three, with a size threshold rather than a filesystem quota;
the last log record can cross the threshold. Never print them without a
fail-closed redactor. Log shipping/alert delivery is not configured here.

Safe health evidence is available without opening private logs:

```sh
systemctl show keycloak.service postgresql.service terrace-identity-health.service \
  -p ActiveState -p SubState -p Result -p ExecMainStatus
journalctl -u terrace-identity-health.service --since '-15 min' --no-pager
```

The health unit emits only a fixed failure message; it never logs response
bodies, credentials or member data. Check runtime input delivery, database and
service state before retrying the health check. Treat a running Keycloak process
and database-aware readiness as separate checks. Inspect the protected Keycloak
log only through an approved redaction path; one is not supplied here.

## Data owner, retention and recovery gate

Accountable operational role: Terrace technical operations. A named primary and
backup identity administrator and an approved retention policy are required
before real accounts are introduced; neither is established by this module.

The `keycloak` PostgreSQL database contains accounts, credentials, realm signing
keys, clients and sessions. It remains on disk if the module is disabled. Keep
the protected realm artifact and consumer credentials recoverable separately;
the import file is not a complete database backup. TLS keys follow the chosen
certificate/secret-manager recovery process. Runtime logs are excluded from
backup and disappear on service/runtime cleanup; no retention claim is made.

**Backup exclusion in this increment:** enabling identity does not enable or
schedule `terrace.backup`. The existing [backup module](backups.md) can include
`terrace.backup.postgresql.databases = [ "keycloak" ];`, but its earlier synthetic
table test is not a Keycloak restore drill. Before deployment, approve owners
and retention, configure encrypted off-site backup and protected credential
recovery, restore the real Keycloak schema into an isolated instance, and verify
issuer/signing keys, accounts, client secrets and revocation behavior. Keep the
production gate closed until that evidence exists. No data is wiped by this
module or its validation.

## Reproduce the VM check

On the configured x86_64 Linux builder, from the Womb root, with the shared
build lock supplied explicitly:

```sh
BUILD_LOCK="$HOME/.falcon-heavy.lock" platform/check identity
```

Use the detached invocation in [the platform guide](../platform/README.md) for
long runs. Never build on a developer's Mac. The two-VM test uses only a
throwaway certificate, a generated runtime database password and a user-free
hosting fixture. It checks HTTPS discovery, forbidden public paths, spoofed
proxy headers, loopback isolation even with test firewall exceptions, PostgreSQL
and realm persistence, create-only imports, rejected artifacts, health failures
and recovery, and effective service limits. It does not test the TFC production
artifact, a real login, mail/provider calls, cloud networking, enrollment or an
off-site restore.

Verified 2026-11-02 (America/Los_Angeles): the two-VM check passed, with a
145.47-second test script, on the Linux builder under the shared build lock.
The platform lock pins Nixpkgs revision
`31ecbaca1bdefa45dffc409f0250bd8039ea0010`. Result:
`/nix/store/brl901fwca1kv90qdl2s9958cwc5m0a6-vm-test-run-terrace-identity`.
This is hosting-fixture evidence, not production authentication or recovery evidence.
