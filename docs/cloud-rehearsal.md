# Private two-host cloud rehearsal

The disposable gateway and independent Headscale relay reuse the accepted
native modules and the same synthetic identity and application fixture. The
gateway uses the [reviewed Linux release pair](website-host.md) and the
hash-checked TFC identity producer. Both profiles boot with their writers
inactive. The relay has no K3s. This composition has no public application
hostname or ACME account.

## Evaluate and prepare the installation

On the Linux builder, run the configuration check before building anything:

```sh
direnv exec . nix eval --raw --impure --file infra/nixos/cloud-rehearsal-check.nix
```

Prepare an owner-only local Nix module per leased host, with its verified
whole disk, Rescue hardware configuration without filesystem declarations,
network settings and the run-specific operator's **public** SSH key. Do not
put the provider token, TLS key, browser fixture, or repository credentials
into this module. The controller must bind the leased provider ID, Rescue
host key and actual disk identity before any install command.

The installer profile evaluates the pinned Disko and NixOS outputs. From
the repository root on the Linux builder, supply the verified host inputs:

```sh
args=(--impure --file infra/nixos/rehearsal-config.nix
      --argstr role relay --arg localModule /absolute/private/relay-host.nix
      --argstr privateIPv4 "$REHEARSAL_RELAY_IPV4")
direnv exec . nix eval "${args[@]}" config.boot.loader.systemd-boot.enable
direnv exec . nix eval "${args[@]}" config.fileSystems
```

For the gateway, use `--argstr role gateway` and supply
`--arg webArchive /absolute/reviewed/web.tar.gz`,
`--arg cmsArchive /absolute/reviewed/cms.tar.gz` and
`--argstr tfcSource /absolute/reviewed/tfc-source` in place of `privateIPv4`.
The source must be a checkout of the website at the pinned revision in
`platform/website-release.json`: its identity contract files are hash-checked
against `platform/test-identity-contract.nix`. The gateway profile keeps the
daily CMS sweep timer off at boot like every other writer, and the service
proof arms it after adoption.

