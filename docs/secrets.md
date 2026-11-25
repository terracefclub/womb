# Secrets delivery

How production hosts receive secrets, who holds which key, and how to create,
rotate, re-key and revoke them. Decided 2026-11-19 with the
[production profiles](production-profile.md). Synthetic values only have ever
been used: no real secret exists yet.

## Requirements

- No secret value in the Nix store, a derivation, a unit's `Environment=`, a
  log, a process argument, a core dump or this public repository.
- Decrypted only on the host, at boot or on an explicit reload, into memory,
  with exact owners and modes; gone at shutdown.
- The existing modules' file contracts stay as they are (root-owned `0400`
  regular files; `EnvironmentFile`, `LoadCredential`, restic password files).
- Exactly two recipients for every host file: the host's own key and an offline
  escrow key the owner holds. A lost host loses no secret; a rebuilt host is
  re-keyed from escrow.
- No trust on first use for a host key; repeatable tooling for every step.

## Mechanism: sops-nix with age

| Option | For | Against |
| --- | --- | --- |
| **sops-nix** (chosen) | Templates render the existing env files and the realm from individual secrets; per-secret owner and mode; age recipients from the host's SSH ed25519 key (no extra host key to stage); one file holds a host's whole set; already used by Womb's legacy flake; MIT | Larger moving part (a Go activation binary); placeholder templates are its own convention |
| agenix | Small, age-native, one file per secret | No templates: the env files and the realm would need a separate renderer or one opaque secret each, duplicating shared values such as the intake secret; re-keying is per file |
| systemd credentials (`systemd-creds`, `LoadCredentialEncrypted`) | No extra software; TPM binding | Encrypted to one host's key, so no escrow recipient and no re-keying for a replacement; a TPM is unavailable on Hetzner Cloud; still needs a renderer for env files |

sops-nix is pinned in `platform/flake.nix` to
`Mic92/sops-nix` `b18e01ce148eb3efff5de2f2d3181ca53b2b2a54` (2026-11-02, MIT),
locked in `platform/flake.lock` with its `nixpkgs` following the platform's.
Its binary cache is not used: `sops-install-secrets` builds from source.
Operator tools come from the pinned nixpkgs: `sops` 3.13.3 (MPL-2.0), `age`
1.3.2 (BSD-3-Clause) and `ssh-to-age` 1.3.0 (MIT).

Settings, all asserted by the production profile:

- `sops.useSystemdActivation = true`: `sops-install-secrets.service` runs before
  `sysinit.target`, and re-running it is how a host reloads secrets.
- Secrets land on **ramfs** under `/run/secrets.d/<generation>`, linked from
  `/run/secrets`. ramfs is never swapped (tmpfs can be) and is gone at shutdown.
  Unencrypted swap is refused, and **core dumps are disabled** (systemd-coredump
  off, `kernel.core_pattern` piped to a no-op, `fs.suid_dumpable = 0`,
  `LimitCORE=0` on every writer), so a crashing process never writes its
  memory, and the secrets in it, to disk.
- `sops.age.sshKeyPaths = ["/etc/ssh/ssh_host_ed25519_key"]`; only an ed25519
  host key exists, so no RSA key is imported.
- `sops.defaultSopsFile = "/var/lib/terrace-secrets/secrets.sops.json"` with
  `validateSopsFiles = false`, and no per-secret `sopsFile`: the ciphertext is
  copied to the host, never into the Nix store, so the builder's world-readable
  store never holds it and a rotation needs no rebuild.
- The escrow recipient is a **build parameter** (`terrace.production.escrowRecipient`).
  The host accepts (`install`) and keeps (`verify`) only ciphertext encrypted to
  exactly {its own key, that recipient} and to no PGP, KMS or other key type.
  Someone who can write the private store cannot add a recipient or swap the
  escrow without a host rebuild that the operator reviews.
- Every secret and rendered template is `root:root 0400`. Consumers read them as
  root before dropping privileges (systemd `EnvironmentFile` and
  `LoadCredential`, the root backup job), so no service account can read them.
