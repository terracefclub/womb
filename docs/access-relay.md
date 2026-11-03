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
   configuration to encrypted off-site storage (`terrace.backup`, see
   [backups](backups.md)). Preserve keys during recovery;
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

## Hosting plan under the US$80/month ceiling

Owner ceiling (2026-10-29): total recurring hosting **strictly below US$80 per
month**, including provider backups, off-site backups, tax and the temporary
overlap with the existing Ashburn CPX11. The earlier EU CPX22 + CPX32 estimate
($82.18 before tax) is withdrawn. This section is a plan, not an order: the
account quote, currency, tax and stock are unverified, and no provider call was
made. `infra/cloud` encodes the recommendation as defaults and refuses a quote
whose overlap month reaches $80.00.

### Price evidence (public pages, read 2026-10-31)

All figures exclude VAT. USD figures are Hetzner's USD columns; I add IPv4 separately.

| Item | Price | Source |
| --- | --- | --- |
| EU CX23 (2 vCPU, 4 GB, 40 GB) | $6.49 (EUR 5.49) | [price adjustment, effective 2026-06-15](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/); specs from [cost-optimized plans](https://www.hetzner.com/cloud/cost-optimized/) |
| EU CX33 (4 vCPU, 8 GB, 80 GB) | $9.99 (EUR 8.49) | same |
| EU CX43 (8 vCPU, 16 GB, 160 GB) | $18.49 (EUR 15.99) | same |
| EU CX53 (16 vCPU, 32 GB, 320 GB) | $34.99 (EUR 29.49) | same |
| EU CPX22 / CPX32 / CPX42 | $22.99 / $41.99 / $81.99 | same; specs from [regular-performance plans](https://www.hetzner.com/cloud/regular-performance/) |
| EU CPX12 (1 vCPU, 2 GB, 40 GB) | no USD list price on the adjustment page; EUR 11.99 on the plan page, which includes IPv4 (the adjustment page excludes it: CPX22 is EUR 19.99 vs 19.49). Derived allowance $13.70 | plan page; derivation is mine |
| US Ashburn/Hillsboro | CPX11 $20.49, CPX21 $37.49, CPX31 $73.49, CPX41 $141.49. No CX or CAX in the US | price adjustment page |
| Existing server | Ashburn CPX11 at the pre-2026-06-15 price $6.99 plus $0.60 IPv4 = $7.59. Existing instances keep their price, but "a rescale reprices you": never resize it | [price adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/), [market report](https://privatedevops.com/news/hetzner-june-2026-cloud-price-increase-what-to-do) |
| Primary IPv4 | $0.60 (EUR 0.50) per address; primary IPv6 free | [IPv4 pricing](https://docs.hetzner.com/general/infrastructure-and-availability/ipv4-pricing/) |
| Provider backups | 20% of the server price, 7 slots, disk only (no attached volumes) | [billing FAQ](https://docs.hetzner.com/cloud/billing/faq/), [backups overview](https://docs.hetzner.com/cloud/servers/backups-snapshots/overview/) |
| Snapshots, volumes | EUR 0.0143 and EUR 0.0572 per GB-month. Third-party calculator dated 2026-10-20, not found on a Hetzner page, so treat as unverified. The plan uses neither volumes nor standing snapshots | [CostGoat](https://costgoat.com/pricing/hetzner) |
| Storage Box BX11 (1 TB, restic/Borg/SFTP) | EUR 3.20 (about $3.78 at the 1.18 USD/EUR ratio of the adjustment tables); Germany or Finland only; setup fee shown but amount not stated | [Storage Box](https://www.hetzner.com/storage/storage-box/) |
| Hetzner Object Storage | EUR 6.49 base for 1 TB (about $7.65); too costly for this dataset | [Object Storage](https://www.hetzner.com/storage/object-storage/) |
| Backblaze B2 (second provider) | $6.95/TB-month, first 10 GB free, egress free up to 3x stored | [B2 pricing](https://www.backblaze.com/cloud-storage/pricing) |

**Stock warning.** On 2026-10-31 the Hetzner plan pages show "currently
unavailable" for every CX, CAX and CPX plan, and third parties report the CX
line unorderable since late June 2026 with no return date
([bitdoze, checked 2026-10-20](https://www.bitdoze.com/es/hetzner-cloud-precios-2026/),
[findstack](https://findstack.com/resources/hetzner-price-increase-2026)).
Existing servers are unaffected. Public pages are a weak signal for a given
project, so the first gate is an authenticated read-only stock check (see
prerequisites). If the CX types cannot be ordered, option C below is the only
shared-vCPU fallback that still fits, and only without VAT.

### Tax and currency assumptions

Prices are quoted excluding VAT, and Hetzner's country selector decides VAT
([plan page](https://www.hetzner.com/cloud/cost-optimized/)). The plan assumes a
US billing address with **no VAT**. An unofficial comment attributed to Hetzner
staff says US customers pay none; it is not documented policy, so confirm on the
account. If VAT applies (for example a German or EU billing address at 19%),
every figure rises by that percentage. The recommended option stays under $80
at 19% VAT (peak $53.28 in the table). The account currency is fixed at
creation ([billing FAQ](https://docs.hetzner.com/cloud/billing/faq/)); if it is
EUR, convert at a conservative rate and margin outside Git before filling the
USD quote fields.

### Monthly arithmetic (USD, US$80 ceiling, strictly below)

Every option adds two IPv4 addresses ($1.20), an off-site backup allowance of
$5.00 (BX11 $4.00 as rounded-up Storage Box cost plus up to 100 GB of a
second-provider B2 copy, about $0.70), and an "other" margin of $1.00 (a
one-time snapshot of the old server's 40 GB disk plus egress). The overlap
month adds the existing server at $7.59. Provider backups are 20% per backed-up host.

| Line | A: CX23 + CX43, provider backups on both (recommended) | B: CX23 + CX43, independent backups only | C: CPX12 + CPX32, app backups only (fallback) | D: CX23 + CPX32, backups on both |
| --- | ---: | ---: | ---: | ---: |
| Relay server | 6.49 | 6.49 | 13.70 | 6.49 |
| Application server | 18.49 | 18.49 | 41.99 | 41.99 |
| Provider backups | 5.00 (1.30 + 3.70) | 0.00 | 8.40 | 9.70 (1.30 + 8.40) |
| Two IPv4 | 1.20 | 1.20 | 1.20 | 1.20 |
| Off-site backup allowance | 5.00 | 5.00 | 5.00 | 5.00 |
| Other margin | 1.00 | 1.00 | 1.00 | 1.00 |
| **Steady state** | **37.18** | **32.18** | **71.29** | **65.38** |
| + existing CPX11 and IPv4 | 7.59 | 7.59 | 7.59 | 7.59 |
| **Overlap month (x2, no VAT)** | **44.77** | **39.77** | **78.88** | **72.97** |
| Steady state at 19% VAT | 44.25 | 38.30 | 84.84 (fails) | 77.81 |
| Overlap month at 19% VAT | 53.28 | 47.33 | 93.87 (fails) | 86.84 (fails) |
| Overlap if the old server were repriced to $21.09, 19% VAT | 69.35 | 63.40 | fails | fails |

The two overlap months cost 2 x $44.77 = $89.54 in option A, but the ceiling is
monthly and the gate checks the worst month, so a longer overlap cannot hide a
breach as long as each month stays below $80. Hetzner bills servers hourly up
to the monthly price, so deleting the old server mid-month costs less.

Not viable: any US location. CX does not exist there, and the cheapest
two-host US pair (CPX11 $20.49 + CPX31 $73.49) is $93.98 before extras. A
single US CPX21 ($37.49, 3 vCPU, 4 GB) cannot hold the application stack.
Originally proposed CPX22 + CPX32 costs $64.98 for servers alone and fails
once backups are added.

### Recommendation: option A

Defaults in `infra/cloud`: relay `cx23` in `hel1`, application `cx43` in
`nbg1`, provider backups on both. Steady state **$37.18/month**, worst overlap
month **$44.77**, which is $35.23 under the ceiling, and $53.28 even with 19% VAT.
It also stays at $69.35 if the old server were silently repriced.

- Relay (2 vCPU, 4 GB): Headscale with SQLite, nginx and embedded DERP need a
  small fraction of this. CX23 is the smallest CX type; the headroom absorbs
  DERP traffic and Nix rebuilds. Different data center from the application host
  so one site outage does not take out both access and apps.
- Application host (8 shared vCPU, 16 GB, 160 GB): single-node NixOS/K3s,
  no HA. Planning allowances, **unmeasured**: OS plus K3s 1.0-1.5 GB, PostgreSQL
  for Payload and Keycloak 1-2 GB, Keycloak 1-1.5 GB, Payload 0.7-1.2 GB,
  SvelteKit 0.2-0.4 GB, GitOps/health/monitoring 0.3-0.6 GB, restic peak
  0.3-0.5 GB. That totals about 4.5-7.7 GB, leaving 8 GB or more (about
  50%) free against the 25% minimum in [the community pilot](community-pilot.md).
  Build artifacts elsewhere; 160 GB holds images, databases and the 5 GB media budget.
- Option B saves $5.00 by dropping provider image backups. They are disk-level,
  not database-consistent, but they give a second whole-host recovery path
  including SQLite and Headscale keys for $5.00 a month, so A is recommended.
  Neither replaces the encrypted restic backups (primary repository on a Storage
  Box sub-account, second copy at B2 so one account suspension cannot erase
  both) or the restore drills.
- Option C is the fallback only if CX stays unavailable: it is $1.12 under the
  ceiling with no VAT and fails with any VAT. D fails with VAT, too. Neither is
  a default; selecting them requires a new owner decision.

**Deferred until measured capacity is proven** (add one at a time; require at
least 25% free memory and no OOM events over a week of the combined workload
after adding the new allowance): Documenso (Next.js plus PostgreSQL, about
1-1.5 GB), Windmill (server, worker and PostgreSQL, about 1.5-3 GB),
Vaultwarden (small, but gated by inspection of the existing instance's data),
HA of any kind (a second application node would at least double the
application-host cost and is not claimed), Home Assistant and Frigate (on
premises later; only bounded synthetic demonstrations before then).

### IPv6-only relay is not recommended

Hetzner allows creating a server without its primary IPv4 and IPv6 is free
([IPv4 pricing](https://docs.hetzner.com/general/infrastructure-and-availability/ipv4-pricing/)),
saving $0.60 a month. It would break Headscale HTTPS, DERP and STUN for every
client on an IPv4-only network (many home, mobile and corporate links), so
the relay's purpose fails exactly where fallback is needed. It cannot reach
IPv4-only services such as GitHub without a NAT64 gateway, and `terrace.relay.publicIPv4`
is required by the module. Keep one IPv4 on each host; optionally enable IPv6
in addition later.

### Open prerequisites

1. **Project access recovery.** The owner must regain Console access to the
   dedicated Terrace project (sign-in is deferred). Do not prompt repeatedly or
   rebuild the existing server.
2. **API token stored outside chat.** A project-scoped read-only token first
   (stock and quote), kept in a protected secret store; compute its SHA-256 locally for
   `tokenSha256`. Never paste it in chat, issues, MRs or arguments.
3. **Existing server inspection.** Before any replacement or deletion: approved
   access, inventory of services, databases, DNS dependencies and credential
   consumers, a protected copy with a proven isolated restore, and a rollback
   path. Do not resize it (repricing). Deletion is a separate owner decision.
4. **Account quote.** Currency, VAT status, itemized invoice of the existing server,
   and authenticated stock for `cx23`/`cx43` in `hel1`/`nbg1`/`fsn1` (for example the
   `hcloud` datacenter and server-type listings with the read-only token). Enter
   dated quote values as Pulumi stack config (see `infra/cloud/README.md`).
5. **Backup targets.** Storage Box sub-account and B2 credentials created and held
   outside Git; encrypted restic repository initialised (`terrace-backup init`);
   restore drill passed (`terrace-backup-restore`, see [backups](backups.md)).

## Sources for the platform design

[Headscale requirements](https://headscale.net/stable/setup/requirements/),
[OIDC](https://headscale.net/stable/ref/oidc/),
[Home Assistant demo integration](https://www.home-assistant.io/integrations/demo/).