The service proof also runs the website lifecycle steps in
`platform/website-gateway/lifecycle_steps.py`: before backup, two applications
(from the host's own address through the gateway, five accepted and a sixth
rate-limited), two approvals into the outbox, and one redemption, sign-in and
automatic link; after the restore, the other (restored, still pending) invitation
is redeemed once, signed in and linked, both members are erased, the sweep timer
runs, and the restored outbox must be owned, written and redacted by the CMS
account. Each step fails with one fixed label (`website-lifecycle-<phase>-<step>`).
These exact scripts are proven in the VM all-services rehearsal
([full-rehearsal.md](full-rehearsal.md)) and ran on Hetzner in lease 5
([result](#lease-5-result-2026-11-20)).
`infra/nixos/rehearsal_services.py` loads that module by a path relative to
itself, so the lease runner must stage the whole repository tree, with both
`infra/nixos/` and `platform/website-gateway/`, not `infra/nixos/` alone. If
`lifecycle_steps.py` is absent the proof stops at import with the fixed error
"lifecycle steps module missing; the whole repository tree must be staged".
The same scripts, with their before and after checks, passed the VM rehearsal at `4348d0b`
(see [full-rehearsal.md](full-rehearsal.md)).
The three matching outputs are `config.system.build.diskoScript`,
`config.system.build.toplevel` and `config.system.build.nixos-install`.
Build each under the shared Linux builder lock with
`direnv exec . nix build "${args[@]}" --no-link --print-out-paths <output>`.
Record the three exact output paths in the private run manifest. The relay
has a separate closure and receives no app archives.

On the matched, leased Rescue host with a working Nix store, copy the three
reviewed closures using host-key-pinned SSH. The following is the direct
install sequence **only after rechecking the leased ID, verified whole disk,
UEFI mode, empty `/mnt` and fresh disposable role**:

```sh
"$DISKO_SCRIPT"
findmnt --mountpoint /mnt --noheadings --output SOURCE,FSTYPE
"$INSTALLER/bin/nixos-install" --root /mnt --system "$NIXOS_TOPLEVEL" --no-root-passwd --no-channel-copy
```

The Disko script destroys and formats the single configured disk. Verify
that the mounted ext4 root and the vfat `/mnt/boot` EFI system partition
belong to the same verified whole disk before running the installer, and that
`/mnt/boot/EFI/BOOT/BOOTX64.EFI` exists after it. Obtain and pin the installed host key independently
before operator SSH. Rescue availability, actual firmware, disk, reboot and
service health are measured provider steps.

## Keep the data and checks private

- Provider ingress and the NixOS global firewall allow operator SSH only.
  The gateway permits identity and the synthetic mail sink on its local
  Podman bridge. Use SSH loopback forwards for gateway and relay HTTPS.
  The relay's local dummy interface carries its synthetic RFC1918 /32 and
  STUN binds there. This requires no provider network and proves no external
  UDP reachability or forced DERP.
- Generate the run CA and the gateway and relay leaves in private operator
  storage. Install each leaf and key in root-owned mode-0700
  `/var/lib/terrace-rehearsal-tls` with the key mode-0400. Install the **public**
  CA as root-owned mode-0644 `/var/lib/terrace-rehearsal-trust/ca.pem` in a
  root-owned mode-0755 directory. The startup guard checks the exact synthetic
  hostnames, server certificate purpose, non-CA leaf and key match before
  nginx starts. Keep the CA key on the operator only.
- Use a bounded isolated operator on the Linux builder. Use a pinned SSH
  local HTTPS forward from an isolated loopback listener on port 443 to
  `127.0.0.1:443` on the gateway. Map the six gateway `*.terrace.invalid`
  names to that listener in the operator environment. Map
  `relay.terrace.invalid` to a separate loopback listener for the relay
  tunnel. Trust only the run CA for this fixture. Keep the accepted browser
  helpers and their private credentials and create/verify receipts in that
  environment.
- Host the encrypted restic repository in separate protected operator
  storage. A pinned reverse SSH forward from each leased host's
  `127.0.0.1:22422` reaches the isolated operator rest-server loopback
  listener. Copy the root-owned mode-0400 repository password to
  `/var/lib/rehearsal-escrow/repository-password`. Backup and check timers
  remain disabled.
- Explicitly run `cloud-rehearsal-inputs.service` once on the original
  gateway, then use the [native rehearsal order](full-rehearsal.md) for
  migration, bootstrap, bounded browser/worker checks (not part of lease 5) and quiescence. Set
  `SSL_CERT_FILE=/var/lib/terrace-rehearsal-trust/ca.pem` for the gateway's
  root-only Python protocol helper and use curl's explicit `--cacert`.
  After verifying the encrypted repository and escrow off both hosts, the
  provider receipt must establish that the originals are gone before the
  fresh restore. Transfer the retained TLS files independently and restore
  the application inputs before starting writers on the replacements.

The gateway contains the native rehearsal's pinned local PVC probe image,
`/etc/rehearsal-probe.yaml` and `rehearsal-cgroups-empty` for the same storage
and writer-stop checks.

The two-host profile covers the gateway and relay install configuration.
It leaves separate automation and peer-topology exercises to the full
native rehearsal or a later explicit cloud composition. The offline check
uses stub releases and evaluates role wiring. Actual provider installation,
private transport, application bootstrap, fresh recovery and resource
absence require the leased run (see the lease 5 result below).

## Cohort shapes

The reviewed `original` or `replacement` input of `infra/cloud/rehearsal-create`
selects one of three shapes. Every shape keeps the same isolation: names
`terrace-r-<run>-<phase>-<role>` (and `-ip`), the label `terrace-rehearsal=<run>`,
only the run's own SSH key and firewall, one IPv4 Primary IP per server and no
IPv6, a create-only preview and saved plan, and cleanup that selects by name
and label, excludes every baseline ID and compares absence with the baseline
across every inventory kind.

| Shape | Input | Hosts | Install | Retire and replacement | Provider use |
|---|---|---|---|---|---|
| Pair | no shape key | app and relay | both roles | yes | leases 1 to 5 |
| Console probe | `"probeOnly": true` (original only) | relay | refused | refused | relay probes |
| Single host | `"roles": ["app"]` (original only) | app | app only | refused | none yet |

The single-host shape is for proving one installable app host on provider
hardware, for example the production profile, without the relay. Its preview
and plan must create exactly the app server and its IPv4. Every review file
now records `roles`, for every shape and phase, and apply refuses a review
whose roles differ from the input; a review from before this field authorizes
only an input without `roles`. Provider actions (Rescue, console, installed
boot), the binding and the disk pin, inspect and install in
`infra/nixos/rescue_install.py` accept only the app. `retire-original` and a
`replacement` phase are refused, so the lease ends with `cleanup`. For every
shape, `retire-original` now also reads the original's reviewed input, review
and dispatch files, and fails closed before deleting anything if one is missing
or altered. The service proof in `infra/nixos/rehearsal_services.py` needs the
pair and refuses a single-host cohort. `relayType` stays in the input, as `appType` does for
the probe, and is unused. This shape is tested offline only (unit and Pulumi
mock tests); it has not been used at a provider.

## Lease 5 result (2026-11-20)

Known-full lease 5 (run `efc9794a`, staged at Womb trunk `bb0d526`, armed 16:12:52
UTC, final audit 17:43:20 UTC, about 90 minutes) passed end to end: all 46 steps finished with
status 0, no guard failed, and everything was torn down. The orchestrator read the
receipts on the builder. It is the first lease to run the service phase, the
backup to a repository on the build machine (off the leased hosts only: not a
storage provider account and not off-site), the retirement of the original pair and the restore onto a
replacement pair on the provider; leases 1 to 4 had proven only create, install and
boot from disk.

- Both pairs (original and replacement app and relay) were installed from stock
  to RAM to disk and booted from it. The service phase (`infra/nixos/rehearsal_services.py`)
  authenticated both hosts, then on the gateway ran PostgreSQL, Keycloak, nginx,
  the CMS and website (explicit migration), the sweep timer, K3s with a retained
  volume probe, Documenso (migrated, bootstrapped, started, health check only) and
  Windmill (migrated, its database role boundary checked, then server and worker
  started with the server's `/api/version` answering and the worker active) and
  Vaultwarden (migrated, started, its bootstrap unit run, `/alive` answering), the
  last two for liveness only; on the relay it ran
  Headscale and nginx with one synthetic user. Browser, signing, automation and
  peer-topology checks are not part of this phase.
- 198 protocol assertions passed before the backup and again after the restore, with
  the website lifecycle steps in both phases (after the restore: the pending
  invitation redeemed, sign-in, link, both members erased, the sweep, the outbox
  redacted).
- Both hosts backed up through the native backup module to an encrypted restic
  repository that was a directory on the build machine, reached by a reverse SSH
  forward from each host (off the leased hosts only; no storage provider account,
  no off-site copy, no drill of recovery after losing the builder); the
  supervisor's check (`restic check --read-data` and a snapshot inventory) passed.
  The originals were retired with scoped absence and baseline-preservation checks.
  The replacement pair restored each role from its snapshot: native database
  adoption, K3s retained volume (the local-retain persistent-volume data under the
  host's own `/var/lib/rancher/k3s/storage`, carried through the backup and copied
  back; the probe's marker was read back, a new write succeeded and the node and
  volume identities matched; it is not a provider volume), and the relay's
  Headscale state with its identity and private HTTPS unchanged. The run's
  certificate authority, TLS files and repository password were not restored from
  the backup: the operator tooling supplied them again, checked by digest.
- Teardown: the final audit recorded all provider resources and scopes off, the
  baseline preserved and the existing server still running; a later read-only
  provider inventory showed only the pre-existing baseline.

Scope: synthetic data on private, temporary hosts. No real members, public DNS,
public certificate authority, mail, payments or public service exposure (the hosts had public addresses with operator SSH ingress only). One run, on one
day, on one choice of server types. Not proven by this lease: anything in production
profile mode (`terrace.production`), ACME, the Keycloak administration tool, real
data migration, cutover, alerting.

## Production lease host (prepared for lease 6)

The owner approved one more lease (lease 6) on 2026-11-26, to install the
**production** app profile on one Hetzner server and exercise its first boot,
adoption, a reboot and the signed deploy path. **Lease 6 has not run.** What
follows is the committed install side only. It has been evaluated and built on
the Linux builder; nothing of it has run at a provider, and no VM has booted
these systems yet. The provider side is the single-host cohort shape above
(`"roles": ["app"]`, tested offline only); the throwaway SFTP endpoint, the VM
rehearsal of the run sheet and the lease driver are separate, later work.

**Hardware.** `nixosModules.hetzner-production-hardware` (`infra/nixos/flake.nix`):
the cloud disk module (GPT, 512 MiB ESP, ext4, systemd-boot with
`canTouchEfiVariables = false`, so the removable `EFI/BOOT/BOOTX64.EFI`) and
`infra/nixos/hetzner-hardware.nix` (the QEMU guest profile's virtio drivers in the
initrd, DHCP on every interface). This is the hardware part leases 3 and 5
installed with the rehearsal profile; see the
[hardware module interface](production-profile.md#hardware-module-interface).

**The builder.** `infra/nixos/lease-host.nix` composes
`infra/nixos/production-config.nix` (role `app`) with that hardware module and a
lease host module: `instance = "rehearsal"`, host name `app`,
`web.terrace.invalid`, `cms.terrace.invalid`, `identity.terrace.invalid`,
`memoryMiB = 15360`, manual TLS (the leaf and key are pushed as `tls/*`) with the
run CA trusted by the CMS, website and expiry check, and **two** trusted deploy
keys, the build machine's and the offline spare the owner keeps (owner decision
2026-11-26; the builder refuses any other count). There is no rehearsal harness:
no synthetic users, no bootstrap administrator, no protocol scripts. Its inputs
are the run's, passed at build time and never committed:

| Input | Meaning |
| --- | --- |
| `disk` | the whole disk the Rescue probe recorded (`/dev/sda` on every CPX server leased so far) |
| `operatorKeys` | the run's installed operator key (not the bootstrap root key) |
| `escrowRecipient` | the run's throwaway escrow key, public recipient |
| `deployKeys` | the two Nix public keys |
| `runCa` | the run CA certificate, as a path |
| `backupPort`, `backupHostKey` (`backupAddress`, default `127.0.0.1`) | the throwaway SFTP endpoint as the host reaches it: the near end of a reverse SSH forward from the build machine, pinned to that endpoint's ed25519 host key. The repository is `sftp:terrace@<address>:app`; the known_hosts line is `[<address>]:<port> <key>`; the SFTP client key is the store's `backup/primary/sftp-key` (its public half from `terrace-secrets public app backup/primary/sftp-key`) |
| `previousWebArchive`, `previousCmsArchive` | the kept `680ad56` release the host is installed at |
| `webArchive`, `cmsArchive`, `tfcSource` | the pinned `dd1aef7` release it is updated to, and the TFC source at that pin for the identity contract |
| `wombRevision` | optional, recorded in each manifest |

Outputs: `install`, a JSON file naming the role, the disk device, and V0's
toplevel, Disko script and installer; `variants`, a JSON file naming the six
toplevels below; `systems`, the evaluated systems (for the evaluation check).
Each variant extends the one the lease runs before it, the way
`platform/production/deploy-test.nix` makes its variants (`extendModules`), so it
differs from the running system by its change alone:

| System | Lease step | Change | What `activate` should do (not run) |
| --- | --- | --- | --- |
| V0 | install | the production app at `680ad56` | installed by `rescue_install.py` |
| V1 | plain switch, then rollback | an `/etc` marker | switch |
| release | release change with migration | `680ad56` to `dd1aef7`, its migration list | switch; down until the CMS migration |
| V2 | boot into a new generation | one more initrd driver (`virtio_input`) | refuse without `--boot`, then `--boot` and a reboot |
| V3 | a switch that restarts sshd | `services.openssh.settings.LogLevel = "VERBOSE"` (sshd's restart trigger is its config file) | switch, restarting sshd |
| V4 | a failed switch | a unit wanted at boot that fails and restarts, as in the update drill | register the generation, fail (exit 4) |

The evaluation check (`infra/nixos/production-eval-check.nix`) asserts that V0
to V3 and the release change break no production invariant and V4 breaks exactly
one (its unit); that the kernel, initrd and modules are V0's in V1 and the
release change and differ from it only in V2's initrd; that only V3 changes the
sshd configuration; the manifests' release and migration list; the two trusted
keys with Nix's defaults otherwise; the backup repository and its pin; and that
the lease host with the production instance, free-form sshd configuration, an
extra unit at boot, password login, an extra port, an extra service or a
container daemon is rejected, as are builder inputs with one deploy key, a
repeated key, a privileged or non-IPv4 backup endpoint, another host key type or
a CA given as a string.

**The host key and the plan.** `terrace-secrets host create app --role app`
stages the host's private key and stores its public half. After
`rescue_install.py ... --inspect-disk` has recorded `<phase>-app-disk.json`,
`infra/nixos/lease_install_plan.py` writes, in the lease directory, exactly what
`rescue_install.py --install-approved-sha` checks: `<phase>-app-install.json`
(the disk receipt's digest and V0's three store paths),
`<phase>-app-installed-ed25519` (the staged private key, byte for byte) and
`<phase>-app-installed-known-hosts` (`<IPv4> ssh-ed25519 <key>`, from the
store's public half at the receipt's address). It refuses a staged key that is
not the stored one, the installer's own key, build outputs for another disk than
the receipt's (the Disko script formats the device it was built for) or another
role, store paths that are not a system, a Disko script and an installer, and
any existing file or link at an output path (each file is created new, mode 0600,
never through a link). It checks that the store paths exist before writing
anything, validates the plan with `rescue_install.checked_plan` and prints the
digest to approve; if a step after the first write fails, it removes exactly the
files it created (the key copy overwritten first), so a rerun starts clean. The installer writes that key to
`/mnt/etc/ssh/ssh_host_ed25519_key`, the only host key the production sshd and
sops use, and `terrace-secrets push` pins the same stored key, so no step trusts
a key on first use. `rescue_install.py` itself is unchanged.

```sh
# Build machine, in the run directory's terms (paths are examples):
terrace-secrets host create app --role app   # prints the staging directory, e.g. $STAGE
# Repository root, shared lock; the inputs file is private to the run:
nix build --impure --option builders "" --out-link "$RUN/lease" \
  --expr '(import ./infra/nixos/lease-host.nix (import /run-dir/lease-inputs.nix)).install'
nix build --impure --option builders "" --out-link "$RUN/variants" \
  --expr '(import ./infra/nixos/lease-host.nix (import /run-dir/lease-inputs.nix)).variants'
# After the disk receipt exists:
python3 -B infra/nixos/lease_install_plan.py "$LEASE" original app --outputs "$RUN/lease" \
  --host-stage "$STAGE" --host-public "$TERRACE_SECRETS_STORE/hosts/app/ssh_host_ed25519_key.pub"
python3 -B infra/nixos/rescue_install.py "$LEASE" original app --install-approved-sha <printed digest>
```

Unit tests (CI): `python3 -B infra/nixos/test_lease_install_plan.py -v`, which
also runs `rescue_install.install` on the written files through its authority,
disk receipt, plan and installed-key stages (it stops at the first remote
command).

Ran on 2026-11-26 on the Linux builder, with throwaway inputs (public halves
only kept): the evaluation check (PASS, including the lease host, its variants
and the Hetzner hardware module); builds of `install` and `variants` (each of
the six system closures about 2.6 GiB, the Disko script's closure 357 MiB, the
installer's 51 MiB); and `lease_install_plan.py` against those real outputs,
with a throwaway secrets store (`terrace-secrets host create app --role app`)
and a stand-in disk receipt at a documentation address: the plan passed
`rescue_install.checked_plan`, the installed key equalled the stored host key
and the pin carried it. The comparison of derivations with trunk showed every
flake check, VM proof and rehearsal driver unchanged. Not run
anywhere: an install of these systems, a boot (VM or provider), DHCP on the
production closure, any activation of a variant, the backup endpoint, and the
lease itself.

## Throwaway SFTP repository for the production lease host

The production backup module accepts only an `sftp:` restic repository with a pinned known-hosts entry, so a lease
that runs the production profile needs an SFTP repository the leased host can reach. `infra/cloud/rehearsal_sftp.py`
provides one on the build machine for the length of the lease; `infra/cloud/rehearsal_sftp_probe.sh` proves its
confinement. Lease 6 was approved once on 2026-11-26 and has not run; nothing here has touched a provider.

**Design.** The client key will sit on a rented machine on the public internet, so the endpoint must confine a
holder of that key by construction, without root. A user-level `sshd` with `ForceCommand internal-sftp` cannot: there
is no chroot, so the account's whole home would be readable. The endpoint is instead `rclone serve sftp` from the
repository's pinned nixpkgs (`github:NixOS/nixpkgs/31ecbaca1bdefa45dffc409f0250bd8039ea0010#rclone`, 1.75.2;
`restic` and `openssh` come from the same pin). It is a userspace SFTP server with exactly one filesystem root: it
has no shell or command execution, accepts only session channels with the `sftp` subsystem, rejects port forwarding,
resolves every path (`..`, absolute) inside the served root, does not follow symlinks, does not create symlinks or
hard links and cannot rename out of the root. It authenticates with the single key in the authorized-keys file (it
advertises a password method but has no user or password configured, so every password is refused; the user name is
ignored, so any name reaches the same root) and offers one ed25519 host key. It is started as a transient
`systemd --user` unit with a hard `RuntimeMaxSec`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=tmpfs`,
`MemoryMax`, `TasksMax`, `LimitNOFILE`, AF_INET/AF_UNIX only, and a mount namespace in which the served root is an
empty read-only directory holding only the repository (`app`, writable) while the run directory shows only the host
key and keys (read-only) and the log. The server's own rooting and the namespace are independent layers, and the
probe runs against both (the second with the namespace off). `start` also verifies the namespace after the server
is up, through `/proc/<pid>/root`: the served root must hold exactly `app` and the run directory view exactly the
host key, the authorized keys and the log directory; if not, or if it cannot be checked, it stops the unit and
fails.

**Size limit.** Nothing without root can set a disk quota, so a companion unit (`<server unit>-watch`) measures the
repository plus the server log every 2 seconds and stops the server when the total exceeds `--limit-mib` (default
2048) or when the number of files and directories exceeds `--max-entries` (default 200000). Every file and directory
counts at least 4096 bytes, so empty files and directories use up the limit too (they cost real storage). The two
units are bound to each other: the watcher has the same runtime limit, ends when the server ends, and the server has
`BindsTo=` the watcher, so neither outlives the other. The server's log is at error level and rotates at 5 MiB
keeping two old files. A tripped limit is recorded in `sftp-limit-tripped.json`; `status` shows the repository and
log sizes, the counted bytes and entries, the limits and whether one tripped, and the receipt records both limits.
The limit is checked between measurements, so a client can overshoot it by about what it can write in 2 seconds; it
is a bound on exposure, not an exact quota. Measured on loopback, a client wrote 64 MiB in 0.7 s against a 16 MiB
limit (about 48 MiB over) before the server stopped; a real reverse forward over the internet is much slower, but
choose `--limit-mib` with that margin. Put the run directory on scratch storage with ample free space that is not
snapshotted (a deleted file's space stays allocated while snapshots hold it); the limit protects the disk, it does
not make snapshotted storage a good place for it.

**Commands** (run from a checkout on the build machine; `RCLONE`, `KEYGEN` are the pinned store paths):

```sh
python3 -B infra/cloud/rehearsal_sftp.py prepare --directory RUN --ssh-keygen KEYGEN --client-public-key CLIENT.pub
python3 -B infra/cloud/rehearsal_sftp.py start   --directory RUN --rclone RCLONE [--seconds 7200] [--limit-mib 2048] [--max-entries 200000] [--port N]
python3 -B infra/cloud/rehearsal_sftp.py forward --directory RUN [--host-port N]
python3 -B infra/cloud/rehearsal_sftp.py status  --directory RUN
python3 -B infra/cloud/rehearsal_sftp.py stop    --directory RUN
bash infra/cloud/rehearsal_sftp_probe.sh RUN CLIENT_PRIVATE_KEY EVIDENCE_DIR OPENSSH_BIN_DIR RESTIC
```

`RUN` is a caller-supplied 0700 directory. `prepare` creates a fresh ed25519 host key, an authorized-keys file with
exactly the one supplied public key, and an empty `app` directory, and refuses any existing file. `start` prints
the receipt as JSON (units, pid, port, host key, `knownHosts` as `[127.0.0.1]:<port> ssh-ed25519 ...`, repository
`sftp:terrace@127.0.0.1:app`, expiry, size limit) and saves it in `RUN`. The default runtime is 2 hours (maximum 4
hours, the lease expiry): the endpoint is needed from the first backup to teardown of a lease planned at about 90
minutes, and the client key is only as safe as the time it is live. `forward` prints the option for the operator's own ssh to the
host, for example `-R 127.0.0.1:<port>:127.0.0.1:<port>`: it binds on the host's loopback only and ends at the
endpoint's loopback port; the helper never opens it. Run the probe against the started endpoint before the real
backup. `stop` stops both units and verifies that they are inactive, no process remains and the listener refuses
connections; if it is forgotten the runtime limit ends the units. If `start` is killed between starting the units
and writing the receipt, stop `terrace-lease-sftp-*` by exact unit name with `systemctl --user stop`.

**After `stop`, delete `RUN`** (it holds the private host key, the repository data and the log) **and the client
private key**, by exact path, and verify both are gone (`test ! -e RUN`, `test ! -e CLIENT_PRIVATE_KEY`). Nothing is
written outside `RUN`, no sudo is used, and private key material is never printed. Unit tests:
`python3 -B infra/cloud/test_rehearsal_sftp.py -v` (also in the GitLab/GitHub `check-supervision` job).
`infra/cloud/rehearsal_sftp_nonamespace_check.py` removes the namespace from a prepared run directory and passes only
if `start` refuses.

**Known limits.** The unit takes a loopback port on the build machine while it runs. The helper needs a systemd
user manager and a `/var/empty` directory on the build machine. The user name is ignored, and `chmod`/`chown`
requests are accepted and ignored.

**What ran** (on the Linux build machine, throwaway client key, no provider): restic `init`, `backup`, `snapshots`,
`check` and `restore` through the endpoint with the production invocation shape (`sftp.args` with `IdentitiesOnly`,
`BatchMode`, `StrictHostKeyChecking=yes` and a pinned known-hosts file), restore checksums equal; refused: shell,
exec, `-L`, `-R`, agent/X11, other subsystems, reads outside the repository by absolute path, `..`, the endpoint's own
key files and a symlink planted inside the repository, client-created symlinks, hard links, renames out of the root,
writes outside, an unlisted second key, password authentication and a non-ed25519 host key; the namespace check
passed on a real start and `start` refused with the namespace removed; a write past a small size limit stopped both
units within seconds, and so did 6000 empty files against a small limit and an entry cap; `stop` left no listener, process or unit; a 300 s runtime limit ended the unit by itself.
Evidence is kept on the build machine, not in the repository.

**What was only read or evaluated:** the production profile's sshd settings were read in `platform/production/base.nix`
and evaluated on the build machine for the lease host of the unmerged lease-host change (the V0 system, repository
`sftp:terrace@127.0.0.1:app`, port 22433, no failed assertion out of 1624): sshd sets no `AllowTcpForwarding`,
`PermitListen` or `Match`, and `GatewayPorts` is `no`, so the OpenSSH defaults apply (forwarding on, remote forwards
bound to the host's loopback). That evaluation is not the full production evaluation check. Nothing was exercised on
a booted host.
