# Service lifecycle acceptance

Audit date: 2026-11-04. This is an acceptance matrix, not a declaration that all
services are deployed. Existing cloud power, billing and service health are
unknown without account access. No provider operation is authorized by this plan.
Old Gravity Forms entries are excluded; protected supplied exports remain unchanged.

## Source and evidence boundary

Womb base `9447a8a1812ecd6b7c4456d8d073ba63dbb2c618` was audited alongside
read-only Millionaire `origin/main`
`e390f26d81c1eff48324f6e5d8f4c40f71a69455` (remote `ls-remote` corroborated).
The user's “trunk” is actually `main` there. Its primary checkout HEAD was older,
`9d9bebdf5f479858839293f2bd78aa9c78b6fe44`; the audit used `git show origin/main:…`,
without changing that checkout or its refs. Source declarations and historical
reports are evidence of design/tests, not a fresh live homelab audit.

NixOS VM tests boot **prebuilt closures** on the Linux builder. They do not prove
blank provider-VM provisioning, initial NixOS installation/image delivery, secret
bootstrap or a complete all-services deployment. Neither the evaluation-only
`hetzner.nix` disk placeholder nor mocked Pulumi inputs proves those stages.
Native website/identity services deliberately coexist with the K3s foundation;
they are not managed by that cluster. This reduces a small host's operational
load, while leaving several homelab automations absent.

## Per-service coverage

“Test” below names committed reproducible checks; result details are in the linked
runbooks. A restart is not a destroy/restore. No row claims a combined full-stack
bootstrap from empty twice, with the original node destroyed before restore.

| Service | Existing test/evidence | Retain/delete and recovery | Remaining acceptance |
| --- | --- | --- | --- |
| K3s single node | `platform/test.nix`: Ready API, blocked external API, pod/PVC write, K3s restart and pod recreation | PVC deletion leaves Released PV and marker; `local-retain` is opt-in, bundled default can delete | Datastore/token + PV backup and fresh-host restore; remove/reinstall, rollout rollback, storage pressure, full install |
| Headscale relay | `relay-test.nix`: TLS/firewall, health, SQLite integrity, user retained after restart | `backup-test.nix` backs synthetic relay state to two encrypted SFTP targets and restores selected snapshots | Native enrollment/SSO contract, real relay fresh-node restore, explicit disposable delete/rebootstrap |
| Backup service | `backup-test.nix`: selected older/latest snapshots, corruption rejection, source untouched on failed restore, synthetic source destruction and restore | Isolated restore auto-stops DB by default; `--keep-running` is explicit; stop checked | Actual offsite target, retention/freshness alert delivery, key escrow retrieval without old node, consistent live-write snapshots |
| Keycloak + PostgreSQL | `identity-test.nix`: real startup, restart, outage/readiness, runtime realm guard, bounded proxy/admin surfaces | `identity-restore-test.nix`: real 101-table schema, realm ID/issuer/JWKS and protected inputs restored into fresh guest; source unchanged | Real per-person enrollment/recovery, module removal/retain + explicit synthetic delete, old-node-off restore, public cutover |
| Website + Payload CMS + PostgreSQL/uploads | `website-test.nix`: actual pinned Linux bundles, six migrations repeated, live application stop ordering, 25-table recovery, anonymous/private/draft controls and image cache | Encrypted inputs/DB/uploads restored to separate guest, source unchanged; code immutable, cache disposable | Native public HTTPS/SSO follow-up pending separately; consistent live-write snapshot, empty bootstrap twice, old-node-off restore, rollback/install |
| Home Assistant / Frigate demos | `demos-test.nix`: both/individual selection, restart, module-off retention, teardown requires `--yes`, scoped synthetic deletion then empty rebootstrap | Unselected/unrelated state preserved; explicitly synthetic fixtures only | Real devices, cameras/NIC/disks, real per-user SSO, secrets escrow and app-aware restore; no physical deployment claim |
| Documenso | Preserved legacy membership workflow only | No new platform bootstrap/restore proof | Runtime module, per-user SSO, signing/email sandbox, storage/secrets/restore lifecycle |
| Windmill | Preserved workflow; `tests/membership` checks synthetic atomic Sheets replay protocol | This is workflow replay, not Windmill/Documenso/Sheets provider recovery | Runtime/bootstrap, per-user SSO, migration and job replay boundaries, no real messages/payments |
| Vaultwarden | No native/new-platform module found | No VM recovery evidence | Runtime/bootstrap, per-user access and recovery, data/attachment/key restore |
| Public static CDN | Policy/plan only, see issue #7 | No external purge/invalidation or recovery proof | Actual provider isolation, cache/auth exclusions, invalidation, geographically distributed measurements |

