"""Disposable real web-vault checks. Never print tokens, URLs or passwords."""
import base64
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import time
import tempfile
import urllib.parse

ROOT = Path('/root/vaultwarden-functional')
VAULT = 'https://vault.terrace.invalid'
IDENTITY = 'identity.terrace.invalid'
ITEM = 'Synthetic retained Vaultwarden login'
ITEM_USER = 'synthetic-vault-user'
ITEM_PASSWORD = 'Synthetic vault item payload - not an account credential'
stage = 'private fixture validation'
active_page = None
active_network = {}


def network_summary(values):
    result = {}
    for name in ['validatedOidcRequests','blockedRequests','pageErrors']:
        value = values.get(name)
        result[name] = value if type(value) is int and 0 <= value <= 10000 else 0
    for name in ['tokenStatus','accountStatus','profileStatus']:
        value = values.get(name)
        result[name] = value if type(value) is int and 100 <= value <= 599 else None
    remaining = values.get('tokenSecondsRemaining')
    result['tokenSecondsRemaining'] = remaining if type(remaining) is int and -86400 <= remaining <= 86400 else None
    for name in ['refreshTokenPresent','profileEmailMatches','profileExpectedIdMatches','tokenSubjectMatchesProfile']:
        value = values.get(name)
        result[name] = value if type(value) is bool else None
    return result


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0 and 0 < info.st_size <= 131072
        data = os.read(fd, 131073)
        assert len(data) == info.st_size and len(data) <= 131072
        return json.loads(data)
    finally:
        os.close(fd)


def private_write(name, value):
    fd = os.open(ROOT / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output)


def private_update(name, value):
    fd, temporary = tempfile.mkstemp(prefix='vault-session-',dir=ROOT)
    try:
        with os.fdopen(fd,'w') as output:
            json.dump(value,output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary,ROOT/name)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def native(path, token):
    assert path.startswith('/api/') and not re.search(r'[\r\n]', path)
    connection = http.client.HTTPSConnection('vault.terrace.invalid', timeout=8, context=ssl.create_default_context())
    try:
        connection.request('GET', path, headers={'Authorization':'Bearer ' + token, 'Accept':'application/json'})
        response = connection.getresponse()
        body = response.read(131073)
        assert len(body) <= 131072
        return response.status, json.loads(body) if body else None
    finally:
        connection.close()


def require_cipher_denial(value):
    status, body = value
    message = 'Cipher is not owned by user'
    assert status == 400 and body == {
        'message':message, 'validationErrors':{'':[message]},
        'errorModel':{'message':message,'object':'error'}, 'error':'', 'error_description':'',
        'exceptionMessage':None, 'exceptionStackTrace':None, 'innerExceptionMessage':None, 'object':'error',
    }


def jwt_claims(token):
    assert isinstance(token, str) and len(token) <= 32768
    payload = token.split('.')[1]
    value = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    assert isinstance(value, dict) and type(value['exp']) is int
    return value


def jwt_exp(token):
    return jwt_claims(token)['exp']


def profile_facts(token, profile, expected_email, expected_id):
    # Diagnostic equality only. The real native HTTP200/signature check below
    # remains mandatory; decoding JWT claims is not authentication.
    identifier = profile.get('id') if isinstance(profile, dict) else None
    return {
        'profileEmailMatches': isinstance(profile, dict) and profile.get('email') == expected_email,
        'profileExpectedIdMatches': identifier == expected_id if expected_id else None,
        'tokenSubjectMatchesProfile': isinstance(identifier, str) and bool(identifier) and jwt_claims(token).get('sub') == identifier,
    }


def dismiss_toasts(page):
    for _ in range(10):
        closes = page.locator('bit-toast').get_by_role('button',name='Close',exact=True)
        if closes.count() == 0:
            return
        button = closes.first.element_handle()
        assert button is not None
        button.click(timeout=2000)
        button.wait_for_element_state('hidden',timeout=2000)
    assert page.locator('bit-toast').count() == 0


