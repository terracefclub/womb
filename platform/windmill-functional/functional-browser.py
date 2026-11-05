"""Synthetic native sign-in/job acceptance; private receipts, fixed public errors.

create/verify retain only stable data identities, never a login cookie, so they
can also bracket an originals-off restore. disabled separately tests old cookies.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.parse import parse_qs, urlsplit
import uuid

ORIGIN = 'https://windmill.terrace.invalid'
ROOT = Path('/root/windmill-functional')
stage = 'private fixture validation'

FRONTEND_PATHS = {'/', '/user/login', '/env.js', '/api/version', '/api/oauth/list_logins',
                  '/api/users/email', '/api/users/whoami', '/api/users/is_first_time_setup',
                  '/api/users/is_password_login_disabled', '/api/workspaces/users'}


def frontend_route(url):
    try:
        value = urlsplit(url)
    except ValueError:
        return 'other-origin'
    if value.scheme != 'https' or value.netloc != 'windmill.terrace.invalid':
        return 'other-origin'
    if value.path in FRONTEND_PATHS:
        return value.path
    if re.fullmatch(r'/_app/immutable/(?:entry|chunks|nodes)/[a-zA-Z0-9_.-]{1,128}\.js', value.path):
        return value.path
    if value.path.startswith('/_app/'):
        return 'other-static-asset'
    return 'other-api' if value.path.startswith('/api/') else 'other-app-path'


def network_error_kind(value):
    return value if value in ['net::ERR_ABORTED', 'net::ERR_FAILED', 'net::ERR_NAME_NOT_RESOLVED',
                              'net::ERR_CERT_AUTHORITY_INVALID', 'net::ERR_CONNECTION_REFUSED',
                              'net::ERR_CONNECTION_RESET', 'net::ERR_TIMED_OUT', 'net::ERR_INSUFFICIENT_RESOURCES'] else 'other-network-error'


def page_error_kind(value):
    for text, label in [('Failed to fetch dynamically imported module', 'dynamic-import'),
                        ('does not provide an export named', 'missing-export'),
                        ('Cannot read properties of undefined', 'undefined-property'),
                        ("Unexpected token '<'", 'html-in-script'),
                        ('Failed to resolve module specifier', 'module-resolution')]:
        if text in value:
            return label
    return 'other-page-error'


def page_error_class(value):
    return value if value in ['Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError', 'EvalError', 'URIError'] else 'other-error-class'


def frontend_facts():
    return {'responses': {}, 'requestFailures': [], 'pageErrors': [], 'consoleErrorCount': 0,
            'responsesTruncated': False, 'requestFailuresTruncated': False, 'pageErrorsTruncated': False,
            'pendingTruncated': False}


def auth_facts():
    return {'providerRequestObserved': False, 'callbackStatus': None, 'pkceS256': None, 'challengePresent': None}


def completion_facts(value, owner):
    return {'success': value.get('success') if type(value.get('success')) is bool else None,
            'resultMatches': value.get('result') == {'status': 'synthetic-per-user-job-ok'},
            'ownerMatches': value.get('email') == owner['email'] and value.get('created_by') == owner['username']}


def wait_for_session_denial(status, clock=time.monotonic, pause=time.sleep):
    # Native session AUTH_CACHE entries live for 120s. Keep the observation
    # bounded without pretending that deleting the DB row flushes that cache.
    start = clock()
    deadline = start + 130
    while True:
        code = status()
        now = clock()
        assert now <= deadline
        if code == 401:
            return round((now - start) * 1000)
        assert code == 200 and now < deadline
        pause(min(0.5, deadline - now))


def frontend_response(facts, url, status, kind, mime):
    route = frontend_route(url)
    assert isinstance(status, int) and 100 <= status <= 599
    truncated = route not in facts and len(facts) >= 64
    if truncated:
        facts.pop(next(iter(facts)))
    facts.pop(route, None)
    facts[route] = {'status': status, 'kind': kind if kind in ['document', 'script', 'stylesheet', 'xhr', 'fetch', 'image', 'font'] else 'other',
                    'mime': mime if mime in ['application/javascript', 'text/javascript', 'text/html', 'application/json', 'text/css', 'image/svg+xml'] else 'other'}
    return not truncated


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0
        assert 0 < info.st_size <= 131072
        value = os.read(fd, 131073)
        assert len(value) == info.st_size
        return json.loads(value)
    finally:
        os.close(fd)


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validated_receipt(value):
    assert set(value) == {'workspace', 'script', 'privateVariable', 'privateDigest', 'job', 'resultDigest', 'operator', 'administrator'}
    assert value['workspace'] == 'terrace' and value['script'] == 'f/sso-rehearsal/proof'
    assert re.fullmatch(r'u/[a-zA-Z0-9_-]{1,100}/sso-private', value['privateVariable'])
    assert all(re.fullmatch('[a-f0-9]{64}', value[key]) for key in ['privateDigest', 'resultDigest'])
    uuid.UUID(value['job'])
    for key in ['operator', 'administrator']:
        assert set(value[key]) == {'username', 'email'}
        assert re.fullmatch('[a-zA-Z0-9_-]{1,100}', value[key]['username'])
        assert value[key]['email'].endswith('@terrace.invalid')
    assert value['operator'] != value['administrator']
    return value


def main(mode):
    global stage
    assert mode in ['create', 'verify', 'disabled']
    info = ROOT.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0
    users = {user['username']: user for user in private_read(ROOT / 'credentials.json')['users']}
    from playwright.sync_api import sync_playwright
    active_page = None
    facts = auth_facts()
    job_facts = {'submissionStatus': None, 'uuidValid': False, 'pollStatus': None, 'pollCount': 0, 'completed': None}
    frontend = frontend_facts()
    pending = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ['TERRACE_BROWSER_BINARY'], headless=True, args=['--no-sandbox'])
        contexts = []
        try:
            def context(state=None):
                ctx = browser.new_context(storage_state=state, locale='en-US')
                ctx.set_default_timeout(30000)
                contexts.append(ctx)
                return ctx

            def call(ctx, method, route, data=None):
                assert route.startswith('/') and not route.startswith('//')
                response = ctx.request.fetch(ORIGIN + '/api' + route, method=method, data=data, max_redirects=0, timeout=10000)
                assert len(response.body()) <= 1048576
                return response

            def whoami(ctx, expected):
                response = call(ctx, 'GET', '/users/whoami')
                assert response.status == 200
                user = response.json()
                assert user['email'] == users[expected]['email'] and user['login_type'] == 'keycloak' and user['disabled'] is False
                response = call(ctx, 'GET', '/w/terrace/users/whoami')
                assert response.status == 200
                member = response.json()
                admin = expected == 'local-editorial-admin'
                assert member['email'] == user['email'] and member['is_admin'] is admin and member['is_super_admin'] is admin
                assert member['operator'] is (not admin) and member['disabled'] is False
                assert re.fullmatch('[a-zA-Z0-9_-]{1,100}', member['username'])
                return {'username': member['username'], 'email': member['email']}

            def login(username, denied=False):
                nonlocal active_page, frontend, pending, facts
                active_page = None
                facts = auth_facts()
                current, current_pending = frontend_facts(), {}
                frontend, pending = current, current_pending
                ctx = context()
                page = ctx.new_page()
                active_page = page
                def request(value):
                    if len(current_pending) < 128:
                        current_pending[id(value)] = frontend_route(value.url)
                    else:
                        current['pendingTruncated'] = True
                def response(value):
                    current_pending.pop(id(value.request), None)
                    if not frontend_response(current['responses'], value.url, value.status, value.request.resource_type,
                                             value.headers.get('content-type', '').split(';', 1)[0].strip()):
                        current['responsesTruncated'] = True
                def request_failed(value):
                    current_pending.pop(id(value), None)
                    if len(current['requestFailures']) < 32:
                        current['requestFailures'].append({'route': frontend_route(value.url), 'error': network_error_kind(value.failure)})
                    else:
                        current['requestFailuresTruncated'] = True
                def page_error(value):
                    if len(current['pageErrors']) < 16:
                        current['pageErrors'].append({'class': page_error_class(getattr(value, 'name', None)), 'kind': page_error_kind(str(value))})
                    else:
                        current['pageErrorsTruncated'] = True
                def console(value):
                    if value.type == 'error':
                        current['consoleErrorCount'] = min(1000, current['consoleErrorCount'] + 1)
                page.on('request', request)
                page.on('response', response)
                page.on('requestfailed', request_failed)
                page.on('pageerror', page_error)
                page.on('console', console)
                page.goto(ORIGIN + '/user/login?no_sso=1')
                with page.expect_request(lambda request: urlsplit(request.url).hostname == 'identity.terrace.invalid' and urlsplit(request.url).path.endswith('/protocol/openid-connect/auth')) as auth:
                    page.get_by_role('button', name='Continue with Keycloak', exact=True).click()
                facts['providerRequestObserved'] = True
                params = parse_qs(urlsplit(auth.value.url).query)
                facts['pkceS256'] = params.get('code_challenge_method') == ['S256']
                facts['challengePresent'] = bool(re.fullmatch('[a-zA-Z0-9_-]{43,128}', params.get('code_challenge', [''])[0]))
                assert facts['pkceS256'] and facts['challengePresent']
                assert params.get('client_id') == ['terrace-windmill'] and params.get('response_type') == ['code']
                assert params.get('redirect_uri') == [ORIGIN + '/user/login_callback/keycloak']
                page.locator('#username').fill(username)
                page.locator('#password').fill(users[username]['password'])
                with page.expect_response(lambda response: urlsplit(response.url).hostname == 'windmill.terrace.invalid' and urlsplit(response.url).path == '/api/oauth/login_callback/keycloak') as callback:
                    page.locator('#kc-login').click()
                facts['callbackStatus'] = callback.value.status
                if denied:
                    # Authentication/approval refusal, never a server error or a
                    # successful callback followed by a misleading empty screen.
                    assert callback.value.status in [400, 401, 403]
                    assert call(ctx, 'GET', '/users/whoami').status == 401
                else:
                    assert callback.value.status == 200
                    whoami(ctx, username)
                return ctx

            stage = 'public password and anonymous denial'
            anonymous = context()
            for route in ['/auth/login']:
                assert call(anonymous, 'POST', route, {'email': 'admin@windmill.dev', 'password': 'changeme'}).status == 403
            assert call(anonymous, 'GET', '/users/whoami').status == 401
            if mode == 'disabled':
                stage = 'existing revoked session denial'
                old = context(private_read(ROOT / 'alumni-session.json'))
                delay = wait_for_session_denial(lambda: call(old, 'GET', '/w/terrace/users/whoami').status)
                private_write(ROOT / 'revocation.json', {'deniedAfterMs': delay})
                assert call(old, 'GET', '/users/whoami').status == 401
                stage = 'fresh revoked OIDC denial'
                login('local-alumni', denied=True)
                stage = 'fresh remaining approved identities after revocation'
                admin, operator = login('local-editorial-admin'), login('local-member')
                assert whoami(admin, 'local-editorial-admin') != whoami(operator, 'local-member')
            else:
                stage = 'unknown per-user OIDC denial'
                login('local-unapproved', denied=True)
                stage = 'distinct approved administrator and operator OIDC'
                admin, operator = login('local-editorial-admin'), login('local-member')
                admin_id, operator_id = whoami(admin, 'local-editorial-admin'), whoami(operator, 'local-member')
                assert admin_id != operator_id
                stage = 'operator administration denial'
                # Native 1.861 keeps superadmin denial at 401; its workspace
                # author-capability gate uses PermissionDenied (403).
                assert call(operator, 'GET', '/users/list_as_super_admin').status == 401
                assert call(operator, 'POST', '/w/terrace/scripts/create', {'path': 'u/' + operator_id['username'] + '/forbidden', 'summary': 'Synthetic forbidden', 'content': 'true', 'language': 'bash'}).status == 403
                if mode == 'create':
                    stage = 'native private variable and executable script creation'
                    private_path = 'u/' + operator_id['username'] + '/sso-private'
                    private_value = os.urandom(32).hex()
                    assert call(admin, 'POST', '/w/terrace/variables/create', {'path': private_path, 'value': private_value, 'is_secret': True, 'description': 'Synthetic isolation proof'}).status == 201
                    assert call(admin, 'POST', '/w/terrace/folders/create', {'name': 'sso-rehearsal', 'summary': 'Synthetic SSO worker proof', 'owners': ['u/' + admin_id['username']], 'extra_perms': {'g/all': False}}).status == 200
                    result = {'status': 'synthetic-per-user-job-ok'}
                    content = "printf '%s\\n' '" + json.dumps(result, separators=(',', ':')) + "' > result.json\n"
                    assert call(admin, 'POST', '/w/terrace/scripts/create', {'path': 'f/sso-rehearsal/proof', 'summary': 'Synthetic per-user worker proof', 'content': content, 'language': 'bash', 'timeout': 20}).status == 201
                    receipt = {'workspace': 'terrace', 'script': 'f/sso-rehearsal/proof', 'privateVariable': private_path, 'privateDigest': digest(private_value), 'operator': operator_id, 'administrator': admin_id}
                else:
                    receipt = validated_receipt(private_read(ROOT / 'receipt.json'))
                    assert receipt['operator'] == operator_id and receipt['administrator'] == admin_id
                    previous = call(operator, 'GET', '/w/terrace/jobs_u/completed/get/' + receipt['job'])
                    assert previous.status == 200 and previous.json()['success'] is True
                    assert digest(previous.json()['result']) == receipt['resultDigest']
                stage = 'per-user private value isolation'
                own = call(operator, 'GET', '/w/terrace/variables/get_value/' + receipt['privateVariable'])
                assert own.status == 200 and digest(own.json()) == receipt['privateDigest']
                alumni = login('local-alumni')
                assert call(alumni, 'GET', '/w/terrace/variables/get_value/' + receipt['privateVariable']).status == 401
                if mode == 'create':
                    private_write(ROOT / 'alumni-session.json', alumni.storage_state())
                    # Folder ACLs are cached per session for 120 seconds. The
                    # administrator created this folder after the first login;
                    # a fresh native session observes its actual read/run grant.
                    stage = 'fresh operator shared-folder session'
                    operator = login('local-member')
                    assert whoami(operator, 'local-member') == operator_id
                stage = 'approved operator real worker execution'
                started = call(operator, 'POST', '/w/terrace/jobs/run/p/' + receipt['script'], {})
                job_facts['submissionStatus'] = started.status
                assert started.status in [200, 201]
                job = started.text().strip().strip('"')
                uuid.UUID(job)
                job_facts['uuidValid'] = True
                deadline = time.monotonic() + 60
                while True:
                    finished = call(operator, 'GET', '/w/terrace/jobs_u/completed/get/' + job)
                    job_facts['pollStatus'] = finished.status
                    job_facts['pollCount'] = min(121, job_facts['pollCount'] + 1)
                    if finished.status == 200:
                        completed = finished.json()
                        job_facts['completed'] = completion_facts(completed, operator_id)
                        assert completed['success'] is True and completed['result'] == {'status': 'synthetic-per-user-job-ok'}
                        assert completed['email'] == operator_id['email'] and completed['created_by'] == operator_id['username']
                        break
                    assert finished.status == 404 and time.monotonic() < deadline
                    time.sleep(0.5)
                if mode == 'create':
                    receipt.update(job=job, resultDigest=digest(completed['result']))
                    private_write(ROOT / 'receipt.json', validated_receipt(receipt))
                else:
                    # Prove this exact retained cookie is still usable after
                    # every other login/restart, immediately before removal.
                    stage = 'retained alumni session positive before revocation'
                    whoami(context(private_read(ROOT / 'alumni-session.json')), 'local-alumni')
            print('Windmill functional browser phase ' + mode + ' passed.')
        except Exception as error:
            # Private actual-page evidence corrects selectors without guessing.
            try:
                value = {'stage': stage, 'facts': facts, 'frontend': frontend,
                         'job': job_facts,
                         'exceptionClass': type(error).__name__ if type(error).__name__ in ['AssertionError', 'TimeoutError', 'Error', 'ValueError'] else 'other-exception'}
                frontend['pendingRoutes'] = {}
                for route in list(pending.values())[-64:]:
                    frontend['pendingRoutes'][route] = frontend['pendingRoutes'].get(route, 0) + 1
                frontend['pendingTruncated'] |= len(pending) > 64
                # Preserve bounded facts before optional renderer calls, which
                # may fail or stall when the frontend process itself is broken.
                private_write(ROOT / ('failure-' + mode + '.json'), value)
                if active_page is not None:
                    try:
                        state = active_page.evaluate("({ready:document.readyState,globalLoader:!!document.querySelector('#svelte-global-loader')})")
                        document = {'ready': state['ready'] if state['ready'] in ['loading', 'interactive', 'complete'] else 'other',
                                    'globalLoader': bool(state['globalLoader'])}
                    except Exception:
                        document = {'ready': 'unavailable', 'globalLoader': None}
                    private_write(ROOT / ('failure-' + mode + '-dom.json'), document)
                if active_page is not None:
                    active_page.screenshot(path=str(ROOT / ('failure-' + mode + '.png')), mask=[active_page.locator('input'), active_page.locator('pre')])
                    (ROOT / ('failure-' + mode + '.png')).chmod(0o600)
            except Exception:
                pass
            raise
        finally:
            for ctx in contexts:
                ctx.close()
            browser.close()


def entrypoint():
    try:
        assert len(sys.argv) == 2
        main(sys.argv[1])
    except Exception:
        print('Windmill functional check failed at ' + stage + '; private diagnostics suppressed.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    entrypoint()
