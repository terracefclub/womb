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
random bytes as 64 hex digits (`generate_value`, `terrace-secrets:422`).

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
| `tls/certificate`, `tls/key` (inv. 12, 13; absent on an ACME host) | nginx credentials for every public name of the role | no | nginx restarts (a few seconds without HTTPS). `terrace-secrets` checks the PEM labels and that the DER starts as a sequence (`private_key`, `certificates`, `terrace-secrets:84-92`), and that the first certificate's public key is the key's (`tls_pair_problem`): `set` warns about a mismatched pair in the store, and the host's `install` refuses one before anything is installed, so nginx is not restarted on it |
| `backup/primary/repository-password` (inv. 14) | restic `passwordFile` of target `primary`, read by `terrace-backup`, `terrace-backup-check`, `terrace-backup-freshness` | no, never (backups.md) | Losing every copy makes the repository permanently unreadable; a changed file that the repository does not accept fails every backup and restore |
| `backup/primary/sftp-key` (inv. 15) | restic SFTP identity (private half); the public half is enrolled at the Storage Box | no | backups fail until the new public half is enrolled |
| `backup/secondary/repository-password` (inv. 16; feature `secondary-backup`) | restic `passwordFile` of target `secondary` | no | as the primary password |
| `backup/secondary/environment` (inv. 17; feature `secondary-backup`) | restic `environmentFile` (B2 S3 key id and key) | no | secondary backups fail until the provider key is valid |

The four client secrets are also placeholders in the rendered realm template
`identity/terrace-realm.json` (`platform/production/app.nix:17-28`;
`platform/production/identity-admin.nix:153` names it as the definition the tool
reads). Keycloak imports that file only on its first start and `realm plan/apply`
reads it, so after R3 it carries the new value while Keycloak holds whatever `sync`
set.