The website's TFC release proof is separate evidence: source `3902e16`, real normal
SSO and publication/privacy tests on relocated bundles. It is not evidence that
Womb's native public deployment has passed. Actual install profiles still leave
optional services disabled. Legacy membership routing and provider records are
not exercised by these disposable tests.

## Homelab conventions and Terrace gaps

Current Millionaire source provides reusable conventions, not a reason to copy
its multi-node resource cost: `README.md`, `nixidy/apps/development/windmill.nix`,
`nixidy/apps/identity/*`, `nixidy/system/storage/{retain,backup-coverage,volsync,postgres}.nix`
and `docs/pvc-and-postgres-restore.md`. Its Windmill declares PostSync bootstrap,
Keycloak OIDC, generated/externally delivered secrets, pinned images, health and
resource settings. `retain.nix` protects PVCs/CNPG Clusters from prune/delete,
with explicit disposable-cache exclusions. Backup coverage declares offsite paths
or exclusions and an uncovered-PVC alert. Restore tooling and cluster checks are
not a disposable all-app install rehearsal. No Documenso/Vaultwarden module was
found in the audited app paths; live inventory was not queried.

| Acceptance dimension | Terrace evidence / explicit gap |
| --- | --- |
| Idempotent bootstrap and individual SSO | Realm startup is create-only, not reconciliation; CMS migrations repeat. Complete per-app per-person bootstrap remains absent for legacy services. No shared operator/admin session is acceptable. |
| Secret creation, delivery and escrow | Native modules consume private runtime files; VM synthetic inputs prove permissions/restoration. No production generator/delivery/independent escrow retrieval is established. Never evaluate secret contents into Nix/store. |
| Retention and restore | Scoped synthetic backup/recovery above; app removal retains state by default. Each dataset needs an owner, retention and tested offsite path or explicit exclusion. Cache may rebuild; signing keys, uploads and DB may not. |
| Ingress and egress | Private K3s API and limited native proxies tested; public gateway is separate work. No complete namespace egress/default-deny policy or production firewall validation. |
| Pins and updates | Pinned Nix inputs and hash-checked TFC archives; no complete automatic promotion/rollback pipeline across services. |
| Health, alerts, logs | Native readiness/restarts/private bounded logs tested locally. No delivered production alert/freshness/redaction proof across all services. |
| Resource and pressure | Native limits and VM sizes are declared. No measured combined host peak, disk-full/OOM/backup pressure or provider capacity proof. |
| Rollback and installation | Explicit migrations prevent accidental startup migration. Schema downgrade/compatible rollback and blank-host install remain separate gates. Bare-metal disk/NIC/camera inventory is unknown. |

