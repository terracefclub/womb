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
  `platform/website.nix`: the CMS (and its sweep and operator tools) start only
  when `/var/lib/terrace-cms/migrated-revision` holds exactly the pinned
  revision. **Migration**: `terrace-cms-migrate.service`, which writes that marker
  when the bundle's `bin/migrate` succeeds.
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
- the private host module changes (operator keys, escrow recipient, TLS mode,
  memory, backup target): the same mechanism, which the
  [credential rotation](credential-rotation.md) and [TLS](tls.md) documents call
  "deploy" without a command.

It does not apply to a first deployment ([cold start](production-profile.md#cold-start-of-a-new-deployment)),
to a replacement host ([restore promotion](restore-promotion.md)), to changing
public host names or the relay's address (see section 7), or to adding Documenso,
Windmill or Vaultwarden, which the production profile refuses
([production profile](production-profile.md#decisions-waiting-on-the-owner)).

### 1.2 How a new configuration reaches a host today

READ, for every row.

| Piece | Exists? | Where |
| --- | --- | --- |
| Building a host's toplevel from a revision, the private module, the hardware module, the archives and the TFC source | Yes | `infra/nixos/production-config.nix`; the command is in [production profile](production-profile.md#building-a-host). Build on the Linux builder, never on a Mac (AGENTS.md) |
| Refusing a wrong archive | Yes | `platform/website-release.nix` refuses an archive whose SHA-256 is not the pinned one before copying it, and checks every packaged file against the bundle's own manifest; `platform/identity-contract.nix` does the same for the three realm-producer files |
| Installing a first closure on a blank disk | Yes, lease tooling only | `infra/nixos/rescue_install.py` copies closures as `root` to a Rescue system and runs `nixos-install` ([Rescue installer](cloud-rescue-install.md)). It is not an update path: it formats the disk |
| A command that copies a new closure to an installed production host | **No** | Nothing in the repository does it. The legacy root `flake.nix` names `deploy-rs` as an input; nothing in the new platform uses it |
| A command that activates or rolls back a generation on a production host | **No** | Nothing in the repository wraps `switch-to-configuration` or the system profile for production |
| Automatic upgrades, garbage collection, a boot-entry limit | No | The production modules set none of `system.autoUpgrade`, `nix.gc`, `boot.loader.systemd-boot.configurationLimit` (grep of `platform/production/` and `infra/nixos/`). RAN: in the pinned nixpkgs `31ecbaca` their defaults are off, off and no limit. A private or hardware module could change them |
| The private host module | Kept by the operator outside this repository | [production profile](production-profile.md#building-a-host). Its content is the operator's; this document cannot see it |

Why copying a closure is not a one-liner on this profile (READ, plus RAN for the
defaults):

- SSH accepts only the `operator` account (`AllowUsers operator`, `PermitRootLogin
  no`, asserted in `platform/production/base.nix`). The installer's `root@` copy
  cannot be reused.
- RAN: in nixpkgs `31ecbaca` the defaults are `nix.settings.trusted-users = ["root"]`
  and `require-sigs = true`, and nothing in the production modules changes them. So,
  by standard Nix behaviour (not tested here), a `nix copy` to the host as
  `operator` is refused for paths that are not signed by a key the host trusts,
  and the builder's paths are not signed by any such key.
- `operator` has passwordless `sudo` (`security.sudo.wheelNeedsPassword = false`,
  `base.nix`), so anything root can do on the host, the operator can.

**Owner decision U0 (once, before the first update of any host): how closures
reach the hosts.** The repository supports none of these; each is a standard Nix
mechanism that nobody has tried on this profile:

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
   configured Linux builder, so this contradicts the project rules.

Options 1 and 2 are themselves private-module changes, so they must be in the
module a host is **installed** with, or the first update has no path. Record the
choice; section 8 lists the drill it needs.

### 1.3 What must exist before an update

| Item | Why | Where |
| --- | --- | --- |
| The new revision merged, with the VM checks that match the change passed at that revision | A pin change must re-run the matching VM check (runbook 9.7) | `platform/production-check` (always), `platform/website-check` and `platform/website-gateway-check` (re-pin), `platform/production-identity-check` (realm producer changed), `platform/production-tls-check` (TLS change); [production profile](production-profile.md#verification), [website host](website-host.md#reproduce-the-actual-artifact-vm-check), [identity administration](identity-admin.md#verification) |
| The **new** kept release archives and the TFC source at the new revision | The build reads them; the archives are not byte-reproducible, so only the one kept build matches its pinned hash; a rebuild of the same commit will not ([website host](website-host.md#accepted-inputs); website `tools/release/README.md` at `dd1aef7`) | Operator's protected storage |
| The **previous** kept archives and TFC source | To rebuild the previous revision if its generation is gone, and for a restore-based rollback onto a replacement (section 4) | Same; runbook 9.17 "Worked if: the previous release is still on disk for rollback" |
| The private host module and hardware module for this host | Part of every build | Operator's private directory |
| Shell access as `operator` with a second session open | Several steps restart services | Operator machine |
| A Keycloak administrator account (app role, if the realm may change) | `realm plan` and `realm apply` need `--admin` and its password | [identity administration](identity-admin.md#before-you-start) |
| The store, escrow identity and the operator tool at the **new** revision (only if `platform/production/inventory.json` changed) | The operator tool reads its own copy of the inventory | [secrets](secrets.md#operator-tool) |
| A decided transfer mechanism (U0) | Section 1.2 | Owner |

### 1.4 Decisions that are the owner's

Each is a stop point: nothing after it runs until the owner says go.

| Point | Decision | Needs to be known first |
| --- | --- | --- |
| U0 | How closures reach the hosts (once) | Section 1.2 |
| U1, before step 1 | Go for this update, on this host, in this window | The diff between the running and the new revision, the VM check results, whether the pin, the realm producer, the inventory, the kernel or Keycloak/Headscale/PostgreSQL versions change (step 2), and the time of the next scheduled backup (03:15) and sweep (03:30) |
| U2, after step 4 | Accept the pre-update snapshot as the way back | Its ID and time; that the CMS was stopped while it was taken; how long retention keeps it (step 4, "How long the snapshot lasts") |
| U3, before step 8 | Apply the realm plan | The printed plan and its hash; that `apply` never deletes and a generation rollback does not undo it (section 4) |
| U4, before step 9 | Run the CMS migration: **the point of no return** for the CMS database | After the migration, the only documented way back for the database is a restore onto a replacement host (section 4) |
| U5 | Reboot (kernel or boot-path change) | Console access at the provider in case the new generation does not boot (section 5, H4) |
| U6, after step 12 | Declare the update done, or go back (section 4) | The checks of steps 10 to 12 |
| U7 | Go back by restore onto a replacement | The data written since the pre-update snapshot is lost (section 4, case B) |

## 2. The app host procedure

All commands run as root on the app host (`ssh -t operator@<app address>`, then
`sudo -i`) unless a step says "operator machine". `<...>` marks a value you
supply. The relay is in section 3, reboots in section 5.

### Part A. Prepare (nothing changes on the host)

**Step 1. Owner decision U1.** Record who decided, when and why.

**Step 2. Operator machine: classify the change.** At the new revision, against the
revision the host runs:

```sh
git diff --stat <running revision> <new revision> -- platform/ infra/nixos/
git diff <running revision> <new revision> -- platform/website-release.json platform/identity-contract.json platform/production/inventory.json platform/flake.lock
```

What each file means for the procedure (READ):

- `platform/website-release.json` `revision` changed: the migration gate closes at
  activation (steps 6 and 9). Same revision: the gate stays open and step 9 does
  not apply.
- `platform/identity-contract.json` changed: the realm definition changes; Keycloak's
  startup import skips an existing realm ([identity host](identity-host.md)), so
  step 8 reconciles it. Run step 8's `plan` after every app update anyway; it says
  `Converged` when nothing changed.
- `platform/production/inventory.json` changed: the secret names change; step 7 has
  the order.
- `platform/flake.lock` (nixpkgs) changed: Keycloak, Headscale, PostgreSQL minor
  versions, Node and the kernel can change. A Keycloak or Headscale version change
  is a schema change on their own databases at start (READ: the comment "the
  upstream notify plugin can announce startup before schema migration finishes"
  in `platform/identity.nix`; Headscale's behaviour is upstream and **not
  determined from the repository**); treat it as irreversible like step 9. A
  PostgreSQL **major** change is not supported by anything here: the app role pins
  `postgresql_18` (`platform/production/app.nix`) and no upgrade tool exists.

**Step 3. Operator machine: build the new toplevel** on the Linux builder, with the
command in [production profile](production-profile.md#building-a-host) (the new
revision's checkout, this host's private and hardware modules, the new archives and
TFC source). Record the printed store path as `<new toplevel>`. Worked: one
`/nix/store/...` path. Failed: an evaluation assertion names the problem (the
profile refuses unsafe configurations, [production profile](production-profile.md#invariants-enforced-at-evaluation));
`Terrace web archive hash mismatch` or `... cms archive hash mismatch` means the
archive is not the kept build (`platform/website-release.nix`). Evidence: the build
is what the VM checks build; READ for a real host.

On the host, record where you start:

```sh
readlink -f /run/current-system
nix-env --profile /nix/var/nix/profiles/system --list-generations
terrace-production status
systemctl --failed
cat /var/lib/terrace-cms/migrated-revision
runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d terrace_cms -c "select name||':'||batch from payload_migrations order by id"
systemctl list-units --plain --no-legend --state=active,activating,deactivating 'terrace-cms-tool-*'
```

Worked: `adopted <time>` and `active`; no failed units; the marker equals the
running pin's revision; the ledger lists the running pin's migrations; the last
command prints nothing (no operator tool runs; a running tool makes step 9 refuse,
[website host](website-host.md#operator-tools-terrace-cms-tool)). Keep this output:
it is what step 12 and a rollback compare against.

### Part B. Back up (the way back)

**Step 4. Quiesce the CMS and take the pre-update backup.**

```sh
systemctl stop terrace-cms-sweep.timer
systemctl stop terrace-cms-sweep.service terrace-web.service terrace-cms.service
systemctl start terrace-backup.service
systemctl show terrace-backup.service -p Result --value
terrace-backup age
terrace-backup restic primary snapshots --json
```

Why stop the CMS: a backup dumps each database in one snapshot but reads media
files afterwards, so a running CMS can leave the database and the uploads
disagreeing ([backups](backups.md#what-runs-and-its-resource-bounds), "Consistency").
Keycloak and PostgreSQL keep running: the Keycloak database is one dump, consistent
on its own (READ). The site is down from here until step 10. This is the same quiesce
as [restore promotion](restore-promotion.md) step 2 minus Keycloak and nginx.

Worked: `success`; `terrace-backup age` exits 0; the newest entry in the list is
from now, with this host's name. Note its `id`. With a secondary target
(`secondary-backup`), the same unit backs up both (`terrace-backup run` loops over
the targets, `platform/backup/backup.sh`): check `terrace-backup restic secondary
snapshots --json` too. Failed: `target primary: backup FAILED` in the log; start
the CMS and website again (`systemctl start terrace-cms.service terrace-web.service
terrace-cms-sweep.timer`) and do not update.

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
seven days. If another snapshot of the same host is taken later that day (an extra
backup after the update, or an update started before 03:15), it stops being the
day's last and only keep-last protects it: after three newer snapshots, it is
pruned, which with the daily timer is about two days. Nothing in the tooling pins a
snapshot ([restore promotion](restore-promotion.md), step 21). So this procedure
does not take a second backup the same day (step 12), and the update window should
start after the day's 03:15 run.

**U2.** The owner accepts the snapshot (ID and time recorded) as the way back.

Evidence: backup and quiesce, VM rehearsal (synthetic data): the same commands
in the "back up, destroy the host" subtest of `platform/production/test.nix`
(backup succeeds, snapshot listed). Stopping only the CMS units and keeping
Keycloak up for a backup: not exercised anywhere. The retention reasoning: not
exercised anywhere.

### Part C. Put the new generation on the host and activate it

**Step 5. Copy the closure to the host.** By the mechanism decided at U0. Not
determined from the repository; not exercised anywhere. Worked: `test -e <new
toplevel>/bin/switch-to-configuration` succeeds on the host.

**Step 6. Activate.** Standard NixOS, READ from the toplevel's layout, never run on
this profile:

```sh
nix-env --profile /nix/var/nix/profiles/system --set <new toplevel>
<new toplevel>/bin/switch-to-configuration switch
```

The first line adds the new generation to the system profile; the second writes
the boot entries (the profile's newest generation becomes the default) and switches
the running system. If the kernel, initrd or kernel modules
differ (compare `readlink -f /run/booted-system/kernel` with `readlink -f <new
toplevel>/kernel`), use `boot` instead of `switch` and follow section 5.

An alternative before the migration: `<new toplevel>/bin/switch-to-configuration
test` (without the first line) switches the running system but leaves the old
generation as the boot default, so a reboot returns to it. It is the only activation
mode any VM check runs on the production profile (below). After step 9 it is the
wrong choice: a reboot would bring back the old generation onto the migrated
database (section 4, case B). If you use it, run the two lines above before step 9.

What activation does to this host (READ; which units a switch restarts, starts or
leaves is systemd and NixOS behaviour that no test here observes on the production
profile):

- Every unit whose definition changed and that runs is restarted. On a re-pin the
  CMS and website units change (new bundle paths), but after step 4 they are not
  running; whether the switch starts them anyway, because the active
  `terrace-production.target` wants them, is not determined here (see the gate
  below for what happens if it does). Keycloak and PostgreSQL restart if their package or settings changed;
  nginx if its configuration changed (a few seconds without HTTPS, as in
  [TLS](tls.md#switching-a-live-manual-host-to-acme)).
- `sops-install-secrets.service` runs with the new manifest
  (`platform/production/secrets.nix`); the host copy of the ciphertext
  (`/var/lib/terrace-secrets/secrets.sops.json`) is not part of the generation and
  does not change ("rotation needs no rebuild").
- **On a re-pin the CMS cannot start until step 9.** The production profile adds
  `ConditionPathExists` on the marker (`platform/production/app.nix`), and the
  marker exists, so the CMS runs its gate and fails: `CMS migration gate is closed;
  run the explicit migration unit for this release.` After three attempts in ten
  minutes it stays failed (`StartLimitBurst = 3`, `platform/website.nix`). The
  **website has no such gate**: `terrace-web.service` checks only the identity
  settings (`identityGuard "web"`) and the input unit checks that both bundles carry
  the new revision, so if anything starts it, it runs the new bundle while the CMS
  is down (READ; section 5, H7). The sweep and `terrace-cms-tool` refuse with the
  same gate message (website VM check, below).
- Units and timers are wanted by `terrace-production.target`, not by a boot target
  (`base.nix`); the adopted marker and the boot gate are unchanged by a switch.

Worked: the command ends; `readlink -f /run/current-system` is `<new toplevel>`;
`systemctl --failed` lists nothing, or on a re-pin only `terrace-cms.service`
(gate) and perhaps `terrace-web.service`. `switch-to-configuration` reports a failed
unit with a non-zero exit status (standard behaviour, not tried here), so a re-pin's
expected gate failure makes the command "fail": read the list, do not retry
blindly. Failed otherwise: anything else in `systemctl --failed` (Keycloak,
PostgreSQL, nginx, `sops-install-secrets`): go to section 4 before step 8.

Evidence: a live `switch-to-configuration test` on a running, adopted production
relay, between two closures prepared in one build (a specialisation), is VM
rehearsal (synthetic data): `platform/production/tls-test.nix`, subtest "relay:
live switch from ACME to the manual certificate and back, never the placeholder"
(clients saw only valid certificates, no failed units). A new closure from a
different revision, the app role, `switch`, `boot` and the system profile: not
exercised anywhere. The gate refusing the CMS, the sweep and the tools with a
missing or wrong marker: VM rehearsal (synthetic data), `platform/website-test.nix`
(the tool refused with the marker moved away), the website module only, not the
production profile.

**Step 7. Secrets, only if `inventory.json` changed (step 2).** The host's tool
reads the inventory of its own generation (`platform/production/package.nix` sets
`TERRACE_SECRETS_INVENTORY` to the packaged file), and `install` refuses a
ciphertext with names this host does not take (`validate_ciphertext` in
`platform/production/terrace-secrets`). So the order is: activate (step 6) first,
then from the operator machine with the new revision's tool. Save a copy of the
store file first and prove the escrow identity opens it, as the
[credential rotation](credential-rotation.md#2-procedures) general preconditions
say. `generate` refuses a host that already has secrets (`cmd_generate`), so a
newly added name is created with `rotate` (a generated value; add `--manual` when
its inventory `rotation` is `manual`) or `set` (an external value, read from stdin):

```sh
terrace-secrets rotate app <new name>        # or: terrace-secrets set app <new name> <file
terrace-secrets check app
terrace-secrets push app --address <app address> --identity <operator ssh key>
```

then on the host `terrace-secrets verify`. Worked: `check` lists no `missing` or
`unexpected` name; `Installed <n> encrypted secrets.` and `Host state: adopted`;
`verify` prints `PASS`. **Between step 6 and
this push, a reboot leaves the writers stopped**: at boot `terrace-production-boot`
runs `terrace-secrets verify` and starts the writers only if it passes
(`base.nix`), and with a newly declared secret missing it fails (READ: `check_file`
reports a missing file). Whether `sops-install-secrets` keeps the previous secrets
generation when the ciphertext lacks a newly declared name is sops-nix behaviour,
**not determined from the repository**. The same window exists for an
`escrowRecipient` change ([secrets](secrets.md#escrow-key-lost-or-leaked)). A
**removed** name needs no push to keep the host running (READ: `verify` checks only
the manifest's names), but every later `push` is refused, by the operator's `check`
(`unexpected <name>`) and by the host's `install`, until the store drops the name,
and the tool has no general command that deletes a name from the store (only `host
tls` drops `tls/*`): how to remove one is **not determined from the repository**.
Evidence: adding or removing an inventory secret on a running host: not exercised
anywhere. A push and `verify` on an adopted host: VM rehearsal (synthetic data),
`platform/production/test.nix` ("rotate the intake secret").

### Part D. Identity, then the migration (app role, re-pin)

**Step 8. Reconcile the realm.** After activation, the tool's configuration names
the new build's realm definition (`/etc/terrace/identity-admin.json`,
`platform/production/identity-admin.nix`):

```sh
terrace-identity-admin realm plan --admin <you>
```

`Converged` means nothing to do. Otherwise read the plan ([identity
administration](identity-admin.md#6-reconcile-the-realm-after-a-new-release-g-39)
explains every line), then **U3**, then:

```sh
terrace-identity-admin realm apply --admin <you> --plan-sha256 <hash from the plan>
terrace-identity-admin realm plan --admin <you>
```

Worked: `Applied N change(s); the realm now matches the definition.` and then
`Converged`. Failed: exit 1 (refused) or 3 (the plan holds a refused item): nothing
was applied; do not continue to step 9 with a realm the new CMS may depend on;
decide with the owner (section 4, case A still applies). A new client needs its
secret delivered first (step 7): `apply` creates a client only if its secret is
delivered. The order "realm before migration" is chosen here so that the CMS
starts against the reconciled realm; whether a CMS release depends on it is **not
determined from the repository**.

Evidence: VM rehearsal (synthetic data), `platform/production/identity-admin-test.nix`,
subtest "realm reconciliation": a realm imported from the previous release's
producer (`680ad56`), re-pinned to `eab4189`'s, planned as exactly one change,
applied, read back from Keycloak's database, users, credentials and the realm
identity unchanged. The re-pin there is simulated by rewriting
`/etc/terrace/identity-admin.json`, not by activating a new generation.

**Step 9. U4, then migrate.** The point of no return for the CMS database
(section 4). Check again that no operator tool runs (the step 3 command; no
output), then:

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
applies nothing is shown by the website VM check below), and writes
the new revision to the marker. It never restarts anything.

Worked: both starts succeed; the marker equals the new `revision` in
`platform/website-release.json`; the ledger names are exactly that file's
`migrations` list, in order (operator machine: `jq -r '.migrations[]'
platform/website-release.json`), and the second reading equals the first (the
website's runtime contract: "Run it once more to confirm no pending migration").
Failed: the unit failed; read `journalctl --namespace=terrace-apps -u
terrace-cms-migrate.service -o cat --no-pager`. A refusal by a guard or an
operator tool changed nothing ([website host](website-host.md#operator-tools-terrace-cms-tool)
has the recovery). A failure **inside** `bin/migrate` prints only `CMS release
migration failed; ...` (the tool hides the cause, `release-migrate.ts`); whether
Payload ran each migration in its own transaction and how far it got is **not
determined from the repository**: read the ledger, and treat the database as
changed (section 4, case B).

Evidence: VM rehearsal (synthetic data), `platform/website-test.nix`, subtest
"explicit migration gate on actual immutable bundles": the pinned bundle migrates an
**empty** database, the ledger equals the pinned list, a second run leaves it
unchanged, refused starts change nothing, an accepted start stops the CMS, website
and a running sweep and leaves the CMS neither active nor failed. On restored data
with the **same** release: the ledger unchanged across the migration, production VM
check (`platform/production/test.nix`, replacement subtest). **A migration of data
written by an older release (a real upgrade): not exercised anywhere**, in this
repository or in the website's release CI, which migrates an empty database
(website `tools/release/README.md` at `dd1aef7`).

### Part E. Start, check, finish

**Step 10. Start the CMS, website and sweep timer.** Nothing starts them after a
migration:

```sh
systemctl reset-failed terrace-cms.service terrace-web.service
systemctl start terrace-cms.service terrace-web.service
systemctl start terrace-cms-sweep.timer
```

`reset-failed` clears the start limit the gate failures of step 6 may have hit (the
website VM check does the same after repeated migrations). Each unit's start waits
for its own health check (`postStart` in `platform/website.nix`: the CMS at
`http://127.0.0.1:13048/api/events?limit=1`, the website at
`http://127.0.0.1:14294/events/`, the module's default ports, which the production
profile does not change), so "active" means it answered. The sweep timer is
persistent (`Persistent = true`): if 03:30 passed while it was stopped, the sweep
runs as soon as it is started (VM rehearsal, `platform/website-test.nix`: a
backdated stamp makes the restarted timer fire at once). Worked: the start returns
0 and both units are active.

**Step 11. Health checks that exist on a production host.**

```sh
systemctl --failed
terrace-production status
terrace-secrets verify
terrace-tls check
terrace-identity-admin status
terrace-identity-admin realm plan --admin <you>
systemctl is-active terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer terrace-cms-sweep.timer terrace-identity-health.timer
```

Worked: no failed units; `adopted <time>` and `active`; `verify` PASS; every public
name passes the expiry check; `Keycloak ready: yes`; `Converged`; `active` for each
timer. The production host has no functional checks beyond these (the 198 protocol
assertions are rehearsal-only scripts; [restore promotion](restore-promotion.md)
step 20). Check the public site in a browser yourself. Alerting does not exist
(G-23): a writer that fails later shows only in `systemctl --failed`.

**Step 12. U6: declare done, or go back.** Do not take a second backup today (step
4, "How long the snapshot lasts"); the 03:15 run takes the first post-update
snapshot. Keep the previous generation (do not garbage-collect it) and the previous
archives until the owner says the update is final. Record the new revision, the
generation number and the snapshot ID.

## 3. The relay host

The relay runs Headscale and nginx (`platform/production/relay.nix`); there is no
migration gate, no realm and no CMS. The app and relay configurations do not refer
to each other (READ: neither role module names the other), so the code imposes no
order between them. Update one host at a time and finish it (step R5) before the
other, so that a failure has one cause. Which goes first is the owner's choice (part
of U1); a suggestion, not from the code: the relay first for a Nix-inputs-only
change, because a failed relay does not take the site down, and it exercises the U0
mechanism on the smaller host.

**R1. Record and back up.**

```sh
readlink -f /run/current-system
nix-env --profile /nix/var/nix/profiles/system --list-generations
systemctl --failed
sha256sum /var/lib/headscale/noise_private.key /var/lib/headscale/derp_server_private.key
systemctl start terrace-backup.service
systemctl show terrace-backup.service -p Result --value
terrace-backup restic primary snapshots --json
```

No quiesce: the backup copies Headscale's SQLite through SQLite's online backup API
(`snapshot_headscale` in `platform/backup/backup.sh`). Worked: `success` and a new
snapshot ID; keep the two key hashes. The relay's own repository and retention
behave as in step 4 (its own group: one repository per role). **U2** for the relay.

**R2. Build, copy and activate** as steps 3, 5 and 6, with `role = "relay"` and the
relay's private module (no archives, no TFC source: `infra/nixos/production-config.nix`
requires them for the app only). Activation restarts Headscale if its package or
settings changed: connected clients reconnect (standard behaviour, not measured);
nginx restarts if its configuration changed. A Headscale version change can migrate
its database at start (upstream behaviour, not determined from the repository):
treat it as irreversible (section 4, case B).

**R3. Secrets** as step 7, only if the relay's inventory changed.

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
the host.

**R5. U6 for the relay.**

Evidence: the production relay role is booted, pushed, adopted, serves Headscale and
HTTPS and backs up, VM rehearsal (synthetic data), `platform/production/test.nix`
"production relay role" subtest; and the live specialisation switch of step 6's
evidence ran on the relay. Updating the relay to a new revision, Headscale version
changes, client reconnection after an update: not exercised anywhere.

## 4. Going back

### 4.1 What a generation rollback reverts, and what it does not

A rollback activates an older generation (READ: standard NixOS, never run here):

```sh
nix-env --profile /nix/var/nix/profiles/system --list-generations
nix-env --profile /nix/var/nix/profiles/system --switch-generation <previous number>
/nix/var/nix/profiles/system/bin/switch-to-configuration switch
```

(With `switch-to-configuration test` in step 6 and no reboot, the boot default is
still the old generation: activate it with `/nix/var/nix/profiles/system/bin/switch-to-configuration
switch` alone.) If the previous generation was garbage-collected, rebuild the
previous revision with the previous kept archives (step 3) instead.

| Reverted (it lives in the generation) | Not reverted (it lives in `/var/lib`, the store or the repository) |
| --- | --- |
| Packages and versions: Keycloak, Headscale, PostgreSQL minor, Node, the release bundles, the kernel (after a reboot) | The CMS database schema and data after step 9; Keycloak's schema after a Keycloak upgrade started; Headscale's database after a Headscale upgrade started |
| Unit definitions, timers, nginx configuration, firewall | The migration marker `/var/lib/terrace-cms/migrated-revision` (it keeps the new revision) |
| The pinned revision the gate expects, the realm definition the identity tool plans against | Realm changes applied in step 8 (`apply` never deletes anything; [identity administration](identity-admin.md#6-reconcile-the-realm-after-a-new-release-g-39)) |
| The secrets manifest and the host tool's inventory | The host ciphertext and the store (they are outside generations, `secrets.nix`): pushed or rotated values stay |
| | The backup repository: snapshots taken and snapshots pruned; ACME state in `/var/lib/acme`; media and outbox files; the adopted marker |

### 4.2 Case A: nothing irreversible ran

No migration (step 9 not run, or the revision did not change), no realm `apply`, no
Keycloak, Headscale or PostgreSQL version change. Then a generation rollback is the
whole way back:

1. Roll back as in 4.1.
2. App, if step 4 stopped them: `systemctl reset-failed terrace-cms.service
   terrace-web.service && systemctl start terrace-cms.service terrace-web.service
   terrace-cms-sweep.timer`. The marker still holds the old revision, which the
   old generation's gate expects.
3. If step 7 pushed a ciphertext with a name the old inventory does not take, the
   host keeps working (READ: `verify` checks only the manifest's names), but the
   next `push` is refused by the old generation's `install`: restore the store file
   saved in step 7 ([credential rotation](credential-rotation.md#2-procedures),
   general preconditions, "the way back") and push it, with the operator tool at
   the old revision.
4. Step 11's checks; the step 3 record is what "back" looks like.

Evidence: not exercised anywhere.

### 4.3 Case B: the migration ran, or an upstream schema changed

After step 9 the old generation's gate expects the old revision and the marker holds
the new one: **the CMS refuses to start** (READ: `requireMigration`). That refusal is
the protection; do not defeat it.

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

1. **Fix forward** (the owner's preference when the new release is close to
   working): a corrected pin, built, checked and deployed by this procedure. Not
   exercised anywhere.
2. **Restore the pre-update snapshot onto a replacement host** built from the
   **previous** revision with the previous archives: [restore promotion](restore-promotion.md),
   whole. **U7** first: everything written since the snapshot (step 4) is lost, the
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
Keycloak's database after a Keycloak upgrade: a generation rollback brings back the
old binary over the new schema, and whether it starts is **not determined from the
repository**; the snapshot from step 4 or R1 is the way back.

### 4.4 Case C: only the realm changed

`realm apply` adds and updates what the new definition declares and never deletes.
After a generation rollback, `realm plan` against the old definition proposes to set
back the fields the old definition also declares, and leaves alone what only the new
one declares (VM rehearsal (synthetic data), `identity-admin-test.nix`, assertion "an
artifact without the keys plans nothing for them", for the two admin-events keys
only). New clients,
roles, mappers and scopes stay. Removing them needs a code change or a restore
(case B). Run `plan`, then `apply` with its hash only if the owner accepts every line
(U3 again).

### 4.5 Secrets, TLS and backups are not rolled back

A push, a rotation ([credential rotation](credential-rotation.md)), a TLS mode change
([TLS](tls.md)), a repository password change: each has its own way back in its
document. A generation rollback changes none of them, but it can change which names
the host accepts (4.2, item 3) and which TLS mode nginx uses: rolling back across a
TLS mode change follows [TLS, "Fall back to the manual path"](tls.md#fall-back-to-the-manual-path)
or "Switching a live manual host to ACME", in their orders, not a bare rollback.

## 5. Reboots, kernels and the hazards found while reading

**Kernel or boot-path changes (U5).** Activate with `boot` instead of `switch` in step
6, then `systemctl reboot`. At boot `terrace-production-boot` verifies the secrets and
starts the writers (`base.nix`). On a re-pin the CMS then fails its gate exactly as
in step 6 until step 9; continue at step 7. Evidence: a reboot of an adopted app host
into the **same** generation, writers back by themselves, secrets from the ciphertext,
VM rehearsal (synthetic data): `platform/production/test.nix` ("reboot: ramfs
vanishes, ...") and `platform/production/tls-test.nix` ("reboot keeps the certificate
and the account key", including the persistent freshness timer firing at boot). A
reboot into a **new** generation: not exercised anywhere.

Hazards, each also in the [backlog](backlog.md#host-update-follow-ups-g-37--97-recorded-2026-11-24):

- **H1. An old migration unit reopens the gate on a newer schema** (section 4.3).
- **H2. The pre-update snapshot can be pruned within about two days** if a second
  snapshot of the host is taken the same day (step 4). There is no pin.
- **H3. No path for a closure to reach an installed production host.** Root SSH is
  refused, `operator` is not a trusted Nix user and signatures are required (RAN
  defaults). Without U0 settled in the module a host is installed with, the first
  update needs a console or a reinstall.
- **H4. A new generation that does not boot.** The profile disables the emergency
  shell (`systemd.enableEmergencyMode = false`, `base.nix`); choosing the previous
  boot entry needs the provider's console, and how to reach it is **not determined
  from the repository** (no provider access exists). `switch-to-configuration test`
  before the migration limits this to reboots after step 9.
- **H5. Deploy-then-push window for a new secret name** (step 7): a reboot in between
  leaves every writer stopped until the push.
- **H6. Going back after a migration means a whole restore promotion onto a
  replacement**, losing everything written since the snapshot (section 4.3).
- **H7. The website has no revision gate.** `production-profile.md` ("a revision
  mismatch still fails through the existing gate"), the comment in
  `platform/production/app.nix` and [website host](website-host.md#runtime-and-migration-boundary)
  ("Startup rejects a missing/different revision marker") read as if both services
  were gated; only the CMS, its sweep and the operator tools are
  (`requireMigration` in `platform/website.nix`). After a re-pin the website can run
  the new bundle against a gated CMS; after a rollback, the old bundle against a CMS
  that refuses to start. Neither loses data (the website holds no database
  credential); what it serves meanwhile is not determined.
- **H8. Generations accumulate.** No garbage collection and no boot-entry limit by
  default (RAN); every update adds a boot entry to the EFI system partition, whose
  size the hardware module sets. Collecting garbage, by hand, removes rollback
  targets. Neither effect is measured.

## 6. Traceability: evidence per step

"Production VM check" is `platform/production/test.nix` (production profile, instance
`rehearsal`, synthetic `.invalid` names, guests on a private test network). Nothing
below ran on a provider: lease 5 ran the rehearsal profile and no update.

| Step | Code that does the equivalent | What it asserts | Evidence, and how it differs from an update |
| --- | --- | --- | --- |
| 2. Classify | none | none | READ only |
| 3. Build | `infra/nixos/production-config.nix`; every production VM check builds the same roles | Evaluation invariants, archive hashes | VM rehearsal (synthetic data) for a fresh host; a build for a real private module: not exercised anywhere |
| 3. Record state | `terrace-production status` in `test.nix` | Status strings | VM rehearsal (synthetic data) |
| 4. Quiesce, back up | "back up, destroy the host" subtest, `test.nix` | Backup succeeds, snapshot listed | VM rehearsal (synthetic data); that subtest also stops Keycloak and nginx. Keeping Keycloak up, retention of the pre-update snapshot: not exercised anywhere |
| 5. Copy the closure | `copy_store` in `infra/nixos/rescue_install.py` (root, Rescue system, install only) | Install-time copy | Not an update path. Copy to an installed production host: not exercised anywhere |
| 6. Activate | `switch-to-configuration test` to a specialisation, relay, `tls-test.nix` | No placeholder served, no failed units | VM rehearsal (synthetic data), relay only, same build. A new revision's closure, `switch`, `boot`, the system profile, the app role: not exercised anywhere |
| 6. Gate closes on a re-pin | `requireMigration`; the tool refusal in `website-test.nix` | `CMS migration gate is closed` | VM rehearsal (synthetic data), website module with the marker moved away, not a re-pin |
| 7. Secrets after activation | `cmd_install`, `cmd_verify`; "rotate the intake secret" in `test.nix` | Push, verify, restarts | VM rehearsal (synthetic data) for a push to an adopted host; an inventory change: not exercised anywhere |
| 8. Realm | "realm reconciliation" in `identity-admin-test.nix` | One planned change applied; users and credentials unchanged; converged; no downgrade | VM rehearsal (synthetic data); re-pin simulated by rewriting the tool's configuration |
| 9. Migrate | "explicit migration gate" in `website-test.nix`; replacement subtest in `test.nix` | Pinned ledger; idempotent; refusals stop nothing; accepted migration stops the apps | VM rehearsal (synthetic data) on an empty database and on restored data of the same release. Upgrade of older data: not exercised anywhere |
| 10. Start | `website-test.nix` (`reset-failed`, start after migrations); persistent sweep timer catch-up | Units active; missed sweep runs at once | VM rehearsal (synthetic data), website module |
| 11. Checks | `terrace-tls check` (`tls-test.nix`), `verify` (`test.nix`), `realm plan` (`identity-admin-test.nix`) | Each check's own output | VM rehearsal (synthetic data), each separately; together after an update: not exercised anywhere |
| R1-R5 Relay | "production relay role" subtest in `test.nix`; the relay switch in `tls-test.nix` | Adoption, HTTPS, backup; live switch | VM rehearsal (synthetic data) for boot and a same-build switch; an update: not exercised anywhere |
| 4.1-4.4 Rollback | none | none | Not exercised anywhere. The "no downgrade" realm behaviour: VM rehearsal (synthetic data) |
| 5. Reboot into a new generation | reboot subtests in `test.nix`, `tls-test.nix` (same generation) | Writers start alone; secrets return; certificate kept | VM rehearsal (synthetic data) for the same generation only |

## 7. What this document does not cover

- Changing public host names, the relay's address or DNS: Keycloak's issuer
  follows the host name and the realm keeps its old redirect addresses
  ([restore promotion](restore-promotion.md#4-what-a-production-promotion-needs-that-nothing-has-exercised),
  "Public names changing"). Not exercised anywhere.
- A PostgreSQL major upgrade: no tool exists (section 2, step 2).
- Choosing pins (which advisories to take, G-44): runbook 9.7's own review.
- Producing a new pin in Womb (re-pin, VM checks): runbook 6.12 to 6.14 and
  [website host](website-host.md#reproduce-the-actual-artifact-vm-check).
- Documenso, Windmill, Vaultwarden, K3s: not in the production profile.
- Rotating credentials ([credential rotation](credential-rotation.md)) and TLS mode
  changes ([TLS](tls.md)), except where an update meets them (sections 2 and 4.5).
- Unattended or scheduled updates, and a promotion pipeline across hosts: none
  exists (runbook 9.7, G-37).
- Members' sessions and tokens across an update; Keycloak's own session handling on
  a Keycloak upgrade: not determined from the repository.

## 8. Drills to add

Not implemented here. A later task would add a VM drill of **this written procedure**
on the production profile, as a script whose commands are taken from this document,
asserting each step's "worked" output. Beyond what the existing checks assert, it
needs:

1. **Two revisions.** Build the app and relay roles at the pinned revision and at a
   second one (a real later pin, or the previous kept pin `eab4189` to the current
   `dd1aef7`), adopt the first, write data with the first release (members, roster
   journal, media, outbox), then update to the second by steps 3 to 12.
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
7. **`switch-to-configuration test` then reboot** before the migration: the old
   generation comes back.
8. **`boot` and reboot into the new generation**: writers start from the boot gate;
   the CMS gate behaves as in step 6.
9. **Inventory change**: a declared name added; activation then push; a reboot in
   between leaves the writers gated (H5); a name removed.
10. **Realm producer change** delivered by a real activation, not by rewriting the
    tool's configuration.
11. **Relay update** with Headscale peers: key hashes unchanged, the three clients
    reconnect without enrolling again.
12. **Retention**: the pre-update snapshot survives the documented number of runs,
    or step 4's wording is corrected (H2).
13. **Restore-based rollback**: the pre-update snapshot restored onto a replacement
    built from the previous revision ([restore promotion](restore-promotion.md)),
    ledger equal to the old pin's.
14. **Timings**: the site's downtime from step 4 to step 10, per step.

Backlog items for the gaps and hazards are in
[backlog](backlog.md#host-update-follow-ups-g-37--97-recorded-2026-11-24).
