# Host updates: deploying a new revision, and going back

Written 2026-11-24 against Womb `trunk` at `d6d7d1c`. This is owner runbook gap
G-37 (step 9.7, updates): how an operator puts a new Womb revision or a new
website pin on a running production host, in what order relative to secrets, TLS,
identity and backups, and what can and cannot be undone when it goes wrong.

**Status of this document.** It was written by reading the modules, the tools and
the VM checks at trunk. Nobody has run it as a written procedure, on any host. No
production host exists, and no production-profile host has ever run on a provider:
the only provider runs were synthetic rehearsals of the rehearsal profile on
temporary Hetzner hosts (lease 5, [cloud rehearsal](cloud-rehearsal.md#lease-5-result-2026-11-20)),
and none of them updated a running host. No test anywhere switches a running
production-profile host to a new system closure, migrates data written by an
older release, or rolls a generation back. Every step below says what evidence
exists for it; most of it is "not exercised anywhere". Do not read "the VM check
for the new pin passed" as "this update is safe": the VM checks prove the new
revision on a fresh host, not the move to it.

**Update of 2026-11-24: a deploy path exists, opt-in.** Option 1 of owner decision
U0 (signed closures, section 1.2) is now built: an option that makes a host trust a
build machine's public signing key, a manifest in every production system, the
commands `terrace-production activate` and `terrace-production rollback`, and an
operator-side helper that signs and copies. It is off unless the private host
module sets the option. **The owner has not chosen it**: U0 is still his decision,
and options 2 and 3 remain open. Section 1.5 describes it; the steps below say where
it replaces a hand-typed command. Its proof is a VM rehearsal of the production app
role, `platform/production/deploy-test.nix` (synthetic data), which updates a
populated host from one website release to the next, including a real CMS
migration, and rolls back; it has not run on any real host. Where the document
below still says "not exercised anywhere", that is true outside that proof.

Evidence levels, as in [restore promotion](restore-promotion.md) and
[credential rotation](credential-rotation.md): **VM rehearsal (synthetic data)**,
with the test file and what it asserts; **unit test**; **READ** (read from code,
never run); **RAN** (a local `nix eval` made for this document, no build, no VM);
**not determined from the repository**; **not exercised anywhere**.

Terms, each defined once:

- **Revision**: a Womb commit. **Pin**: the website release Womb names in
  `platform/website-release.json` (`revision`, the two archive hashes, the
  migration ledger) and the realm producer it names in
  `platform/identity-contract.json`. A **re-pin** changes them.
- **System closure** (or **toplevel**): the store path
  `config.system.build.toplevel` built for one host from one revision, one private
  host module and, for the app role, the two kept release archives and the TFC
  source ([production profile](production-profile.md#building-a-host)).
- **Generation**: one entry of the host's system profile
  `/nix/var/nix/profiles/system`. **Activation**: running a toplevel's
  `bin/switch-to-configuration`. Both are standard NixOS mechanisms; this
  repository neither wraps nor tests them on the production profile.
- **Migration gate**: the CMS start check `requireMigration` in
  `platform/website.nix`: the CMS, its sweep and its operator tools start only
  when `/var/lib/terrace-cms/migrated-revision` holds exactly the pinned
  revision. The website is **not** gated (H7). **Migration**:
  `terrace-cms-migrate.service`, which writes that marker only after the bundle's
  `bin/migrate` exits 0.
- **Restart after quiesce**: the commands that undo step 6's stops, defined once
  in step 6 and used by every failed branch before activation.
- **Writers**, **adoption**, **store**, **escrow**, **push**: as in
  [restore promotion](restore-promotion.md) and [secrets](secrets.md).

## 1. Scope, preconditions and the owner's decisions

### 1.1 When this applies

To an **adopted** production host (`terrace-production status` prints `adopted
<time>`) of either role, when:

- a new Womb revision changes the system (Nix inputs in `platform/flake.lock`,
  modules, tools) with the same website pin;
- a re-pin changes the website release (new archives, possibly new CMS migrations,
  possibly a new realm producer);
- the private host module changes (operator keys, escrow recipient, memory, backup
  target): the same mechanism, which the [credential rotation](credential-rotation.md)
  and [secrets](secrets.md) documents call "deploy" without a command.

It does not apply to:

- a change of TLS mode (manual to ACME or back). Those need steps before and after
  the activation that this procedure does not have (`terrace-tls prepare-acme`
  before it, `terrace-secrets host tls` and a push after it): follow
  [TLS, "Switching a live manual host to ACME"](tls.md#switching-a-live-manual-host-to-acme)
  or ["Fall back to the manual path"](tls.md#fall-back-to-the-manual-path), using
  step 7 below only for its "deploy" line. Do not combine a TLS mode change with
  another update;
- a first deployment ([cold start](production-profile.md#cold-start-of-a-new-deployment)),
  a replacement host ([restore promotion](restore-promotion.md)), changing public
  host names or the relay's address (section 7), or adding Documenso, Windmill or
  Vaultwarden, which the production profile refuses
  ([production profile](production-profile.md#decisions-waiting-on-the-owner)).

### 1.2 How a new configuration reaches a host today

READ, for every row.

| Piece | Exists? | Where |
| --- | --- | --- |
| Building a host's toplevel from a revision, the private module, the hardware module, the archives and the TFC source | Yes | `infra/nixos/production-config.nix`; the command is in [production profile](production-profile.md#building-a-host). Build on the Linux builder, never on a Mac (AGENTS.md) |
| Refusing a wrong archive | Yes | `platform/website-release.nix` refuses an archive whose SHA-256 is not the pinned one before copying it, and checks every packaged file against the bundle's own manifest; `platform/identity-contract.nix` does the same for the three realm-producer files |
| Installing a first closure on a blank disk | Yes, lease tooling only | `infra/nixos/rescue_install.py` copies closures as `root` to a Rescue system and runs `nixos-install` ([Rescue installer](cloud-rescue-install.md)). It is not an update path: it formats the disk |
| A command that copies a new closure to an installed production host | **Opt-in since 2026-11-24** | `platform/production/terrace-deploy.sh` signs the closure on the build machine and copies it as `operator`; the host accepts it only with `terrace.production.deploy.trustedKeys` set (section 1.5). Without the option: no. The legacy root `flake.nix` names `deploy-rs` as an input; nothing in the new platform uses it |
| A command that activates or rolls back a generation on a production host | **Yes since 2026-11-24** | `terrace-production activate` and `terrace-production rollback` (section 1.5). `activate` refuses everything while no deploy key is trusted |
| Automatic upgrades, garbage collection, a boot-entry limit | No | The production modules set none of `system.autoUpgrade`, `nix.gc`, `boot.loader.systemd-boot.configurationLimit` (grep of `platform/production/` and `infra/nixos/`). RAN: in the pinned nixpkgs `31ecbaca` their defaults are off, off and no limit. A private or hardware module could change them |
| The private host module | Kept by the operator outside this repository | [production profile](production-profile.md#building-a-host). Its content is the operator's; this document cannot see it |

Why copying a closure is not a one-liner on this profile without the deploy path
(READ, plus RAN for the defaults; the refusal of an unsigned and of a foreign-signed
copy is now VM rehearsal, section 1.5):

- SSH accepts only the `operator` account: `base.nix` sets `PermitRootLogin = "no"`
  and `AllowUsers = ["operator"]`; it asserts `AllowUsers`, and
  `infra/nixos/production-eval-check.nix` checks `PermitRootLogin`. The installer's
  `root@` copy cannot be reused.
- RAN: in nixpkgs `31ecbaca` the defaults are `nix.settings.trusted-users = ["root"]`
  and `require-sigs = true`, and nothing in the production modules changes them. So,
  by standard Nix behaviour (not tested here), a `nix copy` to the host as
  `operator` is refused for paths that are not signed by a key the host trusts,
  and the builder's paths are not signed by any such key.
- `operator` has passwordless `sudo` (`security.sudo.wheelNeedsPassword = false`,
  `base.nix`), so anything root can do on the host, the operator can.

**Owner decision U0: how closures reach the hosts.** It is a precondition of step
1: **without an agreed, tried transfer mechanism, do not start an update.** The
repository supports none of these; each is a standard Nix mechanism that nobody has
tried on this profile:

1. **Sign on the builder, trust the key on the host.** Add the builder's signing
   public key to `nix.settings.trusted-public-keys` in the private host module and
   copy signed closures as `operator`. Nothing in the production evaluation check
   refuses that option (READ: `infra/nixos/production-eval-check.nix` has no rule on
   Nix settings).
2. **Make `operator` a trusted Nix user** (`nix.settings.trusted-users`). Given
   passwordless `sudo`, it adds no privilege the account lacks, but it lets any
   process running as `operator` add unsigned store paths.
3. **Build on the host** as root from a checkout, the private module, the kept
   archives and the TFC source copied there. AGENTS.md says to build NixOS on the
   configured Linux builder, so this contradicts the project rules, and the build
   competes with the services for memory: the profile sizes the host so that the
   memory ceilings of the writers and the backup job (6912 MiB on the app, nginx
   and the 768 MiB backup job included) fit in 75 percent of it
   ([production profile](production-profile.md#decisions-waiting-on-the-owner),
   item 4) and reserves nothing for a build. Its memory and time are not measured.

Options 1 and 2 are themselves private-module changes, so they must be in the
module a host is **installed** with, or the first update has no path. Record the
choice; section 8 lists the drill it needs. Option 1 now exists in the repository as
an opt-in (section 1.5); building it does not choose it.

### 1.3 What must exist before an update

| Item | Why | Where |
| --- | --- | --- |
| U0 decided and the mechanism present on this host | No closure reaches the host otherwise (1.2) | Owner; the host's private module |
| The new revision merged, with the VM checks that match the change passed at that revision | A pin change must re-run the matching VM check (runbook 9.7) | `platform/production-check` (always), `platform/website-check` and `platform/website-gateway-check` (re-pin), `platform/production-identity-check` (realm producer changed), `platform/production-tls-check` (TLS change); [production profile](production-profile.md#verification), [website host](website-host.md#reproduce-the-actual-artifact-vm-check), [identity administration](identity-admin.md#verification) |
| The **new** kept release archives and the TFC source at the new revision | The build reads them; the archives are not byte-reproducible, so only the one kept build matches its pinned hash; a rebuild of the same commit will not ([website host](website-host.md#accepted-inputs); website `tools/release/README.md` at `dd1aef7`) | Operator's protected storage |
| The **previous** kept archives and TFC source | To rebuild the previous revision if its generation is gone, and for a restore-based rollback onto a replacement (section 4) | Same; runbook 9.17 "Worked if: the previous release is still on disk for rollback" |
| The private host module and hardware module for this host | Part of every build | Operator's private directory |
| Shell access as `operator` with a second session open | Several steps restart services; step 7 can restart sshd | Operator machine |
| A Keycloak administrator account (app role, if the realm may change) | `realm plan` and `realm apply` need `--admin` and its password | [identity administration](identity-admin.md#before-you-start) |
| The store, escrow identity and the operator tool at the **new** revision (only if `platform/production/inventory.json` changed) | The operator tool reads its own copy of the inventory | [secrets](secrets.md#operator-tool) |

### 1.4 Decisions that are the owner's

Each is a stop point: nothing after it runs until the owner says go.

| Point | Decision | Needs to be known first |
| --- | --- | --- |
| U0, before step 1 | How closures reach the hosts | Section 1.2 |
| U1, step 1 | Go for this update, on this host, in this window | The diff between the running and the new revision, the VM check results, whether the pin, the realm producer, the inventory, the kernel or Keycloak/Headscale/PostgreSQL versions change (step 2), that today's scheduled backup has completed (step 5), and the day (the weekly repository check runs on Sundays) |
| U2, after step 6 | Accept the pre-update snapshot as the way back | Its ID and time; that the CMS was stopped while it was taken; how long retention keeps it (step 6, "How long the snapshot lasts") |
| U3, before step 7, only when step 2 found a Keycloak or Headscale version change | Activate: **for such an update this is the point of no return** for that service's database | Upstream schema changes happen when the new version starts; whether the old version starts on the new schema is not determined (section 4.3) |
| U4, before step 9 | Apply the realm plan | The printed plan and its hash; what `apply` changes and never deletes, and that a generation rollback does not undo it (section 4.4) |
| U5, before step 10 | Run the CMS migration: **the point of no return** for the CMS database | After the migration starts, the only documented way back for the database is a restore onto a replacement host (section 4.3) |
| U6 | Reboot (kernel, initrd or kernel-module change) | Console access at the provider in case the new generation does not boot (section 5, H4); that a reboot re-arms every writer timer not held after a restored adoption (`base.nix`, `terrace-production.sh`) and starts the writers, the ungated website among them, so a parked failed migration (step 10) needs its stops repeated after it |
| U7, step 13 | Declare the update done, or go back (section 4) | The checks of steps 11 and 12 |
| U8 | Go back by restore onto a replacement | The data written since the pre-update snapshot is lost (section 4.3) |

### 1.5 The deploy path (option 1 of U0, opt-in)

Built 2026-11-24 as one way to settle U0; **not chosen by the owner**. Nothing in it
is active unless the private host module sets `terrace.production.deploy.trustedKeys`.
Evidence labels in this section: **RAN IN VM** (the deploy VM rehearsal,
`platform/production/deploy-test.nix`, run with `platform/production-deploy-check`,
synthetic data, the production app role at instance `rehearsal`); **unit test**
(`tests/platform/test_terrace_production.py`, class `DeployPath`, and
`tests/platform/test_terrace_deploy.py`, stub tools, run by CI;
`tests/platform/deploy_mutants.py` removes each guard in turn and shows the tests
fail); **RAN (eval)** (`infra/nixos/production-eval-check.nix` or a derivation
comparison); **READ**; **NOT EXERCISED**. Nothing here ran on a real host.

**The pieces.**

| Piece | What it is | Evidence |
| --- | --- | --- |
| `terrace.production.deploy.trustedKeys` (`base.nix`) | A list of the build machine's **public** Nix signing keys (`name:base64`, as `nix key convert-secret-to-public` prints them; a secret-key shape is refused at evaluation). Each is added to the host's Nix trusted public keys (`extra-trusted-public-keys`). `trusted-users` stays `root`, `require-sigs` stays on, root login stays refused, no binary cache is added. Empty (the default): the host's `nix.conf` is exactly what it was before the option existed | RAN (eval): the evaluation check asserts the default settings (`trusted-users = [root]`, `require-sigs = true`, only the nixpkgs cache key, no extra key) and the settings with a key; the derivation comparison shows the default app and relay `nix.conf` equal to trunk's. RAN IN VM: `nix config show` on the host lists `root` alone, `require-sigs true`, the deploy key and not the foreign one |
| The system manifest | `/etc/terrace/system.json`, so `<toplevel>/etc/terrace/system.json` in every production system: `schema`, `role`, `hostName`, `instance`, `wombRevision`, the app's `website.revision` and `website.migrations` (the release's migration list: `terrace.production.app.releaseMigrations`, by default the pin's list when the host runs the pinned revision, otherwise unknown), and `versions` of Keycloak, PostgreSQL and Headscale. `wombRevision` comes from the new `wombRevision` argument of `infra/nixos/production-config.nix` ([production profile](production-profile.md#building-a-host)) | RAN (eval) for both roles; RAN IN VM (read by `activate` and `status`) |
| `platform/production/terrace-deploy.sh` (operator machine) | Refuses unless given a Nix **secret** key file, an SSH identity and a built production toplevel with a manifest; then `nix store sign --recursive` (needs a user the build machine's Nix daemon lets add signatures: root or a trusted user), `nix copy --to ssh-ng://operator@<host>` with strict host key checking, and prints the activation command. It activates nothing | RAN IN VM (the operator guest stands in for the build machine); unit test |
| `terrace-production activate <toplevel> [--boot] [--cms-stays-down] [--accept-upstream-schema-change]` | Root only (`sudo` from `operator`). The checks below, then **inside the transient unit** `systemd-run --unit=terrace-switch --collect --service-type=exec --wait`: `nix-env -p /nix/var/nix/profiles/system --set <toplevel>`, `<toplevel>/bin/switch-to-configuration switch` (or `boot` with `--boot`), and a record | RAN IN VM; unit test |
| `terrace-production rollback [--boot] [--cms-stays-down] [--accept-upstream-schema-change] [--dry-run]` | The newest generation older than the current one (after an attempt that failed, that can be the failed attempt's generation: read `status` first), same checks except the signatures (a generation is already root-trusted), same unit: `nix-env --switch-generation <n>`, then that generation's `switch-to-configuration`. When the boot default is **not** the running system (after `activate --boot`, or a switch that failed after registering its generation), `rollback` instead makes the running system's generation the boot default again: `nix-env --switch-generation <that generation>` and the running system's `switch-to-configuration boot`, in the same unit, recorded as `cancel`; nothing running changes | RAN IN VM (both forms: the second as `activate --boot` cancelled before a reboot); unit test (both, and the failed-switch case) |
| The records and `status` | The unit appends two JSON lines to `/var/lib/terrace-production/deployments.log` (root, 0600): a `started` line before it changes anything (time, action, mode, from, to, generation before, and `failedBefore`: which of the CMS and website were already failed; the unit then clears their failed state so a start limit hit earlier cannot block a unit whose gate is open), and a `finished` line after (generation after, Womb and website revision, result `success`, `switched; down until the CMS migration: <units>` or `failed (exit <n>)`, `failedUnits`, `startingUnits`, `unexpectedUnits`). The unit writes them, so they exist even when the SSH session dropped. `terrace-production status` adds the running system and its revisions, the profile's generation (the boot default) with a WARNING when it is not the running system, the previous generation and its revisions, whether the deploy path is on, and the last record, with a WARNING when the last activation started and never finished (its unit stopped, or the host rebooted mid-switch) | RAN IN VM (records and `status` lines); unit test (both WARNINGs, `failedBefore`) |

**What `activate` and `rollback` refuse, before anything changes.** Each refusal
prints `terrace-production: nothing changed: ...` and exits 1; the unit tests check
that the profile, the running system and the record are unchanged, and a mutant
without each check fails them. The whole command holds a root-only lock
(`/var/lib/terrace-production/deploy.lock`, created once, never replaced), so a
second `activate` or `rollback` is refused at once; the `terrace-switch` unit
repeats the state checks (the running system and the profile's generation the
decision was made against, the idle checks, the target's form) immediately before
it changes anything, and refuses a decision made against an older state; the
command prints only the records it wrote itself (unit tests; a mutant for each).
`--dry-run` runs every check and stops (RAN IN VM, as the control of the
non-deploy-key case below).

| Check | Why (this document) | Evidence |
| --- | --- | --- |
| Not root | The command changes the system | RAN IN VM (`operator` without `sudo` over SSH, and `nobody`; control: `sudo` from `operator` activates); unit test |
| No deploy key trusted (activate) | The deploy path is off | unit test (the VM host has a key) |
| Not a canonical store path, absent, not valid in the host's store, no `bin/switch-to-configuration`, no manifest | Only a whole, copied NixOS system built by this profile | unit test |
| Manifest names another role, host name or instance | Never activate a closure built for another host. Recognised from the manifest, never from the path's name | RAN IN VM: a signed and copied closure built for host name `app-other` and one built for the relay role, both accepted by the copy (control), refused by `activate`; unit test (role, host, instance) |
| The target is the running system | Nothing to do | RAN IN VM; unit test |
| `terrace-switch`, `terrace-backup`, `terrace-backup-check`, (app) `terrace-cms-migrate` or `terrace-cms-sweep` active, or (app) any `terrace-cms-tool-*` unit active; refused too when those units cannot be listed | One switch at a time (step 7); no switch under a backup, a migration, the sweep or an operator tool (steps 6, 10) | RAN IN VM (a running `terrace-switch` unit); unit test (each, and the listing failure) |
| A path of the closure that is in no system generation on this host (current, booted or any profile generation) lacks a valid signature by a trusted deploy key, or its contents do not match (`nix store verify --sigs-needed 1` given **only** the deploy keys) | Paths already in a generation came by a root-trusted route (installation, an earlier activation); everything else must come from the build machine | RAN IN VM: an unsigned closure and one signed with an untrusted key, both put into the store by root (`nix-store --import`, as an operator with `sudo` could), refused; a closure signed only by a second key that the host's Nix trusts through plain `nix.settings` (as it trusts the nixpkgs cache key), accepted by the copy as `operator`, refused by `activate`, while `nix config show` lists that key; controls: the signed closure activates, and the second closure with the deploy key's signature added passes every check (`--dry-run`); unit test |
| Kernel, initrd or kernel modules differ from the booted system, without `--boot` | `switch` would leave the old kernel running (U6, section 5) | unit test only (the VM proof's systems share a kernel; a test VM boots its first system directly, so a boot into a new generation cannot be shown there) |
| Keycloak or Headscale version differs, without `--accept-upstream-schema-change` | **U3**: their schema may migrate at start, a point of no return | unit test |
| PostgreSQL major version differs, or (app) a manifest records no PostgreSQL version | No upgrade path (step 2); no flag | unit test |
| (App) the release changes and `terrace-cms` or `terrace-web` is active, activating (a restart loop) or reloading | Quiesce first (step 6) | RAN IN VM (control: after step 6's stops it activates); unit test |
| (App) the release changes, a migration marker exists and PostgreSQL is down | The ledger cannot be compared | unit test |
| (App) the CMS ledger (`payload_migrations`) holds a migration the target's manifest does not list | **Migrations are forward-only**: the target's CMS will refuse to start. Refused unless `--cms-stays-down`, which then warns that the CMS stays down and that the old migration unit must never be started (H1) | RAN IN VM: after the upgrade migration, `rollback` refused, then `rollback --cms-stays-down` ran and the previous CMS failed its gate; unit test |
| (App) the same, while the marker holds the **target's** revision | H9: a failed or partial migration left the old marker, so the old CMS would start on a newer schema. Refused, no flag | unit test |
| (App) the target's migration list is unknown (`null`) while the ledger is not empty | Nothing shows the target accepts the database | unit test |

**What stays an operator step** (the command does not do or check it, by design: it
stays small, refuses by default and changes only the system profile):

- U0 to U8, every owner decision; step 2's classification (the command only
  refuses Keycloak, Headscale and PostgreSQL version changes it can see in the two
  manifests);
- the backup window and the pre-update backup (steps 5, 6, H2): the command
  checks only that no backup runs at this moment;
- quiescing (step 6): it refuses a release change while the CMS or website runs, it
  does not stop them;
- the secrets order for a changed inventory (step 8), the realm (step 9), the
  migration itself (step 10), starting the units (step 11), the checks (step 12) and
  re-arming the backup timers (step 13);
- free space on `/nix/store` (step 4) and garbage collection (H8);
- operator tools in flight (`terrace-cms-tool-*`): not checked by `activate`; the
  migration's preflight refuses them (step 10);
- timers held after a restored adoption: the command never touches the hold
  markers. RAN IN VM: with the markers in place (written by hand on an adopted
  host, as `adopt --restored` writes them: **not a restored host**), a start of the
  three backup timers and the sweep was skipped by their start condition, they
  stayed off (start condition not met) after the first activation and after both
  rollbacks, and `status` kept naming them; `arm-timers` and `arm-timers
  --in-service` armed them at the end.

**A dropped session.** RAN IN VM: the operator guest ran `ssh -tt operator@app sudo
terrace-production activate <toplevel>` (a terminal, as `ssh -t` gives an operator),
and the second system held its activation for 20 seconds (a probe activation script,
rehearsal only); while `terrace-switch.service` was active the test killed the SSH
client; the `terrace-production` client on the host was gone before the switch
finished, and the switch completed, ran the new generation and wrote its record. The
switch in that proof does not restart sshd, so a session cut by the switch itself is
not shown.

**A switch to a new release, before its migration.** See step 7: the switch starts
the gated CMS and the ungated website; `activate` records `switched; down until the
CMS migration` only under the three conditions there, and tells the operator to
stop the website (RAN IN VM; unit test for each condition and a mutant for each).

**Activating a system that is already the newest generation.** `nix-env --set`
reuses the newest generation when it already points to the same toplevel instead of
creating another (Nix's own behaviour). RAN IN VM: after the case A rollback to
generation 1, activating the next system again made generation 2 current again, and
the record says `1` to `2`.

**A switch that fails.** `activate` registers the generation before it switches,
exactly as `nixos-rebuild` does, and `switch-to-configuration` installs the boot
loader before it changes the running system. So a switch that fails, for example
at the boot loader step, leaves the system profile (the boot default) on the new
generation while the old system keeps running. The command prints `FAILED`, the
record says `failed (exit <n>)`, and `status` prints `WARNING: the boot default,
generation <n> (...), is not the running system: a reboot boots generation <n>.`
(unit test; a failed switch was seen in the VM while the proof was being built,
when its test VM could not take GRUB, but no VM assertion checks it). In that state
**a reboot boots the new, never-activated generation**. The command does not revert
the profile on its own (the operator decides, after reading `journalctl -u
terrace-switch.service -o cat --no-pager`); whether it should is an open question in
the [backlog](backlog.md#host-update-follow-ups-g-37--97-recorded-2026-11-24). The
ways out:

- the cause is fixed: repeat `activate` with the same toplevel (the profile already
  points to it, so Nix reuses that generation);
- keep the running system: `sudo terrace-production rollback`, which in this state
  makes the running system's generation the boot default again (unit test for this
  state; VM rehearsal for the same command after `activate --boot`). Then `status`
  prints no WARNING;
- by hand, if the command cannot run (written procedure, NOT RUN): `status` prints
  the running system's path on its `running system:` line; find its generation
  with `nix-env -p /nix/var/nix/profiles/system --list-generations` and `readlink -f
  /nix/var/nix/profiles/system-<n>-link`, run `nix-env -p
  /nix/var/nix/profiles/system --switch-generation <that n>`, then `systemd-run
  --unit=terrace-switch --collect --service-type=exec --wait -- <the running
  system's path>/bin/switch-to-configuration boot`. **If that boot step fails the
  same way** (the boot loader cannot be written), the profile already names the
  running system, but the boot menu may still default to the failed generation: do
  not reboot; repair the boot loader cause first, and plan for console access at
  the provider before any reboot (H4).

**What the deploy VM proof does not assert.** A real host, provider or network; the
relay role's update (its closure is only refused on the app); a boot loader
installed on a real disk (the test nodes only write the GRUB menu); a kernel change
and a reboot into a new generation (a test VM boots its first system directly;
`--boot` runs only as far as the boot step, then is cancelled); a switch that
restarts sshd; a Keycloak, Headscale or PostgreSQL version
change; the inventory and realm steps (8, 9); a failed or timed-out migration; the
old migration unit on the newer schema (H1); backups, retention and the backup
window (steps 5, 6, 13); a failed switch's state (seen while the proof was built,
when the VM could not take GRUB, but asserted only by unit tests); the bootstrap
route and key rotation; real data sizes. One release pair (`680ad56` to `dd1aef7`)
and small synthetic data. The test nodes enable `switch-to-configuration` (the test
framework turns it off for its nodes; an installed host keeps NixOS's default, on)
and write the GRUB menu without installing it (the VM disk cannot take GRUB).

**Integrity, not a privilege boundary.** `operator` has passwordless `sudo`, so root
on the host can still import any closure (`nix-store --import`, as the VM proof does
for its refusal cases) and run `switch-to-configuration` by hand. The signatures
prove where a closure came from and that it arrived intact; they do not stop the
operator.

**Bootstrap: how the key reaches a host.** The trusted key is part of the installed
configuration. A host installed from a private module that already sets
`deploy.trustedKeys` trusts it from its first boot. **No production host exists yet,
so set it before the first install.** A host installed from a Womb revision BEFORE
this change has no system manifest (`/etc/terrace/system.json`), so `activate`
refuses it even with a key ("the running system has no Terrace manifest"): its first
update also needs the other route below. A host installed without it has no signed path:
its first update needs another route, for example the operator copies an exported
closure (`nix-store --export`) as `operator`, imports it with `sudo nix-store
--import` and activates it with the raw commands of step 7, because `activate`
refuses everything while no key is trusted. That route is NOT EXERCISED as an update
(only the import itself ran, in the VM proof's refusal cases).

**Losing or replacing the signing key** (written procedure, NOT RUN). The secret
key's custody is in [secrets](secrets.md#deploy-signing-key).

- *Rotation, two keys during the changeover*: generate the new key on the build
  machine; add its public half to `deploy.trustedKeys` **beside** the old one;
  build, sign with the **old** key and deploy (the host now trusts both); move
  signing to the new key; remove the old public half; build, sign with the **new**
  key and deploy; destroy the old secret key. Check after each deploy that `nix
  config show trusted-public-keys` on the host lists what you expect.
- *Loss*: no new closure can be signed with that key. If a second trusted key was
  kept (the documented suggestion: an offline spare, trusted from the first
  install), sign with it and rotate. Otherwise use the bootstrap route above for one
  update that trusts a new key.
- *Leak*: copying still needs the `operator` login and activation still needs
  `sudo`, so a leaked key alone does not reach a host; rotate as above.

## 2. The app host procedure

All commands run as root on the app host (`ssh -t operator@<app address>`, then
`sudo -i`) unless a step says "operator machine". `<...>` marks a value you
supply. The relay is in section 3, reboots in section 5. Steps 1 to 5 change
nothing on the host and the site stays up; the site is down from step 6 to step 11.

### Part A. Prepare (the site stays up)

**Step 1. U0 settled, then owner decision U1.** If U0 is not decided and present on
this host, stop here. Record who decided U1, when and why.

**Step 2. Operator machine: classify the change.** At the new revision, against the
revision the host runs:

```sh
git diff --stat <running revision> <new revision> -- platform/ infra/nixos/
git diff <running revision> <new revision> -- platform/website-release.json platform/identity-contract.json platform/production/inventory.json platform/flake.lock
```

What each file means for the procedure (READ):

- `platform/website-release.json` `revision` changed: the migration gate closes at
  activation (steps 7 and 10). Same revision: the gate stays open and step 10 does
  not apply.
- `platform/identity-contract.json` changed: the realm definition changes; Keycloak's
  startup import skips an existing realm ([identity host](identity-host.md)), so
  step 9 reconciles it. Run step 9's `plan` after every app update anyway; it says
  `Converged` when nothing changed.
- `platform/production/inventory.json` changed: the secret names change; steps 7
  and 8 have the order.
- `platform/flake.lock` (nixpkgs) changed: Keycloak, Headscale, PostgreSQL minor
  versions, Node and the kernel can change. A Keycloak or Headscale version change
  is a schema change on their own databases when the new version starts (READ: the
  comment "the upstream notify plugin can announce startup before schema migration
  finishes" in `platform/identity.nix`; Headscale's behaviour is upstream and **not
  determined from the repository**). That happens at activation, so for such an
  update **U3 before step 7 is the point of no return**, not step 10. A PostgreSQL
  **major** change is not supported by anything here: the app role pins
  `postgresql_18` (`platform/production/app.nix`) and no upgrade tool exists.

**Step 3. Operator machine: build the new toplevel** on the Linux builder, with the
command in [production profile](production-profile.md#building-a-host) (the new
revision's checkout, this host's private and hardware modules, the new archives and
TFC source). Record the printed store path as `<new toplevel>`. Worked: one
`/nix/store/...` path. Failed: an evaluation assertion names the problem (the
profile refuses unsafe configurations, [production profile](production-profile.md#invariants-enforced-at-evaluation));
`Terrace web archive hash mismatch` or `... cms archive hash mismatch` means the
archive is not the kept build (`platform/website-release.nix`). Nothing changed on
the host: stop. Evidence: the build is what the VM checks build; READ for a real host.

**Step 4. Copy the closure to the host**, by the mechanism decided at U0, and check
it arrived. With the deploy path (section 1.5), on the build machine, from a
checkout at the new revision:

```sh
platform/production/terrace-deploy.sh --key <secret signing key file> --address <host address> \
  --identity <operator ssh key> --known-hosts <file pinning the host key> <new toplevel>
```

`--known-hosts` is required and pins the host's SSH key: make the file from the
secrets store's copy of the key the host was installed with (no trust on first use):
`printf '%s %s\n' <host address> "$(cut -d' ' -f1,2
"$TERRACE_SECRETS_STORE/hosts/<host>/ssh_host_ed25519_key.pub")" >known_hosts`. The
helper refuses a signing key file that group or others can read, resolves
`./result` to its store path, signs, copies as `operator` and prints the step 7
command with that store path; RAN IN VM (the VM proof built the file the same way
from the staged key). A copy that fails part-way leaves the paths already copied in
the host's store: signed, unused, harmless. Then, on the host:

```sh
test -e <new toplevel>/bin/switch-to-configuration
```

Worked: the test succeeds on the host. The copy adds the new closure to the host's
store, next to every earlier generation (nothing collects them, H8): check free space
on the root filesystem first (`df -h /nix/store`); the space a closure needs is not
measured. Failed: nothing changed on the host and the site is still up; stop and fix
the mechanism. Evidence: with the deploy path, RAN IN VM (signed copies accepted,
unsigned and foreign-signed copies refused by the host's Nix daemon, section 1.5);
the space a closure needs: not measured.

**Step 5. Record where you start, and check the window.**

```sh
readlink -f /run/current-system
nix-env --profile /nix/var/nix/profiles/system --list-generations
terrace-production status
systemctl --failed
cat /var/lib/terrace-cms/migrated-revision
runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d terrace_cms -c "select name||':'||batch from payload_migrations order by id"
systemctl list-units --plain --no-legend --state=active,activating,deactivating 'terrace-cms-tool-*'
terrace-backup age
systemctl list-timers terrace-backup.timer terrace-backup-check.timer
```

Worked: `adopted <time>` and `active` (READ: the strings `terrace-production`
prints, `base.nix`); no failed units; the marker equals the running pin's revision;
the ledger lists the running pin's migrations; the tool listing prints nothing (a
running tool makes step 10 refuse, [website host](website-host.md#operator-tools-terrace-cms-tool));
`terrace-backup age` exits 0; the backup timer's next run is tomorrow, so **today's
scheduled backup has completed**. The `list-timers` line carries the weight here:
`terrace-backup age` prints the seconds since the last success against a limit
(`cmd_age` in `platform/backup/backup.sh`; 26 hours by default), so yesterday's
success passes it too. The timer is `03:15`
with up to 15 minutes' random delay (`RandomizedDelaySec = "15m"`,
`platform/backup.nix`), so "after 03:15" is not enough: check. Do not start while
the weekly `terrace-backup-check` (Sundays 05:30, up to 30 minutes' delay,
[backups](backups.md#what-runs-and-its-resource-bounds)) is due or running: its
`restic check` and step 6's backup and prune use the same repository. Keep all
this output: it is what step 13 and a rollback compare against.

Failed: anything else; nothing has changed on the host, stop.

### Part B. Quiesce and back up (the site goes down)

**Step 6. Quiesce, and take the pre-update backup.**

```sh
systemctl stop terrace-backup.timer terrace-backup-check.timer terrace-cms-sweep.timer
systemctl stop terrace-cms-sweep.service terrace-web.service terrace-cms.service
systemctl start terrace-backup.service
systemctl show terrace-backup.service -p Result --value
terrace-backup age
terrace-backup restic primary snapshots --json
```

Why stop the backup timers: as in [restore promotion](restore-promotion.md) step 2,
so that no scheduled backup or check runs against a half-updated host if the update
overruns. Why stop the CMS: a backup dumps each database in one snapshot but reads
media files afterwards, so a running CMS can leave the database and the uploads
disagreeing ([backups](backups.md#what-runs-and-its-resource-bounds), "Consistency").
Keycloak and PostgreSQL keep running: the Keycloak database is one dump, consistent
on its own (READ).

Worked: `success`; `terrace-backup age` exits 0; the newest entry in the list is
from now, with this host's name. Note its `id`. With a secondary target
(`secondary-backup`), the same unit backs up both (`terrace-backup run` loops over
the targets, `platform/backup/backup.sh`): check `terrace-backup restic secondary
snapshots --json` too.

**Restart after quiesce.** The way back from any failure **before step 7**, and
from step 7 to step 9 after a generation rollback (section 4.2):

```sh
systemctl reset-failed terrace-cms.service terrace-web.service
systemctl start terrace-cms.service terrace-web.service
systemctl start terrace-cms-sweep.timer terrace-backup.timer terrace-backup-check.timer
```

Starting the timers again on the same day after the day's run has completed does
not start a backup (READ: a persistent timer catches up only an elapse it missed;
for the sweep timer this catch-up is VM rehearsal, `platform/website-test.nix`).
Worked: both units active, the three timers active.

Failed (step 6): `target primary: backup FAILED` in the log. Run "restart after
quiesce" and do not update. **Do not repeat step 6 later the same day**: a second
snapshot today makes the first one lose its keep-daily protection (below, H2). Retry
another day, after that day's scheduled run.

What the backup holds and does not (READ: `platform/production/app.nix`,
`platform/website.nix`, [backups](backups.md#datasets-owners-and-retention)): the
`terrace_cms` and `keycloak` databases, the PostgreSQL roles, the media and outbox
directories. Not: the system closure or the release archives (keep them yourself,
section 1.3), the migration marker, the website input files (sops templates), ACME
state, the secrets ciphertext, the operator directory.

**How long the snapshot lasts.** READ from `platform/backup/backup.sh` (`forget
--group-by host,tags --prune` with keep-last 3, daily 7, weekly 4, monthly 6) and
restic's rules as summarised in [backups](backups.md#retention); not tried. If the
pre-update snapshot is the **last snapshot of its day**, keep-daily holds it for
seven days. If another snapshot of the same host is taken later that day (a retry of
step 6, an extra backup after the update, or a scheduled run that had not yet
happened), it stops being the day's last and only keep-last protects it: after three
newer snapshots it is pruned, which with the daily timer is about two days. Nothing
in the tooling pins a snapshot ([restore promotion](restore-promotion.md), step 21).
That is why step 5 checks that the day's run completed, step 6 is not retried the
same day, and step 13 takes no extra backup.

**U2.** The owner accepts the snapshot (ID and time recorded) as the way back.

Evidence: backup and quiesce, VM rehearsal (synthetic data): the same commands
in the "back up, destroy the host" subtest of `platform/production/test.nix`
(backup succeeds, snapshot listed). Stopping only the CMS units and keeping
Keycloak up for a backup, stopping and restarting the backup timers: not exercised
anywhere. The retention reasoning: not exercised anywhere.

### Part C. Activate the new generation

**Step 7. U3 if step 2 found a Keycloak or Headscale version change, then
activate.** With the deploy path (section 1.5), from the operator machine:

```sh
ssh -t operator@<app address> sudo terrace-production activate <new toplevel>
```

It runs the checks of section 1.5 and then exactly the three lines below inside the
`terrace-switch` unit, and records the result; add `--accept-upstream-schema-change`
only after U3, and `--boot` for a kernel change (then section 5). RAN IN VM for the
app role, `switch` mode, a new release. Without the deploy path, or for a host
installed without a deploy key, the standard NixOS commands, READ from the
toplevel's layout:

```sh
nix-env --profile /nix/var/nix/profiles/system --set <new toplevel>
systemd-run --unit=terrace-switch --collect --service-type=exec --wait -- <new toplevel>/bin/switch-to-configuration switch
journalctl -u terrace-switch.service -o cat --no-pager
```

The first line adds the new generation to the system profile; the second writes
the boot entries (the profile's newest generation becomes the default) and switches
the running system. It runs as a transient system unit, not as a child of your SSH
session: an activation can restart sshd, nginx or the network, and a dropped
session must not kill a half-finished switch. That is standard practice
(`nixos-rebuild` does the same), not tested in this repository. `--collect` makes
systemd unload the transient unit when it ends, failed or not, so the same unit
name can be used again at once (a rollback in section 4.1, or `switch` after
`test`); without it a failed run stays loaded and the next `systemd-run
--unit=terrace-switch` is refused until `systemctl reset-failed
terrace-switch.service` (standard systemd behaviour, not tested here). The result
is therefore in the journal only: `--wait` prints it, and if the session drops,
reconnect and read the journal line above.

If the kernel, the initrd or the kernel modules differ, use `boot` instead of
`switch` and follow section 5. Compare all three:

```sh
for item in kernel initrd kernel-modules; do test "$(readlink -f /run/booted-system/$item)" = "$(readlink -f <new toplevel>/$item)" || echo "$item differs"; done
```

An alternative for updates without such a change, before the migration:
`switch-to-configuration test` (without the `nix-env --set` line) switches the
running system but leaves the old generation as the boot default, so a reboot
returns to it. It is the only activation mode any VM check runs on the production
profile (below). After step 10 it is the wrong choice: a reboot would bring back the
old generation onto the migrated database (section 4.3). If you use it, run the
`nix-env --set` line and `switch-to-configuration switch` before step 10.

What activation does to this host (READ unless marked RAN IN VM: the deploy VM
rehearsal observed a re-pin from `680ad56` to `dd1aef7` on the app role):

- Every unit whose definition changed and that runs is restarted. **On a re-pin the
  switch starts the CMS and the website even though step 6 stopped them**: their
  definitions changed (new bundle paths) and the active `terrace-production.target`
  wants them (RAN IN VM, three times). What follows is in the gate item below.
  Keycloak and PostgreSQL restart if their
  package or settings changed; nginx if its configuration changed (a few seconds
  without HTTPS, as in [TLS](tls.md#switching-a-live-manual-host-to-acme)).
- `sops-install-secrets.service` runs with the new manifest
  (`platform/production/secrets.nix`); the host copy of the ciphertext
  (`/var/lib/terrace-secrets/secrets.sops.json`) is not part of the generation and
  does not change ("rotation needs no rebuild"). **When step 2 found an inventory
  addition, expect this unit to fail here**: the new manifest declares a name the
  delivered ciphertext lacks until step 8. That is the likely behaviour; what
  sops-nix actually does (fail, and whether it keeps the previous secrets) is **not
  determined from the repository**. In that case go on to step 8; do not treat it as
  a step 7 failure.
- **On a re-pin the CMS cannot start until step 10.** The production profile adds
  `ConditionPathExists` on the marker (`platform/production/app.nix`), and the
  marker exists, so the CMS runs its gate and fails: `CMS migration gate is closed;
  run the explicit migration unit for this release.` It restarts (`Restart =
  "on-failure"`, 10 s apart) and after three attempts in ten minutes stays failed
  with `start-limit-hit` (`StartLimitBurst = 3`, `platform/website.nix`); RAN IN VM,
  within about 30 s. The sweep has the same gate (READ); the operator tool's refusal
  with that message is VM rehearsal (below). The **website has no such gate** (H7):
  `terrace-web.service` checks only the identity settings (`identityGuard "web"`)
  and the input unit checks that both bundles carry the new revision, so the switch
  starts the new website bundle while the CMS is down. RAN IN VM: its start waits
  for its own health check (`/events/`), which fails after about 60 s against the
  stopped CMS (control process exit 22); it restarts the same way and ends
  `failed` with `start-limit-hit` about three to four minutes after the switch.
  Whether it answered visitors meanwhile is not determined.
- Units and timers are wanted by `terrace-production.target`, not by a boot target
  (`base.nix`); the adopted marker and the boot gate are unchanged by a switch.

Worked: the journal ends without errors; `readlink -f /run/current-system` is `<new
toplevel>`; `systemctl --failed` lists nothing, or only what is expected: on a
re-pin `terrace-cms.service` (gate) and, a few minutes later, `terrace-web.service`;
with an inventory addition `sops-install-secrets.service`. `switch-to-configuration`
exits 4 when a unit failed to start (RAN IN VM on a re-pin). `terrace-production
activate` accepts that exit **only** when the migration marker does not hold the new
release's revision (the gate is closed), the CMS or the website is failed, and no
other unit of any type (service, timer, mount, socket, target...) is failed or in
`auto-restart`, nor named in `switch-to-configuration`'s own "the following units
failed" line. It then exits 0 and records `switched; down until the CMS migration:
<units>`, with `failedUnits` (the CMS or website, failed), `startingUnits` (the CMS
or website still retrying) and `unexpectedUnits` (empty). A unit outside the CMS and
website that is merely `activating` when the switch has returned is not counted:
`switch-to-configuration` waits for the jobs it queued, so such a unit was started by
something else, such as a timer. Anything else, an inventory addition's
`sops-install-secrets` included, is recorded `failed (exit 4)` with every
unexpected unit named: read the list, do not retry blindly. RAN IN VM: the gated
switches were recorded as switched with the CMS failed and the website failed or
still starting (the test accepts either; in the passing run every gated switch
recorded the CMS failed and the website still starting), and a gated switch
whose system also carried a unit that fails and restarts was recorded `failed (exit
4)` naming that unit; unit tests for a failed timer, a service in `auto-restart`
and a unit named only in the switch's own list. **Stop the website now**
(`systemctl stop terrace-web.service`) and keep it stopped until step 11; left alone
it serves the new bundle without its CMS until it gives up. Any later `activate` or
`rollback` that changes the release again refuses while the CMS or the website is
still retrying, and names `systemctl stop terrace-cms.service terrace-web.service`.

Failed: anything else in `systemctl --failed` (Keycloak, PostgreSQL, nginx, sshd,
an unexpected `sops-install-secrets`). Without a Keycloak or Headscale version
change (no U3), roll back to the previous generation (section 4.2) and run "restart
after quiesce". With one, go to section 4.3: a rollback may not start the old
version on the new schema.

Evidence: a live `switch-to-configuration test` on a running, adopted production
relay, between two closures prepared in one build (a specialisation), is VM
rehearsal (synthetic data): `platform/production/tls-test.nix`, subtest "relay:
live switch from ACME to the manual certificate and back, never the placeholder"
(clients saw only valid certificates, no failed units). RAN IN VM
(`deploy-test.nix`): on the app role, a closure built with a different website
release and Womb revision, registered in the system profile and switched with
`switch` under `systemd-run --unit=terrace-switch --collect --wait` by
`terrace-production activate`, with the SSH session killed mid-switch; on a re-pin
the switch started the stopped CMS and website, the new CMS failed with the gate
message at its start limit, and the ungated website failed its health check and
ended at its start limit minutes later. `boot`, the
relay role and a reboot into the new generation: not exercised anywhere. The operator tool refused with the
marker moved away: VM rehearsal (synthetic data), `platform/website-test.nix`, the
website module only.

**Step 8. Secrets, only if `inventory.json` changed (step 2).** The host's tool
reads the inventory of its own generation (`platform/production/package.nix` sets
`TERRACE_SECRETS_INVENTORY` to the packaged file), and `install` refuses a
ciphertext with names this host does not take (`validate_ciphertext` in
`platform/production/terrace-secrets`). So the order is: activate (step 7) first,
then from the operator machine with the new revision's tool. Save a copy of the
store file first and prove the escrow identity opens it, as the
[credential rotation](credential-rotation.md#2-procedures) general preconditions
say. `generate` refuses a host that already has secrets (`cmd_generate`), so a
newly added name is created with `rotate` (a generated value; add `--manual` when
its inventory `rotation` is `manual`) or `set` (an external value, read from
standard input):

```sh
terrace-secrets rotate app <new name>                    # a generated value
terrace-secrets set app <new name> < <file with the value>   # instead, an external value
terrace-secrets check app
terrace-secrets push app --address <app address> --identity <operator ssh key>
```

then on the host:

```sh
terrace-secrets verify
systemctl reset-failed sops-install-secrets.service
systemctl is-active terrace-cms.service terrace-web.service
```

Worked: `check` lists no `missing` or `unexpected` name; `Installed <n> encrypted
secrets.` and `Host state: adopted`; `verify` prints `PASS`. A push restarts
`sops-install-secrets`, and sops-nix restarts the units in each changed secret's or
template's `restartUnits` (`inventory.json`; `restartUnits` of the two templates in
`app.nix`: `terrace-cms.service`, `terrace-web.service`; a push restarting both is VM
rehearsal, `test.nix`, "rotate the intake secret"). Whether that also **starts** a
stopped unit is sops-nix behaviour, **not determined from the repository**. If it
does on a re-pin, the CMS fails its gate again and the website runs ungated (H7): if
the last command shows `terrace-web.service` active before step 11, stop it.

**Between step 7 and this push, a reboot leaves the writers stopped**: at boot
`terrace-production-boot` runs `terrace-secrets verify` and starts the writers only
if it passes (`base.nix`), and with a newly declared secret missing it fails (READ:
`check_file` reports a missing file). The same window exists for an
`escrowRecipient` change ([secrets](secrets.md#escrow-key-lost-or-leaked)). A
**removed** name needs no push to keep the host running (READ: `verify` checks only
the manifest's names), but every later `push` is refused, by the operator's `check`
(`unexpected <name>`) and by the host's `install`, until the store drops the name,
and the tool has no general command that deletes a name from the store (only `host
tls` drops `tls/*`): how to remove one is **not determined from the repository**.

Failed: `push` refused or `verify` FAIL. The host keeps the previous ciphertext
(`install` restores it on failure, `cmd_install`). Fix the store and push again; if
that cannot be done and no U3 was taken, roll back the generation (section 4.2),
whose manifest matches the delivered ciphertext. After a Keycloak or Headscale
version change (U3), section 4.2 does not apply: fix the store and push (the host
keeps running on the previous ciphertext meanwhile), or go to section 4.3. Evidence: adding or removing an inventory secret on a
running host: not exercised anywhere.

### Part D. Identity, then the migration (app role)

**Step 9. Reconcile the realm.** After activation, the tool's configuration names
the new build's realm definition (`/etc/terrace/identity-admin.json`,
`platform/production/identity-admin.nix`):

```sh
terrace-identity-admin realm plan --admin <you>
```

`Converged` means nothing to do. Otherwise read the plan ([identity
administration](identity-admin.md#6-reconcile-the-realm-after-a-new-release-g-39)
explains every line), then **U4**, then:

```sh
terrace-identity-admin realm apply --admin <you> --plan-sha256 <hash from the plan>
terrace-identity-admin realm plan --admin <you>
```

What `apply` changes: it updates declared settings, adds missing roles, clients
(only with a delivered secret), mappers and scopes, and **removes** entries from the
lists it manages exactly (scope mappings, a client's redirect URIs and web origins).
It never deletes a user, credential, session, client, role or the realm
([identity administration](identity-admin.md#6-reconcile-the-realm-after-a-new-release-g-39)).

Worked: `Applied N change(s); the realm now matches the definition.` and then
`Converged`. Failed: exit 1 (refused) or 3 (the plan holds a refused item): nothing
was applied. Do not continue to step 10 with a realm the new CMS may depend on;
decide with the owner: fix forward, or roll back (section 4.2, which still applies
because nothing irreversible ran unless U3 did). A new client needs its secret
delivered first (step 8). The order "realm before migration" is chosen here so that
the CMS starts against the reconciled realm; whether a CMS release depends on it is
**not determined from the repository**.

Evidence: VM rehearsal (synthetic data), `platform/production/identity-admin-test.nix`,
subtest "realm reconciliation": a realm imported from the previous release's
producer (`680ad56`), re-pinned to `eab4189`'s, planned as exactly one change,
applied, read back from Keycloak's database, users, credentials and the realm
identity unchanged. The re-pin there is simulated by rewriting
`/etc/terrace/identity-admin.json`, not by activating a new generation.

**Step 10. U5, then migrate.** The point of no return for the CMS database (section
4.3). Check again that no operator tool runs (the step 5 listing; no output), then:

```sh
systemctl start terrace-cms-migrate.service
cat /var/lib/terrace-cms/migrated-revision
runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d terrace_cms -c "select name||':'||batch from payload_migrations order by id"
systemctl start terrace-cms-migrate.service
runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d terrace_cms -c "select name||':'||batch from payload_migrations order by id"
```

What the unit does (READ: `platform/website.nix`, `platform/cms-migrate-preflight.sh`):
its checks first (identity guard, the gateway's runtime guard, then the preflight's
refusal while a `terrace-cms-tool-*` unit is active), so a refusal stops nothing;
then it stops whichever of the CMS, website and sweep are running, runs the new
bundle's `bin/migrate` (Payload's `db.migrate` with the bundle's migration list, `up`
steps only: website `cms/tools/release-migrate.ts` at `dd1aef7`; that a second run
applies nothing is shown by the website VM check below), and writes the new revision
to the marker **only if `bin/migrate` exits 0**. It never restarts anything.

**Time limit.** The unit has `TimeoutStartSec = 180` (`platform/website.nix`),
shared by the preflight and `bin/migrate`. A migration that runs longer is killed
by systemd mid-run, and the marker is not written. Measured migrations are about 5
to 6 seconds on empty or small synthetic databases (runbook 6.15); none is measured
on real data. Before U5, the owner should know whether the new release's migrations
rewrite large tables; this repository cannot tell, and raising the limit is a code
change (a new revision).

Worked: both starts succeed; the marker equals the new `revision` in
`platform/website-release.json`; the ledger names are exactly that file's
`migrations` list, in order (operator machine: `jq -r '.migrations[]'
platform/website-release.json`), and the second reading equals the first (the
website's runtime contract: "Run it once more to confirm no pending migration").

Failed, by a guard or an operator tool (the journal says so before any stop:
`journalctl --namespace=terrace-apps -u terrace-cms-migrate.service -o cat
--no-pager`): nothing changed; [website host](website-host.md#operator-tools-terrace-cms-tool)
has the recovery, then run step 10 again.

**Failed inside `bin/migrate`, or killed by the time limit.** The journal shows `CMS
release migration failed; ...` (the tool hides the cause, `release-migrate.ts`) or a
timeout. Then:

- The marker still holds the **old** revision. On the new generation the gate stays
  closed (old marker, new pin), so the CMS, the sweep and the tools refuse; keep it
  so. Keep the website stopped (`systemctl stop terrace-web.service`; it has no gate)
  and the sweep and backup timers stopped. **A reboot undoes this**: at boot the
  adopted host starts `terrace-production.target`, which arms the backup, check,
  freshness and sweep timers and starts the writers, the ungated website among
  them (`base.nix`; [restore promotion](restore-promotion.md) step 2 says the same
  for an original). On a host adopted with `--restored` whose timers are still held
  (`terrace-production status` names them), a reboot leaves those held timers off:
  they carry `ConditionPathExists=!` on their hold marker (`base.nix`). After any reboot in this state run `systemctl list-timers` and
  `systemctl is-active terrace-web.service`, then stop the website and the timers
  again (the same commands as step 6's first line and this item). The boot default
  must be the new generation (`nix-env --set` and `switch`, step 7), never an old
  one left by a `test` activation.
- **Do not roll back the generation and start the old CMS.** The old generation's
  gate expects the old revision, which is exactly what the marker holds, so the old
  CMS **would start** on a database that may be partly migrated. Do not run "restart
  after quiesce" either.
- Read the ledger to see which migrations were recorded. Whether Payload runs each
  migration in its own transaction, whether a killed run leaves a migration half
  applied, and whether a second `start` resumes cleanly are **not determined from the
  repository**. Treat the database as changed.
- Owner decision, before anything else: **fix forward** (a corrected pin, or a
  retry when the cause is known and outside the database, such as a guard), or
  **restore** the step 6 snapshot onto a replacement (U8, section 4.3). A blind
  retry of step 10 is not a decision the operator takes alone.
- **Fixing forward does not restart this procedure from the top.** Build and copy
  the corrected closure (steps 3 and 4), then go straight to activation (step 7),
  secrets (step 8) if needed, the realm (step 9) and the migration (step 10). Do
  **not** run step 5's checks as a gate (its marker check fails by design in this
  state) and do **not** run step 6 again the same day: a second snapshot today
  strips the keep-daily protection from the pre-update snapshot, the only restore
  point, and would itself hold a partly migrated database (H2). If the fix waits
  for another day, the scheduled backups stay stopped in the meantime, and the
  pre-update snapshot is then the last of its day.

Evidence: VM rehearsal (synthetic data), `platform/website-test.nix`, subtest
"explicit migration gate on actual immutable bundles": the pinned bundle migrates an
**empty** database, the ledger equals the pinned list, a second run leaves it
unchanged, refused starts change nothing, an accepted start stops the CMS, website
and a running sweep and leaves the CMS neither active nor failed. On restored data
with the **same** release: the ledger unchanged across the migration, production VM
check (`platform/production/test.nix`, replacement subtest). **A real upgrade**: RAN
IN VM (`platform/production/deploy-test.nix`, synthetic data): a host adopted at the
previous kept release `680ad56` (eleven migrations), with synthetic posts, a redirect,
media rows and files and a user written into it, activated to the pinned `dd1aef7`
and migrated: the marker holds the new revision, the ledger equals the new pin's
list, the only migration applied is the one the new pin adds
(`20261119_165021_roster_journal`), a second run applies nothing, the tables written
before are unchanged row for row, the media files are unchanged, and the CMS serves
again. One release pair, small synthetic data. **A failing migration and the time
limit: not exercised anywhere**, in this repository or in the website's release CI,
which migrates an empty database (website `tools/release/README.md` at `dd1aef7`).

### Part E. Start, check, finish

**Step 11. Start the CMS, website and sweep timer.** Nothing starts them after a
migration:

```sh
systemctl reset-failed terrace-cms.service terrace-web.service
systemctl start terrace-cms.service terrace-web.service
systemctl start terrace-cms-sweep.timer
```

`reset-failed` clears the start limit the gate failures of step 7 may have hit (the
website VM check does the same after repeated migrations). Each unit's start waits
for its own health check (`postStart` in `platform/website.nix`: the CMS at
`http://127.0.0.1:13048/api/events?limit=1`, the website at
`http://127.0.0.1:14294/events/`, the module's default ports, which the production
profile does not change), so "active" means it answered. The sweep timer is
persistent (`Persistent = true`): if 03:30 passed while it was stopped, the sweep
runs as soon as it is started (VM rehearsal, `platform/website-test.nix`: a
backdated stamp makes the restarted timer fire at once). Worked: the start returns
0 and both units are active.

Failed: a unit does not become active. Read `journalctl --namespace=terrace-apps -u
terrace-cms.service -o cat --no-pager` (and the same for `terrace-web.service`).
The migration has run: a generation rollback brings the gate mismatch of section
4.3, so the owner decides (U7) between fixing forward and a restore (U8). Keep the
website stopped while the CMS is down.

**Step 12. Health checks that exist on a production host.**

```sh
systemctl --failed
terrace-production status
terrace-secrets verify
terrace-tls check
terrace-identity-admin status
terrace-identity-admin realm plan --admin <you>
systemctl is-active terrace-cms-sweep.timer terrace-backup-freshness.timer terrace-identity-health.timer
```

Worked: no failed units; `adopted <time>` and `active`; `verify` PASS; every public
name passes the expiry check; `Keycloak ready: yes`; `Converged`; `active` for each
timer (the backup and check timers are started in step 13). The production host has
no functional checks beyond these (the 198 protocol assertions are rehearsal-only
scripts; [restore promotion](restore-promotion.md) step 20). Check the public site in
a browser yourself. Alerting does not exist (G-23): a writer that fails later shows
only in `systemctl --failed`.

Failed: name the failing check. A failing `terrace-tls check` follows
[TLS](tls.md#check-status); a failing `verify` follows step 8. Anything that the
update caused, once step 10 has run, is the owner's choice (U7) between fixing
forward and a restore (U8); before step 10, section 4.2.

**Step 13. Re-arm the backup timers, then U7.**

```sh
systemctl start terrace-backup.timer terrace-backup-check.timer
systemctl list-timers terrace-backup.timer terrace-backup-check.timer
```

Worked: both active, the backup's next run tomorrow. Do not take an extra backup
today (step 6, "How long the snapshot lasts"); tomorrow's scheduled run takes the
first post-update snapshot. If the restart crosses into the next day's 03:15, the
persistent timer runs the missed backup at once (READ); that is the next day's
snapshot and harmless to H2. Keep the previous generation (do not garbage-collect it)
and the previous archives until the owner says the update is final. Record the new
revision, the generation number and the snapshot ID. **U7**: the owner declares the
update done, or chooses a way back (section 4).

## 3. The relay host

The relay runs Headscale and nginx (`platform/production/relay.nix`); there is no
migration gate, no realm and no CMS. The app and relay configurations do not refer
to each other (READ: neither role module names the other), so the code imposes no
order between them. Update one host at a time and finish it (step R5) before the
other, so that a failure has one cause. Which goes first is the owner's choice (part
of U1); a suggestion, not from the code: the relay first for a Nix-inputs-only
change, because a failed relay does not take the site down, and it exercises the U0
mechanism on the smaller host.

**R1. Build, copy, record, back up.** Build and copy as steps 3 and 4, with `role =
"relay"` and the relay's private module (no archives, no TFC source:
`infra/nixos/production-config.nix` requires them for the app only). Check step 5's
window (today's run completed, not the weekly check). Then on the relay:

```sh
readlink -f /run/current-system
nix-env --profile /nix/var/nix/profiles/system --list-generations
systemctl --failed
sha256sum /var/lib/headscale/noise_private.key /var/lib/headscale/derp_server_private.key
systemctl stop terrace-backup.timer terrace-backup-check.timer
systemctl start terrace-backup.service
systemctl show terrace-backup.service -p Result --value
terrace-backup restic primary snapshots --json
```

No quiesce: the backup copies Headscale's SQLite through SQLite's online backup API
(`snapshot_headscale` in `platform/backup/backup.sh`). Worked: `success` and a new
snapshot ID; keep the two key hashes. The relay's own repository and retention
behave as in step 6 (its own group: one repository per role). Failed: start the two
timers again (`systemctl start terrace-backup.timer terrace-backup-check.timer`)
and stop. **U2** for the relay.

**R2. Activate** as step 7 (U3 first if Headscale's version changes). Activation
restarts Headscale if its package or settings changed: connected clients reconnect
(standard behaviour, not measured); nginx restarts if its configuration changed. A
Headscale version change can migrate its database at start (upstream behaviour, not
determined from the repository): treat it as irreversible (section 4.3).

**R3. Secrets** as step 8, only if the relay's inventory changed.

**R4. Checks.**

```sh
systemctl --failed
terrace-production status
terrace-secrets verify
terrace-tls check
sha256sum /var/lib/headscale/noise_private.key /var/lib/headscale/derp_server_private.key
```

Worked: no failed units; `adopted`, `active`; `PASS`; the check passes; the key
hashes equal R1's (a changed key means a new network identity for every client:
[secrets](secrets.md#inventory), "Network identity"; the R1 snapshot holds the old
keys, and the only documented way to put them back is a restore onto a replacement,
section 4.3). From outside, `https://<relay hostname>/health` answers 200 (the route
the production VM check uses). A client reconnecting is not checked by anything on
the host. Failed: without a Headscale version change, roll back as in section
4.1, then run only `systemctl start terrace-backup.timer terrace-backup-check.timer`
(the relay variant of "restart after quiesce": R1 stopped nothing else, and the
CMS units of section 4.2 do not exist on the relay); with a version change,
section 4.3.

**R5. Re-arm and U7 for the relay:** `systemctl start terrace-backup.timer
terrace-backup-check.timer`, as step 13.

Evidence: the production relay role is booted, pushed, adopted, serves Headscale and
HTTPS and backs up, VM rehearsal (synthetic data), `platform/production/test.nix`
"production relay role" subtest; and the live specialisation switch of step 7's
evidence ran on the relay. Updating the relay to a new revision, Headscale version
changes, client reconnection after an update: not exercised anywhere.

## 4. Going back

### 4.1 What a generation rollback reverts, and what it does not

With the deploy path: `sudo terrace-production rollback` (the newest older
generation, the checks of section 1.5, the same `terrace-switch` unit and a record;
after a migration it refuses unless `--cms-stays-down`). RAN IN VM. The standard
NixOS commands it runs, also the way without the deploy path (run the switch
detached, as in step 7):

```sh
nix-env --profile /nix/var/nix/profiles/system --list-generations
nix-env --profile /nix/var/nix/profiles/system --switch-generation <previous number>
systemd-run --unit=terrace-switch --collect --service-type=exec --wait -- /nix/var/nix/profiles/system/bin/switch-to-configuration switch
```

(With `switch-to-configuration test` in step 7 and no reboot, the profile and the
boot default are still the old generation: run only the last line.) If the previous
generation was garbage-collected, rebuild the previous revision with the previous
kept archives (step 3) instead.

| Reverted (it lives in the generation) | Not reverted (it lives in `/var/lib`, the store or the repository) |
| --- | --- |
| Packages and versions: Keycloak, Headscale, PostgreSQL minor, Node, the release bundles, the kernel (after a reboot) | The CMS database schema and data once step 10 started; Keycloak's schema after a Keycloak upgrade started; Headscale's database after a Headscale upgrade started |
| Unit definitions, timers, nginx configuration, firewall | The migration marker `/var/lib/terrace-cms/migrated-revision` (new revision after a successful step 10, old revision after a failed one) |
| The pinned revision the gate expects, the realm definition the identity tool plans against | Realm changes applied in step 9 (section 4.4) |
| The secrets manifest and the host tool's inventory | The host ciphertext and the store (they are outside generations, `secrets.nix`): pushed or rotated values stay |
| | The backup repository: snapshots taken and snapshots pruned; ACME state in `/var/lib/acme`; media and outbox files; the adopted marker |

RAN IN VM (`deploy-test.nix`, after the upgrade migration and `rollback
--cms-stays-down`): reverted, the release revision the gate expects (the running
manifest) and the CMS unit definition (it names the previous bundle again); not
reverted, the marker (still the new revision), the CMS ledger and schema, the media
files, the host ciphertext, the adopted marker and the timer hold markers. The rest
of the table: READ.

### 4.2 Case A: nothing irreversible ran

Step 10 not started (or the revision did not change), no realm `apply`, no Keycloak,
Headscale or PostgreSQL version change. Then a generation rollback is the whole way
back:

1. Stop the CMS and the website first: after a gated activation (step 7) they can
   still be retrying (`Restart=on-failure`), and `terrace-production rollback`
   refuses a release change while either is active, activating or reloading,
   naming the command: `systemctl stop terrace-cms.service terrace-web.service`
   (RAN IN VM: refused while the CMS retried, then accepted after the stop). Then
   roll back as in 4.1.
2. App: run "restart after quiesce" (step 6). The marker still holds the old
   revision, which the old generation's gate expects. Relay: run only `systemctl
   start terrace-backup.timer terrace-backup-check.timer` (R1 stopped nothing else;
   the relay has no CMS units).
3. If step 8 pushed a ciphertext with a name the old inventory does not take, the
   host keeps working (READ: `verify` checks only the manifest's names), but the
   next `push` is refused by the old generation's `install`: restore the store file
   saved in step 8 ([credential rotation](credential-rotation.md#2-procedures),
   general preconditions, "the way back") and push it, with the operator tool at
   the old revision.
4. Step 12's checks; the step 5 record is what "back" looks like.

Evidence: RAN IN VM (`deploy-test.nix`, "case A"): after activating the next release
and before its migration, `terrace-production rollback` ran generation 1 again;
"restart after quiesce" brought the previous CMS and website back, serving the
populated data, with the old marker and ledger unchanged. Items 3 and the relay:
not exercised anywhere.

**This case does not cover a step 10 that started and failed**: the marker then
still holds the old revision, and item 2 would start the old CMS on a database that
may be partly migrated. That is case B. `terrace-production rollback` refuses it
whenever the ledger records a migration the old release does not list (unit test;
a failed migration is not exercised in a VM).

### 4.3 Case B: a migration started, or an upstream schema changed

**After a successful step 10**, the old generation's gate expects the old revision
and the marker holds the new one: the old CMS refuses to start (RAN IN VM,
`deploy-test.nix`: after the upgrade migration on populated data, `terrace-production
rollback` refused, `rollback --cms-stays-down` ran the previous generation, and its
CMS failed with `CMS migration gate is closed`; activating the new generation again
brought the site back with the data unchanged). That refusal is the protection; do
not defeat it. **After a
failed step 10** there is no such protection on the old generation (step 10,
"Failed inside `bin/migrate`"): stay on the new generation with the CMS, website,
sweep and tools stopped until the owner decides.

**Hazard (H1). Never start the old generation's `terrace-cms-migrate.service` after
a newer migration ran.** It would run the old bundle's `bin/migrate` against the
newer schema (it has only `up` steps and no `down` entrypoint: the bundle ships
`bin/migrate`, `bin/start`, `bin/sweep`, `bin/roster-invite`, `bin/wp-import`,
`platform/website-release.nix`). What Payload does with ledger rows it does not know
is **not determined from the repository**; if it succeeds, it writes the **old**
revision to the marker and reopens the gate for old code on the new schema. The gate
compares revisions, not schemas; [website host](website-host.md#runtime-and-migration-boundary)
says it "does not prove schema downgrade compatibility". The migrations do carry
`down` functions in the website source (for example
`cms/migrations/20261119_165021_roster_journal.ts` drops the roster tables), but no
packaged tool runs them and running them deletes the data in those tables: not a
rollback path.

The website's runtime contract is explicit: "code rollback does not reverse
migrations. Restore only from a verified backup with the matching uploads and
application version" (website `tools/release/README.md` at `dd1aef7`). What exists:

1. **Fix forward**: a corrected pin, built, checked and deployed by this procedure.
   Suggested here when the new release is close to working; the owner has recorded
   no preference. Not exercised anywhere.
2. **Restore the pre-update snapshot onto a replacement host** built from the
   **previous** revision with the previous archives: [restore promotion](restore-promotion.md),
   whole. **U8** first: everything written since the snapshot (step 6) is lost, the
   site is down for the whole promotion, and the promotion has its own owner
   decisions (fencing, erasures, DNS). A restore **in place** onto the running host
   is not supported: the restore tool refuses live paths and [backups](backups.md#restore-procedure)
   says never to restore over a live host; loading one database into a populated
   cluster is done nowhere ([restore promotion](restore-promotion.md#4-what-a-production-promotion-needs-that-nothing-has-exercised)).
   A restore from a snapshot made by the new release onto the old release is the
   wrong pair; use the pre-update snapshot. Evidence: the restore promotion
   procedure itself is written, not drilled; restoring onto an older revision than
   the one that wrote the data: not exercised anywhere.

The same holds for the relay when Headscale migrated its database (R2), and for
Keycloak's database after a Keycloak upgrade (U3): a generation rollback brings back
the old binary over the new schema, and whether it starts is **not determined from
the repository**; the snapshot from step 6 or R1 is the way back.

### 4.4 Case C: only the realm changed

`realm apply` changes what the new definition declares: it updates declared
settings, adds roles, clients, mappers and scopes, and removes entries from the
exact lists (scope mappings, redirect URIs, web origins). It never deletes a user,
credential, session, client, role or the realm. After a generation rollback,
`realm plan` against the old definition proposes to set back the settings and exact
lists the old definition declares, and keeps what only the new one added. VM
rehearsal (synthetic data), `identity-admin-test.nix`, "realm reconciliation": going
back to the pinned definition plans `kept: realm role ...`, `kept: client ...`,
`kept: protocol mapper ...`, `kept: optional scope ...` together with a
`scope-mapping-remove` and a setting set back, and applies them; and an artifact
without the admin-events keys "plans nothing for them". Removing the kept roles,
clients, mappers or scopes needs a code change or a restore (case B). Run `plan`,
then `apply` with its hash only if the owner accepts every line (U4 again).

### 4.5 Secrets, TLS and backups are not rolled back

A push, a rotation ([credential rotation](credential-rotation.md)), a TLS mode change
([TLS](tls.md)), a repository password change: each has its own way back in its
document. A generation rollback changes none of them, but it can change which names
the host accepts (4.2, item 3) and which TLS mode nginx uses: TLS mode changes are
outside this procedure (1.1), and rolling back across one follows
[TLS, "Fall back to the manual path"](tls.md#fall-back-to-the-manual-path) or
"Switching a live manual host to ACME", in their orders, not a bare rollback.

## 5. Reboots, kernels and the hazards found while reading

**Kernel, initrd or kernel-module changes (U6).** Activate with `boot` instead of
`switch` in step 7 (still detached), then `systemctl reboot`. `test` is not an
option here: it does not load a new kernel. At boot `terrace-production-boot`
verifies the secrets and starts the writers (`base.nix`). On a re-pin the CMS then
fails its gate exactly as in step 7 until step 10; continue at step 8. Evidence: a
reboot of an adopted app host into the **same** generation, writers back by
themselves, secrets from the ciphertext, VM rehearsal (synthetic data):
`platform/production/test.nix` ("reboot: ramfs vanishes, ...") and
`platform/production/tls-test.nix` ("reboot keeps the certificate and the account
key", including the persistent freshness timer firing at boot). A reboot into a
**new** generation: not exercised anywhere.

Hazards, each also in the [backlog](backlog.md#host-update-follow-ups-g-37--97-recorded-2026-11-24):

- **H1. An old migration unit reopens the gate on a newer schema** (section 4.3).
- **H2. The pre-update snapshot can be pruned within about two days** if a second
  snapshot of the host is taken the same day (step 6): a retry of step 6, an extra
  backup, or a scheduled run that had not happened yet. There is no pin.
- **H3. No path for a closure to reach an installed production host** unless the
  module it was installed with sets `deploy.trustedKeys` (section 1.5). Root SSH is
  refused, `operator` is not a trusted Nix user and signatures are required (RAN
  defaults, and RAN IN VM). Without U0 settled in the module a host is installed
  with, the first update needs another route (section 1.5, "Bootstrap"), a console
  or a reinstall.
- **H4. A new generation that does not boot.** The profile disables the emergency
  shell (`systemd.enableEmergencyMode = false`, `base.nix`); choosing the previous
  boot entry needs the provider's console, and how to reach it is **not determined
  from the repository** (no provider access exists). For updates without a kernel,
  initrd or module change, `switch-to-configuration test` before the migration
  limits this to reboots after step 10; a kernel change always needs a reboot.
- **H5. Deploy-then-push window for a new secret name** (step 8): a reboot in between
  leaves every writer stopped until the push; a push can restart (perhaps start) the
  ungated website on a stopped CMS.
- **H6. Going back after a migration means a whole restore promotion onto a
  replacement**, losing everything written since the snapshot (section 4.3).
- **H7. The website has no revision gate.** `production-profile.md` ("a revision
  mismatch still fails through the existing gate"), the comment in
  `platform/production/app.nix` and [website host](website-host.md#runtime-and-migration-boundary)
  ("Startup rejects a missing/different revision marker") read as if both services
  were gated; only the CMS, its sweep and the operator tools are
  (`requireMigration` in `platform/website.nix`). After a re-pin the website runs
  the new bundle against a gated CMS: RAN IN VM, the activation's switch starts it,
  it fails its health check against the stopped CMS and restarts until its start
  limit, three to four minutes; `activate` records it among `startingUnits` and says
  to stop it. After a forced rollback the same happens with the old bundle against a
  CMS that refuses to start (RAN IN VM). Neither loses data (the website holds no
  database credential); what it serves meanwhile is not determined. A proposed
  follow-up, not decided: give the website the CMS's `requireMigration` gate
  ([backlog](backlog.md#host-update-follow-ups-g-37--97-recorded-2026-11-24)).
- **H8. Generations accumulate.** No garbage collection and no boot-entry limit by
  default (RAN); every update adds a boot entry to the EFI system partition, whose
  size the hardware module sets. Collecting garbage, by hand, removes rollback
  targets. Neither effect is measured.
- **H9. A failed or killed migration leaves the old marker.** The old generation's
  gate then opens for a possibly partly migrated database (step 10, case A's
  exclusion). The 180-second start limit can kill a long migration on real data.

## 6. Traceability: evidence per step

"Production VM check" is `platform/production/test.nix` (production profile, instance
`rehearsal`, synthetic `.invalid` names, guests on a private test network). Nothing
below ran on a provider: lease 5 ran the rehearsal profile and no update.

| Step | Code that does the equivalent | What it asserts | Evidence, and how it differs from an update |
| --- | --- | --- | --- |
| 2. Classify | none | none | READ only |
| 3. Build | `infra/nixos/production-config.nix`; every production VM check builds the same roles | Evaluation invariants, archive hashes | VM rehearsal (synthetic data) for a fresh host; a build for a real private module: not exercised anywhere |
| 4. Copy the closure | `platform/production/terrace-deploy.sh` (opt-in deploy path); `copy_store` in `infra/nixos/rescue_install.py` (root, Rescue system, install only) | Signed copy as `operator` accepted; unsigned and foreign-signed copies refused by the host's Nix daemon | RAN IN VM (`deploy-test.nix`, app role, operator guest as build machine). The Rescue copy is not an update path. A real host and the relay: not exercised anywhere |
| 5. Record state | `terrace-production status` in `base.nix`; `test.nix` asserts only the "adoption as new not finished" string | That string | The `adopted <time>` and `active` outputs: READ. The window checks: not exercised anywhere |
| 6. Quiesce, back up | "back up, destroy the host" subtest, `test.nix` | Backup succeeds, snapshot listed | VM rehearsal (synthetic data); that subtest also stops Keycloak and nginx. Keeping Keycloak up, stopping and re-arming the backup timers, retention of the pre-update snapshot: not exercised anywhere |
| 7. Activate | `terrace-production activate` (`deploy-test.nix`); `switch-to-configuration test` to a specialisation, relay, `tls-test.nix` | The refusals of section 1.5 with controls; the new generation runs, its record and `status`; the switch completes after the SSH client is killed | RAN IN VM for the app role, `switch` mode, a new release; the relay's same-build `test` switch as before. `boot`, a reboot into a new generation, the relay's update: not exercised anywhere |
| 7. Gate closes on a re-pin | `requireMigration`; the tool refusal in `website-test.nix`; the re-pin in `deploy-test.nix` | `CMS migration gate is closed` from the tool; on a real re-pin the CMS's gate message, its `start-limit-hit`, the website's health-check failure and start limit, the switch's exit 4 recorded as `switched; down until the CMS migration` | VM rehearsal (synthetic data): the tool with the marker moved away (website module), and the CMS and website on a re-pin of the production app role. The sweep refusal: READ |
| 8. Secrets after activation | `cmd_install`, `cmd_verify`; "rotate the intake secret" in `test.nix` | Push, verify, CMS and website restarted | VM rehearsal (synthetic data) for a push to an adopted host; an inventory change, a push with the CMS stopped: not exercised anywhere |
| 9. Realm | "realm reconciliation" in `identity-admin-test.nix` | One planned change applied; users and credentials unchanged; converged; back to pinned keeps roles, clients, mappers, scopes | VM rehearsal (synthetic data); re-pin simulated by rewriting the tool's configuration |
| 10. Migrate | "explicit migration gate" in `website-test.nix`; replacement subtest in `test.nix`; upgrade in `deploy-test.nix` | Pinned ledger; idempotent; refusals stop nothing; accepted migration stops the apps; upgrade adds exactly the new pin's migration, data unchanged | VM rehearsal (synthetic data) on an empty database, on restored data of the same release, and an upgrade `680ad56` to `dd1aef7` on populated synthetic data. A failed or timed-out migration: not exercised anywhere |
| 11. Start | `website-test.nix` (`reset-failed`, start after migrations); persistent sweep timer catch-up | Units active; missed sweep runs at once | VM rehearsal (synthetic data), website module |
| 12. Checks | `terrace-tls check` (`tls-test.nix`), `verify` (`test.nix`), `realm plan` (`identity-admin-test.nix`) | Each check's own output | VM rehearsal (synthetic data), each separately; together after an update: not exercised anywhere |
| 13. Re-arm timers | none | none | Not exercised anywhere |
| R1-R5 Relay | "production relay role" subtest in `test.nix`; the relay switch in `tls-test.nix` | Adoption, HTTPS, backup; live switch | VM rehearsal (synthetic data) for boot and a same-build switch; an update: not exercised anywhere |
| 4.1-4.3 Rollback | `terrace-production rollback` (`deploy-test.nix`) | Case A: previous site back, data and ledger unchanged. Case B: refused without `--cms-stays-down`; with it, the previous CMS fails its gate; what reverts and what does not (4.1) | RAN IN VM, app role. Restore onto a replacement (4.3, item 2), the relay, a rollback after a Keycloak or Headscale change: not exercised anywhere |
| 4.4 Realm | the realm back-to-pinned block | Realm: kept items, exact-list removals | VM rehearsal (synthetic data) |
| 5. Reboot into a new generation | reboot subtests in `test.nix`, `tls-test.nix` (same generation) | Writers start alone; secrets return; certificate kept | VM rehearsal (synthetic data) for the same generation only |

## 7. What this document does not cover

- Changing public host names, the relay's address or DNS: Keycloak's issuer
  follows the host name and the realm keeps its old redirect addresses
  ([restore promotion](restore-promotion.md#4-what-a-production-promotion-needs-that-nothing-has-exercised),
  "Public names changing"). Not exercised anywhere.
- TLS mode changes: [TLS](tls.md) (section 1.1).
- A PostgreSQL major upgrade: no tool exists (step 2).
- Choosing pins (which advisories to take, G-44): runbook 9.7's own review.
- Producing a new pin in Womb (re-pin, VM checks): runbook 6.12 to 6.14 and
  [website host](website-host.md#reproduce-the-actual-artifact-vm-check).
- Documenso, Windmill, Vaultwarden, K3s: not in the production profile.
- Rotating credentials ([credential rotation](credential-rotation.md)), except where
  an update meets them (steps 8 and 4.5).
- Unattended or scheduled updates, and a promotion pipeline across hosts: none
  exists (runbook 9.7, G-37).
- Members' sessions and tokens across an update; Keycloak's own session handling on
  a Keycloak upgrade: not determined from the repository.

## 8. Drills to add

Status 2026-11-24: the deploy VM rehearsal (`platform/production/deploy-test.nix`,
section 1.5) covers drill 2 (both refusals), drill 3 (one release pair, small
synthetic data), drill 4 (the CMS gate message at its start limit; the website,
started ungated by the switch, recorded until it fails at its start limit; the sweep
and `terrace-cms-tool`: not in it), drill 5, drill 6 without its H1 part (the old
migration unit is never started) and drill 15 (the session dropped by killing the
SSH client; the switch there does not restart sshd). Drill 1 is covered for the app
role only, by its steps 3, 4, 7, 10 and 11 (no backup, realm or secrets step). The
others are open. It is not a drill of this whole written procedure.

Not implemented here. A later task would add a VM drill of **this written procedure**
on the production profile, as a script whose commands are taken from this document,
asserting each step's "worked" output. Beyond what the existing checks assert, it
needs:

1. **Two revisions.** Build the app and relay roles at the pinned revision and at a
   second one (a real later pin, or the previous kept pin `eab4189` to the current
   `dd1aef7`), adopt the first, write data with the first release (members, roster
   journal, media, outbox), then update to the second by steps 3 to 13.
2. **The transfer mechanism chosen at U0**, including the refusal of an unsigned or
   untrusted copy.
3. **Upgrade migration on populated data**: ledger before and after equals the two
   pins' lists; row counts and media hashes unchanged except where the migrations
   say otherwise; the second migration run is a no-op.
4. **Gate behaviour after activation**: the CMS fails with the gate message, the
   website's state is recorded (H7), the sweep and `terrace-cms-tool` refuse.
5. **Generation rollback before the migration** (case A): the old site serves
   again with the old marker and ledger.
6. **Rollback after the migration** (case B): the CMS refuses; then record what the
   old migration unit does on the newer schema (settles H1), and assert the
   documented answer.
7. **A failed and a timed-out migration** (H9): a migration that fails part-way and
   one held past 180 seconds; record the ledger, the marker and what a second start
   does, and assert that the documented failed branch keeps every CMS unit stopped.
8. **`switch-to-configuration test` then reboot** before the migration: the old
   generation comes back.
9. **`boot` and reboot into the new generation**: writers start from the boot gate;
   the CMS gate behaves as in step 7.
10. **Inventory change**: a declared name added; activation (record what
    `sops-install-secrets` does) then push (record whether stopped units start); a
    reboot in between leaves the writers gated (H5); a name removed.
11. **Realm producer change** delivered by a real activation, not by rewriting the
    tool's configuration.
12. **Relay update** with Headscale peers: key hashes unchanged, the three clients
    reconnect without enrolling again.
13. **Retention and timers**: the pre-update snapshot survives the documented number
    of runs, or step 6's wording is corrected (H2); stopping and re-arming the
    backup timers the same day starts no backup.
14. **Restore-based rollback**: the pre-update snapshot restored onto a replacement
    built from the previous revision ([restore promotion](restore-promotion.md)),
    ledger equal to the old pin's.
15. **Activation over SSH**: the session dropped during a `systemd-run` switch that
    restarts sshd; the switch completes.
16. **Timings**: the site's downtime from step 6 to step 11, per step.

Backlog items for the gaps and hazards are in
[backlog](backlog.md#host-update-follow-ups-g-37--97-recorded-2026-11-24).
