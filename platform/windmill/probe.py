"""Guest-only actual API/job checks; never print inputs, tokens or responses."""
import json
import os
from pathlib import Path
import stat
import sys
import time
import urllib.error
import urllib.request

stage = "startup"
try:
    values = json.loads(Path('/var/lib/terrace-windmill-inputs/bootstrap.json').read_text())
    base = 'http://127.0.0.1:18001/api'
    token = None
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(method, path, data=None):
        headers = {}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(base + path, headers=headers, method=method,
                                         data=None if data is None else json.dumps(data).encode())
        try:
            response = opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(1048577)
            assert len(body) <= 1048576
            return response.status, body

    stage = "factory credential rejection"
    assert call('POST', '/auth/login', {'email':'admin@windmill.dev','password':'changeme'})[0] != 200
    status, body = call('POST', '/auth/login', {'email':'admin@windmill.dev','password':values['adminPassword']})
    assert status == 200
    token = body.decode()
    stage = "approved per-user accounts"
    status, body = call('GET', '/users/list_as_super_admin')
    assert status == 200
    users = json.loads(body)
    for approved in values['approvedUsers']:
        matching = [user for user in users if user['email'] == approved['email']]
        assert len(matching) == 1
        assert matching[0]['login_type'] == 'keycloak'
        assert matching[0]['super_admin'] == (approved['role'] == 'admin')
    stage = "real worker job"
    # This is runtime evidence; it does not invoke preserved membership providers.
    status, body = call('GET', '/workspaces/list_as_superadmin')
    assert status == 200 and any(workspace['id'] == 'terrace' for workspace in json.loads(body))
    import uuid
    jobs = [
        ({'content': "printf '%s\\n' '{\"status\":\"synthetic-windmill-ok\"}' > result.json\n",
          'language':'bash','args':{},'path':'f/synthetic/runtime-proof'},
         {'status':'synthetic-windmill-ok'}),
        ({'content': '''import os
import sys

def main():
    assert sys.version_info[:2] == (3, 12)
    assert sys.executable.startswith('/tmp/windmill/cache/py_runtime/')
    assert os.statvfs(sys.executable).f_flag & os.ST_RDONLY
    return {'status':'synthetic-windmill-python-ok','managedReadOnly':True}
''', 'language':'python3','lock':'# py: 3.12\n','args':{},'path':'f/synthetic/python-runtime-proof'},
         {'status':'synthetic-windmill-python-ok','managedReadOnly':True}),
    ]
    for request, expected in jobs:
        stage = 'real ' + request['language'] + ' worker job'
        status, body = call('POST', '/w/terrace/jobs/run/preview', request)
        if status not in [200, 201]:
            print('Windmill worker submission status:', status)
        assert status in [200, 201]
        job = body.decode().strip().strip('"')
        uuid.UUID(job)
        deadline = time.monotonic() + 60
        while True:
            status, body = call('GET', '/w/terrace/jobs_u/completed/get/' + job)
            if status == 200:
                result = json.loads(body)
                # A failed synthetic job is kept only in the already-private,
                # bounded test launcher capture, never in stdout/Nix results.
                if not isinstance(result, dict) or result.get('success') is not True or result.get('result') != expected:
                    print('Windmill completed worker result rejected.')
                    fd = os.open('/var/lib/windmill-test-launcher.log', os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'wb') as output:
                        info = os.fstat(output.fileno())
                        assert stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o077 == 0
                        output.write(b'\nPrivate synthetic worker result: ' + body + b'\n')
                assert isinstance(result, dict) and result['success'] is True and result['result'] == expected
                break
            assert status == 404 and time.monotonic() < deadline
            time.sleep(0.5)
    print('Windmill actual Bash/Python workers, factory rejection and distinct approved-account checks passed.')
except BaseException:
    print('Windmill synthetic protocol check failed at ' + stage + '; raw diagnostics suppressed.', file=sys.stderr)
    sys.exit(1)
