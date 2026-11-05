# Terrace platform foundation

This directory is a standalone pinned Nix flake for a single x86_64 NixOS host.
K3s keeps the Kubernetes manifest and GitOps path available while using SQLite
for one control plane. It does not join or span the millionaire homelab cluster.
No cloud resource or existing Womb infrastructure is changed by this directory.

## Scope and current limits

- `single-node.nix` enables one K3s server, disables bundled Traefik and ServiceLB,
  closes the Kubernetes API at the host firewall, and creates a `local-retain`
  StorageClass. PVCs must set `storageClassName: local-retain` explicitly; K3s's
  bundled default `local-path` class can still delete data when a PVC is removed.
- `hetzner.nix` adds BIOS GRUB configuration and a key-only `operator` account.
  `grub.device = "nodev"` is deliberately evaluation-only. A verified disk path,
  root filesystem, host networking, and operator public key must be supplied
  before any install. Do not install this profile as-is.
- Only SSH TCP/22 is opened on the NixOS host. K3s API TCP/6443 stays blocked
  from outside the node; use an SSH local port forward for administration.
  A Hetzner cloud firewall must also allow only SSH from approved sources.
- Production deployment requires an off-host backup of the SQLite datastore
  (`/var/lib/rancher/k3s/server/db`) and server token, plus application-aware
  backups of local PV data under `/var/lib/rancher/k3s/storage`. The optional
  `terrace.backup` module (`backup.nix`, default off, imported by `single-node.nix`
  and exported as `nixosModules.backup`) backs up PostgreSQL dumps, media and Headscale
  state with an isolated restore tool, see [docs/backups.md](../docs/backups.md); it does
  not cover the K3s datastore or PV data unless listed in `extraPaths`/`media`, and no
  job is enabled. A Retain policy is deletion protection, not a backup.
  A one-node outage is downtime.
- No GitOps controller, applications, DNS, ingress, certificate issuer,
  secrets integration, cloud purchase, or production restore is configured yet.

The optional native [identity host](../docs/identity-host.md)
(`nixosModules.identity`, default off) adds PostgreSQL-backed Keycloak, a limited
HTTPS proxy, runtime-only inputs and local readiness/resource bounds. It seeds no
accounts and requires a reviewed production artifact from TFC's identity contract.
No install profile enables it; enrollment and Keycloak restore remain release gates.
The separate [identity recovery drill](../docs/identity-recovery.md),
`platform/check identity-restore`, checks actual user-free Keycloak schema and
signing-key continuity on a disposable recovery guest using the existing encrypted
backup/restore tooling; production/off-site recovery remains unverified.

The optional [Documenso module](../docs/documenso-host.md) pins an official
patched image, uses a separate native PostgreSQL database and explicit offline
migrations/approved-account bootstrap. It is default off; functional OIDC,
document and recovery checks remain activation gates.

## Evaluate and test

From this directory on the configured Linux build host, with the shared build
lock path supplied as `BUILD_LOCK`:

```sh
nix flake show --no-write-lock-file
: "${BUILD_LOCK:?set the shared build lock path}"
./check single-node
```

Never run this build on a developer's Mac. When using the shared falcon builder,
supply the same shared lock used by the homelab's `tools/falcon-heavy` wrapper;
`platform/check` takes it itself, so do not wrap it in a second lock. The VM check boots K3s, waits for a Ready node,
verifies the retained StorageClass and firewall, starts an offline test pod
with a PVC, writes a marker, restarts K3s, recreates the pod, and checks
the marker remains. It then deletes the PVC and checks the PV and data
remain. It does not exercise off-host backup or restore.

For a long check that must outlive the SSH session, run from the remote checkout:

```sh
mkdir -p .devenv
umask 077
BUILD_LOCK="$HOME/.falcon-heavy.lock" setsid nohup bash -c \
  'platform/check backup; result=$?; echo "EXIT $result"; exit "$result"' \
  > .devenv/backup-check.log 2>&1 < /dev/null &
echo $! > .devenv/backup-check.pid
```

`platform/check` requires x86_64 Linux and the explicit shared lock path. It
accepts one or more flake check names (for example `backup single-node`), takes
the lock for the whole build, and uses one job/two cores. It disables cache-upload
post-build hooks for this invocation only; validation and Git hooks still run.
Inspect the recorded PID before stopping it; never stop unrelated builder jobs. Local detached SSH
wrappers may be reaped by an agent harness, so detach on the builder itself.