def main():
    global stage, active_page
    assert len(sys.argv) == 2 and sys.argv[1] in ['create','verify','disabled','reenabled']
    mode = sys.argv[1]
    info = ROOT.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o700
    users = {u['username']:u for u in private_read(ROOT / 'credentials.json')['users']}
    connection=http.client.HTTPSConnection('vault.terrace.invalid',timeout=8,context=ssl.create_default_context())
    try:
        connection.request('POST','/identity/connect/token',body=urllib.parse.urlencode({'grant_type':'password','client_id':'web','username':users['local-member']['email'],'password':'synthetic-not-a-master-hash','scope':'api offline_access'}),headers={'Content-Type':'application/x-www-form-urlencoded'})
        response=connection.getresponse();body=response.read(131073)
        assert len(body)<=131072 and response.status==400 and 'SSO sign-in is required' in body.decode('utf8')
    finally:
        connection.close()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ['TERRACE_BROWSER_BINARY'], headless=True, args=['--no-sandbox'])
        try:
            def session(user, enroll=False, deny=None):
                global stage, active_page, active_network
                network = {'validatedOidcRequests':0,'blockedRequests':0,'pageErrors':0,'tokenStatus':None,'accountStatus':None}
                active_network = network
                context = browser.new_context(ignore_https_errors=False, service_workers='block')
                context.grant_permissions(['clipboard-read','clipboard-write'],origin=VAULT)
                context.set_default_timeout(30000)
                forbidden = []
                def route(request_route):
                    url = urllib.parse.urlsplit(request_route.request.url)
                    if url.scheme not in ['https','data','blob'] or (url.scheme == 'https' and (url.hostname not in ['vault.terrace.invalid',IDENTITY] or url.username or url.password)):
                        forbidden.append(True)
                        network['blockedRequests'] = min(10000,network['blockedRequests']+1)
                        request_route.abort()
                    else:
                        request_route.continue_()
                context.route('**/*',route)
                page = context.new_page(); active_page = page
                evidence = {'oidc':0,'token':None,'refresh':False,'error':None,'cipher':None}
                def request(value):
                    url = urllib.parse.urlsplit(value.url)
                    if url.hostname == IDENTITY and url.path.endswith('/protocol/openid-connect/auth'):
                        query = urllib.parse.parse_qs(url.query)
                        assert query['client_id'] == ['terrace-vaultwarden'] and query['redirect_uri'] == [VAULT + '/identity/connect/oidc-signin']
                        assert query['code_challenge_method'] == ['S256'] and query.get('state') and query.get('nonce')
                        evidence['oidc'] += 1
                        network['validatedOidcRequests'] = min(10000,evidence['oidc'])
                def response(value):
                    url = urllib.parse.urlsplit(value.url)
                    if url.hostname == 'vault.terrace.invalid' and url.path == '/identity/connect/token':
                        network['tokenStatus'] = value.status
                        body = value.body(); assert len(body) <= 131072
                        data = json.loads(body)
                        if value.status == 200:
                            token = data['access_token']; assert isinstance(token,str) and len(token)<=32768
                            evidence['token'] = token
                            evidence['refresh'] = isinstance(data.get('refresh_token'),str) and 0 < len(data['refresh_token']) <= 32768
                            network['refreshTokenPresent'] = evidence['refresh']
                            network['tokenSecondsRemaining'] = int(jwt_exp(token)-time.time())
                        elif value.status in [400,401,403]:
                            evidence['error'] = json.dumps(data)
                    if url.hostname == 'vault.terrace.invalid' and url.path in ['/api/accounts/register','/api/accounts/register/finish']:
                        network['accountStatus'] = value.status
                    if url.hostname == 'vault.terrace.invalid' and url.path == '/api/ciphers' and value.request.method == 'POST' and value.status in [200,201]:
                        data = value.json(); cipher = data['id']
                        assert re.fullmatch(r'[a-f0-9-]{36}',cipher)
                        evidence['cipher'] = cipher
                def page_error(_error):
                    network['pageErrors'] = min(10000,network['pageErrors']+1)
                page.on('request',request);page.on('response',response);page.on('pageerror',page_error)
                stage = 'native SSO entry'
                page.goto(VAULT + '/',wait_until='domcontentloaded')
                page.locator('input[type=email].vw-email-sso').fill(user['email'])
                assert page.get_by_role('button',name='Other',exact=True).count() == 0
                page.get_by_role('button',name=re.compile('Use single sign-on')).click()
                stage = 'trusted Keycloak sign in'
                page.locator('#username').wait_for()
                assert urllib.parse.urlsplit(page.url).hostname == IDENTITY and page.url.startswith('https://')
                binding = [cookie for cookie in context.cookies() if cookie['name']=='VW_SSO_BINDING']
                assert len(binding)==1 and binding[0]['secure'] and binding[0]['httpOnly'] and binding[0]['sameSite']=='Lax'
                page.locator('#username').fill(user['username']);page.locator('#password').fill(user['password']);page.locator('#kc-login').click()
                if deny:
                    stage = 'native denied account response'
                    deadline = time.monotonic()+45
                    while not evidence['error'] and time.monotonic()<deadline:
                        page.wait_for_timeout(100)
                    assert evidence['oidc'] and evidence['token'] is None and evidence['error'] and deny in evidence['error'] and not forbidden
                    context.close();active_page=None
                    return None
                stage = 'separate master password enrollment' if enroll else 'separate vault unlock'
                if enroll:
                    page.get_by_role('textbox',name='Master password * (required)',exact=True).fill(user['masterPassword'])
                    page.get_by_role('textbox',name=re.compile('Confirm master password')).fill(user['masterPassword'])
                    breach_check=page.get_by_role('checkbox',name='Check known data breaches for this password',exact=True)
                    breach_check.uncheck()
                    assert not breach_check.is_checked()
                    page.get_by_role('button',name='Create account',exact=True).click()
                else:
                    page.get_by_role('heading',name='Your vault is locked',exact=True).wait_for()
                    page.locator('bit-lock').get_by_label(re.compile(r'^Master password(?:\s*\*)?(?:\s*\(required\))?$')).fill(user['masterPassword'])
                    page.get_by_role('button',name='Unlock',exact=True).click()
                stage = 'native optional extension onboarding'
                vault = page.locator('app-header').get_by_role('heading',name='All vaults',exact=True)
                extension_intro = page.get_by_role('heading',name='Autofill your passwords securely with one click',exact=True)
                welcome = page.locator('app-vault-welcome-dialog')
                deadline = time.monotonic()+30
                while not vault.is_visible() and not extension_intro.is_visible() and not welcome.is_visible() and time.monotonic()<deadline:
                    page.wait_for_timeout(100)
                if extension_intro.is_visible():
                    page.get_by_text('Add it later',exact=True).click()
                    confirmation = page.get_by_role('dialog')
                    confirmation.get_by_text("You can't autofill passwords without the browser extension",exact=True).wait_for()
                    confirmation_handle = confirmation.element_handle()
                    assert confirmation_handle is not None
                    confirmation.get_by_text('Skip to web app',exact=True).click()
                    confirmation_handle.wait_for_element_state('hidden')
                # Synthetic accounts are new and each context has fresh local state.
                # The pinned client therefore shows this exact first-use welcome.
                stage = 'native first-use welcome dismissal'
                welcome.get_by_role('heading',name="You're in! Welcome to Bitwarden",exact=True).wait_for()
                welcome_handle = welcome.element_handle()
                assert welcome_handle is not None
                welcome.get_by_role('button',name='Skip',exact=True).click()
                welcome_handle.wait_for_element_state('hidden')
                stage = 'native authenticated vault profile'
                vault.wait_for()
                assert evidence['oidc'] and evidence['token'] and not forbidden
                status, profile = native('/api/accounts/profile',evidence['token'])
                network['profileStatus'] = status
                expected_id = None if enroll else private_read(ROOT/'receipt.json')[{'local-member':'member','local-alumni':'alumni'}[user['username']]]
                network.update(profile_facts(evidence['token'],profile,user['email'],expected_id))
                network['tokenSecondsRemaining'] = int(jwt_exp(evidence['token'])-time.time())
                assert status==200 and profile['email']==user['email'] and profile['id']
                assert network['tokenSubjectMatchesProfile'] and (enroll or network['profileExpectedIdMatches'])
                assert evidence['refresh'] and jwt_exp(evidence['token']) > time.time()+300
                return context,page,evidence,profile

            def read_item(page, suffix=''):
                global stage
                stage='web vault decrypts retained item'
                page.get_by_role('row').filter(has_text=ITEM+suffix).get_by_role('button',name=ITEM+suffix,exact=True).click()
                page.get_by_test_id('copy-username').click()
                assert page.evaluate('navigator.clipboard.readText()') == ITEM_USER+suffix
                page.get_by_test_id('copy-password').click()
                assert page.evaluate('navigator.clipboard.readText()') == ITEM_PASSWORD+suffix

            def create_item(page,evidence,suffix=''):
                global stage
                stage='native web vault encrypts item'
                creator=page.locator('#newItemButton:visible').first
                if creator.is_visible():
                    creator.click()
                    page.locator('vault-add-item-dialog vault-add-item-grid').get_by_role('button',name=re.compile(r'^Login(?:\s|$)')).click()
                else:
                    page.locator('#newItemDropdown:visible').first.click()
                    page.get_by_role('menuitem',name='Login',exact=True).click()
                page.locator('vault-item-details-section').get_by_role('textbox',name=re.compile(r'^Item name(?:\s|$)')).fill(ITEM+suffix)
                login_fields=page.locator('vault-login-details-section')
                login_fields.get_by_label('Username',exact=True).fill(ITEM_USER+suffix)
                login_fields.get_by_label('Password',exact=True).fill(ITEM_PASSWORD+suffix)
                page.get_by_role('button',name='Save',exact=True).click()
                page.get_by_text('Item added',exact=True).wait_for()
                assert evidence['cipher']
                dismiss_toasts(page)
                page.get_by_role('button',name='Close',exact=True).click()
                read_item(page,suffix)
                return evidence['cipher']

            if mode=='create':
                context,page,evidence,profile=session(users['local-member'],enroll=True)
                member_cipher=create_item(page,evidence)
                member_id,member_token=profile['id'],evidence['token']
                context.close()
                context,page,evidence,profile=session(users['local-alumni'],enroll=True)
                assert profile['id'] != member_id
                require_cipher_denial(native('/api/ciphers/'+member_cipher,evidence['token']))
                alumni_cipher=create_item(page,evidence,' alumni')
                assert native('/api/accounts/profile',member_token)[0]==200
                require_cipher_denial(native('/api/ciphers/'+alumni_cipher,member_token))
                private_write('receipt.json',{'cipher':member_cipher,'member':member_id,'alumniCipher':alumni_cipher,'alumni':profile['id']})
                private_write('alumni-session.json',{'token':evidence['token'],'id':profile['id'],'exp':jwt_exp(evidence['token'])})
                context.close();active_page=None
            elif mode=='verify':
                context,page,evidence,profile=session(users['local-member'])
                assert profile['id']==private_read(ROOT/'receipt.json')['member']
                read_item(page);context.close()
                prior=private_read(ROOT/'alumni-session.json')
                prior_status,prior_profile=native('/api/accounts/profile',prior['token'])
                assert prior_status==200 and prior_profile['id']==prior['id'] and prior_profile['email']==users['local-alumni']['email']
                context,page,evidence,profile=session(users['local-alumni'])
                assert profile['id']==prior['id'] and jwt_exp(evidence['token'])>time.time()+60
                read_item(page,' alumni')
                private_update('alumni-revocation-session.json',{'token':evidence['token'],'id':profile['id'],'exp':jwt_exp(evidence['token'])})
                context.close();active_page=None
                session(users['local-unapproved'],deny='Signups are disabled. You will need an invitation')
            elif mode=='disabled':
                stage='previously valid revoked session denial'
                prior=private_read(ROOT/'alumni-revocation-session.json')
                assert prior['exp']>time.time()+10
                assert native('/api/accounts/profile',prior['token'])[0] == 401
                session(users['local-alumni'],deny='This user has been disabled')
                context,page,evidence,profile=session(users['local-member'])
                read_item(page);context.close();active_page=None
            else:
                context,page,evidence,profile=session(users['local-alumni'])
                assert profile['id']==private_read(ROOT/'receipt.json')['alumni']
                read_item(page,' alumni');context.close();active_page=None
            print('Vaultwarden browser phase '+mode+' passed.')
        except Exception:
            if active_page:
                try:
                    image=active_page.screenshot(animations='disabled',mask=[active_page.locator('input,textarea,[contenteditable=true]')])
                    assert len(image)<=4*1024*1024
                    fd=os.open(ROOT/'failure.png',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    with os.fdopen(fd,'wb') as output:
                        output.write(image)
                    private_write('failure-labels.json',{'stage':stage,'headings':[text[:256] for text in active_page.locator('h1,h2,h3').all_text_contents()[:20]],'buttons':[text[:256] for text in active_page.get_by_role('button').all_text_contents()[:40]],'network':network_summary(active_network)})
                except Exception:
                    pass
            raise
        finally:
            browser.close()


if __name__=='__main__':
    try:
        main()
    except BaseException:
        print('Vaultwarden browser check failed at '+stage+'; private diagnostics suppressed.',file=sys.stderr)
        raise SystemExit(1)
