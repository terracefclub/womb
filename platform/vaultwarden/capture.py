"""Disposable guest: retain bounded private evidence, never print raw responses."""
import json
import os
from pathlib import Path
import re
from capture import command, write_private


def main():
    os.umask(0o077)
    units = {}
    for unit in ['vaultwarden.service', 'terrace-vaultwarden-migrate.service', 'terrace-vaultwarden-migration-server.service', 'terrace-vaultwarden-bootstrap.service', 'terrace-vaultwarden-inputs.service', 'terrace-vaultwarden-guard.service']:
        result = command(['systemctl', 'show', unit, '--property=ActiveState,SubState,Result,MainPID,ControlPID,ExecMainCode,ExecMainStatus,ExecStartPre,ExecStartPost,ControlGroup'])
        properties = dict(line.split('=', 1) for line in result['stdout'].splitlines() if '=' in line)
        pids = {int(properties[k]) for k in ['MainPID', 'ControlPID'] if properties.get(k, '').isdigit()}
        for k in ['ExecStartPre', 'ExecStartPost']:
            pids.update(int(v) for v in re.findall(r'(?:^|;)\s*pid=([0-9]+)', properties.get(k, '')))
        identities = []
        for pid in sorted(pids - {0}):
            try:
                start = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(') ', 1)[1].split()[19]
                identities.append({'pid': pid, 'start': start, 'present': True})
            except FileNotFoundError:
                identities.append({'pid': pid, 'present': False})
        units[unit] = {'command': result, 'properties': properties, 'identities': identities}
    write_private({'units': units, 'journal': command(['journalctl', '--namespace=terrace-vaultwarden', '--no-pager', '-n', '200', '-o', 'cat'])}, Path('/var/lib/vaultwarden-test-evidence'))
    print('Vaultwarden private evidence captured; contents suppressed.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Vaultwarden private evidence capture unavailable; contents suppressed.')
        raise SystemExit(1)
