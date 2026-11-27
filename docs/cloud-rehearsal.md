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
(`"roles": ["app"]`, tested offline only) and the backup repository is the
[throwaway SFTP repository](#throwaway-sftp-repository-for-the-production-lease-host)
below (`infra/cloud/rehearsal_sftp.py prepare|start|status|stop|forward`; defaults
7200 s runtime, 2048 MiB, 200000 entries); the VM rehearsal of the run sheet is
separate work, and the lease driver is [below](#production-lease-driver-lease-6).

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
| `backupPort`, `backupHostKey` (`backupAddress`, default `127.0.0.1`) | the throwaway SFTP endpoint (`rehearsal_sftp.py`, below) as the host reaches it: the near end of the reverse SSH forward `rehearsal_sftp.py forward` prints, pinned to the endpoint's ed25519 host key (the `knownHosts` key in the receipt `start` prints). The repository is `sftp:terrace@<address>:app`, as that receipt names it; the known_hosts line is `[<address>]:<port> <key>`; the SFTP client key is the store's `backup/primary/sftp-key` (its public half from `terrace-secrets public app backup/primary/sftp-key`) |
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
production closure, any activation of a variant, a backup from these systems to the SFTP
endpoint, and the lease itself.

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

## Production lease driver (lease 6)

`infra/cloud/rehearsal_lease_driver.py` sequences the existing lease tools for one single-host production app lease:
run sheet steps 0.3 to 1.7, then the host-steps driver `infra/nixos/lease_host_steps.py` (`pin`, then `run` for steps
2.1 to 6), then teardown (step 7). It adds no provider or host logic of its own: every step is a subprocess of an
existing tool with its documented arguments, and the driver writes only the inputs those tools read (the bootstrap
root key, the foundation and cohort inputs, the proof nonce through `rescue_install.write_proof_nonce`, and the
private `rehearsal_lease.py` copy the timer runs).

**Status.** The driver has run only offline: unit tests with a fake tool layer and real child processes, and a
mutation check (below). It has never contacted a provider or a host. Lease 6 was approved once on 2026-11-26 and has
not run. The driver is usable only once `infra/nixos/lease_host_steps.py` is on trunk (it lands with the host-steps
change); until then `plan` refuses with that message.

**Fixed choices.** CPX42 in nbg1, `debian-13` then kexec, one IPv4, no IPv6; the single-host cohort
(`"roles": ["app"]`); the provider firewall allows TCP 22 from the build machine's /32 only; the lease expires at
`now + 14400` and its cleanup timer is armed before anything is created; no build inside the lease; a lease that
stops early is finished (no second attempt in the same run). The host steps start their own SFTP endpoint and
forwards; the driver does not.

**One change to an existing tool.** `infra/cloud/rehearsal_lease.py availability --directory D --credential-file F
--server-type cpx42 --location nbg1` is new. It makes two GET listings (server types by name, datacenters), writes
nothing (no lock file, no manifest) and exits 0 only if the type is listed as available in a datacenter of that
location. The driver calls it at 0.8, before anything is armed, and again right before the cohort preview. **Passing
it does not guarantee that the create succeeds**: stock can change in between, and a create that fails after its
dispatch still ends in the documented unresolved-dispatch state, in which cleanup cannot verify absence and disarm
stays refused (see the table below).

**What runs as root.** Only these, through `sudo -n` (sudoers must allow them without a password): `systemd-run` of
the five bounded writer units `terrace-cloud-<run>-{foundation,original}-{preview,apply}` and
`terrace-cloud-<run>-original-app-apply`, which drop to the invoking user (`--uid`), with `RuntimeMaxSec=230`,
`KillMode=control-group` and `NoNewPrivileges`; `systemctl stop` of those five exact unit names in teardown; and
`rehearsal_timer.py`'s own fixed calls, `systemd-run` of the expiry timer `terrace-cloud-<run>.timer` (its service
runs as the invoking user) and `systemctl stop` of that timer and its service. `rehearsal_escrow.py`'s sudo calls are
not used. No sudo argument comes from an input file: the unit names derive from the validated 32-hex run id, the
user from the process's own uid, and the rest are fixed. The driver itself refuses to run as root.

**Environment.** Every child gets exactly `PATH` (from `--child-path`, which must contain `sudo` for the timer tool,
`ssh`, `ssh-keygen`, `nix`, `bash` and `curl` for the tools), `HOME`, `USER`, `LOGNAME`, `LANG=C.UTF-8` and `TZ=UTC`,
nothing inherited. Python tools run with `-E -s -B` (not `-I`: it implies `-P`, which drops a tool's own directory
from `sys.path` and breaks every sibling import). Every executable is given by absolute path and resolved once at
plan time to its real path, with its sha256 when it is outside the Nix store. Every directory in `--child-path` and
`--unit-path` is resolved too: a store path by its real path (a system profile directory such as
`/run/current-system/sw/bin` resolves into the store, so a profile switch changes it), any other directory by a
digest of its entries (each name's target, size and modification time). Those, the child environment and the host
steps' own step list are part of the approved digest, and `run` refuses if any differs. Use only root-owned
directories in `--child-path`: the timer tool calls `sudo` by name. Every child runs in the run directory.

### Before the plan

On the build machine, as the operator, in a private 0700 directory named by a fresh 32-hex run id (outside any Git
checkout, on scratch storage that is not snapshotted), with `umask 077`:

1. Stage the revision: `git archive <rev> | tar -x -C STAGE`. Compute its digest on both machines from the same
   archive and compare: `python3 -B STAGE/infra/cloud/rehearsal_lease_driver.py manifest STAGE`.
2. Build the kexec archive from `STAGE/infra/nixos/kexec-installer.nix` (see
   [cloud-rescue-install.md](cloud-rescue-install.md#pinned-debian-to-ram-bootstrap)) and record its sha256.
3. `python3 -B STAGE/infra/nixos/lease_host_steps.py prepare RUN ...` (step 0.6), then build `install` and
   `variants` with out-links `RUN/lease` and `RUN/variants` (step 0.7, under the shared lock).
4. Copy the provider token by file to `RUN/provider-token` (mode 0600). Never print it.
5. `python3 -B STAGE/infra/cloud/rehearsal_lease_driver.py stage-project --run-directory RUN --source STAGE --bun BUN`
   (copies the reviewed creator with 0600 files and runs `bun install --frozen-lockfile --ignore-scripts`, which
   fetches the locked packages).

### Plan (contacts nothing)

```sh
python3 -B STAGE/infra/cloud/rehearsal_lease_driver.py plan --run-directory RUN --source STAGE \
  --python /abs/python3 --revision <40-hex> --source-manifest-sha256 <digest> \
  --kexec-archive <store path>/nixos-kexec-installer-x86_64-linux.tar.gz --kexec-archive-sha256 <sha256> \
  --ssh-cidr <build machine IPv4>/32 --unit-path <pulumi bin>:<node bin> --child-path <dirs> \
  --expect-baseline servers=N,primary_ips=N,ssh_keys=N \
  --ssh-keygen /abs --nix /abs --nix-store /abs --rclone /abs --restic /abs --sops /abs --terrace-secrets /abs \
  [--sudo /run/wrappers/bin/sudo] [--curl /abs] [--ssh /abs] [--queue-idle-command /abs] [--start YYYY-MM-DDTHH:MM]
```

It refuses (exit 2) unless: it is not run as root; the run directory is fresh and private and no other lease beside
it is open (a `<run>.open` pointer or a lease directory without `lease-closed.json`); the token copy is a 0600 file in
the run directory (it is never opened by the driver); the host-steps `prepare` layout is complete and was made for
this revision; the staged tree's manifest equals the approved digest and contains `lease_host_steps.py`; every tool
resolves; the staged creator equals the tree's (0.4); the kexec archive is exactly the output of this revision's
`infra/nixos/kexec-installer.nix` (evaluated offline), its digest matches and its path names no diagnostic build
(G5); the install and the seven variants are valid store paths, rooted by `RUN/lease` and `RUN/variants`, so nothing
is built inside the lease (G6); free space suffices; no `terrace-cloud-*` or `terrace-lease-sftp-*` unit is loaded;
and the planned lease, from start to the timer's own cleanup after expiry (4.5 h worst case), does not overlap 03:30
UTC +-15 min or 22:25 to 03:10 UTC (G7). The latest start is therefore 17:55 UTC and the earliest 03:45 UTC; the
window is checked again just before arming, over the lease plus the timer's cleanup. Any unexpected error while
checking also exits 2 with nothing started. The plan prints the expected baseline counts, every prestaged store path,
the kexec archive, each resolved tool, the children's environment, who runs what as root, the exit codes, the cost
(expected about 0.55 USD, cap 5 USD; if the driver and the timer both failed, up to the monthly price until
deleted), the expiry, every check (the heavy-queue check shows `NOT MADE` when no command was supplied) and the
ordered steps, each with its exact command (`{name}` values are filled at run time from the step named in its guard),
guard, expected observation, evidence and stop condition, with the host steps' own list under step 2.1-6. It writes
`RUN/lease-plan.json`; its last line is the digest to approve.

**What a person checks before approving:** the revision and manifest digest are the reviewed ones; the kexec digest
is the one recorded at build time; the SSH source is the build machine's own /32; the expected baseline is the
current provider inventory (the existing server, its addresses and key); the store paths are the ones built for this
run; the window and expiry; the heavy-queue line; the tools and environment; and that every command names only the
run directory, the staged tree and the expected tools.

### Run

```sh
python3 -B STAGE/infra/cloud/rehearsal_lease_driver.py run <the same arguments> --approved-plan-sha <digest>
```

`run` recomputes the plan and refuses if the inputs, tools, steps or teardown differ from the approved file. It then
creates the `<run>.open` pointer beside the run directory and executes the steps in order, recording each command
(paths only), exit code, duration and output (read with a cap while the child runs) in `RUN/driver/`. It stops at
the first non-zero exit or failed guard, with no retry, and goes to teardown (G1). Before each step after 1.1a it
requires the step's bound plus 1500 s before expiry. It refreshes the provider binding every 40 s while pin,
dispatch, verify, inspect, plan and install run (a failed refresh stops the running step), and stops the refresher
before any step that takes the lease lock. SIGINT, SIGTERM and SIGHUP only set a flag and record the signal (the
handler writes nothing); the main flow reports them at the end. The running child is then
stopped (SIGTERM to its process group, a grace period, SIGKILL; the host steps get 240 s for their own clean-up) and
its real exit status is kept, its output is still read while it shuts down (a child that prints a lot on SIGTERM is
not mistaken for a hung one); further signals are ignored. A child that exits while a descendant still holds its
output pipe is reported with its own status and the descendants in its group are stopped.

Exit codes: 0 pass; 1 a step failed and every clean-up was verified; 2 inputs refused before anything was armed;
3 escalate: some clean-up is not verified (the provider side, the host steps' local clean-up, the SFTP endpoint, a
child the driver could not verify gone, a unit, or a deletion), recovery commands are printed first, before any
receipt is written; 4 interrupted and every clean-up verified. The code comes from the state, not from whether a
receipt could be written; an unexpected error after the run started also exits 3. The host steps' codes are recorded
with their meaning: 0, 1, 2 and 4 mean their own clean-up was verified; 3, a death by signal (including a SIGKILL
after the grace) or no status make the driver exit 3.

**Teardown** has one entry, which no exception or signal can bypass, and runs whenever the lease was prepared or its
arming dispatched; every sub-step is guarded on its own and receipts are best effort:

1. wait up to 240 s for a running writer unit to finish, then `systemctl stop` the five units by exact name and read
   each as literally `inactive` or `failed` (a failed read or anything else is unknown, and unknown is never
   inactive); wait for any child the driver could not verify gone (if one stays, the run exits 3);
2. stop the SFTP endpoint if its receipt shows it still started;
3. `rehearsal_lease.py cleanup` (three absence reads);
4. an independent read-only inventory (`rehearsal_lease.py prepare` in a fresh `RUN/audit-*` directory under a new
   run id) that must equal the lease's baseline;
5. the final audit: `closed.json`, the newest absence receipt (this run, three reads, baseline preserved, no
   unresolved dispatch), the inventory, the read-only inventory of 0.8 against the lease's baseline, and the writer
   units inactive;
6. only then `rehearsal_timer.py disarm`, and the timer and its service read inactive afterwards;
7. deletion by exact path inside the run directory (below), a check that no unit of ours remains, the closing
   receipt `RUN/lease-closed.json`, and removal of the pointer.

The inventory and the audit run before disarm (a change from the run sheet's order), so the timer stays armed until
absence has been confirmed twice.

**What is deleted when.** Deletion attempts every path and reports each failure (a failure means exit 3).
- Nothing was armed (a failure or interrupt before 1.1a): the token copy and every key, escrow, deploy-key, store and
  CA file this run holds are deleted, and only then is the run called clean.
- The provider side is not verified (escalate): the token copy, `rehearsal_lease.py`, `lease.json`, `lock` and the
  create, dispatch, action and owned receipts are kept, because the armed timer and `resume-teardown` need them; the
  key, escrow, deploy-key, store, CA and Pulumi secret material is deleted, because no recovery step needs it; the
  recovery text lists both.
- The provider side is verified but a local clean-up is not (host steps exit 3, a failed SFTP stop, a child not
  verified gone): the token copy and all key, escrow, deploy-key, store and CA material are deleted; `sftp/`,
  `host-steps/` and the evidence are kept for diagnosis, and the text lists the local checks to make (units, the
  endpoint, forwards).
- Everything verified: all of it is deleted, including `sftp/` and `host-steps/`.

### Interruption and failure analysis

| State when interrupted or failed | What teardown does | Ends | If teardown itself fails |
|---|---|---|---|
| Before 1.1a: checks, bootstrap key, inputs, availability, read-only inventory, window | Nothing armed or created: unit check, deletion of the token copy and all run secrets, closing receipt, pointer removed | exit 1 or 4 | A deletion or unit check that fails: exit 3, nothing at the provider |
| During 1.1a `prepare` | If `lease.json` was written: cleanup finds nothing, inventory, audit, disarm (no timer yet) | verified-clean | Exit 3; nothing was created |
| 1.1b arm to 1.2a preview | Cleanup (nothing owned), inventory, audit, disarm stops the timer | verified-clean | Exit 3, timer armed; it fires at expiry and finds nothing |
| During 1.2b foundation create | Waits for the apply unit (up to 240 s). If it wrote `foundation-created.json`, cleanup deletes the key and firewall | verified-clean | Cut off or failed after dispatch: unresolved dispatch, **exit 3, timer armed** (row below) |
| 1.3a to 1.3c (foundation exists) | Cleanup deletes the key and firewall | verified-clean | Exit 3, timer armed; expiry cleanup deletes them |
| During 1.3d cohort create, including a create refused for stock after the availability check | As 1.2b: wait, then cleanup deletes server, IPv4, firewall, key | verified-clean if `original-created.json` was written | **Unresolved dispatch: exit 3, timer left armed.** The person: let the timer fire at expiry (its cleanup deletes anything run-owned it finds), then run `resume-teardown --delete-after-person-check`: it stops the SFTP endpoint if needed, checks the writer units, runs the independent inventory and requires it to equal the baseline and both timer units to read `not-found`, and only then deletes the run material, writes `lease-closed.json` and removes the pointer. If the inventory shows a run resource, delete only `terrace-r-<run>-…` resources in the console and record it |
| 1.4 to 1.6b (server exists) | Refresher stopped; the child's process group stopped; cleanup deletes server, IPv4, firewall, key | verified-clean | Exit 3, timer armed; expiry cleanup deletes them |
| 1.7a boot, 1.7b TCP wait, 1.7c, 2.0 | Wait for the boot unit, then cleanup | verified-clean | As above |
| 2.1 to 6 host steps | SIGTERM to the host steps and up to 240 s for their clean-up; their real exit status is kept through further signals; the driver stops the SFTP endpoint itself if its receipt is still open; then cleanup | exit 4 or 1 when the host steps report 0, 1, 2 or 4 | Host steps report 3, die by signal or are SIGKILLed after the grace: exit 3 after the provider side is verified and disarmed; token and secrets deleted, `sftp/` and `host-steps/` kept |
| Any signal while stopping a child or during teardown | Recorded and ignored | as above | — |
| A receipt, transcript or closing write fails (disk full) | Ignored for behaviour; teardown continues to cleanup; recovery text printed before the closing writes (a fixed minimal text if the full one cannot be built) | from the state | — |
| A teardown child cannot be started, raises, or is not verified gone | Read as a failure, never as success: no disarm after a failed cleanup, inventory or disarm child | exit 3 | `resume-teardown` |
| An unexpected error in the driver after the start | The single teardown entry still runs; `main` maps an error that escapes to exit 3 with the recovery text | exit 3 unless verified | — |
| Driver killed hard (SIGKILL, lost machine) after 1.1b | Nothing runs; the transient timer fires at expiry and runs cleanup. A reboot of the build machine loses the transient timer | timer-clean, unverified by the driver | The `<run>.open` pointer stays, so no new plan passes; a person runs `resume-teardown` |
| Teardown: a writer unit not read inactive, cleanup fails, inventory differs, final audit fails | No disarm; token and recovery files kept; secrets deleted; recovery commands printed | **exit 3**; the timer is read and stated as found: ARMED, NOT armed (for example a baseline mismatch at 1.1a, before arming) or UNKNOWN | `resume-teardown` |
| Teardown: disarm fails, or the timer is not read inactive after it | Token and recovery files kept | exit 3, the timer stated as read | Check the timer by hand, then `resume-teardown` |

### Recovery for a lost runner

```sh
python3 -B STAGE/infra/cloud/rehearsal_lease_driver.py resume-teardown --run-directory RUN --source STAGE
```

An unexpected error in `resume-teardown` once a lease exists exits 3, never 2.
This is the documented recovery sequence (controller cleanup, the final audit, then disarm) with the independent
inventory and the final audit before disarm, the deletion policy above and the closing receipt. It exits 0 when
everything verified, 3 otherwise (the commands are printed). After an unresolved dispatch, see the table row above.

### Offline proof

```sh
python3 -B infra/cloud/test_rehearsal_lease_driver.py -v   # also in the check-supervision CI job
python3 -B infra/cloud/test_rehearsal_lease.py -v          # includes the availability action
python3 -B infra/cloud/lease_driver_mutants.py             # each mutant must fail the tests
```

The tests replace every tool with a fake layer that records each call with its arguments and environment, writes the
receipts the real tool would write, and can fail, hang or deliver real signals at any step; some steps are handed to
the real runner with real child processes (a sleeper past its bound in a step and in cleanup, host steps that trap
SIGTERM and report 3 through two signals, host steps that ignore SIGTERM and are killed, a grandchild holding the
pipe, output past the cap, a process group that cannot be signalled). They pin the happy-path call sequence as a
literal list; fail every step and every guard in turn; interrupt each phase; fail every teardown sub-step, every
unit read and every closing write; check the deletion cases, the G7 window at its edges and across midnight, the
re-check at arming with the timer's tail, G5 and G6 refusals, the open-lease refusals, root refusal, that the plan
digest binds steps, teardown, tools and host steps, that no step is retried, that planted token and key values appear
in no argument, environment, log or receipt, and that files outside the run directory (and behind links inside it)
survive deletion; and check the bounded units against what `rehearsal_apply.require_run_unit` requires. Not
verified: anything at a provider or a host, the real tools under the driver, `sudo` and `systemd-run` on the build
machine, and the timings.

**A possible false escalation that cannot be tested offline.** The driver calls a child "gone" only when its whole
process group is verified gone, by signalling the group. A bounded writer step runs `sudo`, which may keep running
as root; if the operator account is not allowed to signal that process group, the driver cannot verify it and
reports "the child or its process group could not be verified gone" (or "a child the driver started (for example a
sudo client) is not verified gone"), so a run whose every other check passed exits 3. This is the safe side and is
accepted. What the operator does: read the closing summary (`problems` names the step); check
`ps -o pid,pgid,user,args -g <pgid>` shows nothing and `systemctl show terrace-cloud-<run>-*` reads inactive; if the
provider side is reported verified and the timer disarmed, nothing remains at the provider, and only the local check
is left; then remove the `<run>.open` pointer by hand.