Tracked in [issue #11](https://gitlab.com/terrace/tech/womb/-/issues/11), milestone
Terrace revival; the supervisor/matrix do not close that acceptance issue.

## Combined disposable rehearsal acceptance

A future orchestrator must implement these steps, not simply run existing checks:

1. Create a unique run ID and ownership manifest; record artifact hashes, deadline,
   node/volume/IP IDs and budget reservation. Validate empty disposable targets.
   Bootstrap infrastructure/install and runtime-only secrets without an old node.
2. Start **all** required services, run idempotent bootstrap twice, then per-user
   SSO/authorization, anonymous/private access, readiness and resource checks.
   External mail/payment/signing integrations remain sandboxed with synthetic data.
3. Quiesce writers and take application-consistent encrypted snapshots of every
   persistent dataset plus recovery inputs. Verify an independent escrow path.
4. Stop and destroy original run-owned compute; verify old node/process absence.
   Preserve encrypted state by default, with ownership/retention/cost recorded.
5. Restore onto a new empty target with no old node alive; verify real schemas,
   realm/signing identity, uploads, permissions and functional per-user flows.
   Record bootstrap, snapshot, destroy, restore and verification durations.
6. Destroy replacement compute, verify inventory absence and release reservations.
   Repeat the whole empty-start rehearsal a second time. A separate explicit
   **synthetic-only** deletion variant verifies exact run-owned state removal and
   empty rebootstrap; retained state and unrelated sentinels must survive other runs.

Failure, cancellation and timeout take the same owned cleanup path. A controller
crash/SIGKILL/SSH loss additionally needs an independent scheduled reaper before
any cloud run; a finally block is insufficient. Never infer deletion from a
successful API request: verify inventory and billable retained volumes/IPs.
No generic deletion tool or real-data deletion is introduced in this increment.

## Bounded VM checks and idle rule

[`platform/run-check`](../platform/run-check) now holds the shared lock only in
its supervisor, uses a new child session, bounds queue/active/Nix-build time and
stops that exact process group on completion, cancellation or timeout. Live group
members after escalation cause failure. `platform/check` and `website-check`
share it. Logs must stay private: Nix output itself is not a secret redactor.
See [rerun and bounds](../platform/README.md#check-supervision).

`test-run-check.py` is synthetic and makes no provider/email/network call. The
native probe boots only a uniquely tagged disposable guest and deliberately
hangs; its controller checks the ready marker, actual Nix timeout/cancellation,
and absence of each observed guest PID/start-time after completion. Failure
recovery signals only the unique QEMU argument + matching kernel start-time.
It never stops Falcon or another worker's services. The independent Nix build
timeout mitigates supervisor death but is not an independent cloud reaper.
SIGKILL, host failure, escaped child sessions and daemon behavior beyond the
observed native proof remain limits; do not claim universal orphan prevention.
Before reporting idle, record actual owned PID/QEMU absence, not only an exit code.

Verified 2026-11-04 on the Linux builder: seven synthetic regressions passed in
5.573 seconds; both real guests reached readiness. Nix rejected the deliberately
hung build after 35 seconds (client exit 1), cancellation returned 143, and both
observed guest PIDs were absent after completion without fallback killing. A
separate `/proc` scan found zero processes with this check checkout as cwd. These
are two specific failure-path checks, not full-service lifecycle acceptance.

## Ephemeral cloud budget gate

Planning figures checked 2026-11-04, **not an account quote or capacity proof**:
[Hetzner official price adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)
indexed EU USD rates CPX22 $0.0368/h and CPX42 $0.1314/h imply a six-hour two-node
drill compute subtotal $1.0092, before IPv4/tax/storage/egress. No automatic standing
CPX42 fallback: its $81.99/month alone exceeds the total limit.
[Powered-off servers remain billed until deleted](https://docs.hetzner.com/cloud/billing/faq/),
and [nested virtualization is unsupported](https://docs.hetzner.com/cloud/servers/faq/):
use provider VMs directly as nodes; QEMU rehearsal stays on Falcon.
[Backblaze B2](https://www.backblaze.com/cloud-storage/pricing) lists $6.95/TB/month,
so 100 GB is about $0.70 before free-tier/tax/egress. Public-static CDN planning
allowance is $0, subject to actual provider terms and measured usage.

Internal planning caps are at most $5/run and less than $20/month aggregate
rehearsals, **inside** the owner's strict less-than-$80/month all-in limit,
including existing server, retained storage, taxes and overlap. Existing-server
current billing is unknown; historical amounts are not current evidence. Before
provision: account-verified total spend/stock/quote, bounded deadline, independent
reaper and deletion/inventory proof are mandatory. No cloud apply, purchase, DNS
change or standing resource is part of this MR.
