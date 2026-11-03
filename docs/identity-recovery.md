# Identity recovery VM drill

`platform/identity-restore-test.nix` adds an isolated recovery check for the
actual Keycloak 26.8.1/PostgreSQL 18 schema. It composes the existing identity,
backup and restore modules without changing their production defaults. No
existing server, account, provider repository or public DNS is involved.

Run from the Womb root on the configured x86_64 Linux builder, under its shared
heavy-job lock:

```sh
BUILD_LOCK="$HOME/.falcon-heavy.lock" platform/check identity-restore
```

Use the [platform guide](../platform/README.md) for detached execution. Do not
run Nix/VM builds on a developer's Mac.

The check boots two disposable guests:

1. The source runs the native identity module with a user-free Terrace realm.
   Keycloak itself creates the schema and signing keys; no dummy identity table
   substitutes for those records. The test captures issuer, public JWKS, realm ID,
   protected input-file hashes and hashes of critical real database rows.
2. Existing `terrace-backup` dumps the real database and PostgreSQL roles, plus
   a separate protected identity-input directory (realm JSON, database password
   and throwaway TLS material), into an encrypted restic repository over SFTP
   on the recovery guest. A complete repository check reads 100% of data.
3. Existing `terrace-backup-restore` restores a selected snapshot into an isolated,
   socket-only database on the recovery guest and reconciles every table's
   backup-time row count and every protected input-file hash.
4. Test-only Keycloak configuration uses the native NixOS-supported JDBC Unix
   socket plugins and the restore OS user to access that private socket. It
   starts against the recovered database and inputs, preserving issuer, realm ID
   and the complete public signing-key set. No native/source database is replaced.
5. The source Keycloak process keeps its original PID, serves its original issuer
   and JWKS, and retains its critical rows and protected inputs. The recovered
   process is stopped, reconciliation is rerun, and the isolated database is
   cleanly stopped.

Repository/SFTP credentials are outside the backed-up input directory. The test
delivers its throwaway repository password privately over pinned SSH stdin with
a restrictive, one-file forced command. No secret bytes pass through driver
values, command arguments or host output artifacts. This fixture is not a
production secret-manager or escrow mechanism. Unprivileged users cannot read
the recovered password/input boundary. Public JWKS and hashes may be compared;
private signing keys, passwords and dump contents are never printed.
The guest's captured restore/reverification output is created with umask077 and
checked to be mode0600; it is not exported as a CI artifact.

The driver reports backup, full repository-check, isolated restore and application
verification durations separately. Application verification includes recovery
Keycloak startup and source-continuity checks. These are small, local two-guest
measurements, not a production RTO or off-site recovery guarantee.

Verified 2026-11-02 (America/Los_Angeles), under the shared Linux build lock:
101 actual Keycloak tables; the two-guest test script passed in 106.15 seconds.

| Phase | Seconds |
| --- | --- |
| Database/input backup over SFTP, including retention | 7.78 |
| Full repository data check | 5.70 |
| Isolated database/file restore and reconciliation | 4.64 |
| Recovered application startup and continuity verification | 19.78 |

Result: `/nix/store/hfdi0gx5qnysp82fdmmdb900k0ql4kkq-vm-test-run-terrace-identity-recovery`.
The platform lock pins Nixpkgs revision
`31ecbaca1bdefa45dffc409f0250bd8039ea0010`. The test does not destroy the source.

Still open: a named primary/backup identity owner and retention approval,
production secret delivery/escrow, a reviewed production realm, populated account,
session and client-secret recovery, actual login/token/introspection/revocation
tests after restore, representative data/capacity, TLS/DNS cutover, and an
off-site provider/network drill. Real users, mail and payment integrations remain
outside the check. See [backup ownership and exclusions](backups.md) and the
[identity activation gates](identity-host.md).
