# Credential rotation

Written 2026-11-24 against Womb `trunk` at `f68cb59`. This is owner runbook gap
G-37 (step 9.9): which credentials the platform uses, how each one is changed, and
which ones cannot be changed safely today.

**Status of this document.** It was written by reading the tools and modules at
trunk; nothing in it was executed. Where the repository has a rotation path it is
given with the commands taken from the tool that implements it. Where the code has
no rotation path (a password that is applied only at first start, a key that is
generated once), the step says so and is written "not determined from the
repository" or "needs a code change"; no SQL or command is invented. No real
credential exists yet, so none of this has been used on a production host.
"Proven" means a named VM check asserts it.

Evidence levels, as in [restore promotion](restore-promotion.md): **VM rehearsal
(synthetic data)**; **also on temporary Hetzner hosts once (synthetic data; ...)**;
**not exercised anywhere**. No rotation has run on a provider, so the second level
appears nowhere below.

Terms, each defined once. **Store**: the private, encrypted per-host secrets
directory ([secrets](secrets.md)). **Escrow identity**: the offline age key that can
decrypt every host's store file. **Push**: `terrace-secrets push`, which delivers the
store file to the host over SSH pinned to its host key. **Consumer**: the unit or
file that reads a credential. **Quiesced**: writers stopped, as in the
[backups](backups.md) and [restore promotion](restore-promotion.md) procedures.

## 1. Inventory

