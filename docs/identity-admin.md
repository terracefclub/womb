# Keycloak production administration

How an operator administers the production Keycloak on the `app` host: the first
administrator, more administrators, site editor and administrator accounts, role
grants and revocations, listing for an access review, client-secret rotation,
reconciling a changed realm definition, and break-glass recovery. Written
2026-11-19. Closes runbook gaps G-15, G-39 and G-40 in the VM proof only: no
production host, real account or real secret exists because of this document.

Everything below runs as root on the app host through one tool,
`terrace-identity-admin`, over Keycloak's loopback backend (`127.0.0.1:8088`).
The public proxy keeps answering 404 for the root, `/admin/` and every
master-realm path (see [identity host](identity-host.md)); no administration
console is published, not even over an SSH tunnel (Keycloak would need an admin
hostname, `hostname-admin`, which the profile refuses).

## How it works

| Piece | What it is |
| --- | --- |
| Tool | `platform/production/terrace-identity-admin` (Python standard library). The app role installs it only as the wrapper `/run/wrappers/bin/terrace-identity-admin`, owner root, mode `0500`, no setuid or capabilities, and not on the general `PATH`. The wrapper adds no privilege; it is there so that the only entry point is root-only. The program also refuses any caller that is not root. `--help` on every command. |
| Configuration | `/etc/terrace/identity-admin.json` (root `0400`): the loopback addresses, the pinned release's realm definition (the realm template with sops placeholders, never secret values), the hosting policy, and each confidential client's delivered secret file and consumer. |
| Bootstrap unit | `terrace-identity-bootstrap-admin.service`: Keycloak's own recovery command, `kc.sh bootstrap-admin user --optimized --password:env`, run with exactly the upstream Keycloak unit's preparation while Keycloak is stopped (upstream requires every node stopped). Never started at boot; only the tool starts it. The password reaches the unit as a systemd credential from a root-only tmpfs file; the unit's script exports it as an environment variable to the short-lived `kc.sh` process, because `kc.sh` reads it only from the environment (never from an argument). The unit conflicts with Keycloak, so starting it stops Keycloak, and its `ExecStopPost`, run by systemd as root whatever happened to the command or to the tool, deletes the credential files, records this run's result for the tool (the tool waits for that record, not for a `systemctl` client a hangup could kill) and starts Keycloak again (clearing Keycloak's three-starts-in-ten-minutes limit first). |
| Administrators | Named accounts in the `master` realm with its `admin` role. They are used only through the tool: each command asks for `--admin NAME` and that account's password (from the terminal, or one line of stdin with `--password-stdin`; never an argument or environment variable). The access token stays in the tool's memory, and the tool ends its Keycloak session when it exits. Any local process (the CMS or website service accounts included) can reach the loopback backend and attempt a master password grant, so `first-admin` enables brute-force protection on the master realm: five wrong passwords lock the account temporarily (growing waits up to 15 minutes; no permanent lockout). This is a deliberate trade-off: it stops password guessing, but anyone who can run code on the host can also lock every named administrator out on purpose, again and again (administrator names are not secret). That is a denial of administration, not a takeover; the remedy is break-glass, which needs root, creates a recovery account whose name ends in a random suffix (so it cannot be guessed and locked in advance) and clears the lockouts with `admin enable`; then find and remove whatever is running on the host. |
| One at a time | Every command that can change Keycloak (dry runs included) takes an exclusive lock, `/run/terrace-identity-admin/lock` (an empty file); a second one is refused while the first runs. State-changing commands ignore a hangup, so a dropped `ssh -t` session lets the command finish (its output may be lost: check the journal). A tool killed before it started the bootstrap unit can leave that unit's credential files under `/run/terrace-identity-admin/kc-bootstrap/` (root-only, tmpfs); the next state-changing command removes them. |
| Site accounts | Editors and administrators of the website live in the `terrace` realm with the identity contract's roles (`editorial-editor`, `editorial-admin`; the member roles `current-member` and `alumni` come from the website's approval flow). They sign in through the proxy as before. |
| Audit | Every state change, refusal and dry run writes one journal line, identifier `terrace-identity-admin`: `operator=<login account> admin=<Keycloak administrator> action=<what> target=<realm/kind/name> result=<done, dry-run, refused-...>`. The operator is the SSH login account (its login uid), else sudo's caller. Values never appear. |
| Handoff files | Generated initial or recovery passwords go to `/run/terrace-identity-admin/handoff/<realm>-<user>.password` (root `0400`, tmpfs, gone at reboot), written before Keycloak receives the password, so a crash never leaves a password nobody has. Nothing is printed. |
| Forgotten temporary accounts, unhardened master realm | `terrace-identity-temporary-admin-check.timer` (armed by adoption, every 15 minutes) fails `terrace-identity-temporary-admin-check.service`, visible in `systemctl --failed`, and logs at `crit` while a `terrace-bootstrap` or `terrace-recovery-*` account is older than `terrace.production.identityAdmin.temporaryMaxAgeMinutes` (default 240), or while a named administrator exists and the master realm's brute-force protection or admin events are off. Run it by hand with `sudo terrace-identity-admin check-temporary`. |

