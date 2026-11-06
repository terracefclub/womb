"""Runs only in the owned client guest under a bounded systemd control group."""
import json
import os
from pathlib import Path
from contextlib import ExitStack
import selectors
import stat
import subprocess
import sys
import time


COMMANDS = {
    'website': (['rehearsal-browser'], 'Real Chromium denied-to-switch-to-approved checks passed: HTTPS trust, native POST Origin, approved page and secure cookie.'),
    'documents': (['rehearsal-documenso-browser', 'verify', '--signing'], 'Documenso functional browser phase verify passed.'),
    'workflow': (['rehearsal-windmill-browser', 'verify', '--roster'], 'Windmill functional browser phase verify passed.'),
    'vault': (['rehearsal-vaultwarden-browser', 'restore'], 'Vaultwarden browser phase restore passed.'),
}
DIRECTORY = Path('/root/rehearsal-concurrent')


def accepted_output(path, expected):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0
        assert 0 < info.st_size <= 4096
        data = os.read(fd, 4097)
        assert len(data) == info.st_size and data.strip() == expected.encode()
    finally:
        os.close(fd)


def run(directory=DIRECTORY, commands=COMMANDS):
    # Exclusive private output prevents accepting a previous phase's witness.
    directory.mkdir(mode=0o700)
    children = {}
    streams = []
    with ExitStack() as files, selectors.DefaultSelector() as selector:
        for name, (arguments, _expected) in commands.items():
            assert name.isascii() and name.isalpha()
            child = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, close_fds=True, bufsize=0)
            children[name] = child
            for suffix, pipe in [('stdout', child.stdout), ('stderr', child.stderr)]:
                output = files.enter_context((directory / (name + '.' + suffix)).open('xb'))
                os.chmod(output.name, 0o600)
                files.callback(pipe.close)
                os.set_blocking(pipe.fileno(), False)
                state = {'output': output, 'bytes': 0, 'truncated': False}
                streams.append(state)
                selector.register(pipe, selectors.EVENT_READ, state)
        deadline = time.monotonic() + 300
        while selector.get_map() or any(child.poll() is None for child in children.values()):
            assert time.monotonic() < deadline
            for key, _event in selector.select(0.2):
                try:
                    data = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                state = key.data
                remaining = max(0, 4 * 1024 * 1024 - state['bytes'])
                state['output'].write(data[:remaining])
                state['bytes'] += len(data)
                state['truncated'] |= len(data) > remaining
        assert not any(state['truncated'] for state in streams)
    for name, child in children.items():
        assert child.returncode == 0
        accepted_output(directory / (name + '.stdout'), commands[name][1])
    fd = os.open(directory / 'result.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({name: True for name in commands}, stream)


if __name__ == '__main__':
    try:
        # The unit owns every browser descendant even after setsid or an error.
        assert len(sys.argv) == 1 and os.environ.get('INVOCATION_ID') and os.geteuid() == 0
        os.umask(0o077)
        run()
    except BaseException:
        print('Concurrent native checks failed; private child evidence retained.', file=sys.stderr)
        raise SystemExit(1)