Not a credential of the store but delivered by it: the host's own age recipient
and the escrow recipient (1.2). Rehearsal-only store entries
(`rehearsal/users-json`) are refused on a production host (`terrace-secrets`
`install`, [secrets](secrets.md#operator-tool)).

### 1.2 Key material outside the sops store

| Credential | Lives | Generated by | Consumers | In backup | What breaks if it changes |
| --- | --- | --- | --- | --- | --- |
| Host SSH ed25519 key (also the host's age recipient) | `/etc/ssh/ssh_host_ed25519_key` on the host; the operator's staging copy on tmpfs until `host unstage` | `terrace-secrets host create` (`cmd_host_create`, `terrace-secrets:525`) | sshd; `sops-install-secrets` (age identity); `terrace-secrets push` pins it | no | The store file is encrypted to it: a new key needs `rekey` and a new push; `push` refuses a host with a pending new key |
| Escrow age identity | owner's offline storage; public recipient in the store (`escrow.recipient`) and pinned as `terrace.production.escrowRecipient` | `terrace-secrets escrow create` (`:430`) | every operator command that decrypts; host `install` and `verify` accept only ciphertext for exactly {host, escrow} | no | Loss: no re-key, no recovery of any host's secrets. Change: `escrow replace` (section 2, R9) |
| Operator SSH keys | the operator's machines; public halves are the profile parameter `terrace.production.operatorKeys` (`platform/production/base.nix:293`, `:441`) | each operator | sshd `AllowUsers operator` | no | losing the last one leaves no console or password login (docs/identity-admin.md "If the operator SSH keys are lost") |
| Headscale Noise and DERP private keys | `/var/lib/headscale/noise_private.key`, `derp_server_private.key` | Headscale on its first start | Headscale | yes (`backups.md`, file set `headscale`) | clients lose their network identity |
| Keycloak realm signing keys and administrator password hashes | the `keycloak` database | Keycloak | Keycloak | yes (database dump) | tokens signed by the old keys stop validating; the repository has no realm-key rotation tool |
| ACME account key and issued key | `/var/lib/acme` (ACME mode) | lego, on the host | nginx credentials | **no**, deliberately ([TLS](tls.md#backup-deliberately-excluded)) | a replacement registers a new account |
| Store ciphertext on the host | `/var/lib/terrace-secrets/secrets.sops.json` | `push` | `sops-install-secrets` | no | gone with the disk; `push` redelivers |
| Website outbox invitation links | `/var/lib/terrace-cms-outbox` (CMS account, `0700`; files `0600`) | the CMS when a member is approved | CMS, sweep, operator reads as root | **yes** (`extraPaths.website-outbox`, `platform/website.nix:578`, backups.md) | each file is a live one-time link: anyone holding a copy can redeem it until it is redeemed, revoked or expired; there is no rotation, only erasure or expiry by the lifecycle |
| Keycloak named-administrator passwords, generated handoff files, break-glass accounts | the `keycloak` database (hashes); handoff files in `/run/terrace-identity-admin/handoff/` (tmpfs, root `0400`); temporary `terrace-recovery-*` accounts | `terrace-identity-admin` (`docs/identity-admin.md`, `terrace-identity-admin:1339-1400`) | Keycloak master realm | hashes yes (database); handoff files no | see R5 |
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
| Per-lease Hetzner SSH key, the Rescue root password the provider returns, and the install banner proof nonce | lease directory files, made once per lease | the rehearsal controller | `infra/cloud/rehearsal_provider_actions.py:63-92`, `infra/nixos/rescue_install.py:388-399` | rehearsal only; short-lived and never reused, so excluded from the procedures; not in any backup |
| Rehearsal certificate authority, TLS leaves, repository password | operator tooling files (`install_runtime`, `infra/nixos/rehearsal_services.py`) | the rehearsal hosts | cloud-rehearsal.md | synthetic, per run |

### 1.4 Credentials held outside the hosts

| Credential | Lives | Consumers | Source |
| --- | --- | --- | --- |
| GitHub to GitLab mirror deploy key | GitHub Actions secret `GITLAB_MIRROR_KEY` | `.github/workflows/gitlab-mirror.yml` (line 24), `tools/mirror-gitlab.py` | `docs/github-migration.md` "One-way mirror" |
| Hetzner project API token | a root-protected file passed as `--credential-file` or as `HCLOUD_TOKEN` at runtime; never in the repository | `infra/cloud/rehearsal_lease.py:363-390`, `rehearsal_apply.py:529-540`, `rehearsal_provider_actions.py`, `infra/cloud/index.ts:19-26` | `infra/cloud/README.md` |
| Token fingerprint pins | `tokenSha256` in the ignored Pulumi stack config; `credentialSha256` in each lease manifest | `index.ts` refuses a different token; every lease command refuses a token that does not match the lease | the same files |
| Pulumi stack passphrase | generated per rehearsal phase under the lease directory (`rehearsal_apply.py:431,441`, read at `:448`) | the rehearsal controller | `infra/cloud/rehearsal_apply.py` |
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

- A backup of the host that finished successfully (`terrace-backup age` exits 0).
- **A saved copy of the host's store file, and proof that the escrow identity opens
  it.** `terrace-secrets check <host>` does **not** decrypt: it lists names and
  compares recipients (`file_report`, `cmd_check`, `terrace-secrets:726-749`). The
  production VM check proves decryption with `sops decrypt --output-type json
  "$TERRACE_SECRETS_STORE/hosts/<host>/secrets.sops.json"` using the escrow identity
  (`platform/production/test.nix:707`). Run that command (with `SOPS_AGE_KEY_FILE`
  set) and discard the output: for example `sops decrypt --output-type json
  "$TERRACE_SECRETS_STORE/hosts/<host>/secrets.sops.json" >/dev/null`, which must
  exit 0.
- **`rotate` and `set` overwrite the value in the store immediately**
  (`cmd_rotate`, `cmd_set`), and the tooling has no history or undo command. Before
  step 1 of any procedure copy the file: `cp -p
  "$TERRACE_SECRETS_STORE/hosts/<host>/secrets.sops.json" <saved copy>`. It holds
  every secret of the host (encrypted to the host key and the escrow key), so keep
  it with the same care as the store, off the host, and delete it only after the
  procedure's last check has passed. **After a compromise of the escrow identity, or
  of the host key of that host, such copies stay readable by the leaked key: delete
  them once the rotation is verified** (section 4). If the store is a private Git repository
  ([secrets](secrets.md#where-things-live)) its history also holds earlier revisions,
  but that is the owner's choice, not something the tooling creates.
- **How the old value comes back (the "way back" below).** Either (a) restore the
  saved file (`cp -p <saved copy> "$TERRACE_SECRETS_STORE/hosts/<host>/secrets.sops.json"`)
  and `push`: this also reverts every other change made to that host since the copy,
  and `push` refuses a file whose recipients no longer match (after a `rekey` or an
  escrow replacement), or (b) read one value and pipe it, never through a file left
  on disk, into `terrace-secrets set <host> <name>` (it reads stdin). The nested
  layout of the file follows the name split on `/` (`nested_set`,
  `terrace-secrets:190`); the VM check pipes `sops decrypt` into `jq`
  (`test.nix:707`). The exact `jq` path expression for one name is not given by the
  repository.
- A second SSH session to the host stays open until the end, because several steps
  restart services.
- Never run two procedures at once; the store takes a lock but the hosts do not.
- **After `host create --replace`**, `rotate` and `set` refuse until `rekey`
  (`recipients()`, `terrace-secrets:377-381`), and `push`/`apply` pin the stored new
  host key (`pinned_ssh`, `:734-752`), so they fail against the old host at once.
  Rotations that follow a host replacement therefore happen on the replacement.

### R1. `website/lifecycle-intake` (shared CMS and website secret)

1. Do the general preconditions.
2. `terrace-secrets rotate app website/lifecycle-intake`
3. `terrace-secrets push app --address <app address> --identity <key>`

`push` installs the new file and `sops-install-secrets` restarts
`terrace-cms.service` and `terrace-web.service` only. Worked: `rotate` prints
"Rotated website/lifecycle-intake on app. `push` delivers it and restarts:
terrace-cms.service, terrace-web.service."; the CMS and website run with the new
value; the old value is refused with 403 at the intake endpoint; PostgreSQL,
Keycloak and nginx are not restarted. Way back: restore the saved copy or `set` the
old value as described in the preconditions, and push. Evidence: VM rehearsal
(synthetic data), the production VM check subtest "rotate the intake secret"
(`platform/production/test.nix:483`). Not exercised anywhere: on a provider.

### R2. `website/payload-secret`

1. Do the general preconditions.
2. `terrace-secrets rotate app website/payload-secret`
3. `terrace-secrets push app --address <app address> --identity <key>`

The CMS restarts and every editorial session ends; nothing else signed with it is
inventoried ("no field encryption is configured", [secrets](secrets.md#rotation)).
Worked: editors sign in again and the CMS answers. Way back: as R1. Evidence: the
store and push path is the same code as R1 and is VM rehearsal; this secret's
rotation was never run: not exercised anywhere.

### R3. A Keycloak client secret (`identity/client/<client>`)

Clients: `terrace-web`, `terrace-community`, `terrace-cms`, `terrace-editorial`.
Sign-ins through the client fail between steps 3 and 4: do them back to back.

1. Do the general preconditions.
2. `terrace-secrets rotate app identity/client/terrace-cms --manual`
3. `terrace-secrets push app --address <app address> --identity <key>` (the
   consumers restart on the new value)
4. On the host, as root: `terrace-identity-admin client-secret sync --admin <you> --client terrace-cms [--dry-run]`

Worked: `Keycloak now uses the delivered secret; the previous one is refused.` and
`Consumers on the delivered value: ...`; a second `sync` says `already has the
delivered secret` ([identity administration](identity-admin.md#5-rotate-a-client-secret)).
Way back: if `sync` fails, the store, the host file and the consumers hold the new
value and Keycloak the old one: repeat `sync`; or restore the old value (saved copy,
as in the preconditions), push, and `sync` again. After a restore from a backup
taken before the rotation, Keycloak's database holds the old client secrets while the
store holds the new: run `sync` again for each rotated client
([restore promotion](restore-promotion.md)). Evidence: VM rehearsal (synthetic data)
for `terrace-cms` only (`platform/production/identity-admin-test.nix:479-497`, which
also asserts the sign-in window, old refused, new accepted and an idempotent second
`sync`); the other three clients share the code path and were not run: not
exercised anywhere.

### R4. `identity/database-password` (Keycloak's PostgreSQL role)

**Order matters.** The code reads the new value from the delivered file, so `push`
comes before `apply` (the inventory's consequence text says so too). **Between `push` and
`apply`, any Keycloak restart fails** (new file, old role password): a crash restart,
`first-admin` or `break-glass` runs, which stop and start Keycloak. Keep the window
short and run no identity administration between the two commands.

1. Do the general preconditions.
2. `terrace-secrets rotate app identity/database-password --manual`
3. `terrace-secrets push app --address <app address> --identity <key>` (Keycloak
   keeps running on its old connection settings until it restarts)
4. `terrace-secrets apply app identity/database-password --address <app address> --identity <key>`

On the host `host-apply` computes a SCRAM verifier from the delivered file, runs
`ALTER ROLE keycloak WITH PASSWORD '<verifier>'` through `psql` with statement
logging off, and restarts `keycloak.service`
(`cmd_host_apply`, `platform/production/terrace-secrets:1033-1045`). Worked: `Database
role keycloak updated with a SCRAM verifier and Keycloak restarted.`; Keycloak is
active; `PGPASSWORD="$(cat /run/secrets/identity/database-password)" psql -X -h 127.0.0.1 -U keycloak -d keycloak -c 'select 1'`
succeeds, and the previous password is refused (`password authentication failed`).
The VM check asserts both (proof run of this change pending), the old password opening the role before the rotation as
a control. Way back: if `apply` fails, the role still has the old password
and the host file holds the new one, so Keycloak cannot log in on its next start.
Restore the old value (saved copy), `push`, and restart Keycloak. If `apply` ran but
Keycloak did not restart, restart it. **After a successful `apply` the role already
has the new password**, so going back needs the old value restored in the store, a
`push` (the host file becomes the old value), and another `terrace-secrets apply app
identity/database-password`, which reads the delivered file and sets the role and
restarts Keycloak again (`cmd_host_apply`, `terrace-secrets:1033-1045`). A restore from a backup taken before the
rotation brings back the old role hash (roles dump, backups.md); run `apply` again
after the restore ([restore promotion](restore-promotion.md)). Evidence: VM
rehearsal (synthetic data), the production VM check subtest "manual secrets: database
password and backup repository password via `apply`; the old ones are refused"; a
failing apply logs neither cleartext nor verifier (the subtest after it).

Other service database roles: the CMS, Windmill and Vaultwarden roles have no
password (peer or socket authentication), so there is nothing to rotate. Documenso's
role password is R11.

### R5. Keycloak administrator passwords and the bootstrap password

Single use bootstrap value (`identity/bootstrap-admin-password`): `terrace-secrets
apply app identity/bootstrap-admin-password --address <app address> --identity <key>`
hands the escrowed value once to `/run/terrace-identity-admin/` (tmpfs, root
`0400`); on the host `terrace-identity-admin first-admin ...` uses it and removes it.
Afterwards `terrace-secrets rotate app identity/bootstrap-admin-password --manual`
changes the store copy only; nothing on the host holds it (inventory line 11;
[identity administration](identity-admin.md)). `apply --finish` is refused for it.
Evidence: VM rehearsal (synthetic data), `platform/production/identity-admin-test.nix:336,376`.

Named administrators change their own password with `terrace-identity-admin
password --admin <you>` (`cmd_password`, `terrace-identity-admin:705-722`; the
command refuses a new password shorter than 16 characters or equal to the current
one, and checks that the old one is refused and the new accepted). Evidence: VM
rehearsal (synthetic data): `identity-admin-test.nix:401-413` runs it for two
administrators and asserts "Password of ops-alice changed; the old one is refused.";
the change it performs is the first change of an initial (handoff) password to a
chosen one, the same code path as any later change. A later rotation of an
established password was not separately run.

**If the only administrator password is lost** during a change, the recovery is
`terrace-identity-admin break-glass begin` over SSH
([identity administration](identity-admin.md#7-break-glass-no-administrator-no-sso-no-proxy-g-40)),
then repair named access and `break-glass end` as a named administrator. Temporary
`terrace-recovery-*` accounts are flagged by the temporary-administrator timer while
older than the configured age. Evidence: VM rehearsal (synthetic data) for
break-glass; not exercised after a restore (identity-admin.md says so).

### R6. TLS certificate and key (manual mode)

**`terrace-secrets` checks that the key belongs to the certificate** (the first
certificate's public key against the key's, with OpenSSL reading both on stdin,
`tls_pair_problem`). `set` stores one half at a time and prints a `WARNING ... set
the matching tls/key next` (or `tls/certificate`) while the stored pair does not
match; `push` refuses a mismatched pair before sending (`...; nothing sent`) when
`SOPS_AGE_KEY_FILE` lets it decrypt the store (without one it prints a note and
sends); and the host's `install` refuses it (`tls/certificate and tls/key do not
belong together ...; nothing installed, nginx not restarted`), so the host keeps its
previous pair. A host on an older `terrace-secrets` has no install check. nginx still restarts on every push that
changes the pair.

1. Do the general preconditions.
2. Obtain the new certificate chain and key outside this repository; the chain must
   cover every public name and not be near expiry.
3. `terrace-secrets set app tls/key < key.pem` and `terrace-secrets set app tls/certificate < fullchain.pem`
   (`set` reads stdin and validates the PEM structure). After the first command a
   warning that the stored pair does not belong together is expected (the old
   certificate is still stored); **after the second there must be none**. A warning
   after both means the two files do not belong together: do not push, find the
   matching file. The key file must never leave the operator machine.
4. `terrace-secrets check app`, then `terrace-secrets push app --address <app address> --identity <key>`
   (refused before sending, or by the host with nothing changed, if the pair still
   does not match)

Worked: `terrace-tls check` exits 0 on the host ([TLS](tls.md#check-status)).
**If nginx does not come up after the push** (`systemctl status nginx.service`,
`journalctl -u nginx.service` on the host), HTTPS is down for every name: restore the
previous pair from the saved copy (preconditions) and push, which restarts nginx on the
old pair. Revoke the old certificate at its issuer afterwards. Evidence: VM rehearsal
(synthetic data), a delivered manual certificate passing the expiry check
(`platform/production/tls-test.nix`); the production VM check's subtest "TLS pair"
(proof run of this change pending)
stores a new certificate for a new key (warning), has the push refused before
sending, then refused by the host's install when pushed without an operator
identity, with the host ciphertext, nginx's process and the served certificate
unchanged, then sets the matching key (no warning) and pushes the new pair. Unit tests with the real OpenSSL
(RSA, EC, Ed25519, chains, mismatches) in `platform/production/test_terrace_secrets.py`. Not
exercised anywhere: a public CA, and the issuer-side revocation.

In ACME mode there is no store entry and renewal is automatic. `terrace-tls renew`
only starts the order unit and does nothing when renewal is not due. A forced order
is `terrace-tls renew --force --yes` (refused without `--yes`; `platform/production/tls.nix:176-179`,
[TLS](tls.md#force-a-renewal)); it counts against the CA's rate limits (tls.md quotes
Let's Encrypt's published 5 certificates a week for the same names as of its writing;
check the CA's current page), the served certificate stays until the new one is
installed, and nginx is reloaded, not restarted. Whether a forced order also makes a
new private key, and revoking the old certificate at the CA, are not covered by this
repository: not determined from the repository. Rotating the ACME **account** key by
hand: not determined from the repository.

### R7. Backup repository password (`backup/<target>/repository-password`)

**WARNING: this procedure can lock you out of the repository, and its last step is
not reversible. Keep the order. Checkpoint before any key is removed.**

Preconditions: the general ones, plus `terrace-backup restic <target> check`
succeeded, and a plan for an **independent escrow copy** of the new password: the
store is not independent of the repository's host, and [backups](backups.md) requires
a copy in the team password manager, outside the servers.

1. `terrace-secrets rotate app backup/primary/repository-password --manual`.
   **Do not run `rotate` for this credential again until the procedure has ended**:
   it overwrites the store value with a third password that no repository key
   accepts (see "If you rotated twice").
2. `terrace-secrets apply app backup/primary/repository-password --address <app address> --identity <key>`
   (sends the NEW value on stdin; the host runs `terrace-backup restic primary key add
   --new-password-file /dev/stdin`, opened by the OLD password still in its file)
3. `terrace-secrets push app --address <app address> --identity <key>`
4. **Checkpoint, on the host as root, before any removal:**
   - `terrace-backup restic primary key list --json` shows two keys and the key with
     `"current": true` is the new one (the one the delivered file opens).
   - `systemctl start terrace-backup.service` succeeds
     (`systemctl show terrace-backup.service -p Result --value` prints `success`)
     using the new delivered file.
   - The new password is copied to the independent escrow (password manager).
5. `terrace-secrets apply app backup/primary/repository-password --finish --keep <id> ... --address <app address> --identity <key>`,
   with `--keep <key id>` (4 to 64 hex digits, a prefix of an id from the checkpoint's
   `key list --json`) for every key that must survive; every other key that is not
   `current` is removed. Each `--keep` must match exactly one key: a prefix that
   matches no key (a typo) or two is refused with the key list before anything is
   removed. Or `--finish --only-current`, which removes every key but the
   current one, **including a separately added escrow key**: run without `--remove`
   it removes nothing, prints every key (`key <id> current (kept)` or `other
   (removed)`, with user, host and creation time) and refuses; read the list, then
   repeat with `--remove <key id>` naming exactly each key marked `other`. A
   `--remove` list that misses one, names the current key, or matches two keys is
   refused with the list again. `--finish` is refused without `--keep` or
   `--only-current`, and with both.

`--finish` sends the store value over the pinned channel on stdin, like step 2, and
the host refuses (`the password delivered to this host is not the store's newest
value; push first ...`) unless its delivered file holds exactly that value, before it
asks restic anything: run before `push` it would otherwise remove the NEW key (the
delivered file still opens the old one, which restic reports as current). Then it
lists keys with `key list --json` and requires exactly one `current` key. Worked:
`Added the new repository key to primary; now push, then apply --finish.` then
`Removed <n> old repository key(s) from primary; ...`; `key list --json` shows the
intended keys; `terrace-backup restic primary snapshots` works with the new file; the
next backup succeeds. Repeat with `secondary` for a second target (app role only).
Evidence: VM rehearsal (synthetic data) (proof run of this change pending), the production VM check subtest "manual
secrets: ... the old ones are refused": key add; `--finish` before `push` refused
with both keys kept; after `push` both passwords open the repository (control);
`--only-current` without `--remove` lists the keys and removes nothing; with
`--remove` the old key goes; afterwards the new password opens the repository and the
previous one is refused (`wrong password`). Not exercised anywhere: a backup or a
restore after the rotation.

**Recoveries, from the code; if the state is not one of these, do not proceed.**

- *Pushed (step 3) before the first `apply` (step 2).* The host file holds a
  password no key opens, so `key add` (opened by the delivered file) and every backup
  fail. The repository is unchanged, with only the old key. Restore the old value
  (saved copy) and `push`, then restart from step 1.
- *`--finish` (step 5) before the `push`.* Refused by the host with nothing removed
  (the delivered file is not the store's value). Push, then repeat step 5. Before
  this check existed the NEW key was removed in this order; a host running an
  older `terrace-secrets` still does that: the repository then holds only the old
  key, so restore the old value in the store (saved copy), do not push the new one,
  and restart.
- *If you rotated twice.* After step 2 and a second `rotate`, the repository holds
  the old key and the first new key, the store a third password. Restore the old
  value (saved copy), push, then run `terrace-backup restic primary key list --json`
  on the host and **confirm which key is marked `current`** (the one the restored old
  file opens). The stray key to remove is the one that is **not** `current`:
  `terrace-backup restic primary key remove <id>` (the command the tool uses), then
  start again. Removing the `current` key is refused by restic itself, but never rely
  on that: read the list first.
- *`--finish` fails after some keys were removed.* Run `key list --json`; if the
  remaining keys are not exactly what you intended and the delivered file does not
  open the repository, do not proceed.

**What this does and does not protect (restic's documented key model; verify against
the restic manual, this is not in this repository):** `key add` and `key remove`
change which passwords open the repository; they do not change the repository's
master key. Someone who learned the old password and kept access to the repository
(the SFTP or B2 credential) can still read snapshots written **after** this rotation.
Moving to a new repository is the only complete answer and is **not determined from
the repository** (no tooling or procedure exists for it).

### R8. Backup SFTP key (`backup/primary/sftp-key`)

**WARNING: the old public key is not kept by the store after step 3.** `rotate`
rewrites `public/backup--primary--sftp-key.pub` (`generate_value`, `replace=True`).

1. Do the general preconditions.
2. Save the old public half: `terrace-secrets public app backup/primary/sftp-key`
3. `terrace-secrets rotate app backup/primary/sftp-key --manual`
4. `terrace-secrets public app backup/primary/sftp-key`, and have the owner enrol the
   new public half at the Storage Box alongside the old one (provider console; not a
   command in this repository)
5. `terrace-secrets push app --address <app address> --identity <key>`
6. On the host run a backup: `systemctl start terrace-backup.service`; check
   `systemctl show terrace-backup.service -p Result --value` prints `success`
7. Remove the old public key at the Storage Box

Worked: step 6 succeeds with the new key. Way back before step 7: the old private
key is in the saved store copy: restore it (preconditions) and push; the old public
half you saved in step 2 shows which key to keep enrolled. Evidence: the store and
`public` path are VM rehearsal (synthetic data); the Storage Box side and the whole
sequence: not exercised anywhere. The relay has its own key and repository (repeat
for `relay`).

### R9. The escrow identity (`escrow replace`)

**WARNING: the old escrow identity is needed to run it, and the new identity file
it writes is the only way to decrypt afterwards. Store the new file offline and
verify a copy before anything else. If the old escrow leaked, rotating the escrow
alone does not help: rotate every host secret as well (section 4).**

1. `terrace-secrets escrow replace /offline/terrace-escrow-2.age` with the OLD
   identity as `SOPS_AGE_KEY_FILE`. It writes the new identity file, re-encrypts
   every host's file under a temporary name, switches them all and records the new
   recipient last; if it stops part-way, run it again with the same file
   (`cmd_escrow_replace`, `terrace-secrets:476-519`).
2. Update `terrace.production.escrowRecipient` in every host's private module,
   deploy the hosts, and `terrace-secrets push` to each straight away. Until the
   push, a host deployed with the new pin fails `verify` and its writers stay gated
   after a reboot.

How to deploy a changed private host module to a running host is **not determined
from the repository** (the documents say "deploy" without a command). Worked:
`terrace-secrets check <host>` (names and recipients only) passes with the new recipient,
`sops decrypt --output-type json` of each host file succeeds with the NEW identity
file (verify a second copy of that file decrypts before the old identity is destroyed),
and `terrace-secrets
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
`host create --replace` overwrites the stored public key at once and `rekey` is
required before any `rotate`, `set` or `push` (see the preconditions). There is no
command that changes the host key of a running host in place: **needs a
code change** (the key is also the sops-nix age identity, `docs/secrets.md`
"Mechanism"). Evidence for the replacement path: VM rehearsal (synthetic data),
production VM check "back up, destroy the host, re-key from escrow".

### R11. Documenso (rehearsal profile only)

Documented ([Documenso host](documenso-host.md), lines about inputs and signing):
stop the writer (`systemctl stop terrace-documenso.service`), replace the protected
file, `systemctl restart terrace-documenso-inputs.service`, start the writer; and
"restart the database input unit when changing its password", with the same
stopped-writer sequence for account changes. **Inferred, not documented:** the
explicit `systemctl restart terrace-documenso-database.service` (the unit has
`RemainAfterExit`, `platform/documenso.nix`, so a plain start would not re-run it)
and running `terrace-documenso-migrate.service` and
`terrace-documenso-bootstrap.service` before starting the writer. Whether
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
  (`platform/windmill/policy.ts:34`). **Inferred, not documented:** stop the worker
  then the server, replace `bootstrap.json`, start them and let
  `terrace-windmill-bootstrap` reconcile; [Windmill host](windmill-host.md) says only
  to stop worker then server before taking a snapshot, and gives no rotation sequence. The Keycloak client secret must be changed to
  match: no tool for that client: **needs a code change**.
- `adminPassword`: **cannot be rotated by editing the file.** `bootstrap.ts:50-56`
  logs in with `adminPassword`; only if that fails does it try the factory password
  `changeme` and set `adminPassword`. After the first bootstrap the factory password
  no longer works, so a changed `adminPassword` makes both logins fail, the
  bootstrap fails, and nginx keeps returning 503 until a bootstrap succeeds
  ([Windmill host](windmill-host.md)). What the code does: it logs in with `adminPassword`; on failure it tries `changeme`
  and sets `adminPassword`; on failure it throws, and nginx stays at 503 until a
  bootstrap succeeds. The repository has no other path in `bootstrap.ts`; whether
  Windmill itself offers another way to change an administrator password is not
  determined here. A rotation by this route **needs a code change**. Do not edit
  that field on a running host.
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
3. Compute `tokenSha256`: per [infra/cloud README](../infra/cloud/README.md) it is
   the lowercase SHA-256 of the project token, computed locally without exposing the
   token. `infra/cloud/index.ts:22-24` computes it as the SHA-256 hex digest (64
   lowercase hex digits) of exactly the `HCLOUD_TOKEN` bytes, and
   `rehearsal_lease.py:361-370` strips surrounding whitespace from the credential file
   first, so hash the token bytes without a trailing newline. The repository gives no
   ready shell command for it. Replace `tokenSha256` in the ignored Pulumi stack
   config; `index.ts` refuses a token whose digest differs.
4. Revoke the old token in the console.

Evidence: not exercised anywhere (no provider apply is authorised; WORKER_RULES).
Only the matching checks are covered by `infra/cloud/test_rehearsal_lease.py` and
the other unit tests.

### R17. Operator SSH keys

Change `terrace.production.operatorKeys` in each host's private module and deploy
(public keys, ed25519 only, `base.nix:375`). **WARNING: add the new key and verify a
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

Not exercised anywhere. The order for a compromised host follows
[secrets](secrets.md#revoke-after-a-host-compromise). Each case below separates what
leaked, because the responses differ. Before starting: save the store file copy,
take a backup if the host is trustworthy, and stop making changes through any
channel you suspect. Remember that `rotate`/`set` re-encrypt only to the current
recipients: a key that stays a recipient reads the new ciphertext too, so replace the
host (or the escrow) first, then rotate values.

**A. A laptop with an operator SSH key only.**
1. Add a new key and verify a login, then remove the stolen key from every host (R17).
2. Rotate what the laptop could reach: GitHub and GitLab sessions and the mirror
   key (R15), the Hetzner token (R16) if it was on the laptop, the store remote
   credentials (R18).

**B. A copy of the store ciphertext only** (no escrow identity, no host private
key). The file is encrypted to {host key, escrow key} only, so it is unreadable
without one of them. Verify that neither leaked. Nothing to rotate; record the date,
because a later leak of either key reads this copy.

**C. The escrow identity (or `SOPS_AGE_KEY_FILE`), host key safe.** No host
replacement is needed.
1. `terrace-secrets escrow replace` (R9), update `escrowRecipient` and push to every
   host.
2. Rotate every value of every host, because the old escrow reads every ciphertext it
   ever saw: R4, R3, R2, R1, R7, R8, R6, B2 credentials (`set backup/secondary/environment`
   after creating a new key at the provider, then push, then delete the old provider
   key), and the bootstrap password (R5).
3. **Delete every saved pre-rotation copy of the store files** made under the general
   precondition: the leaked escrow identity still decrypts them, and they hold the
   old values.

**D. A host private key (a staged host key from `host create` before `host
unstage`, or a stolen host disk), escrow safe.**
1. Replace the host: `host create <host> --role <role> --replace`, `rekey`, install,
   `push` (R10; [restore promotion](restore-promotion.md) steps 4 to 6). From
   `host create` on, `push` and `apply` pin the new key, so **rotate on the
   replacement host**, after `rekey` and `push`.
2. Rotate every value in secrets.md's order: R4, R3, R2, R1, R7, R8, R6, B2
   credentials (as in C.2), R5.

**E. A leaked backup credential** (repository password, SFTP key or B2 key).
1. SFTP key: R8. B2 key: create a new key at the provider, `terrace-secrets set app
   backup/secondary/environment`, push, delete the old provider key.
2. Repository password: R7. **R7 does not make later snapshots unreadable to someone
   who has both the old password and repository access** (restic's documented key
   model; verify against the restic manual); a new repository is the only complete
   answer and is not determined from the repository.
3. A repository copy exposes what the backups hold: the Keycloak realm signing keys,
   client secrets and user password hashes (the `keycloak` database dump), all PostgreSQL
   role hashes, Headscale's Noise and DERP private keys, and the outbox's live
   invitation links. Rotatable with a procedure here: R3 (client secrets), R4
   (database role password). **Keycloak realm signing keys, user password hashes and
   Headscale keys have no rotation procedure in this repository**: the owner can
   expire or revoke outbox invitation links through the lifecycle and can ask members
   to change passwords, but cannot rotate those keys with the tooling. R1 and R2 are
   not in any backup (inventory) and do not need rotating for a backup leak alone.
4. Check the repository for tampering by someone with write access, not only for
   later snapshots: `terrace-backup restic <target> check` and `terrace-backup restic
   <target> snapshots` (a snapshot you did not make is a sign).

## 5. What nothing has exercised, and tests to add

Nothing has exercised, anywhere:

- Any rotation on a provider host, or with real credentials, data or consumers.
- R2 (`PAYLOAD_SECRET`), R3 for three of the four clients, R5's rotation of an
  established administrator password (the initial-to-chosen change is run, see R5),
  R8 (the SFTP key and the Storage Box side), R9 (`escrow replace` and `escrow adopt`
  beyond unit tests), R11 to R14, R15, R16, R17 and the compromise orders of
  section 4.
- Any backup, restore or `key list` check between the steps of R7, and the recoveries
  listed there.
- A backup running after a repository password rotation, and a restore after it.
- Any in-place change of a host SSH key, Headscale key, Keycloak realm key,
  Windmill key, Documenso key or Vaultwarden RSA key.
- Rotating a Documenso, Windmill or Vaultwarden OIDC client secret on the Keycloak
  side.
- Deploying a changed private host module (escrow recipient, operator keys).

Tests to add (not implemented), each as a VM assertion:

1. R4: Keycloak serves a token request after `apply`; a backup and a restore
   afterwards still succeed. (The previous password being refused is asserted.)
2. R7: a backup succeeds and an isolated restore reconciles after `--finish`. (The
   previous password being refused, and `--finish` before `push` being refused, are
   asserted.)
3. R2 and R3 for each client: the old value is refused, each consumer restarted,
   sessions as documented.
4. R8: with a second SFTP identity enrolled at the test repository, backup works
   with the new key and fails with the removed one.
5. R9: `escrow replace` then a host restart, a `push`, and a restore from the new
   identity; the old identity can no longer decrypt the new file.
6. R10: a replacement with a new host key; the retired key can no longer decrypt
   the new store file and `push` before `rekey` is refused (both exist in the
   production VM check, `test.nix:708-720`, for the app role; add the relay).
7. A rotation under the stopped-writer rule for Documenso `databasePassword`: old
   password refused, new accepted, documents still decrypt.
8. A negative test that a changed Windmill `adminPassword` fails closed (documents
   the code behaviour), until the code is changed.
9. A compromise drill per list in section 4, asserting each listed credential
   changed and every consumer works.

Backlog items for the gaps are in [backlog](backlog.md#credential-rotation-follow-ups-g-37-recorded-2026-11-24).
