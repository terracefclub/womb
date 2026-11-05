"""Stop one statically named migration unit; never signal a numeric PID."""
import json
import os
from pathlib import Path
import subprocess
import stat
import sys

UNIT = 'terrace-vaultwarden-migration-server.service'
PROPERTIES = 'ActiveState,SubState,Result,MainPID,ControlPID,ExecMainCode,ExecMainStatus'


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=25, close_fds=True)
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise ValueError()
    return result.stdout


def stop_owned(run=command, cgroup=Path('/sys/fs/cgroup/system.slice') / UNIT):
    def state():
        return dict(line.split('=', 1) for line in run(['systemctl', 'show', UNIT, '--property=' + PROPERTIES]).splitlines())
    original = state()
    run(['systemctl', 'stop', UNIT])
    stopped = state()
    if stopped.get('MainPID') != '0' or stopped.get('ControlPID') != '0' or stopped.get('ActiveState') not in ['inactive', 'failed']:
        raise ValueError()
    try:
        mode = cgroup.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if not stat.S_ISDIR(mode):
            raise ValueError()
        def inspection_error(error):
            raise error
        for directory, _children, files in os.walk(cgroup, onerror=inspection_error):
            if 'cgroup.procs' in files or 'cgroup.procs' in _children:
                contents = (Path(directory) / 'cgroup.procs').read_text()
                if contents.strip():
                    raise ValueError()
            else:
                raise ValueError()
    if stopped['ActiveState'] == 'failed':
        run(['systemctl', 'reset-failed', UNIT])
    final = state()
    if final.get('ActiveState') != 'inactive' or final.get('MainPID') != '0' or final.get('ControlPID') != '0':
        raise ValueError()
    return {'unit': UNIT, 'original': original, 'afterStop': stopped, 'final': final}


def require_completed_migration(result):
    original, stopped = result['original'], result['afterStop']
    if (original.get('ActiveState') != 'active' or original.get('SubState') != 'running'
            or not original.get('MainPID', '').isdigit() or int(original['MainPID']) == 0
            or original.get('ControlPID') != '0' or stopped.get('ActiveState') != 'inactive'
            or stopped.get('Result') != 'success'
            or (stopped.get('ExecMainCode'), stopped.get('ExecMainStatus')) not in [('1', '0'), ('2', '15')]):
        raise ValueError()


if __name__ == '__main__':
    try:
        os.umask(0o077)
        if sys.argv[1:] not in [[], ['complete']]:
            raise ValueError()
        result = stop_owned()
        path = Path('/run/terrace-vaultwarden-inputs/migration-stop.json')
        # Server pre-start clears only its prior receipt. Preserve the first
        # stopped result when the parent's ExecStopPost repeats exact cleanup.
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(result, stream)
        if sys.argv[1:] == ['complete']:
            require_completed_migration(result)
        print('Vaultwarden owned migration unit stopped; private result retained.')
    except Exception:
        print('Vaultwarden migration result rejected; release remains unmarked.')
        raise SystemExit(1)