- Env files and the Keycloak realm are sops **templates**. The realm template is
  built from the hash-pinned TFC producer (`identity/tools/production-realm.ts`)
  with sops placeholders in place of the four client secrets, so the store holds
  only placeholders and the realm and the env files cannot disagree.

### Value formats

Every inventory entry has a `format` (`platform/production/inventory.json`),
checked by `generate`, `set`, `rotate`, by `install` on the decrypted values
before anything consumes them (fail closed: the old ciphertext stays), and by
`verify` at every boot before the writers start:

| Format | Alphabet |
| --- | --- |
| `hex64` | exactly 64 lowercase hex digits (all generated secrets) |
| `pem-certificate`, `pem-private-key`, `openssh-private-key` | PEM blocks of the stated type, base64 lines only |
| `env-s3` | exactly `AWS_ACCESS_KEY_ID=` and `AWS_SECRET_ACCESS_KEY=` lines with a restricted alphabet |

Single-line values can therefore never contain whitespace, newlines, quotes,
backslashes, `$` or control characters. That enforced alphabet is what makes the
templates safe: sops-nix substitutes placeholders textually (it cannot
JSON-escape), and every value that lands in an env file, in the realm JSON or in
the SQL of the database-password action is `hex64`.

Beyond the formats, the TLS pair must belong together: the first certificate's
public key must be the key's (OpenSSL reads both on stdin, `tls_pair_problem`).
`set` warns while the stored pair does not match (one half is set at a time);
`push` checks the pair before it sends anything when `SOPS_AGE_KEY_FILE` lets it
decrypt the store (without one it prints a note and leaves the check to the host);
and the host's `install` refuses a mismatched pair before anything is installed, so
nginx is not restarted on it. A host still running an older `terrace-secrets`
does not check, so the operator-side check is the only one until the host is
updated. `check` decrypts nothing and does not check the pair. Known false
refusals, not tested: a certificate whose key is RSASSA-PSS (OpenSSL reports a PSS
public key for the certificate and an RSA one for the key) and an EC key with
explicit curve parameters where the certificate names the curve (the encodings
differ); a working pair of that kind is refused, never a mismatched one accepted.

## Where things live

