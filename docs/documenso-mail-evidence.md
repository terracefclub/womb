# Read-only failed-test mail evidence

`platform/documenso/inspect-mail-disk.py` extracts Mailpit's database, WAL and
shared-memory files from one stopped disposable Documenso test disk. It does not
mount or reboot the guest, modify the original, fetch a URL or print mail/token
content. This is a diagnostic for synthetic VM tests, not an import or backup.

Run only on the Linux builder, under the shared queue. Retain the actual host
process receipt from the failed run: both recorded QEMU PIDs must be absent,
including before conversion/extraction. Reused PIDs cause a conservative refusal;
the helper never stops another process. Confirm the original image's owner/mode
from the protected run evidence rather than guessing them.

Use `qemu-img` and `debugfs` from the existing pinned VM derivation's closure.
List its public store paths with `nix-store --query --requisites --include-outputs
TEST_DERIVER`; no package installation or new VM build is required. Supply the
exact executable paths. From the repository's Linux dev shell, a bounded run is:

```sh
sudo "$(command -v flock)" --no-fork --timeout 3600 "$BUILD_LOCK" \
  "$(command -v timeout)" --signal=TERM --kill-after=10 300 "$(command -v python3)" \
  platform/documenso/inspect-mail-disk.py \
  --image /private/failed-build/vm-state-documents/documents.qcow2 \
  --output /private/owned/evidence/new-mail-inspection \
  --qemu-img /nix/store/PINNED-QEMU/bin/qemu-img \
  --debugfs /nix/store/PINNED-E2FSPROGS/bin/debugfs \
  --process-receipt /private/owned/evidence/owned-processes.json \
  --source-uid RECORDED_IMAGE_UID --source-mode 0644
```

The output parent must be an existing non-root operator-owned mode 0700 directory;
the new output must not exist. Paths with shell/debugfs metacharacters are
rejected. The source is opened once read-only without following a symlink, with
the exact recorded UID/mode. Header checks reject backing files, external data,
encryption, corruption flags and virtual sizes above 16 GiB before invoking parsers.
At least 32 GiB free is required for bounded private copies. The pinned default
Nix VM disk contains an unpartitioned ext4 filesystem; other layouts fail closed.

After copying, the helper permanently drops root privileges to the output
parent's owner. Conversion and read-only `debugfs` dumps operate only on that
verified copy, with process deadlines and file-size limits. Parser stdout is discarded; the
small image-info response uses a private regular file capped at 64 KiB before
execution and is rejected at the cap. Output remains
mode 0700/0600. Hidden-process `/proc` mounts are refused so dropping privileges
cannot make a live recorded PID appear absent. A receipt records pre/post original hashes and byte counts, never
mail contents. Keep the QCOW2 copy, sparse raw copy and extracted files protected;
no files are automatically removed or overwritten. Inspect the database only
internally and report safe counts/shapes. Do not publish or commit it.