Verified on 2026-10-27 (America/Los_Angeles) on falcon with Nixpkgs revision
`31ecbaca1bdefa45dffc409f0250bd8039ea0010`: the VM check passed in
132 seconds. The result is
`/nix/store/wsdkagnmncx0nrag23c7zaxpw4fz1jn3-vm-test-run-terrace-single-node`
on falcon. The client VM's API connection timed out; the probe pod became
Ready using pinned airgap images; the marker survived K3s restart, pod
recreation, and PVC deletion, with the PV in `Released` state. These VM
results do not verify Hetzner firewall rules or any real-host deployment.

To administer an eventual host, forward the API locally and use the server's
root-owned K3s kubeconfig with its `server:` endpoint set to
`https://127.0.0.1:6443`:

```sh
ssh -L 6443:127.0.0.1:6443 operator@HOST
```

Do not copy kubeconfig into Git or make it world-readable. The host's
filesystem, disk, IP, firewall rules, SSH source addresses, repository, and
credential delivery depend on the selected machine and cloud account.

## Backup activation and restore gate

Before production use, add an off-host backup job with runtime-only credentials,
retention, freshness checks, and application-aware PV backups. Restore the
control-plane database and server token together into an isolated replacement
VM while K3s is stopped, then verify the API and workloads. Restore PV data
from the application backups. Record a successful restore drill; until then the
production restore gate remains open.

The target is a relay plus one application host with backups included in a
total budget strictly below $80/month, including the temporary overlap with the
existing server; see [the hosting plan](../docs/access-relay.md). No disk layout
or account price is asserted here: those must be checked against the live
account before purchase. On-premise
hardware later uses the same `single-node.nix` module with a separate host
profile; it does not imply a WAN-spanned cluster.


## Check supervision

`check` and `website-check` use the Linux Python standard-library `run-check`
supervisor. Queue timeout `BUILD_LOCK_TIMEOUT` defaults to 3600 seconds (max
28800); active wall deadline `CHECK_TIMEOUT` defaults to 1800 (max 14400);
per-derivation Nix `CHECK_BUILD_TIMEOUT` defaults to the smaller of 900 and the
wall deadline, and cannot exceed it. `CHECK_STOP_GRACE` defaults to 30 seconds
(max 60), followed by KILL and a bounded cleanup check. Explicit bounds must be
positive whole seconds. A build timeout may abort a slow initial dependency
build: warm the driver/closure in a separate bounded run if appropriate.

The lock descriptor is not inherited by the build. Cancellation returns 130/143,
wall timeout 124, queue timeout 75 and invalid inputs/uncertain cleanup 2. A child
returning zero after a timeout is still rejected. Completion also stops stray
members of the owned child group; no process-name or cwd kill is used. Nix's
per-build timeout remains a fallback if the supervisor dies, not a guarantee
against host failure/SIGKILL or escaped sessions. Do not leave detached checks
unattended. Verify exact owned VM/client PIDs absent before reporting idle.

Run synthetic regressions on Linux (no Nix/provider/network calls):

```sh
python3 platform/test-run-check.py -v
```

Run the actual timeout/cancellation regression from a clean remote checkout.
The proof directory must not exist. Root is needed only to inspect and, on
failure, stop uniquely marked Nix-sandbox QEMU processes; the script takes the
shared lock separately for each bounded build. Never wrap this in another lock.
It creates private synthetic-only logs and two ready disposable guests, then
requires each observed guest PID/start-time absent after timeout/cancellation:

```sh
sudo env BUILD_LOCK="$HOME/.falcon-heavy.lock" python3 \
  platform/test-run-check-native.py "$PWD/.devenv/runner-proof-$(date +%s)"
```

Each driver warm-up has a 900-second active/build bound; each deliberate VM
hang a 120-second wall/35-second Nix build bound, plus a bounded queue and cleanup.
The controller handles TERM/INT/HUP and performs exact-owned fallback cleanup
on failure, which never converts failure into acceptance. Keep supervising the
controller and inspect its private proof on any failure. The full-stack
[service lifecycle matrix](../docs/service-lifecycle.md) lists what this does
and does not establish.

Documenso real-browser acceptance: `BUILD_LOCK=/absolute/shared/build.lock platform/documenso-functional-check /source/tfc` on the x86_64 Linux builder; see [the functional contract](../docs/documenso-host.md#functional-acceptance-companion).