Decisions, with the alternatives considered:

- **Command-line tool on the host, not the web console.** The console needs a
  public or tunnelled admin hostname, a master-realm login page and a browser;
  the tool needs only SSH, which the recovery path needs anyway. The cost: there
  is no second factor for administrators (below).
- **Hand-written reconciliation, not keycloak-config-cli.** nixpkgs carries
  keycloak-config-cli 6.5.1, but it documents no plan or dry-run mode that shows
  the changes before applying them, it purges resources it manages by default,
  it takes the administrator password as an argument or environment variable,
  and it would push the template's placeholder client secrets unless they were
  stripped first. The reconciler here compares only declared fields, prints a
  plan with a hash, applies exactly that plan, never deletes, and refuses what it
  does not understand.
- **Escrowed bootstrap password for the first administrator, a host-generated
  one for break-glass.** The first administrator follows the escrow design
  ([secrets](secrets.md)); break-glass must work when the escrow key is not at
  hand, so it depends on nothing but root on the host.

## Before you start

- The host is adopted and Keycloak is ready (runbook 5.4):
  `sudo systemctl show keycloak.service -p ActiveState` says `active`.
- You can reach the host: `ssh operator@<app address>` with your operator key.
  For an interactive password prompt use `ssh -t`.
- Status at any time (read-only, no login):

  ```sh
  sudo terrace-identity-admin status
  ```

  Worked if: `Keycloak ready: yes`, and the lists of named and temporary
  administrators and waiting handoff files are what you expect.

## 1. First administrator (once per deployment)

A new deployment's master realm has **no** administrator: the profile refuses a
bootstrap administrator in its configuration. The first one comes from the
escrowed `identity/bootstrap-admin-password`, delivered once at run time.

1. On the operator machine, hand the escrowed value to the host over the pinned
   SSH channel (it travels on stdin and lands in `/run/terrace-identity-admin/`,
   root `0400`, tmpfs):

   ```sh
   terrace-secrets apply app identity/bootstrap-admin-password --address <app address>
   ```

   Worked if: `Delivered identity/bootstrap-admin-password to /run/terrace-identity-admin/bootstrap-admin-password`.
2. On the host, look first, then do it:

   ```sh
   sudo terrace-identity-admin first-admin --username <you> --email <you@...> --first-name <First> --last-name <Last> --dry-run
   sudo terrace-identity-admin first-admin --username <you> --email <you@...> --first-name <First> --last-name <Last>
   ```

   It stops Keycloak (about a minute in which nobody can sign in anywhere),
   creates the temporary administrator `terrace-bootstrap` with the escrowed
   password, starts Keycloak, signs in with it, creates your named
   administrator with the `admin` role and an initial password, **proves** the
   named administrator signs in with that password and holds `admin`, enables
   master-realm brute-force protection and admin events (without
   representations), deletes `terrace-bootstrap`, checks that it can no longer
   sign in and removes the delivered file. Add `--set-password` to type the
   initial password instead of receiving a generated one.

   Worked if: `First administrator <you> ready (...); it signs in and holds the
   admin role.` and `terrace-bootstrap deleted and refused`; `status` shows only
   your account and `Delivered bootstrap password: absent`. Running it again
   says `Already done`.

   If it was interrupted (killed, or the connection dropped), check `status`
   and `systemctl is-active keycloak` (Keycloak is started again by systemd
   even if the tool died while it was stopped), then run the **same command**
   again while the delivered file is still there: it resumes with the
   bootstrap account it left, repairs a half-created named administrator (new
   initial password in the handoff file, `admin` role) and deletes the
   bootstrap account only after the named administrator is proven. It repairs
   only an account with no password yet or still carrying the tool's initial
   password; an account with a password of its own (for example after you ran
   `password` in between) is never reset, enabled or granted anything: the
   command refuses and keeps the bootstrap account, and if that account is your
   working administrator you finish with `break-glass end --admin <you>`, which
   removes the bootstrap account. The master realm's hardening (brute-force
   protection and admin events) is applied by `first-admin` and, idempotently,
   at the administrator login of every state-changing command, so that remedy
   also completes it (`Master realm hardening was incomplete; turned on: ...`).
   `status` shows whether it is on; `first-admin` says `WARNING` on its
   `Already done` path if it is not; and the timer-driven check fails while it
   is off and a named administrator exists. If the
   delivered file is gone (a reboot clears tmpfs), deliver it again with step 1.
   If the escrow key is not at hand, use break-glass (section 7) instead; `break-glass
   end` later removes the stranded `terrace-bootstrap` account.
