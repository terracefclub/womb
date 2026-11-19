# Secrets delivery

How production hosts receive secrets, who holds which key, and how to create,
rotate, re-key and revoke them. Decided 2026-11-19 with the
[production profiles](production-profile.md). Synthetic values only have ever
been used: no real secret exists yet.

## Requirements

- No secret value in the Nix store, a derivation, a unit's `Environment=`, a
  log, a process argument or this public repository.
- Decrypted only on the host, at boot or on an explicit reload, into memory,
  with exact owners and modes; gone at shutdown.
- The existing modules' file contracts stay as they are (root-owned `0400`
  regular files; `EnvironmentFile`, `LoadCredential`, restic password files).
- At least two recipients for every secret: the host's own key and an offline
  escrow key the owner holds. A lost host loses no secret; a rebuilt host is
  re-keyed from escrow.
- No trust on first use for a host key; repeatable tooling for every step.

## Mechanism: sops-nix with age

| Option | For | Against |
| --- | --- | --- |
| **sops-nix** (chosen) | Templates render the existing env files and the realm from individual secrets; per-secret owner and mode; age recipients from the host's SSH ed25519 key (no extra host key to stage); one file holds a host's whole set; `sops` edits single values without exposing the rest; already used by Womb's legacy flake; MIT | Larger moving part (a Go activation binary); placeholder templates are its own convention |
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
  The profile forbids unencrypted swap as well.
- `sops.age.sshKeyPaths = ["/etc/ssh/ssh_host_ed25519_key"]`; only an ed25519
  host key exists, so no RSA key is imported.
- `sops.defaultSopsFile = "/var/lib/terrace-secrets/secrets.sops.json"` with
  `validateSopsFiles = false`: the ciphertext is copied to the host, never into
  the Nix store, so the builder's world-readable store never holds it and a
  rotation needs no rebuild.
- Every secret and rendered template is `root:root 0400`. Consumers read them as
  root before dropping privileges (systemd `EnvironmentFile` and
  `LoadCredential`, the root backup job), so no service account can read them.
- Env files and the Keycloak realm are sops **templates**. The realm template is
  built from the hash-pinned TFC producer
  (`identity/tools/production-realm.ts`) with sops placeholders in place of the
  four client secrets, so the store holds only placeholders and the realm and
  the env files cannot disagree.

## Where things live

| Item | Location | Public repo? |
| --- | --- | --- |
| Secret names, owners, modes, consumers, generators | `platform/production/inventory.json` | yes (no values) |
| Encrypted store (one JSON per host), recipients, public keys | a **private** directory, `TERRACE_SECRETS_STORE` (for example a private Git repository) | **no** |
| Escrow private key | owner's offline storage (password manager entry plus a paper or offline copy) | no |
| Host private key | the host's `/etc/ssh/ssh_host_ed25519_key`; the operator's staging copy is deleted after install | no |
| Ciphertext on the host | `/var/lib/terrace-secrets/secrets.sops.json` (`0600`) | no |
| Plaintext | ramfs on the host only | never |

**Why ciphertext stays out of the public repository.** sops files are safe to
publish only while every recipient key stays secret forever. Public Git
history cannot be withdrawn: a host key stolen from a retired disk, or an
escrow copy found years later, would decrypt every historical version,
including secrets "rotated away" since. age's X25519 is also open to
harvest-now-decrypt-later by a future quantum adversary. The key names, value
sizes, rotation dates and recipient list would be public metadata. A private
store costs nothing for a club this size and removes all of that. The private
store is backed up by its own Git remote; it never enters a host backup.

Threat model in short: an attacker with this public repository learns only
names and policy. One with a host's disk learns that host's key and therefore
its current secrets; re-key and rotate (below). One with the escrow key learns
everything, which is why it stays offline and is used only for maintenance
and recovery. The builder never sees a value.

## Inventory

Generated by `terrace-secrets generate` unless marked external. All are
`root:root 0400`. "Restart" lists what `sops-install-secrets` restarts
(`try-restart`) when the value changes.

