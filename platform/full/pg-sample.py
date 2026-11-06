"""Private, bounded connection sampling inside the disposable PostgreSQL guest."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


QUERY = "SELECT current_setting('max_connections')||'|'||count(*) FROM pg_stat_activity WHERE backend_type='client backend'"
DIRECTORY = Path('/run/rehearsal-pg-sample')


def parse_sample(value):
    match = re.fullmatch(rb'([1-9][0-9]{0,3})\|([0-9]{1,4})\n?', value)
    if not match:
        raise ValueError('Connection sample rejected.')
    maximum, clients = map(int, match.groups())
    if maximum > 1024 or clients > maximum:
        raise ValueError('Connection sample rejected.')
    return maximum, clients


def store(value):
    temporary = DIRECTORY / 'next.json'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    temporary.replace(DIRECTORY / 'result.json')


def main():
    os.umask(0o077)
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    for signum in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
        signal.signal(signum, stop)
    started = time.monotonic()
    deadline = started + 350
    result = {'samples': 0, 'sampledPeakClients': 0, 'configuredMaximum': None, 'elapsedMs': 0}
    while not stopped and time.monotonic() < deadline:
        try:
            command = subprocess.run(['psql', '-XAt', '-w', '-v', 'ON_ERROR_STOP=1', '-d', 'postgres', '-c', QUERY],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=5)
        except subprocess.CalledProcessError:
            # A normal control-group stop also terminates an in-flight psql.
            # Only an observed stop signal can classify that as cancellation.
            if stopped:
                break
            raise
        maximum, clients = parse_sample(command.stdout)
        if result['configuredMaximum'] not in [None, maximum]:
            raise ValueError('Connection sample rejected.')
        result['configuredMaximum'] = maximum
        result['sampledPeakClients'] = max(result['sampledPeakClients'], clients)
        result['samples'] += 1
        result['elapsedMs'] = round((time.monotonic() - started) * 1000)
        store(result)
        time.sleep(0.2)
    if result['samples'] == 0:
        raise ValueError('Connection sample rejected.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('PostgreSQL connection sampling failed; diagnostics suppressed.', file=sys.stderr)
        sys.exit(1)