3. Replace the initial password (every new administrator must, before any other
   command is accepted):

   ```sh
   sudo cat /run/terrace-identity-admin/handoff/master-<you>.password   # only if it was generated; on your own terminal
   ssh -t operator@<app address> sudo terrace-identity-admin password --admin <you>
   ```

   Worked if: `Password of <you> changed; the old one is refused.` The handoff
   file is removed.
4. The bootstrap value is single use: rotate it in the store (nothing to push;
   it is never delivered):

   ```sh
   terrace-secrets rotate app identity/bootstrap-admin-password --manual
   ```

## 2. More administrators

```sh
sudo terrace-identity-admin admin create --admin <you> --username <them> --email <...> --first-name <...> --last-name <...> [--dry-run]
sudo terrace-identity-admin admin disable --admin <you> --username <them> [--dry-run]
sudo terrace-identity-admin admin enable  --admin <you> --username <them> [--dry-run]
```

Hand the generated initial password over privately and delete the handoff file;
they run `password` before anything else. `disable` ends the account's sessions
and refuses to disable the last enabled named administrator. `enable` also
clears a brute-force lockout (`... enabled and its brute-force lockout
cleared.`). Worked if: `list --realm master` shows the account with `admin`.

## 3. Site editors and administrators

```sh
sudo terrace-identity-admin user create --admin <you> --username <name> --email <...> --first-name <...> --last-name <...> --role editorial-editor [--dry-run]
sudo terrace-identity-admin grant  --admin <you> --username <name> --role editorial-admin [--dry-run]
sudo terrace-identity-admin revoke --admin <you> --username <name> --role editorial-admin [--dry-run] [--keep-sessions]
sudo terrace-identity-admin user disable --admin <you> --username <name> [--dry-run]
```

- `user create` makes the account with a verified address and a **temporary**
  password: Keycloak forces a new password at the first sign-in (through the
  normal CMS **Sign in with Terrace** page). The generated password is in
  `/run/terrace-identity-admin/handoff/terrace-<name>.password`; hand it over
  privately and delete the file. Only editorial roles: member accounts follow
  the website's approval and provisioning (runbook 5.9 onwards, G-16).
- `grant` and `revoke` take only the contract's editorial roles
  (`editorial-editor`, `editorial-admin`, read from the pinned release's
  definition). Member roles are refused: members are granted and offboarded
  through the website's approval flow. Any other role is refused.
- `revoke` ends the user's sessions so the change is immediate (without
  `--keep-sessions`); `disable` disables the account and ends its sessions.
- The last enabled holder of `editorial-admin` cannot lose the role or be
  disabled: grant it to someone else first. The count covers direct role
  holders only, so the tool refuses these changes outright when the realm has
  groups or a composite role containing the role (none exist in the contract
  today); make such a change by hand after reviewing who inherits the role.
  The same applies to the master realm's `admin` role.
- Every command is idempotent: a repeated grant says `already has`, a repeated
  create says `already exists` and changes neither password nor roles.

Worked if: the person signs in at the CMS admin and sees only what the role
allows; `list` shows the role.