| Item | Location | Public repo? |
| --- | --- | --- |
| Secret names, formats, owners, modes, consumers, rotation consequences | `platform/production/inventory.json` | yes (no values) |
| Encrypted store (one JSON per host), recipients, public keys | a **private** directory, `TERRACE_SECRETS_STORE` (for example a private Git repository) | **no** |
| Escrow private key | owner's offline storage (password manager entry plus a paper or offline copy) | no |
| Escrow public recipient | each host's build parameters (private host module) and the store | not secret |
| Host private key | the host's `/etc/ssh/ssh_host_ed25519_key`; the operator's staging copy is on tmpfs and removed with `host unstage` | no |
| Ciphertext on the host | `/var/lib/terrace-secrets/secrets.sops.json` (`0600`) | no |
| Plaintext | ramfs on the host; the operator tool's memory and its tmpfs scratch | never |
| Deploy signing key, secret half (only with the opt-in deploy path) | the build machine, outside any checkout and outside the Nix store ([below](#deploy-signing-key)) | **no** |
| Deploy signing key, public half | each host's `terrace.production.deploy.trustedKeys` (private host module) | not secret |

**Why ciphertext stays out of the public repository.** sops files are safe to
publish only while every recipient key stays secret forever. Public Git history
cannot be withdrawn: a host key stolen from a retired disk, or an escrow copy
found years later, would decrypt every historical version. age's X25519 is also
open to harvest-now-decrypt-later. The key names, value sizes, rotation dates
and recipient list would be public metadata. A private store removes all of
that. It is backed up by its own private Git remote; it never enters a host
backup.

**A retired key still reads what it saw.** Re-keying and rotation change future
ciphertext only. A retired host key (or a leaked escrow key) can still decrypt
every older ciphertext it was a recipient of, including copies in the private
store's Git history and on the host's old disk. So a compromise always means
**rotating the values**, not only re-keying.

Threat model in short: an attacker with this public repository learns only
names and policy. One with a host's disk learns that host's key and therefore
its current and past secrets; rotate. One with the escrow key learns everything,
which is why it stays offline and is used only for maintenance and recovery.
The builder never sees a value.

## Inventory

Generated by `terrace-secrets generate` unless marked external. All are
`root:root 0400`. "Restart" is what `sops-install-secrets` restarts
(`try-restart`) when the value changes; the full consequence text is in the
inventory and printed by `rotate`.

| Secret | Role | Consumed by | Rotation |
| --- | --- | --- | --- |
| `website/payload-secret` | app | `cms.env` `PAYLOAD_SECRET` | `rotate`; CMS restarts; editorial sessions end |
| `website/lifecycle-intake` | app | `cms.env` and `web.env` `LIFECYCLE_INTAKE_SECRET` (one value, both sides) | `rotate`; CMS and website restart together |
| `identity/client/terrace-web` | app | realm client `terrace-web`, `web.env` `MEMBER_AUTH_CLIENT_SECRET` | manual (below) |
| `identity/client/terrace-community` | app | realm, `cms.env` `MEMBER_AUTH_INTROSPECTION_SECRET` | manual |
| `identity/client/terrace-cms` | app | realm, `cms.env` `CMS_AUTH_CLIENT_SECRET` | manual |
| `identity/client/terrace-editorial` | app | realm, `cms.env` `CMS_AUTH_INTROSPECTION_SECRET` | manual |
| `identity/database-password` | app | Keycloak database role | `rotate --manual`, push, `apply` |
| `identity/bootstrap-admin-password` | app | escrow only in production: not declared on a production host, so never in `/run/secrets` there (the evaluation check refuses it); handed over once by `apply` to `/run/terrace-identity-admin/` (tmpfs, root `0400`) for `terrace-identity-admin first-admin`, which removes it; the rehearsal harness declares it, and any host whose build declares it can decrypt it (it is in that host's ciphertext) | single use: `apply`, first-admin, then `rotate --manual` ([identity administration](identity-admin.md)) |
| `tls/certificate`, `tls/key` | app, relay, not on an ACME host (feature `acme-tls`, [TLS](tls.md)) | nginx credentials | external: `set`, push |
| `backup/primary/repository-password` | app, relay | restic `passwordFile` | `rotate --manual`, `apply`, push, `apply --finish` |
| `backup/primary/sftp-key` | app, relay | restic SFTP identity | manual (below) |
| `backup/secondary/repository-password` | app (optional) | second restic target | as primary |
| `backup/secondary/environment` | app (optional) | B2 S3 key id and key | external: `set`, push |

That is 14 entries for the app (12 core, 2 optional secondary-backup) and 4 for
the relay, each host with its own values. A host that gets its certificate by
ACME (`host create ... --feature acme-tls`) has no `tls/*`: 12 and 2. `host tls
HOST manual` and `host tls HOST acme --yes` switch an existing host's store
([TLS](tls.md)); the second drops the manual pair and refuses without `--yes`.
While it switches, a host still accepts `tls/*` it no longer needs, ignores
them, and `verify` warns about them. Rehearsal-only:
`rehearsal/users-json` (synthetic users for the shared protocol; refused by
`install` on a production host).

Key material deliberately **outside** the sops store:

| Material | Generated by | Backed up | Notes |
| --- | --- | --- | --- |
| Host SSH ed25519 key (= age recipient) | `terrace-secrets host create` on the operator machine | no | Pinned before install; a replacement gets a new one |
| Escrow age key | `terrace-secrets escrow create` | owner's offline copies | Decrypts every host's store |
| Operator SSH keys | each operator | no | Public halves are profile parameters |
| Headscale Noise and DERP private keys | Headscale on first start | relay backup | Network identity; restored, never reset |
| Keycloak realm signing keys, client secret copies | Keycloak in its database | app backup (database) | Inherent: the database holds the client secrets |
| PostgreSQL role password hashes | PostgreSQL | backup (globals) | SCRAM hashes, not values |

Deferred services, to wire when each is enabled in production (owner sign-off,
see [production profile](production-profile.md#decisions-waiting-on-the-owner)):
Documenso `authSecret`, `encryptionKey`, `encryptionSecondaryKey`,
`oidcClientSecret`, `databasePassword`, the recovery user's password,
`signing.p12` and its passphrase; Windmill superadmin password and OIDC client
secret; Vaultwarden `adminToken` and OIDC client secret. Their OIDC clients must
also join the realm template.

Cross-check with the website's production configuration table
(`docs/member-lifecycle.md`, website `dev`): every CMS secret (`PAYLOAD_SECRET`,
`CMS_AUTH_CLIENT_SECRET`, `CMS_AUTH_INTROSPECTION_SECRET`,
`MEMBER_AUTH_INTROSPECTION_SECRET`, `LIFECYCLE_INTAKE_SECRET`) and website secret
(`MEMBER_AUTH_CLIENT_SECRET`, `LIFECYCLE_INTAKE_SECRET`) is covered. Non-secret
settings are rendered into the same templates from the hostnames or forced by
the module after loading the file. `CMS_LOCAL_BOOTSTRAP` and `CMS_BOOTSTRAP_*`
are refused.

## Operator tool

`platform/production/terrace-secrets` (Python standard library plus `sops`,
`age`, `ssh-to-age` and `ssh-keygen`, all in the dev shell; also built as
`packages.x86_64-linux.terrace-secrets`). It never prints a value and never puts
one in a command argument. Plaintext exists only in its memory (it disables its own core
dumps), in pipes to `sops`/`ssh`, and (for `ssh-keygen` output) in a private
scratch directory on tmpfs/ramfs that it removes; it refuses to run if the scratch directory is not
memory-backed. Mutating commands take a lock on the store, so two operators
cannot interleave. Decrypting commands read the escrow (or an operator) identity
from `SOPS_AGE_KEY_FILE`, which must be private to you (mode `0600`).
`check` and `push` also need `TERRACE_ESCROW_RECIPIENT`, the recipient pinned in
the host parameters, and refuse a store whose escrow recipient differs.

On macOS there is no tmpfs: create a RAM disk (`hdiutil attach -nomount
ram://262144` then `diskutil erasevolume APFS terrace-scratch <device>`), point
`TERRACE_SECRETS_SCRATCH` at it and set `TERRACE_SECRETS_SCRATCH_IS_RAMDISK=1`
(the tool cannot verify the filesystem type there). Linux needs nothing:
`/dev/shm` is used and checked.

```sh
export TERRACE_SECRETS_STORE=/private/deployment/secrets   # private, never this repo
export TERRACE_ESCROW_RECIPIENT=age1...                     # as pinned in the host module
terrace-secrets escrow create /offline/terrace-escrow.age     # once; prints only the public recipient
terrace-secrets host create app --role app                    # host key staged on tmpfs; prints where
terrace-secrets generate app                                  # random values, encrypted to host + escrow
terrace-secrets set app tls/certificate < fullchain.pem       # each external value, from stdin
terrace-secrets check app                                     # names, encryption, exact recipients
terrace-secrets push app --address HOST                       # over SSH pinned to the host key
terrace-secrets host unstage app                              # remove the staged host key
terrace-secrets rotate app website/lifecycle-intake           # one generated value, then push
terrace-secrets rotate app identity/database-password --manual   # a secret whose service keeps a copy
terrace-secrets apply app identity/database-password --address HOST  # hand it to that service
terrace-secrets host create app --role app --replace && terrace-secrets rekey app
terrace-secrets host tls app manual | acme --yes             # how the host gets its certificate (docs/tls.md)
terrace-secrets host reset-replacement app --confirm app      # only to redeploy a wiped host as NEW
terrace-secrets escrow replace /offline/terrace-escrow-2.age  # new escrow; re-encrypts every host
terrace-secrets public app backup/primary/sftp-key            # public half, for enrolment
# on a host, as root:
terrace-secrets verify                                        # every secret present, owner, mode, format, ramfs, recipients
terrace-production adopt --new | --restored                   # once per host: arm the writers
terrace-production arm-timers [--in-service]                   # after adopt --restored: backups once verified, the sweep once in service
```

`push` and `apply` connect with `ssh -F /dev/null`, `HostKeyAlias` set to the
host name and a known_hosts file built from the stored host public key (strict
checking, no agent forwarding, the destination after `--`), and run a fixed
command through `sudo` on the host. `install` accepts only a sops file whose
values are all encrypted, whose recipients are exactly the host and the pinned
escrow, whose names belong to this host (rehearsal names only on a rehearsal
host) and whose decrypted values match their formats. It keeps the previous
file, re-runs `sops-install-secrets` (which `try-restart`s the consumers of
changed values) and `verify`, and restores the previous file if either fails.

## Bootstrap of a brand-new host

1. `terrace-secrets host create <host> --role <role>` makes the ed25519 host key
   on the operator machine, in a private directory on tmpfs by default (never
   stage inside the store; the tool refuses it). It records the public key and
   age recipient in the store. This is the "separate per-host Ed25519 host key"
   the [Rescue installer](cloud-rescue-install.md) already stages, so the host's
   identity is known before it ever boots: no trust on first use, and SSH to the
   installed host is pinned to the same key.
2. `generate`, then `set` each external value, then `check`.
3. Install with the host key staged. **The installer does not stage the
   ciphertext** (a gap, see
   [production profile](production-profile.md#cold-start-of-a-new-deployment)),
   so the first boot has no secrets and nothing writes.
4. `push` (SSH pinned to the host's public key kept in the store), then `host
   unstage`, then `terrace-production adopt --new` on the host. The order of `push`
   and `host unstage` does not matter once the host runs the staged key (the pin is
   the store's copy of the public key); the production VM check unstages first.

## Rotation

- **Generated, no copy elsewhere** (`website/payload-secret`,
  `website/lifecycle-intake`): `rotate`, then `push`. The changed templates
  restart their units; nothing else restarts. `PAYLOAD_SECRET` ends every
  editorial session and invalidates anything Payload signed with it (no field
  encryption is configured).
- **Database password**: `rotate --manual identity/database-password`; `push`
  (Keycloak keeps running on its old connection settings); `apply` runs, on the
  host, `ALTER ROLE keycloak PASSWORD 'SCRAM-SHA-256$4096:...'` with a verifier
  the tool computes from the delivered file (PBKDF2-HMAC-SHA256, random salt;
  the cleartext never enters SQL), in a session that logs no statement text
  (`log_min_error_statement = panic`, `log_statement = none`, no duration
  logging), and restarts Keycloak. The upstream Keycloak database init would
  log its `CREATE ROLE ... PASSWORD '<cleartext>'` if that statement ever failed;
  the profile runs it with the same no-statement-logging session options. Upstream
  also runs that `psql --file` without `ON_ERROR_STOP`, so a failed role creation
  used to exit 0 and the unit reported success; the profile sets `PSQLRC` with
  `ON_ERROR_STOP` so it fails visibly. The VM forces that failure and finds the
  error logged without the statement or the password.
- **Backup repository password**: `rotate --manual`; `apply` (before `push`:
  sends the NEW value over the pinned channel on stdin; the host adds it as a
  restic key while the old one still opens the repository); `push`; `apply
  --finish --keep <id>` for every key to keep (for example a separately
  escrowed one; an id is 4 to 64 lowercase hex digits, checked on both ends), or `--finish --only-current` to remove every key but the one
  the delivered password opens: run alone it lists every key and removes nothing,
  then `--remove <id>` must name exactly each key it removes. `--finish` sends the
  store value again and the host refuses it until `push` has delivered that value
  (before the push it would remove the new key). The escrowed store holds the new value.
- **SFTP key**: `rotate --manual backup/primary/sftp-key`; `public` and enrol the
  new public key at the Storage Box (owner, alongside the old); `push`; run a
  backup; remove the old public key at the Storage Box.
- **Keycloak client secrets** (`identity/client/*`): Keycloak keeps its own copy
  from the first realm import. `rotate --manual`, then `push` (the consumer
  restarts on the new value), then on the host `terrace-identity-admin
  client-secret sync --client <id>`, which sets Keycloak to the delivered value
  and checks that the old one is refused ([identity administration](identity-admin.md#5-rotate-a-client-secret)).
  Sign-ins through that client fail between `push` and `sync`. No operator
  handles the plaintext. Proven in the VM for `terrace-cms`.
- **Bootstrap administrator password**: single use for the first administrator:
  `apply` hands it to the host (stdin over the pinned channel, never declared on
  the host), `terrace-identity-admin first-admin` uses and removes it; then
  `rotate --manual` (store only; there is nothing to push).
- **TLS and B2 credentials**: external; obtain the new value, `set`, `push`
  (nginx restarts for TLS), then revoke the old one at its issuer.

## Revoke after a host compromise

1. Treat every value that host could decrypt as known: its current and every
   older ciphertext it held.
2. Contain: take the host offline (provider console), keep its disk for
   evidence; do not reuse its host key.
3. `host create <host> --replace` and `rekey` (the compromised key leaves the
   recipients), install a replacement, `push`.
4. Rotate every secret of that host in this order: the database password,
   Keycloak client secrets, `PAYLOAD_SECRET`, the intake secret, the repository
   password, the SFTP key (and remove its old public key at the Storage Box),
   TLS (revoke the old certificate), B2 credentials, the bootstrap administrator
   password.
5. If the escrow key could have been on that host or reachable from it, follow
   the escrow procedure below as well.
6. Destroy the old disk once evidence is no longer needed; confirm absence
   (runbook 6.5).

## Escrow key lost or leaked

- **Leaked (still in hand)**: `escrow replace NEWFILE` with the old escrow as
  `SOPS_AGE_KEY_FILE` makes a new escrow key, writes every host's re-encrypted
  file under a temporary name, then switches them all and records the new
  recipient last; if it stops part-way, run it again with the same NEWFILE and
  it resumes; an aborted run removes its temporary files, and until the
  replacement is finished every other mutating command, `check` and `push`
  refuse (finish a half-done replace first). Update every host's `escrowRecipient` parameter, deploy the hosts
  (they refuse the new ciphertext until then) and `push` to each one straight
  away: a host deployed with the new pin but not yet pushed fails `verify`, so a
  reboot in between leaves its writers gated until the push. Store the new key
  offline. Because the old
  escrow decrypts every past ciphertext, rotate every secret of every host
  (above).
- **Lost (no copy left)**: while the hosts run, each can re-encrypt its own
  file: make a new escrow key (`age-keygen`, offline), record its recipient as
  the store's `escrow.recipient`, then for each host run `terrace-secrets escrow
  adopt <host> --address HOST` with the new key as `SOPS_AGE_KEY_FILE`: over the
  pinned channel the host re-encrypts its own file to {itself, new escrow}, and
  the tool decrypts it with the new key and checks every name and format before
  storing it. Then update the host parameters, deploy, `push`. If a host is
  gone as well, its secrets are gone: regenerate and rotate them.

## Re-keying for a replacement host

`host create <host> --replace` archives the old public key and makes a new host
key; `rekey <host>` decrypts with the escrow identity and re-encrypts the same
values to {new host, escrow} with a fresh data key. Install the replacement with
the new key and `push`.

## Deploy signing key

Only when the opt-in deploy path is used ([host updates](host-updates.md#15-the-deploy-path-option-1-of-u0-opt-in);
the owner has not chosen it). It is a new secret, separate from every value above:
the host never receives it and `terrace-secrets` does not manage it. Written
procedure, NOT RUN outside the VM proof (which makes its keys at build time).

- **Create** on the build machine, as the user that builds:
  `umask 077; mkdir -p ~/.config/terrace/deploy && nix key generate-secret --key-name terrace-deploy-1 >~/.config/terrace/deploy/terrace-deploy-1.sec`,
  then `nix key convert-secret-to-public <~/.config/terrace/deploy/terrace-deploy-1.sec`
  prints the public half for `deploy.trustedKeys`. Number the name (`-1`, `-2`):
  rotation trusts two names at once.
- **Where and mode**: a directory `0700` and the file `0600`, owned by the build
  user, outside every checkout (the repository runs gitleaks, but keep it out of
  reach anyway) and never passed to a Nix build (a store path is world-readable).
  `terrace-deploy.sh` reads it only for `nix store sign`.
- **Who may sign**: `nix store sign` writes into the build machine's store, so the
  user must be root or trusted by its Nix daemon.
- **Backup**: none of the secret half. Instead keep a **second, offline key** (same
  commands on an offline machine, stored with the escrow key) and trust both public
  halves from the first install; the spare signs only when the main key is lost.
  Whether to keep a spare is the owner's choice; without one, a lost key means the
  manual bootstrap route of the host updates document.
- **Loss, leak and rotation**: [host updates, section 1.5](host-updates.md#15-the-deploy-path-option-1-of-u0-opt-in),
  "Losing or replacing the signing key". A leaked key alone does not reach a host:
  copying still needs the `operator` login and activation needs `sudo`.

## Verification

`platform/production-check` (VM) proves: delivery only through this mechanism
(the first boot has no secrets and stays gated; `push` installs; `verify` PASS
with formats); a ciphertext with a foreign recipient is refused by `install`;
absence of every value, and of the host's derived age identity, from the system
closure, unit files and properties, every journal, process arguments, files on
the root disk and the raw disk, each scan first shown to find a planted
non-secret control (the closure scan a string built into the closure); no core
file or coredump journal entry after a writer is aborted; `root:root 0400` on
ramfs and nothing left after a reboot; rotation of the intake secret (old value
refused, both services on the new one, PostgreSQL, Keycloak and nginx not
restarted); the database-password and repository-password `apply` procedures,
each previous password refused afterwards, and `apply --finish` refused before the
`push` and, with `--only-current`, until every removed key is named; a mismatched
TLS pair refused by `push` before sending and by `install` with nginx untouched
(these last checks first passed at `9664be1`, 2026-11-24);
and a host replacement re-keyed from escrow (the retired key can no longer
decrypt the new file), restored with reconciliation, identical data and the
lifecycle continuing. The operator guest's disk and journal hold no value either.

What the disk scans exclude, and why: file scans skip `/nix` (scanned
separately as the system closure), `/var/lib/postgresql`, `/var/lib/terrace-backup`
(database dumps) and `/var/lib/terrace-restore` (restored dumps and data),
because Keycloak keeps its own copy of the four OIDC client secrets in its
database by design; the raw-disk scan therefore omits only those four values
and covers everything else, including deleted blocks and those directories.
`/proc`, `/sys`, `/run` and other memory filesystems are where secrets are meant
to be, and are not disk.

Which commands are proven where: in the VM, against real `sops`, `age` and
`ssh-to-age`: `host create` (and `--replace`), `generate`, `set`, `check`,
`push`, `install` (including its refusal of a foreign recipient), `verify`,
`rotate`, `rotate --manual`, `rekey`, `apply` for the database password and the
backup repository password, and `host unstage` for the app and the relay. In the
identity administration VM proof (`platform/production-identity-check`): `apply`
of the bootstrap password and `rotate --manual` plus `push` of the `terrace-cms`
client secret, completed by `terrace-identity-admin client-secret sync`.
Against stand-in tools only (unit tests): `escrow replace` (including resume),
`escrow adopt`, `reencrypt`, the store lock, `host-apply` argument and SQL
handling, and every format rule. `infra/nixos/production-eval-check.nix` proves
the evaluation invariants.