Sources are file and line at `f68cb59` (READ for every row; nothing was run).
"In backup" means the item is in a `terrace-backup` set on the production profile,
read from `platform/production/app.nix`, `relay.nix` and the modules' backup
blocks; the host ciphertext is **not** in any backup, and the store has its own
private remote ([secrets](secrets.md#where-things-live)).

### 1.1 Secrets in the host store (`platform/production/inventory.json`)

Delivered by `terrace-secrets push` to root-only files on ramfs under
`/run/secrets` and rendered into templates (`platform/production/app.nix`,
`sops.templates`). Generator `hex32` means `terrace-secrets generate` makes 32
random bytes as 64 hex digits (`generate_value`, `terrace-secrets:403`).

| Credential (store name, inventory line) | Consumers | In backup | What breaks if it changes |
| --- | --- | --- | --- |
| `website/payload-secret` (inv. 4) | `cms.env` `PAYLOAD_SECRET` read by `terrace-cms.service` | no (store only) | CMS restarts; every editorial session ends (inventory `consequence`) |
| `website/lifecycle-intake` (inv. 5) | `cms.env` and `web.env` `LIFECYCLE_INTAKE_SECRET`, one value for both | no | CMS and website restart together; one apply or redeem in flight fails once |
| `identity/client/terrace-web` (inv. 6) | realm client `terrace-web`; `web.env` `MEMBER_AUTH_CLIENT_SECRET` | the Keycloak copy is in the `keycloak` database dump | Keycloak keeps its own copy: sign-ins through the client fail between push and `client-secret sync` |
| `identity/client/terrace-community` (inv. 7) | realm client; `cms.env` `MEMBER_AUTH_INTROSPECTION_SECRET` | Keycloak copy in the dump | as above |
| `identity/client/terrace-cms` (inv. 8) | realm client; `cms.env` `CMS_AUTH_CLIENT_SECRET` | Keycloak copy in the dump | as above |
| `identity/client/terrace-editorial` (inv. 9) | realm client; `cms.env` `CMS_AUTH_INTROSPECTION_SECRET` | Keycloak copy in the dump | as above |
| `identity/database-password` (inv. 10) | `services.keycloak.database.passwordFile` (`platform/identity.nix:74`); the PostgreSQL role `keycloak`, created once by `keycloakPostgreSQLInit` | the role's SCRAM hash is in the roles dump (`pg_dumpall --globals-only`) | The role keeps the password it was created with; replacing the file alone breaks Keycloak's login ([identity host](identity-host.md), "replacing its file does **not** rotate an existing role's password") |
| `identity/bootstrap-admin-password` (inv. 11) | escrow only; never declared on a production host; handed once to `/run/terrace-identity-admin/` for `terrace-identity-admin first-admin` | no | single use |
| `tls/certificate`, `tls/key` (inv. 12, 13; absent on an ACME host) | nginx credentials for every public name of the role | no | nginx restarts (a few seconds without HTTPS); a wrong pair fails `terrace-secrets` format checks at install |
| `backup/primary/repository-password` (inv. 14) | restic `passwordFile` of target `primary`, read by `terrace-backup`, `terrace-backup-check`, `terrace-backup-freshness` | no, never (backups.md) | Losing every copy makes the repository permanently unreadable; a changed file that the repository does not accept fails every backup and restore |
| `backup/primary/sftp-key` (inv. 15) | restic SFTP identity (private half); the public half is enrolled at the Storage Box | no | backups fail until the new public half is enrolled |
| `backup/secondary/repository-password` (inv. 16; feature `secondary-backup`) | restic `passwordFile` of target `secondary` | no | as the primary password |
| `backup/secondary/environment` (inv. 17; feature `secondary-backup`) | restic `environmentFile` (B2 S3 key id and key) | no | secondary backups fail until the provider key is valid |

Not a credential of the store but delivered by it: the host's own age recipient
and the escrow recipient (1.2). Rehearsal-only store entries
(`rehearsal/users-json`) are refused on a production host (`terrace-secrets`
`install`, [secrets](secrets.md#operator-tool)).

### 1.2 Key material outside the sops store

| Credential | Lives | Generated by | Consumers | In backup | What breaks if it changes |
| --- | --- | --- | --- | --- | --- |
| Host SSH ed25519 key (also the host's age recipient) | `/etc/ssh/ssh_host_ed25519_key` on the host; the operator's staging copy on tmpfs until `host unstage` | `terrace-secrets host create` (`cmd_host_create`, `terrace-secrets:506`) | sshd; `sops-install-secrets` (age identity); `terrace-secrets push` pins it | no | The store file is encrypted to it: a new key needs `rekey` and a new push; `push` refuses a host with a pending new key |
| Escrow age identity | owner's offline storage; public recipient in the store (`escrow.recipient`) and pinned as `terrace.production.escrowRecipient` | `terrace-secrets escrow create` (`:430`) | every operator command that decrypts; host `install` and `verify` accept only ciphertext for exactly {host, escrow} | no | Loss: no re-key, no recovery of any host's secrets. Change: `escrow replace` (section 2, R9) |
| Operator SSH keys | the operator's machines; public halves are the profile parameter `terrace.production.operatorKeys` (`platform/production/base.nix:248`, `:441`) | each operator | sshd `AllowUsers operator` | no | losing the last one leaves no console or password login (docs/identity-admin.md "If the operator SSH keys are lost") |
| Headscale Noise and DERP private keys | `/var/lib/headscale/noise_private.key`, `derp_server_private.key` | Headscale on its first start | Headscale | yes (`backups.md`, file set `headscale`) | clients lose their network identity |
| Keycloak realm signing keys and administrator password hashes | the `keycloak` database | Keycloak | Keycloak | yes (database dump) | tokens signed by the old keys stop validating; the repository has no realm-key rotation tool |
| ACME account key and issued key | `/var/lib/acme` (ACME mode) | lego, on the host | nginx credentials | **no**, deliberately ([TLS](tls.md#backup-deliberately-excluded)) | a replacement registers a new account |
| Store ciphertext on the host | `/var/lib/terrace-secrets/secrets.sops.json` | `push` | `sops-install-secrets` | no | gone with the disk; `push` redelivers |
| Restic repository keys (inside the repository) | in each target | `restic init`, `key add` | `terrace-backup` | they are the repository | see 3.4 |

### 1.3 Credentials of services that exist only on the rehearsal profile

The production profile refuses Documenso, Windmill, Vaultwarden and K3s
([production profile](production-profile.md#decisions-waiting-on-the-owner)), and
their inputs are not rendered from the store yet. Today their credentials live in
root-owned `0400` files in input directories that the rehearsal profile **does**
back up (`extraPaths`):

| Credential | File and key | Consumers | Source | What breaks if it changes |
| --- | --- | --- | --- | --- |
| Documenso `authSecret`, `encryptionKey`, `encryptionSecondaryKey`, `oidcClientSecret`, `databasePassword` | `/var/lib/terrace-documenso-inputs/secrets.json` | `terrace-documenso-inputs` prepares runtime files; `terrace-documenso-database` applies `databasePassword` to role `terrace_documenso` with `ALTER ROLE ... PASSWORD` on every run (`platform/documenso/inputs.ts:62`, `platform/documenso.nix` database unit) | `docs/documenso-host.md` (inputs table) | Changing the encryption keys can make stored data unreadable (documenso-host.md); the module does not generate or rotate them |
| Documenso recovery user password; approved users | same file, `recoveryUser`, `approvedUsers` | `terrace-documenso-bootstrap` | same | bootstrap reconciles accounts and revokes sessions of changed managed accounts |
| Documenso signing certificate and passphrase | `signing.p12`, `signing-passphrase` in the same directory | the signing validation container and the writer | `docs/documenso-host.md` ("Optional local signing certificate") | an invalid or expired certificate stops the writer; the module does not generate, replace or rotate them |
| Windmill `adminPassword`, `clientSecret` | `/var/lib/terrace-windmill-inputs/bootstrap.json` | `terrace-windmill-bootstrap` (`platform/windmill/bootstrap.ts`) | `docs/windmill-host.md` | see R12 |
| Windmill workspace encryption key | not determined from the repository (no option in `platform/windmill.nix`; the database holds the workspace secrets, windmill-host.md "Persistence") | Windmill | `docs/migration.md`, runbook 9.9 only say it must not change before backup and verified recovery | changing it alone can make stored resources unreadable |
| Windmill database role `terrace_windmill` | no password: peer authentication through the host and container UID (`docs/windmill-host.md`) | the Windmill containers | same | nothing to rotate |
| Vaultwarden `adminToken`, `clientSecret` | `/var/lib/terrace-vaultwarden-inputs/input.json`; the token is hashed with argon2id before it reaches the service (`platform/vaultwarden/runtime.ts:33,118`) | `vaultwarden.service`, `terrace-vaultwarden-bootstrap` | `docs/vaultwarden-host.md` | see R13 |
| Vaultwarden RSA key | `/var/lib/vaultwarden/rsa_key.pem` (hashed in the rehearsal's signature) | Vaultwarden | `platform/vaultwarden.nix:288`, `platform/full/scenario.py` | not determined from the repository (no rotation path exists) |
| Vaultwarden users' vault keys | derived from each person's master password; not held by the server | the users' devices | `docs/vaultwarden-host.md`, full-rehearsal.md | not rotatable by the operator |
| Vaultwarden database role | no password: peer authentication, `DATABASE_URL` over the socket (`platform/vaultwarden.nix:161`) | `vaultwarden.service` | same | nothing to rotate |
| CMS database role `terrace_cms` | no password: local socket, "no remote database or password is accepted" (`platform/website.nix`, option description near line 346) | CMS and sweep | same | nothing to rotate |
| K3s server token | `/var/lib/rancher/k3s/server/token`, not in any backup (`docs/backups.md`) | K3s | single-node module | rehearsal only; not determined |
| Headscale pre-auth keys | created per client by the rehearsal driver, single use, expired before capture (`platform/full/scenario.py` `bootstrap_tailnet_peers`) | `tailscale up` | scenario.py | rehearsal only |
| Rehearsal certificate authority, TLS leaves, repository password | operator tooling files (`install_runtime`, `infra/nixos/rehearsal_services.py`) | the rehearsal hosts | cloud-rehearsal.md | synthetic, per run |

### 1.4 Credentials held outside the hosts

| Credential | Lives | Consumers | Source |
| --- | --- | --- | --- |
| GitHub to GitLab mirror deploy key | GitHub Actions secret `GITLAB_MIRROR_KEY` | `.github/workflows/gitlab-mirror.yml` (line 24), `tools/mirror-gitlab.py` | `docs/github-migration.md` "One-way mirror" |
| Hetzner project API token | a root-protected file passed as `--credential-file` or as `HCLOUD_TOKEN` at runtime; never in the repository | `infra/cloud/rehearsal_lease.py:363-390`, `rehearsal_apply.py:529-540`, `rehearsal_provider_actions.py`, `infra/cloud/index.ts:19-26` | `infra/cloud/README.md` |
| Token fingerprint pins | `tokenSha256` in the ignored Pulumi stack config; `credentialSha256` in each lease manifest | `index.ts` refuses a different token; every lease command refuses a token that does not match the lease | the same files |
| Pulumi stack passphrase | generated per rehearsal phase under the lease directory (`rehearsal_apply.py:439`) | the rehearsal controller | `infra/cloud/rehearsal_apply.py` |
| The private secrets store and its remote | the owner's private Git remote (not this repository) | `terrace-secrets` | `docs/secrets.md` |
| Storage Box sub-account, B2 account, GitHub and GitLab personal accounts | provider accounts | people | not determined from the repository (no code reads them) |

Not inventoried because no code in this repository reads them: Stripe, Google
Sheets and mail credentials (mail transport is gap G-20), the legacy root
`windmill/` and `nixos/` trees, and anything in the website repository's own
configuration beyond the environment files rendered here.

## 2. Procedures

Every command was checked against the implementing script. All
`terrace-secrets` operator commands need `TERRACE_SECRETS_STORE`,
`TERRACE_ESCROW_RECIPIENT` and `SOPS_AGE_KEY_FILE` set as in
[secrets](secrets.md#operator-tool). `push` and `apply` take `--address`, and
`--identity` for the operator's SSH key; the hidden `--rehearsal` flag used by the
VM checks is not part of a production command.

**General preconditions for every procedure.**

- A backup of the host that finished successfully (`terrace-backup age` exits 0)
  and an escrow copy that you have checked can decrypt the host's current store file
  (`terrace-secrets check <host>`).
- **`rotate` and `set` overwrite the value in the store immediately**
  (`cmd_rotate`, `cmd_set`). The previous value then exists only on the host and in
  the private store's Git history. Keep a copy of the previous ciphertext
  (`secrets.sops.json` for that host) until the procedure's last step has passed.
- A second SSH session to the host stays open until the end, because several steps
  restart services.
- Never run two procedures at once; the store takes a lock but the hosts do not.

### R1. `website/lifecycle-intake` (shared CMS and website secret)

1. `terrace-secrets rotate app website/lifecycle-intake`
2. `terrace-secrets push app --address <app address> --identity <key>`

`push` installs the new file and `sops-install-secrets` restarts
`terrace-cms.service` and `terrace-web.service` only. Worked: `rotate` prints
"Rotated website/lifecycle-intake on app. `push` delivers it and restarts:
terrace-cms.service, terrace-web.service."; the CMS and website run with the new
value; the old value is refused with 403 at the intake endpoint; PostgreSQL,
Keycloak and nginx are not restarted. Way back: set the previous value back with
`terrace-secrets set app website/lifecycle-intake` (reads stdin) and push. Evidence:
VM rehearsal (synthetic data), the production VM check subtest "rotate the intake
secret" (`platform/production/test.nix:479`). Not exercised anywhere: on a provider.

### R2. `website/payload-secret`

1. `terrace-secrets rotate app website/payload-secret`
2. `terrace-secrets push app --address <app address> --identity <key>`

The CMS restarts and every editorial session ends; nothing else signed with it is
inventoried ("no field encryption is configured", [secrets](secrets.md#rotation)).
Worked: editors sign in again and the CMS answers. Way back: as R1. Evidence: the
store and push path is the same code as R1 and is VM rehearsal; this secret's
rotation was never run: not exercised anywhere.

### R3. A Keycloak client secret (`identity/client/<client>`)

Clients: `terrace-web`, `terrace-community`, `terrace-cms`, `terrace-editorial`.
Sign-ins through the client fail between steps 2 and 3: do them back to back.

1. `terrace-secrets rotate app identity/client/terrace-cms --manual`
2. `terrace-secrets push app --address <app address> --identity <key>` (the
   consumers restart on the new value)
3. On the host, as root: `terrace-identity-admin client-secret sync --admin <you> --client terrace-cms [--dry-run]`

Worked: `Keycloak now uses the delivered secret; the previous one is refused.` and
`Consumers on the delivered value: ...`; a second `sync` says `already has the
delivered secret` ([identity administration](identity-admin.md#5-rotate-a-client-secret)).
Way back: if `sync` fails, the store, the host file and the consumers hold the new
value and Keycloak the old one: repeat `sync`, or `set` the old value back, push,
and `sync`. Evidence: VM rehearsal (synthetic data)
for `terrace-cms` only (`platform/production/identity-admin-test.nix:479`); the
other three clients share the code path and were not run: not exercised anywhere.

### R4. `identity/database-password` (Keycloak's PostgreSQL role)

**Order matters and the inventory text is wrong about it.** The code reads the new
value from the delivered file, so `push` comes before `apply`.

1. `terrace-secrets rotate app identity/database-password --manual`
2. `terrace-secrets push app --address <app address> --identity <key>` (Keycloak
   keeps running on its old connection settings)
3. `terrace-secrets apply app identity/database-password --address <app address> --identity <key>`

On the host `host-apply` computes a SCRAM verifier from the delivered file, runs
`ALTER ROLE keycloak WITH PASSWORD '<verifier>'` through `psql` with statement
logging off, and restarts `keycloak.service`
(`cmd_host_apply`, `platform/production/terrace-secrets:999-1011`). Worked: `Database
role keycloak updated with a SCRAM verifier and Keycloak restarted.`; Keycloak is
active; `PGPASSWORD="$(cat /run/secrets/identity/database-password)" psql -X -h 127.0.0.1 -U keycloak -d keycloak -c 'select 1'`
succeeds. **The VM check asserts the new password works; it does not assert that the
old one is refused.** Way back: if `apply` fails, the role still has the old password and the host file
holds the new one, so Keycloak cannot log in on its next start. Set the old value
back with `terrace-secrets set app identity/database-password` (stdin), `push`, and
restart Keycloak. If `apply` ran but Keycloak did not restart, restart it. Preconditions:
a verified backup, and the previous ciphertext kept. Evidence: VM
rehearsal (synthetic data), `platform/production/test.nix:507-523`; a failing apply
logs neither cleartext nor verifier (subtest at `:525`).

Other service database roles: the CMS, Windmill and Vaultwarden roles have no
password (peer or socket authentication), so there is nothing to rotate. Documenso's
role password is R11.

### R5. The Keycloak bootstrap administrator password (`identity/bootstrap-admin-password`)

Single use. To create the first administrator: `terrace-secrets apply app
identity/bootstrap-admin-password --address <app address> --identity <key>` hands the
escrowed value once to `/run/terrace-identity-admin/` (tmpfs, root `0400`); on the
host `terrace-identity-admin first-admin ...` uses it and removes it. Afterwards
`terrace-secrets rotate app identity/bootstrap-admin-password --manual` changes the
store copy only; nothing on the host holds it (`identity/bootstrap-admin-password`
inventory line 11; [identity administration](identity-admin.md)). `apply
--finish` is refused for it. Evidence: VM rehearsal (synthetic data),
`platform/production/identity-admin-test.nix:336,376`.

Named administrators change their own password with `terrace-identity-admin
password --admin <you>` (`terrace-identity-admin:1467`). Keycloak keeps these hashes
in its database, so they are in the backup. Evidence: no VM check
that was searched (`platform/production/identity-admin-test.nix` and its probe) runs
`terrace-identity-admin password`: not exercised anywhere.

### R6. TLS certificate and key (manual mode)

1. Obtain the new certificate chain and key outside this repository; the chain must
   cover every public name and not be near expiry.
2. `terrace-secrets set app tls/key < key.pem` and `terrace-secrets set app tls/certificate < fullchain.pem`
   (`set` reads stdin and validates the PEM format, `cmd_set`).
3. `terrace-secrets check app`, then `terrace-secrets push app --address <app address> --identity <key>`

nginx restarts (a few seconds without HTTPS; its credential snapshot refreshes
only on restart, [identity host](identity-host.md)). Worked: `terrace-tls check`
exits 0 on the host ([TLS](tls.md#check-status)). Revoke the old certificate at its
issuer afterwards. Way back: `set` the previous pair and push. Evidence: VM
rehearsal (synthetic data), a delivered manual certificate passing the expiry check
(`platform/production/tls-test.nix`); not exercised anywhere: a public CA, and the
issuer-side revocation. In ACME mode there is no store entry; renewal is automatic
and a forced renewal is `terrace-tls renew` ([TLS](tls.md#force-a-renewal)). Rotating
the ACME **account** key by hand: not determined from the repository (the account
is created on first order and kept in `/var/lib/acme`).

### R7. Backup repository password (`backup/<target>/repository-password`)

**WARNING: this procedure can lock you out of the repository. Keep the order
below. If you `push` (step 3) before the first `apply` (step 2), the host holds a
password the repository does not accept yet and cannot add the new key. If you run
`apply --finish` (step 4) before `push`, the key the host's file opens is still the
old one, so it counts as "current" and the NEW key is the one removed.** Preconditions: a verified backup repository (`terrace-backup restic
<target> check`), the previous ciphertext kept, and a second SSH session.

1. `terrace-secrets rotate app backup/primary/repository-password --manual`
2. `terrace-secrets apply app backup/primary/repository-password --address <app address> --identity <key>`
   (sends the NEW value on stdin; the host runs `terrace-backup restic primary key
   add --new-password-file /dev/stdin`, opened by the OLD password still in its
   file)
3. `terrace-secrets push app --address <app address> --identity <key>`
4. `terrace-secrets apply app backup/primary/repository-password --finish --only-current --address <app address> --identity <key>`
   (or `--keep <key id>` for every key to keep, for example a separately escrowed
   one; ids are 4 to 64 hex digits)

`--finish` lists keys with `terrace-backup restic <target> key list --json`,
requires exactly one `current` key, and removes every other key not listed in
`--keep` (`terrace-secrets:1038-1059`). The "current" key is the one the host's
delivered file opens: that is why `push` must come first. Worked: `Added the new
repository key to primary; now push, then apply --finish.` then `Removed 1 old
repository key(s) from primary; kept the current key.`; `terrace-backup restic
primary key list --json` shows one key; `terrace-backup restic primary snapshots`
works with the new file; the next backup succeeds. Repeat with `secondary` for a
second target (the app role only). Way back: until step 4 both keys open the
repository, so stop after step 3 and restore the old file with `set` and push; after
step 4 the old password is gone from the repository, and only a kept copy of the old
value helps if the new one is lost. Evidence: VM rehearsal (synthetic data), the
production VM check `platform/production/test.nix:517-523` (key add, push, remove
old, one key left). That check does not assert that the old password is refused and
does not run a backup after the rotation: not exercised anywhere.

### R8. Backup SFTP key (`backup/primary/sftp-key`)

**WARNING: the old public key is not kept by the store after step 1.** `rotate`
rewrites `public/backup--primary--sftp-key.pub` (`generate_value`, `replace=True`).
Print and save the old public half first (`terrace-secrets public app
backup/primary/sftp-key`).

1. Save the old public half: `terrace-secrets public app backup/primary/sftp-key`
2. `terrace-secrets rotate app backup/primary/sftp-key --manual`
3. `terrace-secrets public app backup/primary/sftp-key` and have the owner enrol the
   new public half at the Storage Box alongside the old one (provider console; not
   a command in this repository)
4. `terrace-secrets push app --address <app address> --identity <key>`
5. On the host run a backup: `systemctl start terrace-backup.service`; check
   `systemctl show terrace-backup.service -p Result --value` prints `success`
6. Remove the old public key at the Storage Box

Worked: step 5 succeeds with the new key. Way back before step 6: `set` the old
private key back (you need it from the previous ciphertext) and push. Evidence: the
store and `public` path are VM rehearsal (synthetic data); the Storage Box side and
the whole sequence: not exercised anywhere. The relay has its own key and repository
(repeat for `relay`).

### R9. The escrow identity (`escrow replace`)

**WARNING: the old escrow identity is needed to run it, and the new identity file
it writes is the only way to decrypt afterwards. Store the new file offline and
verify a copy before anything else. If the old escrow leaked, rotating the escrow
alone does not help: rotate every host secret as well (section 4).**

1. `terrace-secrets escrow replace /offline/terrace-escrow-2.age` with the OLD
   identity as `SOPS_AGE_KEY_FILE`. It writes the new identity file, re-encrypts
   every host's file under a temporary name, switches them all and records the new
   recipient last; if it stops part-way, run it again with the same file
   (`cmd_escrow_replace`, `terrace-secrets:457-500`).
2. Update `terrace.production.escrowRecipient` in every host's private module,
   deploy the hosts, and `terrace-secrets push` to each straight away. Until the
   push, a host deployed with the new pin fails `verify` and its writers stay gated
   after a reboot.

How to deploy a changed private host module to a running host is **not determined
from the repository** (the documents say "deploy" without a command). Worked:
`terrace-secrets check <host>` passes with the new recipient and `terrace-secrets
verify` passes on each host. If the old escrow identity is lost with no copy left,
`terrace-secrets escrow adopt <host> --address <addr>` re-encrypts each running
host's own file to a new escrow ([secrets](secrets.md#escrow-key-lost-or-leaked)); a
host that is gone loses its secrets. Evidence: unit tests only (`secrets.md`:
`escrow replace` and `escrow adopt` are proven against stand-in tools); not
exercised in a VM or anywhere else.

### R10. Host SSH key and the host's age recipient

`terrace-secrets host create <host> --role <role> --replace` makes a new key for a
**replacement host**; `rekey` re-encrypts the same values to it; the retired key can
still read any older ciphertext it saw ([restore promotion](restore-promotion.md), step 4).
There is no command that changes the host key of a running host in place: **needs a
code change** (the key is also the sops-nix age identity, `docs/secrets.md`
"Mechanism"). Evidence for the replacement path: VM rehearsal (synthetic data),
production VM check "back up, destroy the host, re-key from escrow".

### R11. Documenso (rehearsal profile only)

Documented sequence for changing `secrets.json`, `signing.p12` or
`signing-passphrase` ([Documenso host](documenso-host.md)): stop the writer
(`systemctl stop terrace-documenso.service`), replace the protected file,
`systemctl restart terrace-documenso-inputs.service`, then (for the database
password) `systemctl restart terrace-documenso-database.service` because the unit
stays active after its first run, then `terrace-documenso-migrate.service`,
`terrace-documenso-bootstrap.service`, `terrace-documenso.service`. Whether
restarting the inputs unit alone re-runs the database unit is systemd behaviour this
document has not verified; check that the new password logs in
(`PGPASSWORD=... psql -h /run/postgresql -U terrace_documenso -d terrace_documenso -c 'select 1'`,
the check `scenario.py` uses). **Never change `encryptionKey`, `encryptionSecondaryKey`
or `authSecret`**: see 3.2. Changing `databasePassword` has a code path
(`inputs.ts:62`); changing `oidcClientSecret` needs the matching Keycloak client,
and no tool here syncs the Documenso client (the production `client-secret sync`
refuses a client that is not one of the four, `terrace-identity-admin:954`): **needs
a code change** for the Keycloak side. Evidence: the stopped-writer sequence after
input changes is documented; no rotation of any of these values was run: not
exercised anywhere.

### R12. Windmill (rehearsal profile only)

- `clientSecret`: the bootstrap builds the provider settings with this secret on every run
  (`platform/windmill/policy.ts:34`). Stop the worker then the server, replace
  `bootstrap.json`, start them and let `terrace-windmill-bootstrap` reconcile
  ([Windmill host](windmill-host.md)). The Keycloak client secret must be changed to
  match: no tool for that client: **needs a code change**.
- `adminPassword`: **cannot be rotated by editing the file.** `bootstrap.ts:50-56`
  logs in with `adminPassword`; only if that fails does it try the factory password
  `changeme` and set `adminPassword`. After the first bootstrap the factory password
  no longer works, so a changed `adminPassword` makes both logins fail, the
  bootstrap fails, and nginx keeps returning 503 until a bootstrap succeeds
  ([Windmill host](windmill-host.md)). There is no path to change it later:
  **needs a code change**. Do not edit that field on a running host.
- The workspace encryption key: see 3.1.

Evidence: not exercised anywhere.

### R13. Vaultwarden (rehearsal profile only)

Documented input rotation ([Vaultwarden host](vaultwarden-host.md)): stop
`vaultwarden.service`, install the new protected `input.json`, restart
`terrace-vaultwarden-inputs.service`, then start the application; ingress stays
closed until the bootstrap reconciles. This changes `adminToken` (re-hashed with
argon2id) and `clientSecret`. As for Windmill, the Keycloak client has no sync tool
here: **needs a code change**. The RSA key and users' vault keys: see 3.3. Evidence:
not exercised anywhere.

### R14. Headscale keys

The Noise and DERP private keys exist from Headscale's first start; they are
restored with the host and never reset ([access relay](access-relay.md)). The
repository has no procedure to rotate them and rotation would change the network
identity of every client: **not determined from the repository**.

### R15. GitHub to GitLab mirror deploy key

From [github-migration](github-migration.md#one-way-mirror): create a new GitLab
project deploy key; grant only that key protected-branch write; replace the GitHub
Actions secret `GITLAB_MIRROR_KEY`; run **Actions, GitLab mirror, Run workflow** on
`trunk` and verify a successful mirror; then revoke the old project key. The old key
stays valid until the last step, so a failed mirror run is not an outage. Evidence:
documented; the rotation itself has not been run (the doc records a hosted mirror
verification on 2026-11-06 only): not exercised anywhere.

### R16. Hetzner API token and its pins

1. Do this only when **no lease is open**: every lease command compares the token's
   SHA-256 with `credentialSha256` in the lease manifest and refuses a different
   token ("credential does not match the lease", `infra/cloud/rehearsal_lease.py:390`,
   `rehearsal_apply.py:540`).
2. Create the new token inside the dedicated project in the Hetzner console and keep
   it in the protected file; never in a command line, chat or the repository.
3. Compute its SHA-256 locally without exposing the token and replace `tokenSha256`
   in the ignored Pulumi stack config; `index.ts` refuses a token whose fingerprint
   differs ([infra/cloud README](../infra/cloud/README.md)). The exact command to
   compute the digest is not given in the repository: **not determined from the
   repository**.
4. Revoke the old token in the console.

Evidence: not exercised anywhere (no provider apply is authorised; WORKER_RULES).
Only the matching checks are covered by `infra/cloud/test_rehearsal_lease.py` and
the other unit tests.

### R17. Operator SSH keys

Change `terrace.production.operatorKeys` in each host's private module and deploy
(public keys, ed25519 only, `base.nix:324`). **WARNING: add the new key and verify a
login with it before removing the old one; with no password and no emergency shell,
a host with no working operator key has no access at all** ([identity
administration](identity-admin.md), "If the operator SSH keys are lost"). The
deploy command is **not determined from the repository**. Evidence: not exercised
anywhere.

### R18. Private store remote

How the private store's own Git remote credentials are rotated is outside this
repository: **not determined from the repository**.

## 3. Dangerous or irreversible rotations

Rule for all of this section: **nothing here is rotated in normal operation.** A
rotation needs the owner's decision, a verified restore of that service from backup
onto a replacement (not a restart), and an escrow copy of the old key outside the
host, held until the new state has been verified.

### 3.1 Windmill's encryption key

Where the key lives and how it is delivered is not determined from the repository.
The only statement is that changing it alone can make stored resources unreadable
([migration](migration.md), runbook 9.9). Precondition for any change: Windmill
state backed up, recovery verified (the VM rehearsal restores the database, inputs
and state and decrypts a private variable: `windmill_browser('verify')`), and the
procedure for the key written first. The repository supports no change.

### 3.2 Documenso encryption keys and signing certificate

`encryptionKey`, `encryptionSecondaryKey` and `authSecret` in `secrets.json` must
stay with the independently protected recovery inputs: changing them can make
existing encrypted application data unreadable ([Documenso host](documenso-host.md)).
The rehearsal decrypts the restored two-factor secrets and recovery codes, which is
what proves a restored key still works. The repository has no re-encryption tool.
The signing certificate may be replaced by the stopped-writer sequence in R11 only
while the old one is kept to verify earlier signed documents (the signed envelopes
are in the database; whether a new certificate affects old ones is not determined
from the repository).

### 3.3 Vaultwarden keys

The server's RSA key (`/var/lib/vaultwarden/rsa_key.pem`) is part of the retained
state; no rotation path exists and its effect on sessions is not determined from the
repository. The users' vault keys come from their master passwords and are not held
by the server, so the operator cannot rotate them and the administrator token cannot
decrypt a vault (full-rehearsal.md).

### 3.4 Restic repository keys

What the repository supports: `terrace-backup restic <target> <args>` passes any
argument to restic (`cmd_restic`, `backup.sh:199`), so the full set of restic
`key` subcommands is reachable by hand. The tooling uses only `key add
--new-password-file /dev/stdin`, `key list --json` and `key remove <id>`
(`host-apply`). Changing the password therefore means **adding a second key, then
removing the old one**, as R7; a bare replacement of the password file locks you
out. Preconditions: a verified repository check, the previous ciphertext kept, a
separately escrowed key if the owner wants one (`--keep <id>`), and a second
session. Losing every copy of every key leaves the repository permanently
unreadable.

### 3.5 The escrow identity

R9. Preconditions: the new identity stored offline and a copy verified to decrypt
before the old one is destroyed; hosts re-pinned and pushed. A leaked escrow
identity means every historical ciphertext it saw stays readable: rotate all values
(section 4). Loss with no copy loses every host secret that cannot be recovered
from a running host by `escrow adopt`.

### 3.6 Headscale keys, host SSH keys, Keycloak realm keys

No in-place rotation exists in the repository (R10, R14; realm keys are in the
Keycloak database and the identity tool has no key command). Changing them changes
the identity every client, host or token relies on.

## 4. Compromise response

Not exercised anywhere. Orders follow
[secrets](secrets.md#revoke-after-a-host-compromise), which lists the same order
for a compromised host; each item points to the procedure above. Before starting,
keep the old ciphertext, take a backup if the host is trustworthy, and stop making
changes through any channel you suspect.

**Lost or stolen operator laptop** (holds an operator SSH key and possibly the
escrow identity or the store):
1. Remove its public key from every host (R17), adding a new key first.
2. If it held the escrow identity or the store: treat as an escrow leak, R9, then
   rotate every value of every host as in the next list. A retired key still reads
   every older ciphertext it saw.
3. Rotate what the laptop could reach: GitHub and GitLab sessions and the mirror key
   (R15), the Hetzner token (R16) if it was on the laptop, the store remote
   credentials (R18).

**Leaked host secret store** (ciphertext only, escrow safe): the ciphertext is
encrypted to the host key and the escrow recipient only, so nothing is readable
unless one of those keys also leaked; verify that first. If either did:
1. Revoke and replace the host as in secrets.md ("Revoke after a host compromise":
   `host create --replace`, `rekey`, install, `push`), which also changes the host
   key (R10).
2. Rotate in that document's order: the database password (R4), the Keycloak client
   secrets (R3), `PAYLOAD_SECRET` (R2), the intake secret (R1), the repository
   password (R7), the SFTP key (R8), TLS (R6), B2 credentials (R8 step list with
   `set`), the bootstrap administrator password (R5).

**Leaked backup credential** (repository password, SFTP key or B2 key):
1. SFTP key or B2 key: R8, or for B2 create a new key at the provider,
   `terrace-secrets set app backup/secondary/environment`, push, delete the old key.
2. Repository password: R7. Data already copied from the repository can be
   decrypted with the old password for as long as that copy exists, so also treat
   everything in the backups as exposed: every database password hash, the Keycloak
   realm and client secrets, and the outbox's invitation links. Rotate R4, R3, R2
   and R1 and expire the pending invitations.
3. Check that no backup ran with the old credential after the leak
   (`terrace-backup restic <target> snapshots`).

## 5. What nothing has exercised, and tests to add

Nothing has exercised, anywhere:

- Any rotation on a provider host, or with real credentials, data or consumers.
- R2 (`PAYLOAD_SECRET`), R3 for three of the four clients, R5's named administrator
  password change, R8 (the SFTP key and the Storage Box side), R9 (`escrow replace`
  and `escrow adopt` beyond unit tests), R11 to R14, R15, R16, R17 and the
  compromise orders of section 4.
- Asserting that an **old** credential is rejected after R4 (database password) and
  R7 (repository password).
- A backup running after a repository password rotation, and a restore after it.
- Any in-place change of a host SSH key, Headscale key, Keycloak realm key,
  Windmill key, Documenso key or Vaultwarden RSA key.
- Rotating a Documenso, Windmill or Vaultwarden OIDC client secret on the Keycloak
  side.
- Deploying a changed private host module (escrow recipient, operator keys).

Tests to add (not implemented), each as a VM assertion:

1. R4: after `apply`, `psql` with the previous password is refused and Keycloak
   serves a token request; a backup and a restore afterwards still succeed.
2. R7: after `--finish`, the previous password fails `restic snapshots`, the new
   one passes, a backup succeeds and an isolated restore reconciles; a negative
   case runs `--finish` before `push` and asserts it removes the wrong key (so the
   warning is tested), or the code is changed to refuse it.
3. R2 and R3 for each client: the old value is refused, each consumer restarted,
   sessions as documented.
4. R8: with a second SFTP identity enrolled at the test repository, backup works
   with the new key and fails with the removed one.
5. R9: `escrow replace` then a host restart, a `push`, and a restore from the new
   identity; the old identity can no longer decrypt the new file.
6. R10: a replacement with a new host key and the retired key refused on the next
   push (exists in the production VM check; add the relay).
7. A rotation under the stopped-writer rule for Documenso `databasePassword`: old
   password refused, new accepted, documents still decrypt.
8. A negative test that a changed Windmill `adminPassword` fails closed (documents
   the code behaviour), until the code is changed.
9. A compromise drill per list in section 4, asserting each listed credential
   changed and every consumer works.

Backlog items for the gaps are in [backlog](backlog.md#credential-rotation-follow-ups-g-37-recorded-2026-11-24).
