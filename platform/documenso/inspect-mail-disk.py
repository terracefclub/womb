"""Read an owned, stopped disposable Documenso test disk; never print its contents."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import stat
import struct
import subprocess
import sys
import tempfile

GIB = 1024 ** 3


def qcow_header(value):
    if len(value) < 104 or value[:4] != b"QFI\xfb":
        raise ValueError()
    version = struct.unpack_from(">I", value, 4)[0]
    backing, backing_size = struct.unpack_from(">QI", value, 8)
    size = struct.unpack_from(">Q", value, 24)[0]
    encryption = struct.unpack_from(">I", value, 32)[0]
    incompatible = struct.unpack_from(">Q", value, 72)[0] if version == 3 else 0
    if version not in [2, 3] or backing or backing_size or encryption or incompatible & 6 or not 0 < size <= 16 * GIB:
        raise ValueError()
    return size


def qemu_records(data):
    if not isinstance(data, list) or len(data) > 128:
        raise ValueError()
    rows = []
    for row in data:
        if not isinstance(row, dict) or not isinstance(row.get("comm"), str):
            raise ValueError()
        if row["comm"].startswith("qemu"):
            if type(row.get("pid")) is not int or not 1 < row["pid"] < 2 ** 31 or type(row.get("startTicks")) is not int or row["startTicks"] <= 0:
                raise ValueError()
            rows.append(row)
    if len(rows) != 2 or len({row["pid"] for row in rows}) != 2:
        raise ValueError()
    return rows


def require_absent(rows, proc=Path("/proc")):
    # A reused PID is uncertainty too. Never stop or adopt another process.
    for row in rows:
        try:
            (proc / str(row["pid"])).stat()
        except FileNotFoundError:
            continue
        raise ValueError()


def visible_proc(options):
    return not any(value.startswith("hidepid=") and value != "hidepid=0" for value in options.split(","))


def private_receipt(path, owner):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o077 or info.st_size > 1024 * 1024:
            raise ValueError()
        return json.loads(os.read(descriptor, 1024 * 1024 + 1))
    finally:
        os.close(descriptor)


def stream_source(descriptor, size, output=None):
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = os.read(descriptor, min(remaining, 4 * 1024 * 1024))
        if not block:
            raise ValueError()
        digest.update(block)
        if output is not None:
            output.write(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise ValueError()
    return digest.hexdigest()


def command(arguments, seconds, maximum, stdout_directory=None):
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))
    if stdout_directory is None:
        result = subprocess.run(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=seconds, preexec_fn=limits, close_fds=True)
        if result.returncode:
            raise ValueError()
        return b""
    # qemu info has no large file artifact: cap its regular stdout file before
    # execution, then read only the bounded result. Never buffer a parser pipe.
    with tempfile.TemporaryFile(dir=stdout_directory) as captured:
        result = subprocess.run(arguments, stdout=captured, stderr=subprocess.DEVNULL,
                                timeout=seconds, preexec_fn=limits, close_fds=True)
        captured.seek(0)
        value = captured.read(65537)
        if result.returncode or len(value) >= 65536:
            raise ValueError()
        return value


def inspect(options):
    image, output = Path(options.image), Path(options.output)
    if sys.platform != "linux" or os.geteuid() != 0 or image.resolve() != image or output.parent.resolve() != output.parent:
        raise ValueError()
    if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9/_.-]*", str(output)) or any(part in [".", ".."] for part in str(output).split("/")):
        raise ValueError()
    parent = output.parent.stat()
    if parent.st_uid == 0 or parent.st_mode & 0o777 != 0o700:
        raise ValueError()
    for path, name in [(options.qemu_img, "qemu-img"), (options.debugfs, "debugfs")]:
        resolved = Path(path).resolve()
        if not str(resolved).startswith("/nix/store/") or resolved.name != name or not os.access(resolved, os.X_OK):
            raise ValueError()
    rows = qemu_records(private_receipt(options.process_receipt, parent.st_uid))
    mounts = Path("/proc/mounts").read_text().splitlines()
    proc = [line.split()[3] for line in mounts if line.split()[1:3] == ["/proc", "proc"]]
    if len(proc) != 1 or not visible_proc(proc[0]):
        raise ValueError()
    require_absent(rows)
    descriptor = os.open(image, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        original = os.fstat(descriptor)
        mode = int(options.source_mode, 8)
        if mode not in [0o600, 0o644] or not stat.S_ISREG(original.st_mode) or original.st_uid != options.source_uid or stat.S_IMODE(original.st_mode) != mode or not 0 < original.st_size <= 8 * GIB:
            raise ValueError()
        size = qcow_header(os.pread(descriptor, 104, 0))
        space = os.statvfs(output.parent)
        if space.f_bavail * space.f_frsize < 32 * GIB:
            raise ValueError()
        output.mkdir(mode=0o700)
        before = stream_source(descriptor, original.st_size)
        copied = output / "documents.qcow2"
        with open(copied, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as destination:
            if stream_source(descriptor, original.st_size, destination) != before:
                raise ValueError()
        copy_descriptor = os.open(copied, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if stream_source(copy_descriptor, original.st_size) != before:
                raise ValueError()
        finally:
            os.close(copy_descriptor)
        require_absent(rows)
        os.chown(copied, parent.st_uid, parent.st_gid)
        os.chown(output, parent.st_uid, parent.st_gid)
        # Parsers run permanently unprivileged, against the verified private copy.
        os.setgroups([])
        os.setgid(parent.st_gid)
        os.setuid(parent.st_uid)
        require_absent(rows)
        info = json.loads(command([options.qemu_img, "info", "--output=json", str(copied)], 15, 65536, stdout_directory=output))
        if info["format"] != "qcow2" or info["virtual-size"] != size or any("backing" in key for key in info):
            raise ValueError()
        raw = output / "documents.raw"
        command([options.qemu_img, "convert", "-f", "qcow2", "-O", "raw", str(copied), str(raw)], 120, 16 * GIB)
        raw.chmod(0o600)
        require_absent(rows)
        files = []
        for name in ["mail.db", "mail.db-wal", "mail.db-shm"]:
            target = output / name
            command([options.debugfs, "-R", "dump -p /var/lib/documenso-test-mailpit/" + name + " " + str(target), str(raw)], 15, 128 * 1024 * 1024)
            if target.exists():
                if target.is_symlink() or not target.is_file() or target.stat().st_size > 128 * 1024 * 1024:
                    raise ValueError()
                target.chmod(0o600)
                files.append({"name": name, "bytes": target.stat().st_size})
        if not any(row["name"] == "mail.db" for row in files):
            raise ValueError()
        after = stream_source(descriptor, original.st_size)
        final = os.fstat(descriptor)
        if after != before or (final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (original.st_size, original.st_mtime_ns, original.st_ctime_ns):
            raise ValueError()
        receipt = {"sourceSha256Before": before, "sourceSha256After": after, "sourceUnchanged": True,
                   "sourceDevice": original.st_dev, "sourceInode": original.st_ino, "virtualBytes": size,
                   "qemuProcessesAbsent": len(rows), "files": files}
        with open(output / "receipt.json", "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(receipt, stream)
        print(json.dumps({"sourceUnchanged": True, "privateFiles": files}))
    finally:
        os.close(descriptor)


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        raise ValueError()


if __name__ == "__main__":
    try:
        os.umask(0o077)
        parser = SafeParser(description=__doc__)
        for key in ["image", "output", "qemu-img", "debugfs", "process-receipt", "source-mode"]:
            parser.add_argument("--" + key, required=True)
        parser.add_argument("--source-uid", type=int, required=True)
        inspect(parser.parse_args())
    except Exception:
        print("Read-only retained mail inspection failed; private diagnostics suppressed.", file=sys.stderr)
        sys.exit(1)
