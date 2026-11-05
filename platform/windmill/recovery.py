"""Root-only native readback while the caller has stopped the identity service."""
import http.client
import json
import os
from pathlib import Path
import stat
import sys


def protected_input(directory, owner=0):
    try:
        parent = Path(directory).lstat()
        assert stat.S_ISDIR(parent.st_mode) and parent.st_uid == owner and parent.st_mode & 0o777 == 0o700
        fd = os.open(Path(directory) / 'bootstrap.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            assert stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_mode & 0o777 == 0o400 and 0 < info.st_size <= 65536
            body = os.read(fd, 65537)
            assert len(body) == info.st_size
        finally:
            os.close(fd)
        value = json.loads(body)
        password = value['adminPassword']
        assert isinstance(password, str) and 32 <= len(password.encode()) <= 4096 and password == password.strip() and password.isprintable()
        approved = value['approvedUsers']
        assert isinstance(approved, list) and 1 <= len(approved) <= 10
        assert all(isinstance(user, dict) and isinstance(user.get('email'), str) and 0 < len(user['email']) <= 254 and user.get('role') in ['admin', 'operator'] for user in approved)
        assert len({user['email'] for user in approved}) == len(approved)
        return password, approved
    except Exception:
        raise ValueError('Windmill recovery inputs rejected.') from None


def inspect(directory, port):
    password, approved = protected_input(directory)
    token = None

    def call(method, path, data=None):
        # No proxy, discovery, redirect or non-loopback destination is involved.
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            headers = {} if token is None else {'Authorization': 'Bearer ' + token}
            if data is not None:
                headers['Content-Type'] = 'application/json'
            connection.request(method, '/api' + path, body=None if data is None else json.dumps(data), headers=headers)
            response = connection.getresponse()
            body = response.read(1048577)
            assert len(body) <= 1048576
            return response.status, body
        finally:
            connection.close()

    status, body = call('POST', '/auth/login', {'email': 'admin@windmill.dev', 'password': password})
    assert status == 200
    token = body.decode()
    assert 0 < len(token) <= 4096 and token.isprintable() and '\r' not in token and '\n' not in token
    try:
        status, body = call('GET', '/users/whoami')
        account = json.loads(body)
        assert status == 200 and account['email'] == 'admin@windmill.dev' and account['super_admin'] is True and account['disabled'] is False
        status, body = call('GET', '/workspaces/list_as_superadmin')
        workspaces = json.loads(body)
        assert status == 200 and isinstance(workspaces, list) and len(workspaces) < 100
        assert sum(workspace.get('id') == 'terrace' for workspace in workspaces) == 1
        status, body = call('GET', '/users/list_as_super_admin?per_page=100&page=1')
        users = json.loads(body)
        assert status == 200 and isinstance(users, list) and len(users) < 100
        for wanted in approved:
            matching = [user for user in users if user.get('email') == wanted['email']]
            assert len(matching) == 1
            user = matching[0]
            assert user['disabled'] is False and user['super_admin'] is (wanted['role'] == 'admin') and user['login_type'] == 'keycloak'
    finally:
        # Discard only this helper's native session, without changing app data,
        # approval, passwords or any person's identity-provider session.
        status, body = call('POST', '/auth/logout')
        assert status == 200 and body == b'logged out successfully'


def main():
    assert len(sys.argv) == 3 and os.geteuid() == 0
    assert sys.argv[2].isascii() and sys.argv[2].isdigit() and 1024 <= int(sys.argv[2]) <= 65533
    inspect(sys.argv[1], int(sys.argv[2]))
    print('Native Windmill private admin readback passed.')


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        print('Windmill private recovery readback rejected; diagnostics suppressed.', file=sys.stderr)
        raise SystemExit(1)
