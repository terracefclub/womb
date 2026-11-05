# Private restore-failure disk evidence

`platform/full/inspect-restore-disk.py` reads a **stopped, owned synthetic**
rehearsal gateway disk when a failed restore did not export its diagnostics.
It does not modify backup/restore behavior or start any guest. Do not use this
helper for an untrusted disk, a live guest or real user data.

The source must be a canonical regular QCOW2 file owned by the declared nonroot
UID, mode0600, at most8GiB allocated file length and at most32GiB virtual size.
The complete private host process receipt must contain the declared exact
number of unique PID/start identities. Every recorded PID must be absent,
including reused PIDs; unreadable/hidden process evidence fails closed. The
output parent must already be owned by that UID and mode0700. The output leaf
must not exist. Backing files, encryption, external data and corrupt QCOW2 flags
are rejected before any image parser runs.

The helper hashes the original through one retained read-only descriptor,
exclusively copies it, verifies the copy and later rechecks the original hash
and metadata. Only the private copy reaches `qemu-img` and `debugfs`, always
unprivileged (a root invocation permanently drops privileges before parsing).
The default rehearsal disk is raw EXT4 inside QCOW2 without a partition table.
No host mount, NBD, reboot, writable original, host container or package install
is involved. Copies and extracted files remain private and retained on failure.

Exactly eight paths are attempted: `/var/tmp/rehearsal-restore.log`,
`/var/lib/rehearsal-recovery/globals.err`, `reconciliation.txt`, and the five
`restore-<database>.err` files for `terrace_cms`, `keycloak`,
`terrace_documenso`, `terrace_windmill`, `terrace_vaultwarden`. Missing optional
files are recorded by omission; `restore.log` is mandatory. There is no glob,
directory listing or arbitrary guest path argument. Each extracted file is
limited to8MiB and mode0600. Parser stdout is discarded or capped in a private
regular file before capture; parser stderr is suppressed. The receipt/stdout
contains only unchanged-source evidence and fixed filenames/byte counts, never
diagnostic contents. Inspect any recovered errors privately; do not paste raw
restore output into chat, CI or Git.

From a clean committed checkout on the Linux builder, select `qemu-img` and
`debugfs` from the already-realized pinned VM closure (both paths must resolve
inside `/nix/store`). No evaluation or build is needed for this extraction.
Set the following shell variables to the reviewed source, receipt, new output
and existing tools; none contains credential values:

```sh
install -d -m 0700 "$EVIDENCE_PARENT"
direnv exec . timeout --signal=TERM --kill-after=60 700 \
  flock --exclusive --timeout 300 "$BUILD_LOCK" \
  timeout --signal=TERM --kill-after=10 300 \
  python -B platform/full/inspect-restore-disk.py \
  --image "$STOPPED_GATEWAY_QCOW2" --source-uid "$SOURCE_UID" \
  --process-receipt "$OWNED_PROCESS_RECEIPT" --process-count "$PROCESS_COUNT" \
  --output "$EVIDENCE_PARENT/restore-diagnostics" \
  --qemu-img "$QEMU_IMG" --debugfs "$DEBUGFS"
```

Run as the source/output owner when possible. Review the exact process receipt,
source owner/mode/size and command scope before execution; record the launcher
PID/start identity and verify it and its exact children are absent afterward.
Use the existing shared builder lock, not a new lock path. The 300-second active
bound includes copy/hash work; individual image-info/convert/dump bounds are
15/150/10seconds. The queue bound is300seconds and outer deadline700seconds
(with60seconds final kill grace). The output
includes the verified QCOW2 copy, private sparse raw image and `receipt.json`;
retain them with the original. A receipt is only written after all guards and
the final original hash check pass. Free disk space must cover the virtual
image, source copy and an additional1GiB. The raw file has a32GiB file-size cap.

Focused guards (no disk parser or VM launched):

```sh
direnv exec . python tests/platform/test_restore_disk.py -v
```