## 4. Access review (runbook 9.11)

```sh
sudo terrace-identity-admin list --admin <you> [--realm terrace|master|all] [--show-email] [--json]
```

Shown per account: username, first and last name, enabled, direct realm roles.
The email address only with `--show-email`. Never credentials, attributes,
sessions or tokens. Also the realm roles and the clients with their redirect
URIs. Check every account against your named list; disable or revoke the rest.

## 5. Rotate a client secret

Keycloak keeps its own copy of each confidential client's secret, so a rotation
has three steps. The store comes first, so the escrow always holds the value in
service.

```sh
terrace-secrets rotate app identity/client/terrace-cms --manual     # operator machine
terrace-secrets push app --address <app address>                     # consumers restart on the new value
sudo terrace-identity-admin client-secret sync --admin <you> --client terrace-cms [--dry-run]   # on the host
```

`sync` reads the delivered file, sets Keycloak's secret to it (no operator
handles the plaintext), checks that
Keycloak accepts the new value and refuses the previous one (client
authentication at the introspection endpoint), and restarts any consumer that is
not yet running with the delivered value. Clients and consumers:
`terrace-web` (website), `terrace-community`, `terrace-cms`, `terrace-editorial`
(CMS). Between `push` and `sync`, sign-ins through that client fail: do it in a
quiet moment, back to back. Worked if: `Keycloak now uses the delivered secret;
the previous one is refused.` and `Consumers on the delivered value: ...`; a
second `sync` says `already has the delivered secret`.

## 6. Reconcile the realm after a new release (G-39)

Keycloak's startup import creates a missing realm and skips an existing one. When
a re-pinned website release changes the identity contract, reconcile:

```sh
sudo terrace-identity-admin realm plan  --admin <you>
sudo terrace-identity-admin realm apply --admin <you> --plan-sha256 <hash from the plan>
```

The definition is the pinned release's realm template on the host (same build as
the rest of the system). The plan compares only what the contract declares:

| Declared | Reconciliation |
| --- | --- |
| Realm settings (`displayName`, registration, reset password, email login, duplicate emails, username edit, brute force, `sslRequired`, token and session lifetimes, refresh token reuse, `verifyEmail`) | updated to the declared value |
| Realm roles | missing ones created; undeclared ones **kept** and listed |
| User profile (declarative) | declared attributes and groups added or updated, others' keys kept; a definition that **drops** an attribute is refused |
| Clients | missing ones created only if their secret is delivered; declared flags, redirect URIs, web origins (exact lists) and declared attributes updated; changing `publicClient`, `protocol`, `clientAuthenticatorType` or `serviceAccountsEnabled`, or disabling a client, is refused; undeclared clients **kept** |
| Protocol mappers, default and optional client scopes | declared ones added or updated; undeclared ones **kept** and listed |
| Scope mappings (client to realm roles) | exact: missing added, undeclared removed |
| Anything else (a new top-level key, client roles, other components) | refused as not understood |

It never deletes a user, credential, session, client, role or the realm. It never
changes an existing client's secret (section 5); a client it creates gets the
value already delivered to the host for it, and the plan shows `(secret: the
delivered value)`, never the value. It runs the hosting policy
(`platform/identity/realm-policy.jq`) on the definition first; that policy bounds
registration, TLS, redirect URIs, web origins, direct and implicit grants and
service accounts, but **not** protocol mappers, scope mappings,
`fullScopeAllowed`, `consentRequired` or free-form client attributes: review
those in the plan, which prints each mapper's type and configuration, each new
client's full definition, each added scope and each changed user-profile
attribute's declared content. The plan records exactly what will be applied
(including that content) and its sha256 covers it; `apply` recomputes the plan
from the pinned definition and refuses unless the hash equals `--plan-sha256`,
then applies the recorded changes and re-plans, failing unless converged. Exit
status: 0 (planned, applied or converged), 1 (refused), 3 (the plan contains a
refused item; nothing can be applied). `realm plan --definition FILE` plans
against another root-owned definition for review; `realm apply` never takes
another definition, only the pinned release's.

Worked if: `Applied N change(s); the realm now matches the definition.` and a
second `plan` says `Converged`.

## 7. Break-glass: no administrator, no SSO, no proxy (G-40)

