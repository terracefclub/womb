# Independent access and recovery host

Decision updated 2026-10-28: use `terracefclub.org`, a small Headscale host and a
separate heavier application host in a dedicated Terrace Hetzner project. Do
not stretch Kubernetes quorum or storage over the WAN. This is not HA.

`platform/relay.nix` is the first service module. It runs Headscale with SQLite,
an embedded DERP relay, direct HTTPS through nginx, and an explicit deny-all
peer policy. It does not enable K3s, override clients' existing DNS resolvers,
copy personal homelab identities, or provision a server. Metrics, gRPC and the
Headscale backend bind to loopback. Only HTTPS/ACME and UDP STUN are opened by
this module. Existing SSH recovery is a separate host responsibility.

Import `nixosModules.relay`, enable `terrace.relay.enable`, and supply the verified
`terrace.relay.publicIPv4`. The default endpoint is
`headscale.terracefclub.org`; direct DNS is required, without a CDN proxy.
The enclosing host must supply its verified disk/boot/network profile,
individual SSH public keys, and ACME account settings. No installable machine
definition or DNS record is implied by this module. The application-specific
`hetzner` profile enables K3s and must not be used for this relay.

## Before operator enrollment

1. Recover project access, inspect the existing server and identify data owners.
   Create a protected copy and prove it restores before changing that host.
   The owner's unknown SSH key is not permission to rebuild it.
2. Confirm the project/account, actual prices, stock, tax and spending ceiling.
   Preview only the named new resources. Do not replace or import unknown live
   resources into an apply without inspecting the resulting plan.
3. Add the Terrace Keycloak operator client with PKCE and an explicit operator
   group allowlist, plus declarative bootstrap. Website membership must never
   imply infrastructure access. Keep independently tested SSH/CLI recovery.
4. Replace the deny-all policy with reviewed, tested grants to specific service
   ports. Validate both an allowed operator path and a denied member path with
   real Tailscale clients, including DERP fallback across the WAN.
5. Back up a consistent SQLite snapshot, Noise/DERP identity keys and recovery
   configuration to encrypted off-site storage. Preserve keys during recovery;
   do not casually reset the network identity. Restore to an isolated host and
   prove client reconnection before calling recovery ready.
6. Supply a repo-local userspace Tailscale client and connection commands. Do
   not replace the operator's existing personal/employer Tailscale session.

## Verification

The `platform` flake's `checks.x86_64-linux.relay` boots two disposable NixOS VMs.
It verifies trusted HTTPS, HTTP redirect, blocked public management ports,
Headscale configuration, explicit deny-all policy, generated identity-key
presence, SQLite integrity and user persistence across service restart, and
absence of K3s. Run on the configured Linux builder, never the Mac:

```sh
nix build path:./platform#checks.x86_64-linux.relay --no-link
```

Passed 2026-10-28 via the shared Linux build queue. This is service isolation
and persistence evidence, not live enrollment, DERP connectivity, OIDC, a
backup restore or a deployed health check. Those remain explicit release gates.

## Application host and synthetic demonstrations

Start with website, Payload, PostgreSQL and Keycloak. Add bounded demos of the
kept services after measuring capacity. Home Assistant's built-in `demo:`
integration can provide synthetic sensors. A low-resolution black video source
can exercise Frigate/go2rtc ingest with detection and recording disabled.
Never connect demonstration automations to real devices, email, signing requests
or payment delivery. Label all synthetic records. The detailed demo manifests
and tests are a separate increment; none are deployed by this module.

## Budget proposal, not an order

Public USD prices checked 2026-10-28, excluding tax and existing-server overlap:

| Item | Monthly estimate |
| --- | ---: |
| EU CPX22 relay | $22.99 |
| EU CPX32 application host | $41.99 |
| Provider backups, planning allowance at 20% | $13.00 |
| Two primary IPv4 addresses | $1.20 |
| Independent encrypted backup storage allowance | $3.00 |
| Total | $82.18 |

The relay can use a smaller/cheaper available machine; these are conservative
planning sizes, not measured requirements. Off-site storage is an allowance,
not a provider quote. Public pages do not prove project stock. A proposed $120
total ceiling including tax and temporary old-server overlap awaits the owner;
the original $20 target is not silently raised in provisioning code.

Sources: [Hetzner server prices](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/),
[primary IP prices](https://docs.hetzner.com/general/infrastructure-and-availability/ipv4-pricing/),
[backup coverage](https://docs.hetzner.com/cloud/servers/backups-snapshots/overview/),
[Headscale requirements](https://headscale.net/stable/setup/requirements/),
[OIDC](https://headscale.net/stable/ref/oidc/),
[Home Assistant demo integration](https://www.home-assistant.io/integrations/demo/).
