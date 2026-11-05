"""Export only bounded fixed restore-error files into private test evidence."""
import os
from pathlib import Path
import re
import stat
import sys

LIMIT = 1024 * 1024
FILES = {
    'restore.log': '/var/tmp/rehearsal-restore.log',
    'globals.err': '/var/lib/rehearsal-recovery/globals.err',
    'reconciliation.txt': '/var/lib/rehearsal-recovery/reconciliation.txt',
    **{'restore-' + name + '.err': '/var/lib/rehearsal-recovery/restore-' + name + '.err'
       for name in ['keycloak', 'terrace_cms', 'terrace_documenso', 'terrace_windmill', 'terrace_vaultwarden']},
}


def export(output, files=FILES):
    output.mkdir(mode=0o700)
    for name, source in files.items():
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > LIMIT:
                raise ValueError()
            value = os.read(descriptor, LIMIT + 1)
            after = os.fstat(descriptor)
            if len(value) != before.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                raise ValueError()
            target = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(target, 'wb') as stream:
                stream.write(value)
        finally:
            os.close(descriptor)


if __name__ == '__main__':
    try:
        os.umask(0o077)
        if len(sys.argv) != 2 or not re.fullmatch(r'/tmp/shared/restore-failure-[a-z0-9_]+/files', sys.argv[1]):
            raise ValueError()
        export(Path(sys.argv[1]))
    except Exception:
        print('Private restore evidence export failed.', file=sys.stderr)
        sys.exit(1)
