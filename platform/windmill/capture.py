"""Guest-only private evidence capture; stdout contains tested public summaries only."""
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time

from diagnostics import summarize_text, summarize_unit

ROOT = Path('/var/lib/windmill-test-evidence')
UNIT = 'podman-terrace-windmill-server.service'
PROPERTIES = ['ActiveState', 'SubState', 'Result', 'ExecMainCode', 'ExecMainStatus', 'MainPID', 'ControlPID',
              'ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic', 'ExecStartPost', 'ControlGroup']


def command(arguments, timeout=5, limit=1024 * 1024):
    data = bytearray()
    truncated = False
    timed_out = False
    try:
        child = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True, bufsize=0)
    except OSError:
        return {'exitStatus': None, 'stdout': '', 'unavailable': True}
    try:
        os.set_blocking(child.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while selector.get_map() or child.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _events in selector.select(min(remaining, 0.1)):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        data.extend(chunk)
                        if len(data) > limit:
                            truncated = True
                            del data[:-limit]
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
        child.stdout.close()
    return {'exitStatus': child.returncode, 'stdout': data.decode('utf8', 'replace'),
            'truncated': truncated, 'timedOut': timed_out}


def read_launcher(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'present': False, 'text': ''}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError()
        limit = 4 * 1024 * 1024
        os.lseek(fd, max(0, info.st_size - limit), os.SEEK_SET)
        return {'present': True, 'bytes': info.st_size, 'truncated': info.st_size > limit,
                'text': os.read(fd, limit).decode('utf8', 'replace')}
    finally:
        os.close(fd)


def collect():
    raw_properties = command(['/run/current-system/sw/bin/systemctl', 'show', UNIT,
                              '--property=' + ','.join(PROPERTIES)])
    properties = dict(line.split('=', 1) for line in raw_properties['stdout'].splitlines() if '=' in line)
    pids = {int(properties[key]) for key in ['MainPID', 'ControlPID'] if properties.get(key, '').isdigit()}
    pids.update(int(pid) for pid in re.findall(r'(?:^|;)\s*pid=([0-9]+)', properties.get('ExecStartPost', '')))
    group = properties.get('ControlGroup')
    if group == '/system.slice/' + UNIT:
        for path in Path('/sys/fs/cgroup' + group).rglob('cgroup.procs'):
            pids.update(int(pid) for pid in path.read_text().split() if pid.isdigit())
    receipts = []
    for pid in sorted(pids - {0}):
        try:
            start = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(') ', 1)[1].split()[19]
            receipts.append({'pid': pid, 'presentAtCapture': True, 'kernelStartTicks': start})
        except FileNotFoundError:
            receipts.append({'pid': pid, 'presentAtCapture': False})
    try:
        launcher = read_launcher('/var/lib/windmill-test-launcher.log')
    except (OSError, ValueError):
        launcher = {'present': True, 'unavailable': True, 'text': ''}
    journal = '/run/current-system/sw/bin/journalctl'
    return {'schema': 1, 'capturedMonotonicNs': time.monotonic_ns(), 'unit': UNIT,
            'propertiesCommand': raw_properties, 'properties': properties, 'pidObservations': receipts,
            'launcher': launcher,
            'privateJournal': command([journal, '--namespace=terrace-apps', '--no-pager', '-n', '300', '-o', 'cat']),
            'unitJournal': command([journal, '-u', UNIT, '--no-pager', '-n', '100', '-o', 'cat'])}


def write_private(bundle, root=ROOT):
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o700:
        raise ValueError()
    fd = os.open(root / 'raw.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(bundle, stream)
        stream.write('\n')


def main():
    os.umask(0o077)
    if len(sys.argv) != 1 or os.geteuid() != 0:
        raise ValueError()
    bundle = collect()
    write_private(bundle)
    text = bundle['launcher']['text'] + '\n' + bundle['privateJournal']['stdout'] + '\n' + bundle['unitJournal']['stdout']
    for line in summarize_text(text) + summarize_unit(bundle['properties']):
        print(line)
    print('Windmill private raw evidence captured; content suppressed.')
    if bundle['launcher'].get('truncated'):
        print('Windmill launcher capture was truncated; no stage-absence conclusion is valid.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Windmill private evidence capture unavailable; raw diagnostics suppressed.', file=sys.stderr)
        sys.exit(1)