When no named administrator can sign in (passwords lost, accounts disabled) and
single sign-on or the proxy may be down:

```sh
ssh -t operator@<app address>                    # host access by SSH key only
sudo terrace-identity-admin break-glass begin --dry-run
sudo terrace-identity-admin break-glass begin
sudo cat /run/terrace-identity-admin/handoff/master-terrace-recovery-<time>-<random>.password
sudo terrace-identity-admin admin enable --admin terrace-recovery-<time>-<random> --username <you>   # or admin create
sudo terrace-identity-admin break-glass end --admin <you>
```

`begin` needs no Keycloak login: it writes a random password generated on the
host to the handoff file, stops Keycloak (**nobody can sign in anywhere for
about a minute**), creates `terrace-recovery-<UTC time>-<random>` with that password,
starts Keycloak and checks the account signs in. If `begin` is killed or the
connection drops, Keycloak is started again by systemd and the account, if
created, is usable from its handoff file. If the bootstrap unit cannot start
at all (a failed dependency or condition), the tool starts Keycloak again itself,
says whether it is ready, and removes the unused handoff file. `begin` refuses while a **working**
temporary administrator exists (one whose handoff file signs in): use that
one. Temporary accounts nobody can use (a stranded `terrace-bootstrap`, or a
recovery account whose handoff was lost to a reboot) do not block it; it
creates a new one and names them. Repair named access with the recovery
account (`admin enable`, which also clears a lockout, `admin create`,
`password`), then `end` **as a named administrator** (refused as the recovery
account): it deletes every temporary administrator, checks a recovery password
still on hand no longer signs in, removes the handoff files and confirms none is
left in the database. Both steps are journal lines, and the temporary-account
check fails loudly while one is left (see above).

What it depends on:

- an operator SSH key listed in the host's `operatorKeys` and `sudo` (the
  `operator` account);
- the host itself and its PostgreSQL `keycloak` database (Keycloak need not be
  running; the proxy is not used).

It does **not** depend on Terrace single sign-on, the proxy, any named
administrator, the escrow key or the bootstrap password.

