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
  backups of local PV data under `/var/lib/rancher/k3s/storage`. No backup job
  is configured yet. A Retain policy is deletion protection, not a backup.
  A one-node outage is downtime.
- No GitOps controller, applications, DNS, ingress, certificate issuer,
  secrets integration, cloud purchase, or production restore is configured yet.

## Evaluate and test

From this directory on the configured Linux build host, with the shared build
lock path supplied as `BUILD_LOCK`:

```sh
nix flake show --no-write-lock-file
: "${BUILD_LOCK:?set the shared build lock path}"
flock "$BUILD_LOCK" nix build .#checks.x86_64-linux.single-node --no-write-lock-file \
  --max-jobs 1 --cores 2 --option builders "" --option use-build-hook false
```

Never run this build on a developer's Mac. When using the shared falcon builder,
use the homelab's `tools/falcon-heavy` wrapper and its shared lock; do not start
an unlocked parallel job. The VM check boots K3s, waits for a Ready node,
verifies the retained StorageClass and firewall, starts an offline test pod
with a PVC, writes a marker, restarts K3s, recreates the pod, and checks
the marker remains. It then deletes the PVC and checks the PV and data
remain. It does not exercise off-host backup or restore.

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