| Secret | Role | Consumed by | Rotation consequence |
| --- | --- | --- | --- |
| `website/payload-secret` | app | `cms.env` `PAYLOAD_SECRET` | Restart CMS; editorial sessions end |
| `website/lifecycle-intake` | app | `cms.env` and `web.env` `LIFECYCLE_INTAKE_SECRET` (one value, both sides) | Restart CMS and website together; an apply or redeem in flight fails once. `terrace-secrets rotate` |
| `identity/client/terrace-web` | app | realm client `terrace-web`, `web.env` `MEMBER_AUTH_CLIENT_SECRET` | Realm is imported only on first start: update the client in Keycloak, then rotate (manual, G-15) |
| `identity/client/terrace-community` | app | realm, `cms.env` `MEMBER_AUTH_INTROSPECTION_SECRET` | as above |
| `identity/client/terrace-cms` | app | realm, `cms.env` `CMS_AUTH_CLIENT_SECRET` | as above |
| `identity/client/terrace-editorial` | app | realm, `cms.env` `CMS_AUTH_INTROSPECTION_SECRET` | as above |
| `identity/database-password` | app | Keycloak database role (`keycloakPostgreSQLInit` creates it once) | `ALTER ROLE keycloak PASSWORD` in the same change, then restart Keycloak (manual) |
| `identity/bootstrap-admin-password` | app | not delivered in production; escrow only, for the first-administrator procedure (G-15); the rehearsal harness delivers it | Single use; regenerate after use |
| `tls/certificate`, `tls/key` | app, relay | nginx credentials (all public hostnames of the role) | External (until ACME, G-12); restart nginx |
| `backup/primary/repository-password` | app, relay | restic `passwordFile` | `restic key add`, switch, `restic key remove`; escrow the new value |
| `backup/primary/sftp-key` | app, relay | restic SFTP identity; public half enrolled at the Storage Box | Enrol new public key, switch, remove old |
| `backup/secondary/repository-password` | app (optional) | second restic target | as primary |
| `backup/secondary/environment` | app (optional) | B2 S3 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | External; replace the application key at the provider |

That is 14 secret entries for the app (12 core, 2 optional secondary-backup)
and 4 for the relay, each host with its own values. Rehearsal-only:
`rehearsal/users-json` (synthetic users for the shared protocol).

Key material deliberately **outside** the sops store:

| Material | Generated by | Backed up | Notes |
| --- | --- | --- | --- |
| Host SSH ed25519 key (= age recipient) | `terrace-secrets host create` on the operator machine | no | Pinned before install; a replacement gets a new one |
| Escrow age key | `terrace-secrets escrow create` | owner's offline copies | Decrypts every host's store |
| Operator SSH keys | each operator | no | Public halves are profile parameters |
| Headscale Noise and DERP private keys | Headscale on first start | relay backup | Network identity; restored, never reset |
| Keycloak realm signing keys, client secret copies | Keycloak in its database | app backup (database) | Inherent: the database holds the client secrets |
| PostgreSQL role password hashes | PostgreSQL | backup (globals) | SCRAM hashes, not values |

Deferred services, to wire when each is enabled in production (6.22):
Documenso `authSecret`, `encryptionKey`, `encryptionSecondaryKey`,
`oidcClientSecret`, `databasePassword`, the recovery user's password,
`signing.p12` and its passphrase; Windmill superadmin password and OIDC client
secret; Vaultwarden `adminToken` and OIDC client secret. Their OIDC clients must
also join the realm template.

Cross-check with the website's production configuration table
(`docs/member-lifecycle.md`, website `dev`): every CMS secret
(`PAYLOAD_SECRET`, `CMS_AUTH_CLIENT_SECRET`, `CMS_AUTH_INTROSPECTION_SECRET`,
`MEMBER_AUTH_INTROSPECTION_SECRET`, `LIFECYCLE_INTAKE_SECRET`) and website secret
(`MEMBER_AUTH_CLIENT_SECRET`, `LIFECYCLE_INTAKE_SECRET`) is covered. Its
non-secret settings are rendered into the same templates from the hostnames
(`CMS_PUBLIC_DEPLOYMENT=1`, origins, issuers, client ids, audiences,
`CONTENT_SOURCE=cms`) or forced by the module after loading the file
(`DATABASE_URI`, `CMS_MEDIA_DIR`, `LIFECYCLE_OUTBOX_DIR`,
`LIFECYCLE_PUBLIC_ORIGIN`, `CMS_PUBLIC_ORIGIN`, `ADDRESS_HEADER`, `XFF_DEPTH`).
The retention variables keep their defaults until the owner decides them;
`CMS_LOCAL_BOOTSTRAP` and `CMS_BOOTSTRAP_*` are refused.

## Operator tool