If the operator SSH keys are lost: there is no console login (no passwords, no
emergency shell). Build a replacement host with a new operator key and restore
it from the backup ([production profile](production-profile.md#host-replacement-rehearsed)),
which needs the escrow key for its secrets; then break-glass on the replacement.
If the escrow key is lost as well and the host is gone, its secrets are gone
([secrets](secrets.md#escrow-key-lost-or-leaked)): the database restore still
brings back the realm and accounts, and every secret must be regenerated.

## Audit

```sh
sudo journalctl -t terrace-identity-admin --since today
```

Admin events are Keycloak's record of what each administrator changed. With
details off (the setting here) an event records the operation, the resource
path (which includes user IDs), the acting administrator, the client address
and the time. It carries no submitted or returned values, so no passwords,
attributes or other personal data.

- **Master realm:** enabled by `first-admin` (details off).
- **`terrace` realm:** managed by `realm plan` and `realm apply` when the
  release's realm artifact declares `adminEventsEnabled` and
  `adminEventsDetailsEnabled`; the artifact is the source of truth, so drift in
  either direction is planned and applied like any other realm setting (an
  ordinary realm update, the same path as the other scalar settings). If the
  artifact does not declare them, reconciliation neither changes nor refuses
  anything about them. The website's realm contract declares them
  (`adminEventsEnabled: true`, `adminEventsDetailsEnabled: false`), but the
  website release pinned today predates that, so the `terrace` realm's admin
  events stay as they are until Womb re-pins to a release that carries them.
  Until then the journal lines are the record of changes to site accounts.
- **Both values must be booleans.** `adminEventsDetailsEnabled: true` is
  refused by hosting, in the plan (`REFUSED`, exit 3, nothing applied) and by
  the realm hosting policy that also guards the first import: representations
  in the audit log can contain personal data, and that decision belongs to the
  hosting side, not to an artifact.

## Not provided, and why

- **Second factor for administrators.** Enrolling a one-time-password device
  needs a browser on a master-realm login page, which the proxy denies and no
  tunnel provides. Using an administrator needs root on the host (SSH key and
  `sudo`) and the password; the password grant itself is reachable by any local
  process, limited by the master realm's brute-force protection. The tool
  forces a change of every initial password before use. A second factor would
  need the admin console behind an SSH tunnel with `hostname-admin`, a decision
  for the owner.
- **Member accounts.** Created by the website's approval flow; a production
  provisioning path is G-16.
- **A web admin console.** See above.

## Verification

- Unit tests (CI, check-supervision job): `python3 platform/production/test_terrace_identity_admin.py -v`
  and `python3 platform/production/test_terrace_secrets.py -v`.
- Evaluation check: `nix eval --raw --impure --option builders "" --file infra/nixos/production-eval-check.nix`
  rejects an administration route through the proxy (any host spelling of
  Keycloak's ports 8088 and 9000, in a location, a `proxy_pass` in free-form
  configuration or an upstream), the backend or management interface off
  loopback, a bootstrap credential or admin hostname in the configuration, the
  tool runnable by non-root, setuid, owned by another account or on the general
  `PATH`, and a bootstrap unit started at boot or without its Keycloak restart.
- VM proof, on the Linux builder under the shared lock:
  `BUILD_LOCK=/path/to/shared-heavy.lock platform/production-identity-check WEB_ARCHIVE CMS_ARCHIVE TFC_SOURCE`
  (`platform/production/identity-admin-test.nix`). It boots the same app role
  with a real Keycloak whose master realm has no administrator and proves, in
  order: the tool is root-only and the proxy denies administration; the escrowed
  password handed over by `terrace-secrets apply`; `first-admin` dry run, then
  killed with SIGKILL while Keycloak is stopped (Keycloak comes back by itself,
  the credential files are gone), then a half-created named administrator
  injected, an administrator with a password of its own refused repair, then
  `break-glass end` as that administrator removing the bootstrap account and
  turning the master realm's hardening on (seen in `status`), a second kill,
  then a half-created named administrator injected and the next run repairing and proving it before deleting the
  bootstrap account, which is then refused, and an idempotent re-run; the
  forced change of an initial password; a second administrator, a brute-force
  lockout and `admin enable` clearing it; site accounts with dry runs and
  refusals; `list` over SSH and sudo with only the documented fields; grant and
  revoke with the member-role and last-administrator refusals and the lock
  against a concurrent command; a first sign-in through the HTTPS gateway with
  Keycloak's forced password change; the `terrace-cms` secret rotated end to end
  (store, push, sign-in failing in the window, `sync`, old secret refused with
  an OAuth refusal, the editor signing in again); a converged plan, a changed
  definition with all nine change kinds shown, applied from the pinned path with
  users, credentials and sessions unchanged, a second run as a no-op, three
  refused plans, two hosting-policy refusals and a return to the pinned
  definition; revoke and disable ending sessions and `--keep-sessions`
  keeping them; break-glass with the proxy stopped and every administrator
  disabled, `begin` killed in the window and still usable, refused while that
  account works, a lost handoff not blocking a second `begin` that survives a
  SIGHUP, the temporary-account check failing with a `crit` line, repair and
  `end`; a bootstrap unit that never starts (Keycloak restarted by the tool, a
  truthful message, no leftover handoff); every named administrator locked out
  by an unprivileged local process and break-glass still working; the journal
  lines; and no password, client secret or captured access
  token in the journal and auditd's log (a kernel exec audit trace of every
  process's arguments, plain and hex-encoded, with no record lost while the
  proof ran), the process list, world-readable files, the
  root disk, the tool's output or the operator machine (which has the same exec
  trace), each scan first shown to find a planted control.

Not proven: a real host, real accounts or passwords, a real operator's
terminal (the proof feeds passwords on stdin and sends signals to a transient
unit, not to an SSH session), Keycloak upgrades, a definition change from an
actual website release (the proof edits the pinned template), rotation of the
other three client secrets (same code path as `terrace-cms`), the tool's own
access tokens (the scans look for one captured by the test, of the same kind),
the exec audit trace on a production host (test-only configuration), the
lockout used as a denial of service over a long period (the proof shows one
round and its remedy), a bootstrap unit failing for reasons other than an unmet
condition (a failed dependency or start limit takes the same code path), a kill
at every other point of `first-admin` (the proof kills it in the Keycloak window
and simulates a death right after creating the user), and a restore of the
Keycloak database after any of these commands.
