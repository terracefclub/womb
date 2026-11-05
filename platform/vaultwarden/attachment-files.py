"""Bounded encrypted-file continuity evidence inside disposable Vault guests."""
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import stat
import sys


ROOT = Path('/var/lib/vaultwarden/attachments')
RECEIPT = Path('/var/lib/terrace-vaultwarden-inputs/attachment-files.json')


def bounded(path, maximum, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode) and 0 < info.st_size <= maximum
        if private:
            assert info.st_uid == os.getuid() and info.st_mode & 0o077 == 0
        value = os.read(fd, maximum+1)
        assert len(value) == info.st_size and len(value) <= maximum
        return value
    finally:
        os.close(fd)


def inventory(root):
    assert stat.S_ISDIR(root.lstat().st_mode)
    directories = list(islice(root.iterdir(), 3))
    assert 0 < len(directories) <= 2
    result = {}
    for directory in directories:
        assert re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', directory.name)
        assert stat.S_ISDIR(directory.lstat().st_mode)
        files = list(islice(directory.iterdir(), 3))
        assert 0 < len(files) <= 2
        for path in files:
            assert re.fullmatch(r'[a-f0-9]{20}', path.name)
            data = bounded(path, 4096)
            assert b'Synthetic encrypted attachment bytes' not in data
            result[str(path.relative_to(root))] = {'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    assert len(result) == 2
    return result


def main():
    assert len(sys.argv) == 2 and sys.argv[1] in ['capture','verify']
    value = inventory(ROOT)
    if sys.argv[1] == 'capture':
        fd = os.open(RECEIPT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
    else:
        assert json.loads(bounded(RECEIPT, 16384, private=True)) == value
    print('Vaultwarden encrypted attachment inventory '+sys.argv[1]+' passed.')


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        print('Vaultwarden attachment inventory rejected; diagnostics suppressed.', file=sys.stderr)
        raise SystemExit(1)
