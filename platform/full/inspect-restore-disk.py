"""Extract fixed private restore diagnostics from a stopped synthetic disk copy."""
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys

support_path = Path(__file__).parents[1] / "documenso/inspect-mail-disk.py"
spec = importlib.util.spec_from_file_location("retained_disk_support", support_path)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)

GIB = 1024 ** 3
DATABASES = ("terrace_cms", "keycloak", "terrace_documenso", "terrace_windmill", "terrace_vaultwarden")
FILES = (("restore.log", "/var/tmp/rehearsal-restore.log"),
         ("globals.err", "/var/lib/rehearsal-recovery/globals.err"),
         ("reconciliation.txt", "/var/lib/rehearsal-recovery/reconciliation.txt")) + tuple(
             ("restore-" + name + ".err", "/var/lib/rehearsal-recovery/restore-" + name + ".err")
             for name in DATABASES)


def qcow_header(value):
    if len(value) < 104 or value[:4] != b"QFI\xfb":
        raise ValueError()
    version = struct.unpack_from(">I", value, 4)[0]
    backing, backing_size = struct.unpack_from(">QI", value, 8)
    size = struct.unpack_from(">Q", value, 24)[0]
    encryption = struct.unpack_from(">I", value, 32)[0]
    incompatible = struct.unpack_from(">Q", value, 72)[0] if version == 3 else 0
    if version not in [2, 3] or backing or backing_size or encryption or incompatible & 6 or not 0 < size <= 32 * GIB:
        raise ValueError()
    return size


def process_records(value, expected):
    if type(expected) is not int or not 1 <= expected <= 256 or not isinstance(value, list) or len(value) != expected:
        raise ValueError()
    rows = []
    for row in value:
        if not isinstance(row, dict) or type(row.get("pid")) is not int or not 1 < row["pid"] < 2 ** 31:
            raise ValueError()
        start = row.get("start")
        origins = row.get("origins")
        if not isinstance(start, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", start):
            raise ValueError()
        if not isinstance(origins, list) or not 1 <= len(origins) <= 32 or any(not isinstance(item, str) or not 1 <= len(item) <= 1024 for item in origins):
            raise ValueError()
        rows.append({"pid": row["pid"], "start": start})
    if len({row["pid"] for row in rows}) != expected:
        raise ValueError()
    return rows


def source_info(info, owner):
    if type(owner) is not int or owner <= 0 or not stat.S_ISREG(info.st_mode) or info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= 8 * GIB:
        raise ValueError()


def inspect(options):
    image, output = Path(options.image), Path(options.output)
    if sys.platform != "linux" or image.resolve() != image or output.parent.resolve() != output.parent:
        raise ValueError()
    if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9/_.-]*", str(output)) or any(part in [".", ".."] for part in str(output).split("/")):
        raise ValueError()
    parent = output.parent.stat()
    if parent.st_uid == 0 or parent.st_uid != options.source_uid or stat.S_IMODE(parent.st_mode) != 0o700 or os.geteuid() not in [0, parent.st_uid]:
        raise ValueError()
    for path, name in [(options.qemu_img, "qemu-img"), (options.debugfs, "debugfs")]:
        resolved = Path(path).resolve()
        if not str(resolved).startswith("/nix/store/") or resolved.name != name or not os.access(resolved, os.X_OK):
            raise ValueError()
    rows = process_records(support.private_receipt(options.process_receipt, parent.st_uid), options.process_count)
    proc = [line.split()[3] for line in Path("/proc/mounts").read_text().splitlines() if line.split()[1:3] == ["/proc", "proc"]]
    if len(proc) != 1 or not support.visible_proc(proc[0]):
        raise ValueError()
    support.require_absent(rows)
    descriptor = os.open(image, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        original = os.fstat(descriptor)
        source_info(original, options.source_uid)
        size = qcow_header(os.pread(descriptor, 104, 0))
        space = os.statvfs(output.parent)
        if space.f_bavail * space.f_frsize < size + original.st_size + GIB:
            raise ValueError()
        output.mkdir(mode=0o700)
        before = support.stream_source(descriptor, original.st_size)
        copied = output / "gateway.qcow2"
        with open(copied, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as destination:
            if support.stream_source(descriptor, original.st_size, destination) != before:
                raise ValueError()
        copy_descriptor = os.open(copied, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if support.stream_source(copy_descriptor, original.st_size) != before:
                raise ValueError()
        finally:
            os.close(copy_descriptor)
        support.require_absent(rows)
        if os.geteuid() == 0:
            os.chown(copied, parent.st_uid, parent.st_gid)
            os.chown(output, parent.st_uid, parent.st_gid)
            os.setgroups([])
            os.setgid(parent.st_gid)
            os.setuid(parent.st_uid)
        if os.geteuid() == 0:
            raise ValueError()
        support.require_absent(rows)
        info = json.loads(support.command([options.qemu_img, "info", "--output=json", str(copied)], 15, 65536, stdout_directory=output))
        if info["format"] != "qcow2" or info["virtual-size"] != size or info.get("encrypted", False) or any("backing" in key for key in info):
            raise ValueError()
        raw = output / "gateway.raw"
        support.command([options.qemu_img, "convert", "-f", "qcow2", "-O", "raw", str(copied), str(raw)], 150, 32 * GIB)
        raw_info = raw.lstat()
        if not stat.S_ISREG(raw_info.st_mode) or raw_info.st_size != size:
            raise ValueError()
        raw.chmod(0o600)
        support.require_absent(rows)
        files = []
        for name, source in FILES:
            target = output / name
            # debugfs reads an unmounted raw EXT4 filesystem; -w is never used.
            support.command([options.debugfs, "-R", "dump " + source + " " + str(target), str(raw)], 10, 8 * 1024 * 1024)
            if target.exists():
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
                    raise ValueError()
                target.chmod(0o600)
                files.append({"name": name, "bytes": info.st_size})
        if not any(row["name"] == "restore.log" for row in files):
            raise ValueError()
        support.require_absent(rows)
        after = support.stream_source(descriptor, original.st_size)
        final = os.fstat(descriptor)
        if after != before or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns, original.st_ctime_ns):
            raise ValueError()
        receipt = {"sourceSha256Before": before, "sourceSha256After": after, "sourceUnchanged": True,
                   "sourceDevice": original.st_dev, "sourceInode": original.st_ino, "virtualBytes": size,
                   "ownedProcessesAbsent": len(rows), "files": files}
        with open(output / "receipt.json", "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(receipt, stream)
        print(json.dumps({"sourceUnchanged": True, "ownedProcessesAbsent": len(rows), "privateFiles": files}))
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        os.umask(0o077)
        parser = support.SafeParser(description=__doc__)
        for key in ["image", "output", "qemu-img", "debugfs", "process-receipt"]:
            parser.add_argument("--" + key, required=True)
        for key in ["source-uid", "process-count"]:
            parser.add_argument("--" + key, type=int, required=True)
        inspect(parser.parse_args())
    except Exception:
        print("Read-only retained restore inspection failed; private diagnostics suppressed.", file=sys.stderr)
        sys.exit(1)
