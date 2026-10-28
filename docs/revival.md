# Terrace revival

Source audit: 2026-10-27. Owner decisions updated: 2026-10-28. No existing Terrace host or application was verified
healthy in this audit. Source evidence is not evidence of a live deployment.

## Agreed scope

- Temporary hosting plus backups: target **US$20/month**, including IPv4, taxes,
  and storage. Confirm actual account pricing and stock before provisioning.
- One x86_64 server on premises later. Hardware, disks, accelerators and network
  are unknown; do not invent a disk layout. Single-node downtime is acceptable.
- Cloud recovery resources stay outside its cluster. No quorum or storage
  replication stretched across the WAN; backups and a CDN do not create HA.
- Keep Documenso, Home Assistant, Frigate, Windmill and Vaultwarden. Windmill is
  the only planned pipeline platform. Prioritize Google Workspace/Drive for
  shared files, with local storage for services and backups.
- Use a dedicated Terrace Hetzner project/account; never provision into the
  personal homelab project. Payload with SvelteKit is the selected CMS/frontend.
- Website: warm, colorful, welcoming, events first; emphasize newsletters,
  club/alumni events, initiatives and donations.
- Community: current members and alumni together, with profiles, stories, groups
  and opt-in mentorship. Keep private member data separate from public publishing.
  Evaluate HumHub for the pilot; membership approval and moderation owners remain
  prerequisites for real invitations.
- Enable CDN only after origin behavior, authentication and cache exclusions
  pass tests.

## Source inventory

| Project/ref | Evidence | Preserve |
| --- | --- | --- |
| `womb/trunk`, `c946cbf` | NixOS/nginx/Vaultwarden; Python Pulumi; Windmill membership flow | Integrations, schemas, data ownership and credential references |
| `tfc-website/dev`, `9635832` | Django/Wagtail pages, posts, events, menus; TypeScript/webpack | Content models, Terrace assets, Google Calendar sync, menu/document import requirements |
| `tfc-website/prod`, `ce00538` | Older independent branch | History; do not equate it with today's WordPress deployment |
| `wombnet/main`, `0d5b886` | Django/DRF scaffold, Metronic templates, unfinished Constant Contact work | Integration requirements; licensed assets only after review |

WombNet's README says Strapi, but current main is Django. It does not establish
an existing member database or functioning community product.

Womb declares Vaultwarden at `vaultwarden.terracefclub.org`, staging ACME, and
incomplete credential wiring. Old Pulumi declares an Ashburn `cpx11` with a
Debian bootstrap image. Neither proves live state. Its Nix EFI disk layout must
not be carried over without testing the actual Hetzner boot requirements.

## Service decisions

| Service | Treatment | Remaining evidence/decision |
| --- | --- | --- |
| Website/CMS | Payload + SvelteKit; first hosted workload | Editorial/security spike and complete WordPress inventory |
| PostgreSQL | One instance, separate roles/databases | App compatibility and restore test |
| Identity | Per-user OIDC and roles | CMS adapter; independent emergency access |
| Documenso | Keep, after website platform | Existing endpoint; signing/storage/email requirements |
| Windmill | Keep one bounded worker initially | Preserve membership flow; synthetic tests |
| Home Assistant | Keep on premises | Device inventory and LAN access |
| Frigate | Keep on premises | Cameras, accelerator, storage/retention; no cloud inference |
| Vaultwarden | Keep | Confirm live endpoint, data and backup before migration |
| Human-facing file sharing | Google Workspace/Drive first | Integration identity and existing permissions |
| Community | Profiles, stories, groups, opt-in mentorship | HumHub pilot; see [community plan](community-pilot.md) |
| Monitoring | Health, certificates, capacity, backup age, external probe | Bounded retention and actionable alerts |
| Backups | Encrypted, off-site, independently recoverable | Credentials, cost cap, isolated restore |

Omit Ceph, distributed metrics/logging/tracing stacks, data warehouses, extra ETL,
desktop/home-manager modules, gaming/media hobbies, GPU model serving, and a
second node presented as HA. Add components for demonstrated requirements.

## Foundation proposal

Reuse the homelab's operating contracts: pinned dependencies, declarative hosts,
GitOps reconciliation, idempotent bootstrap, individual identity, retained data,
generated-manifest checks, health gates and restore tests. Do not copy personal
inventory, credentials, addresses or replication assumptions.

The small-host candidate is NixOS with single-node K3s, local retained storage,
and a small GitOps controller. K3s reduces control-plane overhead while retaining
Kubernetes deployment contracts. This is a proposal, not measured production
capacity. Its published baseline excludes app resources: [K3s requirements](https://docs.k3s.io/installation/requirements).

Initially run the public site, CMS, database, identity essentials and backups.
Build artifacts elsewhere. Measure memory and load before adding Documenso and
Windmill; the entire wishlist is not promised to fit the temporary VM.

Checked 2026-10-27: Hetzner lists EU CX33 at $9.99/month excluding IPv4/tax, while
US CPX11 is $20.49 before extras. Public listings do not prove stock availability.
An EU preview may fit; measure US latency and introduce a CDN later.
[Provider pricing](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/).

## CMS shortlist

| Platform | Strength | Constraint |
| --- | --- | --- |
| Payload | MIT core, TypeScript, drafts/versions, PostgreSQL | Next.js CMS alongside SvelteKit; turnkey SSO paid, custom strategy needs security tests |
| Wagtail | BSD core, editorial workflow/accessibility, existing Terrace models | Python backend; headless preview integration |
| Ghost | Polished publication/newsletter experience, MIT | MySQL; headless membership limitations; less natural event models |
| Strapi | MIT community core, structured CMS | SSO/history/workflow features gated by paid tiers |
| Directus | Strong data UI | Current MSCL license is not an unrestricted FLOSS choice |
| WordPress | Production content and integrations already exist | Preserve as migration source and rollback until reconciliation passes |

Selected 2026-10-28: Payload with SvelteKit. The comparison above is retained
as decision context. Compare editing an event, illustrated newsletter,
initiative and redirect; preview, roles, export/import and memory. AI assistance
is optional and must not receive private member content by default.

Sources: [Payload license](https://github.com/payloadcms/payload/blob/main/LICENSE.md),
[authentication](https://payloadcms.com/docs/authentication/custom-strategies),
[deployment](https://payloadcms.com/docs/production/deployment),
[Wagtail features](https://wagtail.org/features/),
[accessibility](https://wagtail.org/accessibility/),
[Ghost headless limits](https://ghost.org/docs/jamstack/),
[Strapi plans](https://strapi.io/pricing-cms), [Directus license](https://directus.com/license).

## Community shortlist

HumHub best matches profiles, groups and a member directory; review commercial
module dependencies. Discourse best matches searchable stories, moderation,
discussion and chat. Mobilizon centers events/groups. WorkAdventure resembles
a virtual clubhouse, but its Commons Clause license and video infrastructure
make it a poor initial budget/FLOSS fit.

Profiles and mentorship should be opt-in. Decide invitation verification,
membership lifecycle, visibility, moderation ownership, reporting, export and
deletion before collecting personal data. Offer bounded themes/blocks for
creative profiles; never run arbitrary member HTML/JavaScript.

Sources: [HumHub](https://docs.humhub.org/docs/about/humhub),
[Discourse](https://www.discourse.org/features),
[Mobilizon](https://docs.mobilizon.org/about/),
[WorkAdventure license](https://workadventu.re/faq/is-workadventure-open-source/).
