# PostgreSQL bootstrap identity during isolated restore

PostgreSQL18 distinguishes bootstrap role OID10 from other superusers when it
restores explicit membership grantors. Initializing the temporary cluster as a
new SQL `terrace-restore` superuser could reject the unchanged dump's
`GRANTED BY postgres` membership. The isolated cluster now initializes with the
source OID10 role's original name. The OS process/file owner is still
`terrace-restore`, and the isolated listener remains its private Unix socket.
No native HBA, production app privilege or role flag is widened.

At backup staging, `bootstrap-role.json` records schema1, sourceOID10/name,
server version, its `quote_ident`-derived canonical `CREATE ROLE` line, its
LOGIN/SUPERUSER prerequisites and SHA256 of the **exact** private `globals.sql`.
Both capture and restore reject unsupported or inconsistent metadata. The
bootstrap name must be nonempty UTF8 at most63bytes, without Unicode control
characters or embedded double quotes. Quoted spaces are supported. Inputs are bounded stable
private regular-file reads with no symlink/FIFO following; outputs are private,
exclusive and published only after the complete bytes have been written.

The helper requires exactly one complete canonical bootstrap `CREATE ROLE`
line and removes only that statement's bytes/newline into private
`globals-prepared.sql`. That role already exists atOID10 from `initdb`; every
other byte is retained, including password hashes, attributes, `GRANTED BY`,
ADMIN/INHERIT/SET options and role settings. Import retains `ON_ERROR_STOP=1`.
Missing/duplicate statements, wrong schema/OID/hash, trailing SQL or malformed
identifiers fail before a database starts. Real subsequent SQL errors still
fail restoration; they are not ignored or reclassified as harmless.

This is the existing trusted-source-superuser boundary of
[pg_dumpall](https://www.postgresql.org/docs/18/app-pg-dumpall.html), whose dump
executes source superusers' SQL. The helper is a consistency guard, not a SQL
sanitizer for untrusted dumps. The
[PostgreSQL18 grantor check](https://github.com/postgres/postgres/blob/REL_18_0/src/backend/commands/user.c)
has the special bootstrap exemption. No grantor rewrite, added ADMIN edge,
extra SQL superuser or role-password change is used to avoid that check.

This narrow contract requires the source bootstrap role to retain LOGIN and
SUPERUSER. Both are checked at capture and restore and preserved unchanged.
Old snapshots without this metadata, with an embedded-quote bootstrap name,
or with a NOLOGIN/non-superuser bootstrap role (including the former adopted
`terrace-restore` model), **fail closed**.
Keep those snapshots and their matching pinned tools; they require a separately
reviewed compatibility path. Do not edit dumps or temporarily alter source role
flags to pass this gate. No declared production profile has been deployed by
this change, and no real snapshot is rewritten.

Measured2026-11-05 on pinned PostgreSQL18.6 (`postgres --version` and
`initdb --version` from the proof's actual recorded executable): `initdb -U` with an embedded double
quote fails inside its own initial privilege setup, before our restore helper.
The [18.6 initdb implementation](https://github.com/postgres/postgres/blob/REL_18_6/src/bin/initdb/initdb.c)
formats ACL text with the username but escapes single quotes/backslashes rather
than identifier double quotes. The helper therefore rejects this bootstrap
edge at both capture and restore; it does not add a temporary SQL superuser or
rename workaround. **Other** dumped roles containing quotes are preserved
unchanged and included in the native graph comparison.

The source metadata is retained as `<dest>/bootstrap-role.json` for later
isolated verify connections. `--stop` still stops the exact data directory
under its OS owner and does not need a new SQL connection. File/SQLite-only
restores do not require PostgreSQL metadata. The existing backup, website and
identity restore fixtures use `-U postgres` for their source's normal bootstrap
admin while retaining the OS owner and each application's own SQL identity.

Run the focused guards on any supported development host:

```sh
direnv exec . python tests/platform/test_bootstrap_identity.py -v
```

The native proof runs **only on the Linux builder**, as an unprivileged Nix
sandbox derivation with private socket-only PostgreSQL18 clusters. It has no
VM, live host DB, network listener, provider connection or real credentials.
It resolves the helper from both **actual module-produced** backup/restore wrapper
assignments without sourcing/evaluating those root scripts, requires a readable
regular Nix-store file byte-identical to the reviewed helper, and executes its
query/capture/prepare/name commands through those resolved paths. It then
reproduces the old42501 grantor failure; compares all role flags/password
hashes, memberships/grantors/options and role settings using the final
rehearsal's three semantic fingerprints; restores a real custom DB dump; covers
a quoted-space bootstrap and an ordinary quoted role; rejects an embedded-quote
bootstrap and genuine SQL errors before a later
sentinel statement, missing/hash-mismatched/duplicate metadata and NOLOGIN
source identity; and stops/reaps every owned cluster. Only fixed phase labels
and a token-free result are returned. SQL/diagnostics stay in private build
scratch, never stdout or the Nix result.

From a clean committed checkout on the builder:

```sh
direnv exec . env BUILD_LOCK=/home/tristan/.falcon-heavy.lock \
  BUILD_LOCK_TIMEOUT=300 CHECK_TIMEOUT=300 CHECK_BUILD_TIMEOUT=300 \
  CHECK_STOP_GRACE=30 \
  ./platform/run-check --impure --file platform/backup-bootstrap-check.nix
```

Supervise the detached command with an independent outer900-second deadline
and60-second kill grace; record actual host build/process identities and verify
them absent afterward. Namespace-local proof PIDs are not host PID evidence.
The full combined originals-off recovery remains a separate source-matched
gate after this prerequisite; local guards alone do not establish it.


Packaging correction measured2026-11-05: the first full quiescent-backup run
failed before metadata capture because the wrapper used `toString` for the
helper source. Python received a builder-host pathname that did not exist in
the guest. Explicit Nix path interpolation now retains that same helper in the
backup **and** restore closures. The earlier tiny PostgreSQL proof imported
the helper directly and did not cover this boundary; its database results do
not establish packaged-shell execution. The expanded sandbox proof above must
pass before another full rehearsal. No metadata, role, SQL or error policy is
changed by this packaging correction.


For the packaging correction alone, the same check has an explicit no-database
mode (same shared lock/300-second bounds and outer supervision):

```sh
direnv exec . env BUILD_LOCK=/home/tristan/.falcon-heavy.lock \
  BUILD_LOCK_TIMEOUT=300 CHECK_TIMEOUT=300 CHECK_BUILD_TIMEOUT=300 \
  CHECK_STOP_GRACE=30 \
  ./platform/run-check --impure --file platform/backup-bootstrap-check.nix \
  --arg packagedOnly true
```

It builds the actual module-installed backup and restore wrappers, resolves their
helper assignments as data (never sources the root scripts), verifies the exact
readable store file, and executes query/capture/prepare/name with synthetic inputs.
It starts no PostgreSQL process and does not replace the prior six-cluster SQL
proof or the pending root/restic/native-adoption rehearsal.
