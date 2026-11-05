# Independently contained rehearsal drivers

This is a prerequisite for the combined bootstrap/recovery rehearsal. It does
not enable an actual host, install a cloud VM or claim a complete service drill.

`host.py` starts a uniquely named transient system service with
`RuntimeMaxSec`, `KillMode=control-group` and a bounded stop grace. Its main
`guardian.py` owns the shared build lock and watches the controller through a
pidfd plus kernel start time. Controller loss stops the driver and every child
in this exact unit, including children that change their process group/session.
Host systemd enforces the deadline and cgroup cleanup even if the guardian is
killed or frozen. No Falcon service, daemon setting or linger setting is changed.

The caller must have existing passwordless permission to create/stop its own
transient system unit. It runs as the invoking non-root user; unavailable host
support fails closed. Inputs, outputs, logs, receipts and runtime state live in
an existing mode0700 owner-only proof directory outside the Nix store. No runtime
secret is passed on a command line or written into a build input. The standalone
driver is built through `platform/run-check`; actual guest execution is owned by
the transient service. Logs are private and are not a general secret redactor.

Run on the x86-64 Linux builder:

```sh
install -d -m 0700 /absolute/private/rehearsal-proof
BUILD_LOCK=/path/to/shared-heavy.lock platform/containment-check \
  /absolute/private/rehearsal-proof
```

The committed check builds a minimal driver and boots a uniquely tagged real
QEMU guest for each case: controller SIGKILL, guardian SIGKILL, a frozen guardian
which forces the independent host deadline, and launcher failure after a real
unit/guest has been created. Each guest must reach its ready marker first.
Observed guest PID/start-time pairs and the guardian must be absent afterwards;
an unrelated bounded sentinel process must remain alive. Failure cleanup only
targets the exact random unit and processes created by that case. Needing
fallback cleanup never converts a failed case into a pass.

Each case writes a machine-readable private receipt and the check writes
`containment-result.json`. Driver startup is limited to35seconds and the host
unit to95seconds plus its15second stop deadline; queue timeout is60seconds for
these tiny probes. Cold driver builds are separately bounded at900seconds and
use an explicit60second queue bound,15second cleanup grace and a tracked child
with TERM/wait cleanup. This build phase uses the existing Nix-build timeout;
the independent cgroup contains execution of the already-built VM driver.
The harness reserves the shared lock for its full four-case window (at most
600seconds waiting), while the inner probe build/runtime operations use a
private lock. The reservation descriptor is not inherited by the simulated
controller, so another worker cannot interleave a long build between fault cases.
The test's deliberately sleeping workload cannot become
an idle VM. Before reporting idle, verify the recorded units/PIDs are gone.

This protects the standalone driver lifetime; it does not prove service backup
consistency, full-stack bootstrap, restore or scoped data deletion. Those are
separate checks built on this containment boundary. A physical host failure or
kernel failure is outside this host-level mechanism.