`platform/production/terrace-secrets` (Python standard library plus `sops`,
`age`, `ssh-to-age` and `ssh-keygen`, all in the dev shell; also built as
`packages.x86_64-linux.terrace-secrets`). It never prints a value, never puts
one in a command argument, and creates plaintext only in memory or in a
private directory on tmpfs that it removes. It refuses to run if its scratch
directory is not tmpfs or ramfs. Decrypting commands read the escrow (or an
operator) identity from `SOPS_AGE_KEY_FILE`.

```sh
export TERRACE_SECRETS_STORE=/private/deployment/secrets   # private, never this repo
terrace-secrets escrow create /offline/terrace-escrow.age     # once; prints only the public recipient
terrace-secrets host create app --role app --stage /private/stage/app   # per host
terrace-secrets generate app                                  # random values, encrypted to host + escrow
terrace-secrets set app tls/certificate < fullchain.pem       # each external value, from stdin
terrace-secrets check app                                     # names and recipients, no decryption
terrace-secrets push app --address HOST                       # over SSH pinned to the host key
terrace-secrets rotate app website/lifecycle-intake           # one generated value
terrace-secrets host create app --role app --stage DIR --replace && terrace-secrets rekey app
terrace-secrets public app backup/primary/sftp-key            # public half, for enrolment
# on a host, as root:
terrace-secrets verify                                        # every secret present, owner, mode, ramfs
terrace-production adopt                                      # once per host: arm the writers
```

`push` sends the ciphertext over SSH with `HostKeyAlias` set to the host name
and a known_hosts file built from the stored host public key, then runs
`sudo terrace-secrets install` on the host. `install` accepts only a sops file
whose values are all encrypted and whose recipients include the host's own
key, writes it atomically (`0600`), re-runs `sops-install-secrets` (which
`try-restart`s the consumers of changed values) and runs `verify`.

## Bootstrap of a brand-new host

1. `terrace-secrets host create <host> --role <role> --stage <private dir>` makes
   the ed25519 host key on the operator machine, records its public key and
   age recipient in the store. This is the "separate per-host Ed25519 host
   key" the [Rescue installer](cloud-rescue-install.md) already stages, so the
   host's identity is known before it ever boots: no trust on first use, and
   SSH to the installed host is pinned to the same key.
2. `generate`, then `set` each external value, then `check`.
3. Stage `secrets.sops.json` with the host key: the installer copies it to
   `/mnt/var/lib/terrace-secrets/secrets.sops.json` (`0600`) beside
   `/mnt/etc/ssh/ssh_host_ed25519_key`. Delete the staging copy of the host
   key after the first boot verifies.
4. First boot decrypts; nothing writes until `terrace-production adopt`.
   `push` is for every later change.

## Rotation of one secret

`terrace-secrets rotate <host> <name>` replaces one generated value in place
(`sops set --value-stdin`), then `push`. The changed templates restart their
units. Secrets whose consumer keeps its own copy (realm client secrets, the
Keycloak database password, the backup repository password, the SFTP key)
are refused by `rotate` with the documented manual procedure above.

## Re-keying for a replacement host

`host create <host> --replace` archives the old public key and makes a new
host key; `rekey <host>` runs `sops rotate --add-age <new> --rm-age <old>`
with the escrow identity: a new data key, the same values, the old host key
removed. Stage it with the new host key as in a bootstrap.

## Revoking a retired host

Re-keying removes the retired key from future ciphertext, but that host could
still read the values it had. If its disk might be exposed (stolen, not
wiped, or a provider disk not yet destroyed), also `rotate` every generated
secret and replace the external ones. Destroy the provider disk and confirm
absence (runbook 6.5).

## Verification

`platform/production-check` (VM) proves: delivery only through this mechanism
(`verify` PASS, 13 secrets and 5 templates in the rehearsal instance); absence
of every value from the system closure, unit files and properties, every
journal, process arguments, files on the root disk outside the databases and
the raw disk (Keycloak's own client-secret copies in PostgreSQL excepted), each
scan first shown to find a planted non-secret canary; `root:root 0400` on ramfs
and nothing left after a reboot; rotation of the intake secret (old value
refused with 403, both services restarted onto the new value, website and CMS
still agree); and a host replacement: backup, original disk destroyed, new host
key, `rekey` from escrow (the retired key can no longer decrypt), restore with
reconciliation, identical data and the lifecycle continuing. The operator
guest's disk and journal hold no value either.
`infra/nixos/production-eval-check.nix` proves the evaluation invariants, and
`platform/production/test_terrace_secrets.py` the tool's own guards.
