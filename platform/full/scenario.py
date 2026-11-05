# Runs outside the Nix store under the independent host deadline/cgroup.
# All credential-bearing guest command output is redirected into private files.
import concurrent.futures
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
import time
from typing import Any

run = Path(os.environ['TERRACE_REHEARSAL_RUN_DIR']).resolve(strict=True)
assert not str(run).startswith('/nix/store/')
assert run.stat().st_mode & 0o077 == 0
assert re.fullmatch(r'/[A-Za-z0-9/_.-]+', str(run))
assert not os.environ.get('NIX_DISK_IMAGE')
assert client.out_dir.resolve() == run / 'evidence'
started = time.monotonic()
results: dict[str, Any] = {'schema': 1, 'scope': 'terrace-disposable-synthetic', 'services': {}, 'timings': {}, 'originalGuests': [], 'checks': {}}
nodes = [gateway, automation, relay]
BACKUP_SECRETS = '/var/lib/rehearsal-escrow'
RESTORE = '/var/lib/rehearsal-recovery'
GATEWAY_INPUTS = '/var/lib/terrace-gateway-test'
WEB_INPUTS = '/var/lib/terrace-website-inputs'
MEDIA = '/var/lib/terrace-cms-media'
DOCUMENSO_INPUTS = '/var/lib/terrace-documenso-inputs'
DOCUMENSO_STATE = '/var/lib/terrace-documenso'
DOCUMENSO_BROWSER = '/root/documenso-functional'
VAULT_INPUTS = '/var/lib/terrace-vaultwarden-inputs'
VAULT_STATE = '/var/lib/vaultwarden'
VAULT_RELEASE = '/var/lib/terrace-vaultwarden'
VAULT_BROWSER = '/root/vaultwarden-functional'
WIND_INPUTS = '/var/lib/terrace-windmill-inputs'
WIND_STATE = '/var/lib/terrace-windmill'
WIND_BROWSER = '/root/windmill-functional'
TAILNET_INPUTS = '/var/lib/rehearsal-tailnet'
TAILNET_STATE = '/var/lib/tailscale'
TAILNET_PEERS = [(gateway, 'application'), (automation, 'private'), (client, 'denied')]
BLANK_WRITERS = {
    'gateway': ['postgresql.service', 'keycloak.service', 'nginx.service', 'terrace-cms.service', 'terrace-web.service', 'k3s.service'],
    'automation': ['home-assistant.target', 'home-assistant.service', 'terrace-demo-ha.socket', 'terrace-demo-ha.service', 'terrace-demo-ha-network.service', 'frigate.service', 'go2rtc.service', 'nginx.service'],
    'relay': ['headscale.service', 'nginx.service'],
}
if DOCUMENSO_ENABLED:
    BLANK_WRITERS['gateway'] += ['terrace-documenso.service', 'terrace-documenso-migrate.service', 'terrace-documenso-bootstrap.service', 'terrace-documenso-database.service']
if DOCUMENSO_SIGNING_ENABLED:
    BLANK_WRITERS['gateway'].append('documenso-test-mailpit.service')
if RELAY_PEERS_ENABLED:
    BLANK_WRITERS['gateway'].append('tailscaled.service')
    BLANK_WRITERS['automation'] += ['tailscaled.service', 'rehearsal-private-sensor.service']
APPLICATION_UNITS = [
    'podman-terrace-windmill-server.service', 'podman-terrace-windmill-worker.service',
    'terrace-windmill-db.service', 'terrace-windmill-migrate.service', 'terrace-windmill-bootstrap.service',
    'vaultwarden.service', 'terrace-vaultwarden-inputs.service', 'terrace-vaultwarden-database.service',
    'terrace-vaultwarden-migration-server.service', 'terrace-vaultwarden-migrate.service',
    'terrace-vaultwarden-guard.service', 'terrace-vaultwarden-bootstrap.service', 'rehearsal-pg-sample.service',
]
if APPLICATIONS_ENABLED:
    BLANK_WRITERS['gateway'] += APPLICATION_UNITS


def assert_blank_writers(machine):
    for unit in BLANK_WRITERS[machine.name]:
        assert machine.succeed('systemctl show ' + unit + ' -p ActiveState --value', timeout=10).strip() == 'inactive'
    if APPLICATIONS_ENABLED and machine.name == 'gateway':
        for unit in APPLICATION_UNITS:
            values = dict(line.split('=', 1) for line in machine.succeed('systemctl show ' + unit + ' -p LoadState,ActiveState,MainPID,ControlPID', timeout=10).splitlines())
            assert values == {'LoadState': 'loaded', 'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0'}


def timed(label, function):
    before = time.monotonic()
    result = function()
    results['timings'][label] = round(time.monotonic() - before, 2)
    return result


def sql(query, database='terrace_cms'):
    return gateway.succeed('runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d ' + shlex.quote(database) + ' -c ' + shlex.quote(query) + ' 2>/dev/null').strip()


def transfer(source, source_path, target, target_path):
    # Never embed bytes in a shell command or use copy_from_host_via_shell.
    # Unprivileged virtiofs cannot chown to guest UIDs. Retain private modes;
    # each destination is newly created by root inside its disposable guest.
    with tempfile.TemporaryDirectory(prefix='escrow-', dir=source.shared_dir) as private:
        name = Path(private).name
        source.succeed('cp -rP --preserve=mode ' + shlex.quote(source_path) + ' /tmp/shared/' + name + '/value')
        target.succeed('cp -rP --preserve=mode /tmp/shared/' + name + '/value ' + shlex.quote(target_path))


def bootstrap_app():
    gateway.succeed('systemctl start postgresql.target keycloak.service nginx.service', timeout=360)
    fixture_count = int(sql("select count(*) from user_entity where realm_id=(select id from realm where name='terrace')", 'keycloak'))
    if fixture_count == 0:
        gateway.succeed('python /etc/gateway-protocol.py seed', timeout=120)
    else:
        assert fixture_count == 5
    assert sql("select count(*) from user_entity where username='gateway-fixture-bootstrap'", 'keycloak') == '0'
    # The test-only master bootstrap is no longer needed after enrollment.
    gateway.succeed('chmod 0600 ' + GATEWAY_INPUTS + '/keycloak.env && truncate -s 0 ' + GATEWAY_INPUTS + '/keycloak.env && chmod 0400 ' + GATEWAY_INPUTS + '/keycloak.env')
    gateway.succeed('systemctl start terrace-cms-migrate.service && systemctl start terrace-cms.service terrace-web.service', timeout=300)
    gateway.succeed('systemctl start k3s.service')
    gateway.wait_until_succeeds("k3s kubectl get nodes --no-headers | grep ' Ready '", timeout=360)
    gateway.wait_until_succeeds("k3s kubectl get storageclass local-retain -o jsonpath='{.reclaimPolicy}' | grep -qx Retain", timeout=180)
    if DOCUMENSO_ENABLED:
        assert sql("select count(*) from user_entity where email='recovery@terrace.invalid'", 'keycloak') == '0'
        gateway.succeed('systemctl start terrace-documenso-migrate.service && systemctl start terrace-documenso-bootstrap.service && systemctl start terrace-documenso.service', timeout=360)
        gateway.wait_until_succeeds('curl --fail --silent --max-time 5 http://127.0.0.1:13050/api/health | jq -e \x27.checks.database.status == "ok"\x27 >/dev/null', timeout=90)
    if APPLICATIONS_ENABLED:
        try:
            gateway.succeed('systemctl start terrace-vaultwarden-migrate.service', timeout=300)
            gateway.succeed('systemctl start terrace-windmill-migrate.service', timeout=360)
            assert windmill_role_policy()
            gateway.succeed('systemctl start podman-terrace-windmill-server.service podman-terrace-windmill-worker.service', timeout=300)
            gateway.succeed('systemctl start vaultwarden.service && systemctl start terrace-vaultwarden-bootstrap.service', timeout=300)
            gateway.wait_until_succeeds('curl --fail --silent --max-time 5 http://127.0.0.1:18222/alive >/dev/null', timeout=90)
        except Exception:
            application_server_evidence('windmill')
            application_server_evidence('vaultwarden')
            raise


def app_signature():
    signature = {'realm': sql("select id from realm where name='terrace'", 'keycloak'),
            'users': sql('select md5(string_agg(id||username,chr(10) order by id)) from user_entity', 'keycloak'),
            'migrations': sql('select name||batch from payload_migrations order by id'),
            'inputs': gateway.succeed('cd ' + WEB_INPUTS + ' && sha256sum *.env').strip()}
    if DOCUMENSO_ENABLED:
        signature['documenso'] = {
            'migrations': sql('SELECT migration_name||checksum FROM "_prisma_migrations" ORDER BY migration_name', 'terrace_documenso'),
            'accounts': sql('SELECT md5(string_agg(id::text||email||coalesce(source,\x27\x27)||disabled::text||roles::text||coalesce("twoFactorSecret",\x27\x27)||coalesce("twoFactorBackupCodes",\x27\x27),chr(10) ORDER BY id)) FROM "User"', 'terrace_documenso'),
            'inputs': gateway.succeed('sha256sum ' + DOCUMENSO_INPUTS + '/secrets.json').strip(),
        }
    if DOCUMENSO_SIGNING_ENABLED:
        signature['documenso']['signingInputs'] = gateway.succeed('cd ' + DOCUMENSO_INPUTS + ' && sha256sum signing.p12 signing-passphrase').strip()
    if APPLICATIONS_ENABLED:
        # Account/data bytes are hashed privately while writers are stopped;
        # native browser receipts additionally prove retained job/script values.
        signature['windmill'] = {
            table: sql('SELECT md5(coalesce(string_agg(row_to_json(r)::text,chr(10) ORDER BY row_to_json(r)::text),\x27\x27)) FROM ' + table + ' r', 'terrace_windmill')
            for table in ['password', 'workspace', 'usr', 'script', 'variable']
        }
        signature['windmill']['inputs'] = gateway.succeed('sha256sum ' + WIND_INPUTS + '/bootstrap.json').strip()
        signature['windmill']['release'] = gateway.succeed('sha256sum ' + WIND_STATE + '/migrated-image').strip()
        signature['vaultwarden'] = {
            table: sql('SELECT md5(coalesce(string_agg(row_to_json(r)::text,chr(10) ORDER BY ' + key + '),\x27\x27)) FROM ' + table + ' r', 'terrace_vaultwarden')
            for table, key in [('users', 'uuid'), ('ciphers', 'uuid'), ('attachments', 'id')]
        }
        signature['vaultwarden']['migrations'] = sql('SELECT version FROM __diesel_schema_migrations ORDER BY version', 'terrace_vaultwarden')
        signature['vaultwarden']['inputs'] = gateway.succeed('sha256sum ' + VAULT_INPUTS + '/input.json').strip()
        if results['checks'].get('vaultwardenEncryptedFileCapture'):
            signature['vaultwarden']['attachmentInventory'] = gateway.succeed('sha256sum ' + VAULT_INPUTS + '/attachment-files.json').strip()
        signature['vaultwarden']['rsa'] = gateway.succeed('sha256sum ' + VAULT_STATE + '/rsa_key.pem').strip()
    return signature


def retain_private_artifact(source, destination, limit):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        assert stat.S_ISREG(info.st_mode) and info.st_mode & 0o077 == 0 and info.st_size <= limit
        value = os.read(descriptor, limit + 1)
        assert len(value) == info.st_size and len(value) <= limit
        # Shared socket scratch and durable evidence can be different filesystems.
        with destination.open('xb') as output:
            destination.chmod(0o600)
            output.write(value)
    finally:
        os.close(descriptor)


def documenso_browser(mode):
    assert mode in ['create', 'sign', 'verify']
    signing_option = ' --signing' if DOCUMENSO_SIGNING_ENABLED and mode in ['create', 'verify'] else ''
    try:
        assert client.succeed('rehearsal-documenso-browser ' + mode + signing_option, timeout=240).strip() == 'Documenso functional browser phase ' + mode + ' passed.'
    except Exception:
        if DOCUMENSO_SIGNING_ENABLED:
            documenso_signing_evidence(mode)
        try:
            destination = run / 'evidence' / ('documenso-failure-' + mode)
            destination.mkdir(mode=0o700)
            with tempfile.TemporaryDirectory(prefix='documenso-failure-', dir=client.shared_dir) as private:
                name = Path(private).name
                for filename in ['failure-' + mode + '-labels.json', 'failure-' + mode + '.png'] + (['signed.pdf', 'signing.crt'] if DOCUMENSO_SIGNING_ENABLED else []):
                    client.execute('test ! -f ' + DOCUMENSO_BROWSER + '/' + filename + ' || cp --preserve=mode ' + DOCUMENSO_BROWSER + '/' + filename + ' /tmp/shared/' + name + '/', timeout=10)
                    artifact = Path(private) / filename
                    if artifact.exists():
                        retain_private_artifact(artifact, destination / filename, 3 * 1024 * 1024)
            print('Private Documenso failure artifacts retained; contents suppressed.')
        except Exception:
            print('Private Documenso failure evidence unavailable; original failure preserved.')
        raise


def application_server_evidence(component):
    assert component in ['windmill', 'vaultwarden']
    # Reuse the tested private capture/parser, not a raw journal dump to stdout.
    try:
        program = 'capture.py' if component == 'windmill' else 'vaultwarden-capture.py'
        status, summary = gateway.execute('python /etc/windmill-evidence/' + program + ' 2>/dev/null', timeout=60)
        if status != 0:
            raise RuntimeError('Private capture unavailable.')
        print(summary)
        destination = run / 'evidence' / (component + '-server-failure')
        destination.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(prefix=component + '-server-', dir=gateway.shared_dir) as private:
            name = Path(private).name
            gateway.succeed('cp -P --preserve=mode /var/lib/' + component + '-test-evidence/raw.json /tmp/shared/' + name + '/raw.json', timeout=10)
            retain_private_artifact(Path(private) / 'raw.json', destination / 'raw.json', 12 * 1024 * 1024)
    except Exception:
        print('Private bounded application capture unavailable; original failure retained.')


def windmill_browser(mode):
    assert mode in ['create', 'verify']
    try:
        assert client.succeed('rehearsal-windmill-browser ' + mode + ' --roster', timeout=240).strip() == 'Windmill functional browser phase ' + mode + ' passed.'
    except Exception:
        application_server_evidence('windmill')
        try:
            destination = run / 'evidence' / ('windmill-failure-' + mode)
            destination.mkdir(mode=0o700)
            with tempfile.TemporaryDirectory(prefix='windmill-failure-', dir=client.shared_dir) as private:
                name = Path(private).name
                for filename in ['failure-' + mode + '.json', 'failure-' + mode + '-dom.json', 'failure-' + mode + '.png']:
                    status, _ = client.execute('test ! -f ' + WIND_BROWSER + '/' + filename + ' || cp -P --preserve=mode ' + WIND_BROWSER + '/' + filename + ' /tmp/shared/' + name + '/', timeout=10)
                    assert status == 0
                    artifact = Path(private) / filename
                    if artifact.exists():
                        retain_private_artifact(artifact, destination / filename, 4 * 1024 * 1024)
        except Exception:
            print('Private Windmill evidence unavailable; original failure retained.')
        raise


def vault_browser(mode):
    assert mode in ['create', 'restore']
    try:
        assert client.succeed('rehearsal-vaultwarden-browser ' + mode, timeout=240).strip() == 'Vaultwarden browser phase ' + mode + ' passed.'
    except Exception:
        application_server_evidence('vaultwarden')
        try:
            destination = run / 'evidence' / ('vaultwarden-failure-' + mode)
            destination.mkdir(mode=0o700)
            with tempfile.TemporaryDirectory(prefix='vaultwarden-failure-', dir=client.shared_dir) as private:
                name = Path(private).name
                for filename in ['failure.png', 'failure-network.json', 'failure-labels.json']:
                    source = VAULT_BROWSER + '/' + filename
                    status, _ = client.execute('test ! -f ' + source + ' || cp -P --preserve=mode ' + source + ' /tmp/shared/' + name + '/', timeout=10)
                    assert status == 0
                    copied = Path(private) / filename
                    if copied.exists():
                        retain_private_artifact(copied, destination / filename, 4 * 1024 * 1024)
        except Exception:
            print('Private Vaultwarden evidence unavailable; original failure retained.')
        raise


def vault_attachments(mode):
    assert mode in ['capture', 'verify']
    assert gateway.succeed('python3 /etc/vaultwarden-attachments.py ' + mode, timeout=10).strip() == 'Vaultwarden encrypted attachment inventory ' + mode + ' passed.'
    if mode == 'capture':
        results['checks']['vaultwardenEncryptedFileCapture'] = True


def documenso_signing_evidence(phase):
    # Fixed labels/counts only; raw native errors stay in bounded private files.
    try:
        for state in ['PENDING', 'PROCESSING', 'COMPLETED', 'FAILED']:
            query = 'SELECT count(*) FROM "BackgroundJob" WHERE "jobId"=\'send.signing.requested.email\' AND status=' + "'" + state + "'"
            count = gateway.succeed('runuser -u postgres -- psql -w -XAt -d terrace_documenso -c ' + shlex.quote(query) + ' 2>/dev/null', timeout=10).strip()
            assert count.isascii() and count.isdigit() and len(count) <= 10
            print('Synthetic signing-email job ' + state + ' count:', int(count))
        assert gateway.succeed('python /etc/documenso-signing-proof.py evidence', timeout=10).strip() == 'Private bounded signing runtime evidence retained.'
        destination = run / 'evidence' / ('documenso-signing-server-' + phase)
        destination.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(prefix='documenso-server-', dir=gateway.shared_dir) as private:
            name = Path(private).name
            gateway.succeed('cp --preserve=mode /var/lib/documenso-test-evidence/runtime-copy.log /tmp/shared/' + name + '/runtime-copy.log', timeout=10)
            artifact = Path(private) / 'runtime-copy.log'
            retain_private_artifact(artifact, destination / artifact.name, 4 * 1024 * 1024)
        print('Private bounded signing runtime evidence retained; contents suppressed.')
    except Exception:
        print('Private signing runtime evidence unavailable; original failure preserved.')


def documenso_signing_prepare():
    try:
        assert gateway.succeed("podman network inspect podman | jq -er '.[0].subnets[0].gateway'", timeout=10).strip() == '10.88.0.1'
        gateway.succeed('systemctl start documenso-test-mailpit.service', timeout=30)
        gateway.wait_until_succeeds('curl --fail --silent --max-time 2 http://127.0.0.1:8025/api/v1/info >/dev/null', timeout=15)
        assert gateway.succeed('podman exec --interactive terrace-documenso node - </etc/documenso-smtp-probe.cjs', timeout=12).strip() == 'Native container SMTP alias, protocol and image mailer verified without sending mail.'
        for port in [1025, 8025]:
            client.fail('nc -z -w 2 gateway ' + str(port), timeout=5)
        # Public fixture certificate only; no private key/passphrase reaches client.
        transfer(gateway, DOCUMENSO_INPUTS + '/signing.crt', client, DOCUMENSO_BROWSER + '/signing.crt')
    except Exception:
        documenso_signing_evidence('smtp')
        raise


def documenso_sign():
    try:
        assert gateway.succeed('python /etc/documenso-signing-proof.py mail', timeout=90).strip() == 'Synthetic signing request captured in the local mail sink.'
        query = 'SELECT count(*) FROM "BackgroundJob" WHERE "jobId"=\'send.signing.requested.email\' AND status=\'COMPLETED\''
        gateway.wait_until_succeeds('runuser -u postgres -- psql -w -XAt -d terrace_documenso -c ' + shlex.quote(query) + ' 2>/dev/null | grep -qx 1', timeout=10)
        assert sql('SELECT count(*) FROM "BackgroundJob" WHERE "jobId"=\'send.signing.requested.email\'', 'terrace_documenso') == '1'
        transfer(gateway, '/var/lib/terrace-documenso-mail-test/recipient.json', client, DOCUMENSO_BROWSER + '/recipient.json')
    except Exception:
        documenso_signing_evidence('mail')
        raise
    documenso_browser('sign')
    assert sql('SELECT count(*) FROM "Envelope" WHERE status=\'COMPLETED\'', 'terrace_documenso') == '1'
    return True


def documenso_recovery_access():
    gateway.succeed('systemctl stop terrace-identity-health.timer terrace-identity-health.service keycloak.service', timeout=90)
    state = dict(line.split('=', 1) for line in gateway.succeed('systemctl show keycloak.service -p ActiveState,MainPID,ControlPID', timeout=10).splitlines())
    assert state == {'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0'}
    assert gateway.succeed('systemctl show terrace-identity-health.timer -p ActiveState --value', timeout=10).strip() == 'inactive'
    health = dict(line.split('=', 1) for line in gateway.succeed('systemctl show terrace-identity-health.service -p MainPID,ControlPID', timeout=10).splitlines())
    assert health == {'MainPID': '0', 'ControlPID': '0'}
    gateway.succeed('listeners=$(ss -H -ltn \'( sport = :8088 or sport = :9000 )\') && test -z "$listeners"', timeout=10)
    assert gateway.succeed('python /etc/documenso-recovery.py', timeout=60).strip() == 'Native loopback recovery administrator authenticated without identity.'
    assert client.succeed("curl --silent --max-time 5 -X POST -o /dev/null -w '%{http_code}' https://documents.terrace.invalid/api/auth/email-password/authorize", timeout=10).strip() == '403'
    if APPLICATIONS_ENABLED:
        recovery_sessions = sql("SELECT count(*) FROM token WHERE email='admin@windmill.dev'", 'terrace_windmill')
        assert gateway.succeed('terrace-windmill-inspect', timeout=55).strip() == 'Native Windmill private admin readback passed.'
        assert sql("SELECT count(*) FROM token WHERE email='admin@windmill.dev'", 'terrace_windmill') == recovery_sessions
        assert client.succeed("curl --silent --max-time 5 -X POST -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid/api/auth/login", timeout=10).strip() == '403'
        results['checks']['windmillRestoredIdentityIndependentAdminRecovery'] = True
        assert gateway.succeed('terrace-vaultwarden-inspect', timeout=120).strip() == 'Native Vaultwarden private admin readback passed.'
        assert client.succeed("curl --silent --max-time 5 -o /dev/null -w '%{http_code}' https://vault.terrace.invalid/admin", timeout=10).strip() == '403'
        vault_attachments('verify')
        results['checks']['vaultwardenRestoredIdentityIndependentAdminRecovery'] = True
    gateway.succeed('test ! -s ' + GATEWAY_INPUTS + '/keycloak.env && systemctl start keycloak.service', timeout=300)
    gateway.wait_until_succeeds('curl --fail --silent --max-time 5 https://identity.terrace.invalid/realms/terrace/.well-known/openid-configuration | jq -e \' .issuer == "https://identity.terrace.invalid/realms/terrace" \' >/dev/null', timeout=90)
    assert sql("select count(*) from user_entity where username='gateway-fixture-bootstrap'", 'keycloak') == '0'
    return True


def documenso_container_identity():
    value = gateway.succeed("podman inspect --format '{{.Id}} {{.Image}}' terrace-documenso").strip().split()
    assert len(value) == 2 and all(re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', item) for item in value)
    return value


def bootstrap_automation():
    automation.succeed('systemctl start go2rtc.service frigate.service nginx.service home-assistant.service', timeout=300)
    automation.wait_until_succeeds('curl --fail --silent --max-time 5 http://127.0.0.1:8123/api/onboarding >/dev/null', timeout=300)
    automation.wait_until_succeeds("curl --fail --silent --max-time 5 http://127.0.0.1:5000/api/stats | jq -e '.cameras.synthetic_black.camera_fps > 0' >/dev/null", timeout=300)
    automation.succeed('if test -f /var/lib/rehearsal-demo-token/token; then cp /var/lib/rehearsal-demo-token/token /run/demos-test-token; demos-ha-test config >/dev/null; else demos-ha-test onboard; install -d -m 0700 /var/lib/rehearsal-demo-token; cp /run/demos-test-token /var/lib/rehearsal-demo-token/token; fi')


def bootstrap_relay():
    relay.succeed('systemctl start headscale.service nginx.service')
    users = json.loads(relay.succeed('headscale users list -o json'))
    if not users:
        relay.succeed('headscale users create rehearsal-synthetic >/dev/null 2>&1')
        users = json.loads(relay.succeed('headscale users list -o json'))
    assert len(users) == 1 and users[0]['name'] == 'rehearsal-synthetic'
    return users


def tailnet_client_identity(machine):
    value = json.loads(machine.succeed("tailscale status --json --peers=false 2>/dev/null | jq -ce '{state:.BackendState,id:.Self.ID,key:.Self.PublicKey,ips:.TailscaleIPs}'"))
    assert value['state'] == 'Running' and isinstance(value['id'], str) and value['id']
    assert re.fullmatch(r'nodekey:[0-9a-f]{64}', value['key'])
    assert len(value['ips']) in [1, 2]
    for address in value['ips']:
        assert ipaddress.ip_address(address) in ipaddress.ip_network('100.64.0.0/10') or ipaddress.ip_address(address) in ipaddress.ip_network('fd7a:115c:a1e0::/48')
    return value


def headscale_peer_identities():
    # Never return pre_auth_key or the full administrator response to the log.
    values = json.loads(relay.succeed("headscale nodes list -o json 2>/dev/null | jq -ce '[.[] | {id,machine_key,node_key,ip_addresses,given_name,tags}] | sort_by(.id)'"))
    assert len(values) == 3
    assert sorted(tag for value in values for tag in value['tags']) == sorted('tag:rehearsal-' + tag for _, tag in TAILNET_PEERS)
    assert sorted(value['given_name'] for value in values) == sorted('rehearsal-' + machine.name for machine, _ in TAILNET_PEERS)
    for value in values:
        assert value['id'] and value['machine_key'] and value['node_key'] and value['given_name']
    return values


def bootstrap_tailnet_peers():
    relay.succeed('install -d -m 0700 ' + TAILNET_INPUTS)
    for machine, role in TAILNET_PEERS:
        stem = TAILNET_INPUTS + '/' + machine.name
        relay.succeed('if test ! -e ' + stem + '.key; then umask 077; ulimit -f 128; headscale preauthkeys create --tags tag:rehearsal-' + role + ' --expiration 1h -o json >' + stem + '.json 2>/dev/null && jq -er \x27.key | select(type == "string" and length >= 20 and length <= 256 and test("^[A-Za-z0-9_-]+$"))\x27 ' + stem + '.json >' + stem + '.key 2>/dev/null && chmod 0400 ' + stem + '.key; fi', timeout=20)
        machine.succeed('systemctl start tailscaled.service', timeout=30)
        machine.wait_until_succeeds('tailscale status --json --peers=false 2>/dev/null | jq -e \x27.BackendState == "NeedsLogin" or .BackendState == "Running"\x27 >/dev/null', timeout=30)
        state = machine.succeed('tailscale status --json --peers=false 2>/dev/null | jq -er .BackendState').strip()
        if state == 'NeedsLogin':
            transfer(relay, stem + '.key', machine, TAILNET_INPUTS + '/auth-key')
            machine.succeed('umask 077; tailscale up --login-server https://relay --auth-key=file:' + TAILNET_INPUTS + '/auth-key --accept-dns=false --accept-routes=false --ssh=false --hostname=rehearsal-' + machine.name + ' >' + TAILNET_INPUTS + '/enrollment.log 2>&1', timeout=60)
        machine.wait_until_succeeds('tailscale status --json --peers=false 2>/dev/null | jq -e \x27.BackendState == "Running"\x27 >/dev/null', timeout=60)
    # These single-use fixture keys are expired before recovery. Replacement
    # peers must reconnect using their retained machine/node identities.
    for machine, _role in TAILNET_PEERS:
        key_id = relay.succeed('jq -er \x27.id | tonumber\x27 ' + TAILNET_INPUTS + '/' + machine.name + '.json 2>/dev/null').strip()
        assert re.fullmatch(r'[1-9][0-9]{0,5}', key_id)
        marker = TAILNET_INPUTS + '/' + machine.name + '.expired'
        relay.succeed('if test ! -e ' + marker + '; then headscale preauthkeys expire --id ' + key_id + ' >/dev/null 2>&1 && install -m 0400 /dev/null ' + marker + '; fi', timeout=15)
    relay.succeed('headscale preauthkeys list -o json 2>/dev/null | jq -e \x27length == 3 and all(.[]; .used == true and (.reusable // false) == false and (.ephemeral // false) == false)\x27 >/dev/null', timeout=15)
    return {machine.name: tailnet_client_identity(machine) for machine, _role in TAILNET_PEERS}


def check_tailnet_access(identities):
    address = next(value for value in identities['automation']['ips'] if ipaddress.ip_address(value).version == 4)
    url = 'http://' + address + ':18444'
    automation.succeed('systemctl start rehearsal-private-sensor.service')
    # Only a private file path, never a bearer value, enters the curl argv/log.
    command = 'curl --silent --show-error --max-time 5 --max-filesize 65536 --header @' + TAILNET_INPUTS + '/ha-header '
    gateway.wait_until_succeeds(command + '--fail -D ' + TAILNET_INPUTS + '/headers -o ' + TAILNET_INPUTS + '/sensor ' + url + '/api/states/sensor.synthetic_kitchen_temperature 2>/dev/null', timeout=60)
    gateway.succeed('jq -e \x27.entity_id == "sensor.synthetic_kitchen_temperature" and .state != "unknown" and .state != "unavailable"\x27 ' + TAILNET_INPUTS + '/sensor >/dev/null && grep -qi \x27^Cache-Control: private, no-store\x27 ' + TAILNET_INPUTS + '/headers')
    assert gateway.succeed(command + "-o /dev/null -w '%{http_code}' " + url + '/api/config 2>/dev/null').strip() == '403'
    assert gateway.succeed(command + "-X POST -o /dev/null -w '%{http_code}' " + url + '/api/states/sensor.synthetic_kitchen_temperature 2>/dev/null').strip() == '403'
    # A missing HTTP credential must not mask an accidentally allowed peer:
    # require TCP connection failure, rather than accepting an HTTP 401/403.
    client.fail('nc -z -w 3 ' + address + ' 18444 >/dev/null 2>&1')
    client_address = next(value for value in identities['client']['ips'] if ipaddress.ip_address(value).version == 4)
    client.succeed('nc -z -w 3 127.0.0.1 22')
    gateway.fail('nc -z -w 3 ' + client_address + ' 22 >/dev/null 2>&1')
    gateway.fail('nc -z -w 3 ' + address + ' 8123 >/dev/null 2>&1')
    client.fail('nc -z -w 3 automation 18444 >/dev/null 2>&1')
    relay.wait_until_succeeds('headscale nodes list -o json 2>/dev/null | jq -e \x27length == 3 and all(.[]; .online == true)\x27 >/dev/null', timeout=60)
    return {'nativeSensorRead': True, 'deniedPeer': True, 'deniedOtherPeerSsh': True, 'deniedPathAndWrite': True, 'deniedBackendPortAndLAN': True}


def check_automation():
    states = json.loads(automation.succeed('demos-ha-test states'))
    expected = ['sensor.synthetic_' + area + '_temperature' for area in ['kitchen', 'pool', 'solarium']]
    expected += ['binary_sensor.synthetic_' + area + '_occupancy' for area in ['kitchen', 'pool', 'solarium']]
    assert sorted(item['entity_id'] for item in states if item['entity_id'] in expected) == sorted(expected)
    assert all(item['state'] not in ['unknown', 'unavailable'] for item in states if item['entity_id'] in expected)
    preferences = json.loads(automation.succeed('demos-ha-test analytics'))['preferences']
    assert preferences and not any(preferences.values())
    settings = json.loads(automation.succeed('curl --fail --silent --max-time 5 http://127.0.0.1:5000/api/config'))
    for name in ['record', 'snapshots', 'detect', 'mqtt']:
        assert settings[name]['enabled'] is False
    assert settings['telemetry']['version_check'] is False
    assert list(settings['cameras']) == ['synthetic_black']
    automation.succeed('curl --fail --silent --max-time 10 http://127.0.0.1:5000/api/synthetic_black/latest.jpg >/var/tmp/synthetic-frame.jpg && test -s /var/tmp/synthetic-frame.jpg')
    if DOCUMENSO_ENABLED:
        automation.succeed('curl --fail --silent --max-time 10 https://documents.terrace.invalid/api/health | jq -e \x27.checks.database.status == "ok"\x27 >/dev/null')
    return True


def check_app(phase='', browser_enabled=True):
    output = gateway.succeed('TERRACE_SYNTHETIC_PHASE=' + shlex.quote(phase) + ' python /etc/gateway-protocol.py test', timeout=180).strip()
    assert re.fullmatch(r'Native HTTPS protocol checks passed: [0-9]+ bounded assertions; SSO, roles, state, CSRF, cookies, publication, private cache and logout\.', output)
    if browser_enabled:
        transfer(gateway, GATEWAY_INPUTS + '/credentials.json', client, '/root/gateway-fixture.json')
        browser = client.succeed('rehearsal-browser', timeout=90).strip()
        assert browser == 'Real Chromium denied-to-switch-to-approved checks passed: HTTPS trust, native POST Origin, approved page and secure cookie.'
        client.succeed('rm /root/gateway-fixture.json')
        check_backend_ports()
    count = re.search(r': ([0-9]+) ', output)
    assert count is not None
    return {'protocolAssertions': int(count[1]), 'browserSwitch': browser_enabled}


def check_backend_ports():
    ports = [5432, 6443, 8088, 9000, 13048, 14294] + ([13050] if DOCUMENSO_ENABLED else [])
    if APPLICATIONS_ENABLED:
        ports += [18001, 18002, 18003, 18222, 18224]
    for port in ports:
        client.fail(f'nc -z -w 1 gateway {port}')


def concurrent_browsers(phase):
    assert phase in ['original', 'restored']
    try:
        client.succeed('systemctl start --wait rehearsal-concurrent.service', timeout=345)
        state = dict(line.split('=', 1) for line in client.succeed('systemctl show rehearsal-concurrent.service -p ActiveState,MainPID,ControlPID,ControlGroup,ExecMainStatus', timeout=10).splitlines())
        assert state == {'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0', 'ControlGroup': '', 'ExecMainStatus': '0'}
        value = json.loads(client.succeed('cat /root/rehearsal-concurrent/result.json', timeout=10))
        assert value == {'website': True, 'documents': True, 'workflow': True, 'vault': True}
        # Preserve each phase's witnesses. The next phase must create a new
        # exclusive directory and cannot accidentally consume an old result.
        target = '/root/rehearsal-concurrent-' + phase
        client.succeed('test ! -e ' + target + ' && test ! -L ' + target + ' && mv -T /root/rehearsal-concurrent ' + target, timeout=10)
        return value
    except Exception:
        # Stop the owned whole cgroup before reading bounded private evidence.
        client.succeed('systemctl stop rehearsal-concurrent.service', timeout=30)
        try:
            destination = run / 'evidence' / ('concurrent-failure-' + phase)
            destination.mkdir(mode=0o700)
            with tempfile.TemporaryDirectory(prefix='concurrent-failure-', dir=client.shared_dir) as private:
                name = Path(private).name
                for service in ['website', 'documents', 'workflow', 'vault']:
                    for suffix in ['stdout', 'stderr']:
                        filename = service + '.' + suffix
                        client.succeed('test ! -f /root/rehearsal-concurrent/' + filename + ' || cp -P --preserve=mode /root/rehearsal-concurrent/' + filename + ' /tmp/shared/' + name + '/', timeout=10)
                        artifact = Path(private) / filename
                        if artifact.exists():
                            retain_private_artifact(artifact, destination / filename, 4 * 1024 * 1024)
        except Exception:
            print('Private concurrent evidence unavailable; original failure retained.')
        raise


def concurrent_failure_artifacts(phase):
    # Called after both driver threads join, so gateway/client console commands
    # cannot overlap their work. Fixed private paths only; no page text printed.
    application_server_evidence('windmill')
    application_server_evidence('vaultwarden')
    documenso_signing_evidence('concurrent-' + phase)
    sources = [
        ('documents', DOCUMENSO_BROWSER, ['failure-verify-labels.json', 'failure-verify.png', 'signed.pdf', 'signing.crt']),
        ('workflow', WIND_BROWSER, ['failure-verify.json', 'failure-verify-dom.json', 'failure-verify.png']),
        ('vault', VAULT_BROWSER, ['failure.png', 'failure-labels.json', 'failure-network.json']),
    ]
    try:
        for component, directory, files in sources:
            destination = run / 'evidence' / ('concurrent-' + phase + '-' + component)
            destination.mkdir(mode=0o700)
            with tempfile.TemporaryDirectory(prefix='concurrent-ui-', dir=client.shared_dir) as private:
                name = Path(private).name
                for filename in files:
                    client.succeed('test ! -f ' + directory + '/' + filename + ' || cp -P --preserve=mode ' + directory + '/' + filename + ' /tmp/shared/' + name + '/', timeout=10)
                    artifact = Path(private) / filename
                    if artifact.exists():
                        retain_private_artifact(artifact, destination / filename, 4 * 1024 * 1024)
    except Exception:
        print('Private concurrent UI evidence unavailable; original failure retained.')


def stop_units(machine, units):
    machine.succeed('systemctl stop ' + ' '.join(unit + '.service' for unit in units), timeout=180)
    for unit in units:
        machine.fail('systemctl is-active --quiet ' + unit + '.service')


def container_memory(container, limit):
    # OCI scopes may be outside their launcher service. Read the actual
    # container's kernel counters instead of treating conmon as its memory use.
    identity = gateway.succeed("podman inspect --format '{{.Id}} {{.State.CgroupPath}}' " + container, timeout=10).strip().split()
    assert len(identity) == 2 and re.fullmatch('[a-f0-9]{64}', identity[0])
    group = identity[1]
    assert re.fullmatch('/[A-Za-z0-9_./-]+', group) and identity[0] in group
    assert all(part not in ['.', '..'] for part in group.split('/'))
    values = gateway.succeed('cat ' + shlex.quote('/sys/fs/cgroup' + group + '/memory.peak') + ' ' + shlex.quote('/sys/fs/cgroup' + group + '/memory.max'), timeout=10).splitlines()
    assert len(values) == 2 and all(value.isascii() and value.isdigit() for value in values)
    peak, maximum = map(int, values)
    assert peak > 0 and maximum == limit
    return {'peakBytes': peak, 'maximumBytes': maximum}


def connection_limits():
    expected = {'keycloak': 10, 'terrace_cms': 10, 'terrace_documenso': 6, 'terrace_windmill': 10, 'terrace_vaultwarden': 6}
    names = ','.join("'" + name + "'" for name in expected)
    observed = json.loads(sql('SELECT json_object_agg(rolname,rolconnlimit) FROM pg_roles WHERE rolname IN (' + names + ')', 'postgres'))
    assert observed == expected
    maximum = int(sql('SHOW max_connections', 'postgres'))
    reserved = int(sql('SHOW superuser_reserved_connections', 'postgres'))
    assert maximum == 64 and reserved >= 3 and maximum - reserved - sum(observed.values()) >= 10
    return {'roles': observed, 'configuredMaximum': maximum, 'superuserReserved': reserved,
            'nonAppHeadroom': maximum - reserved - sum(observed.values())}


def windmill_role_policy():
    assert sql("SELECT count(*)=2 AND bool_and(NOT rolcanlogin AND NOT rolsuper AND NOT rolcreaterole AND NOT rolcreatedb AND NOT rolreplication AND rolpassword IS NULL AND rolbypassrls=(rolname='windmill_admin')) FROM pg_authid WHERE rolname IN ('windmill_user','windmill_admin')", 'postgres') == 't'
    # Public role names/flags only; retain a precise witness before assertion.
    role_flags = sql("SELECT rolname,rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls FROM pg_roles WHERE rolname IN ('keycloak','terrace_cms','terrace_documenso','terrace_windmill','terrace_vaultwarden') ORDER BY rolname", 'postgres').splitlines()
    expected_roles = ['keycloak', 'terrace_cms', 'terrace_documenso', 'terrace_vaultwarden', 'terrace_windmill']
    assert len(role_flags) == 5
    for name, row in zip(expected_roles, role_flags):
        columns = row.split('|')
        assert len(columns) == 7 and columns[0] == name and all(value in ['t', 'f'] for value in columns[1:])
    print('Application role flags: ' + json.dumps(role_flags), flush=True)
    assert role_flags == [name + '|t|f|f|f|f|f' for name in expected_roles]
    expected = ['terrace_windmill:windmill_admin:false', 'terrace_windmill:windmill_user:false', 'windmill_admin:windmill_user:false']
    grants = sql("SELECT member.rolname||':'||group_role.rolname||':'||m.admin_option::text FROM pg_auth_members m JOIN pg_roles member ON member.oid=m.member JOIN pg_roles group_role ON group_role.oid=m.roleid WHERE group_role.rolname IN ('windmill_user','windmill_admin') ORDER BY 1", 'postgres').splitlines()
    assert grants == expected
    assert sql("SELECT count(*)=4 AND bool_and(NOT pg_has_role(oid,'windmill_admin','MEMBER') AND NOT pg_has_role(oid,'windmill_admin','SET') AND NOT pg_has_role(oid,'windmill_user','MEMBER') AND NOT pg_has_role(oid,'windmill_user','SET')) FROM pg_roles WHERE rolname IN ('keycloak','terrace_cms','terrace_documenso','terrace_vaultwarden')", 'postgres') == 't'
    assert sql("SELECT pg_has_role('terrace_windmill','windmill_admin','SET') AND pg_has_role('terrace_windmill','windmill_user','SET')", 'postgres') == 't'
    return True


def simultaneous_functional_checks(phase):
    if APPLICATIONS_ENABLED:
        transfer(gateway, GATEWAY_INPUTS + '/credentials.json', client, '/root/gateway-fixture.json')
        assert windmill_role_policy()
        limits = connection_limits()
        if 'postgresConnectionLimits' in results:
            assert limits == results['postgresConnectionLimits']
        results['postgresConnectionLimits'] = limits
        gateway.succeed('systemctl start rehearsal-pg-sample.service', timeout=10)
        gateway.wait_until_succeeds('test -s /run/rehearsal-pg-sample/result.json', timeout=10)
    try:
        # Each thread uses a different guest console. The four browser children
        # share only the client-owned systemd control group, not its console.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            # The accepted protocol has exactly two publication fixtures.
            # Run each once: empty suffix before backup, -restored after.
            protocol_phase = '' if phase == 'original' else '-restored'
            application = pool.submit(check_app, protocol_phase, not APPLICATIONS_ENABLED)
            automation_checks = pool.submit(check_automation)
            native = concurrent_browsers(phase) if APPLICATIONS_ENABLED else None
            value = application.result()
            assert automation_checks.result()
            if APPLICATIONS_ENABLED:
                value['browserSwitch'] = True
                value['nativeApplications'] = native
    except Exception:
        if APPLICATIONS_ENABLED:
            concurrent_failure_artifacts(phase)
        raise
    finally:
        if APPLICATIONS_ENABLED:
            stop_units(gateway, ['rehearsal-pg-sample'])
            state = dict(line.split('=', 1) for line in gateway.succeed('systemctl show rehearsal-pg-sample.service -p ActiveState,MainPID,ControlPID,ExecMainStatus', timeout=10).splitlines())
            assert state == {'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0', 'ExecMainStatus': '0'}
            sample = json.loads(gateway.succeed('cat /run/rehearsal-pg-sample/result.json', timeout=10))
            assert set(sample) == {'samples', 'sampledPeakClients', 'configuredMaximum', 'elapsedMs'}
            assert all(type(item) is int for item in sample.values())
            assert sample['samples'] >= 2 and 0 < sample['sampledPeakClients'] <= sample['configuredMaximum'] == 64
            assert 100 <= sample['elapsedMs'] <= 355000
            results.setdefault('postgresConnectionSamples', {})[phase] = sample
    if APPLICATIONS_ENABLED:
        client.succeed('rm /root/gateway-fixture.json')
        check_backend_ports()
    return value


def quiesce_writers():
    if APPLICATIONS_ENABLED:
        stop_units(gateway, ['rehearsal-pg-sample'])
        stop_units(gateway, ['vaultwarden'])
        # Stop the job writer before its server and keep both down throughout
        # all database and file snapshots, including other target snapshots.
        stop_units(gateway, ['podman-terrace-windmill-worker'])
        stop_units(gateway, ['podman-terrace-windmill-server'])
        for name in ['podman-terrace-windmill-worker', 'podman-terrace-windmill-server']:
            state = dict(line.split('=', 1) for line in gateway.succeed('systemctl show ' + name + '.service -p ActiveState,MainPID,ControlPID', timeout=10).splitlines())
            assert state == {'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0'}
        assert gateway.succeed('systemctl show vaultwarden.service -p ActiveState --value', timeout=10).strip() == 'inactive'
    if RELAY_PEERS_ENABLED:
        stop_units(automation, ['rehearsal-private-sensor'])
        for machine, _role in TAILNET_PEERS:
            stop_units(machine, ['tailscaled'])
    if DOCUMENSO_ENABLED:
        stop_units(gateway, ['terrace-documenso'])
        assert gateway.succeed('systemctl show terrace-documenso.service -p ExecMainStatus --value').strip() in ['0', '143']
        assert gateway.succeed('systemctl show terrace-documenso.service -p ActiveState --value').strip() == 'inactive'
        assert gateway.succeed('podman ps -q').strip() == ''
    if DOCUMENSO_SIGNING_ENABLED:
        stop_units(gateway, ['documenso-test-mailpit'])
        assert gateway.succeed('systemctl show documenso-test-mailpit.service -p ActiveState --value', timeout=10).strip() == 'inactive'
    deployments = json.loads(gateway.succeed('k3s kubectl get deployments --all-namespaces -o json'))['items']
    deployment_replicas = [(item['metadata']['namespace'], item['metadata']['name'], item['spec']['replicas']) for item in deployments]
    for namespace, name, _replicas in deployment_replicas:
        gateway.succeed(f'k3s kubectl scale deployment {name} -n {namespace} --replicas=0 >/dev/null')
    gateway.succeed('k3s kubectl delete pod --all --all-namespaces --wait=true --timeout=120s >/dev/null', timeout=150)
    # Stop controllers too; K3s service stop alone leaves its container runtime.
    gateway.succeed('systemctl stop k3s.service; systemctl kill --kill-whom=all --signal=SIGKILL k3s.service >/dev/null 2>&1 || true')
    gateway.wait_until_succeeds('rehearsal-cgroups-empty', timeout=30)
    gateway.fail("pgrep -x 'containerd|containerd-shim|k3s' >/dev/null")
    stop_units(gateway, ['terrace-web', 'terrace-cms', 'keycloak', 'nginx'])
    automation.succeed('systemctl stop terrace-demo-ha.socket')
    stop_units(automation, ['nginx', 'terrace-demo-ha', 'home-assistant', 'frigate', 'go2rtc', 'terrace-demo-ha-network'])
    stop_units(relay, ['nginx', 'headscale'])
    return deployment_replicas


def stopped_guest(machine):
    assert machine.process is not None
    pid = machine.process.pid
    assert Path(f'/proc/{pid}/comm').read_text().startswith('qemu-system')
    start = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
    machine.execute('systemctl poweroff --no-block', check_return=False, check_output=False, timeout=10)
    machine.wait_for_shutdown(timeout=datetime.timedelta(seconds=90))
    assert machine.process.poll() is not None
    assert not Path(f'/proc/{pid}').exists()
    results['originalGuests'].append({'name': machine.name, 'pid': pid, 'start': start, 'absentBeforeRestore': True})
    disk = run / 'state' / (machine.name + '.qcow2')
    assert disk.is_file() and not disk.is_symlink()
    original_disk = disk.with_name(machine.name + '.original.qcow2')
    assert not original_disk.exists() and not original_disk.is_symlink()
    disk.rename(original_disk)
    # Preserve the entire original disk, not just the files selected for backup.
    retired = machine.state_dir.with_name(machine.state_dir.name + '-original')
    assert not retired.exists()
    machine.state_dir.rename(retired)
    machine.state_dir.mkdir(mode=0o700)
    return retired


def backup(machine):
    machine.succeed('systemctl start terrace-backup.service', timeout=300)
    snapshots = json.loads(machine.succeed('terrace-backup restic primary snapshots --json 2>/dev/null'))
    own = [snapshot for snapshot in snapshots if snapshot['hostname'] == machine.name]
    assert len(own) == 1
    return own[0]['id']


def fresh_restore(machine, snapshot, paths):
    machine.start()
    machine.wait_for_unit('multi-user.target')
    assert_blank_writers(machine)
    machine.succeed('test ! -e ' + BACKUP_SECRETS)
    transfer(client, '/root/escrow/' + machine.name, machine, BACKUP_SECRETS)
    try:
        machine.succeed('umask 077; terrace-backup-restore --target primary --snapshot ' + snapshot + ' --dest ' + RESTORE + ' >/var/tmp/rehearsal-restore.log 2>&1', timeout=300)
    except Exception:
        try:
            destination = run / 'evidence' / ('restore-failure-' + machine.name)
            destination.mkdir(mode=0o700)
            with tempfile.TemporaryDirectory(prefix='restore-failure-', dir=machine.shared_dir) as private:
                machine.succeed('python /etc/rehearsal-restore-evidence.py /tmp/shared/' + Path(private).name + '/files', timeout=15)
                for artifact in (Path(private) / 'files').iterdir():
                    retain_private_artifact(artifact, destination / artifact.name, 1024 * 1024)
        except Exception:
            print('Private restore evidence could not be exported.', flush=True)
        raise
    machine.succeed("grep -qx 'RECONCILIATION OK' " + RESTORE + '/reconciliation.txt')
    for path in paths:
        # NixOS tmpfiles installs this one immutable manifest before K3s starts.
        # Permit only that exact skeleton and the same restored store symlink;
        # any mutable or unexpected file still rejects before the copy.
        if path == '/var/lib/rancher/k3s/server':
            expected = shlex.quote('d manifests\nl manifests/local-retain.yaml')
            machine.succeed('entries="$(find ' + path + ' -mindepth 1 -printf \x27%y %P\\n\x27)" && entries="$(printf \x27%s\\n\x27 "$entries" | LC_ALL=C sort)" && test "$entries" = ' + expected)
            manifest = path + '/manifests/local-retain.yaml'
            machine.succeed('test -L ' + manifest + ' && test -f ' + manifest + ' && readlink ' + manifest + ' | grep -q \x27^/nix/store/\x27')
            machine.succeed('test "$(readlink ' + manifest + ')" = "$(readlink ' + RESTORE + '/files' + manifest + ')"')
        else:
            machine.succeed('test ! -L ' + shlex.quote(path) + ' && { test ! -e ' + shlex.quote(path) + ' || { entries="$(ls -A ' + shlex.quote(path) + ')" && test -z "$entries"; }; }')
        machine.succeed('mkdir -p ' + shlex.quote(path) + ' && cp -a ' + RESTORE + '/files' + path + '/. ' + shlex.quote(path) + '/')


for machine in [gateway, automation, relay, client]:
    disk = run / 'state' / (machine.name + '.qcow2')
    assert not disk.exists() and not disk.is_symlink()
start_all()
for machine in nodes:
    machine.wait_for_unit('multi-user.target')
client.wait_for_unit('sshd.service')
client.succeed('install -d -m 0700 /root/.pki/nssdb && certutil -N -d sql:/root/.pki/nssdb --empty-password')
client.succeed('certutil -A -d sql:/root/.pki/nssdb -n TerraceGatewayTest -t C,, -i /etc/rehearsal-browser-ca.pem')

with subtest('empty state and two explicit bootstrap passes'):
    for machine in nodes:
        assert_blank_writers(machine)
    for machine, path in [(gateway, GATEWAY_INPUTS + '/terrace-realm.json'), (automation, '/var/lib/terrace-demo-hass/.storage/auth'), (relay, '/var/lib/headscale/db.sqlite')]:
        machine.fail('test -e ' + path)
    if DOCUMENSO_ENABLED:
        gateway.fail('test -e ' + DOCUMENSO_INPUTS + '/secrets.json')
    if RELAY_PEERS_ENABLED:
        for machine, _role in TAILNET_PEERS:
            assert machine.succeed('systemctl show tailscaled.service -p ActiveState --value').strip() == 'inactive'
            machine.fail('test -e ' + TAILNET_STATE + '/tailscaled.state')
    timed('bootstrapApplication', bootstrap_app)
    signature = app_signature()
    timed('secondBootstrapApplication', bootstrap_app)
    assert app_signature() == signature
    gateway.succeed('install -m 0600 /dev/null ' + MEDIA + "/private.png && echo 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGPgUbL4DwACCgFmeGgpMwAAAABJRU5ErkJggg==' | base64 -d >" + MEDIA + '/private.png && cp ' + MEDIA + '/private.png ' + MEDIA + '/public.png && chown terrace_cms:terrace_cms ' + MEDIA + '/*.png')
    sql("insert into media(alt,public,filename,mime_type,filesize,width,height) values ('Synthetic private',false,'private.png','image/png',70,1,1),('Synthetic public',true,'public.png','image/png',70,1,1)")
    timed('bootstrapAutomation', bootstrap_automation)
    automation.wait_until_succeeds("demos-ha-test states | jq -e '[.[] | select(.entity_id | startswith(\"sensor.synthetic_\")) | select(.state != \"unknown\" and .state != \"unavailable\")] | length == 3' >/dev/null", timeout=180)
    automation_identity = json.loads(automation.succeed('demos-ha-test identity'))
    automation_token = automation.succeed('sha256sum /var/lib/rehearsal-demo-token/token').strip()
    timed('secondBootstrapAutomation', bootstrap_automation)
    assert json.loads(automation.succeed('demos-ha-test identity')) == automation_identity
    assert automation.succeed('sha256sum /var/lib/rehearsal-demo-token/token').strip() == automation_token
    relay_identity = timed('bootstrapRelay', bootstrap_relay)
    assert timed('secondBootstrapRelay', bootstrap_relay) == relay_identity
    gateway.succeed('k3s kubectl apply -f /etc/rehearsal-probe.yaml >/dev/null')
    gateway.wait_until_succeeds('k3s kubectl wait --for=condition=Ready pod/rehearsal --timeout=5s', timeout=360)
    gateway.succeed("k3s kubectl exec rehearsal -- /bin/busybox sh -c 'echo retained-synthetic > /data/marker'")
    cluster_identity = gateway.succeed("k3s kubectl get node gateway -o jsonpath='{.metadata.uid}'").strip()
    volume = gateway.succeed("k3s kubectl get pvc rehearsal -o jsonpath='{.spec.volumeName}'").strip()
    workload_identity = gateway.succeed("k3s kubectl get pod rehearsal -o jsonpath='{.metadata.uid}'").strip()
    gateway.succeed('k3s kubectl apply -f /etc/rehearsal-probe.yaml >/dev/null')
    assert gateway.succeed("k3s kubectl get pod rehearsal -o jsonpath='{.metadata.uid}'").strip() == workload_identity
    assert gateway.succeed("k3s kubectl get pvc rehearsal -o jsonpath='{.spec.volumeName}'").strip() == volume
    gateway.succeed('k3s kubectl exec rehearsal -- /bin/busybox grep -qx retained-synthetic /data/marker')

if RELAY_PEERS_ENABLED:
    with subtest('real tailnet clients, scoped native sensor read and denied peers'):
        tailnet_identities = timed('bootstrapTailnetPeers', bootstrap_tailnet_peers)
        tailnet_nodes = headscale_peer_identities()
        assert timed('secondBootstrapTailnetPeers', bootstrap_tailnet_peers) == tailnet_identities
        assert headscale_peer_identities() == tailnet_nodes
        relay_identity = json.loads(relay.succeed('headscale users list -o json'))
        transfer(automation, '/var/lib/rehearsal-demo-token/token', gateway, TAILNET_INPUTS + '/ha-token')
        # Bytes are read inside the guest and written only to a private file.
        # The literal command never contains the token itself.
        gateway.succeed('umask 077; token="$(cat ' + TAILNET_INPUTS + '/ha-token)" && printf \x27Authorization: Bearer %s\\n\x27 "$token" >' + TAILNET_INPUTS + '/ha-header && chmod 0400 ' + TAILNET_INPUTS + '/ha-header')
        results['checks']['tailnetPrivateAccess'] = timed('tailnetFunctionalAccess', lambda: check_tailnet_access(tailnet_identities))

if DOCUMENSO_ENABLED:
    with subtest('native Documenso SSO, unsigned PDF and encrypted account data'):
        client.succeed('install -d -m 0700 ' + DOCUMENSO_BROWSER)
        transfer(gateway, GATEWAY_INPUTS + '/documenso-browser.json', client, DOCUMENSO_BROWSER + '/credentials.json')
        if DOCUMENSO_SIGNING_ENABLED:
            timed('documensoSyntheticSmtp', documenso_signing_prepare)
        timed('documensoCreate', lambda: documenso_browser('create'))
        if DOCUMENSO_SIGNING_ENABLED:
            results['checks']['documensoNativeSignedEnvelope'] = timed('documensoSyntheticSign', documenso_sign)
        assert sql('SELECT count(*) FROM "User" WHERE "twoFactorEnabled" AND length("twoFactorSecret") > 80 AND length("twoFactorBackupCodes") > 80', 'terrace_documenso') == '1'
        assert sql("SELECT count(*) FROM \"User\" WHERE email IN ('local-unapproved@terrace.invalid','local-editorial-editor@terrace.invalid')", 'terrace_documenso') == '0'
        documenso_identity = documenso_container_identity()
        results['checks']['documensoNativeCreate'] = True

if APPLICATIONS_ENABLED:
    with subtest('individual native workflow identities and real worker execution'):
        client.succeed('install -d -m 0700 ' + WIND_BROWSER)
        transfer(gateway, GATEWAY_INPUTS + '/windmill-browser.json', client, WIND_BROWSER + '/credentials.json')
        timed('windmillCreate', lambda: windmill_browser('create'))
        windmill_identities = {name: gateway.succeed("podman inspect --format '{{.Id}} {{.Image}}' " + name, timeout=10).strip().split() for name in ['terrace-windmill-server', 'terrace-windmill-worker']}
        assert all(len(value) == 2 and all(re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', item) for item in value) for value in windmill_identities.values())
        roster_receipt_digest = client.succeed('sha256sum ' + WIND_BROWSER + '/roster-receipt.json', timeout=10).split()[0]
        assert re.fullmatch('[a-f0-9]{64}', roster_receipt_digest)
        results['retainedDeviceReceipts'] = {'windmillRosterSha256': roster_receipt_digest}
        results['checks']['windmillIndividualSsoAndWorkerExecution'] = True
        results['checks']['windmillOriginalRosterContract'] = True
    with subtest('individual native vault encryption and retained attachment bytes'):
        client.succeed('install -d -m 0700 ' + VAULT_BROWSER)
        transfer(gateway, GATEWAY_INPUTS + '/vaultwarden-browser.json', client, VAULT_BROWSER + '/credentials.json')
        timed('vaultwardenCreate', lambda: vault_browser('create'))
        vault_attachments('capture')
        assert sql('SELECT count(*) FROM ciphers', 'terrace_vaultwarden') == '2'
        assert sql('SELECT count(*) FROM attachments', 'terrace_vaultwarden') == '2'
        # Master passwords belong to the retained synthetic user device, not
        # server recovery inputs. Remove only the verified fixture copy after
        # both real client encrypt/decrypt and byte-for-byte transfer checks.
        source_digest = gateway.succeed('sha256sum ' + GATEWAY_INPUTS + '/vaultwarden-browser.json', timeout=10).split()[0]
        device_digest = client.succeed('sha256sum ' + VAULT_BROWSER + '/credentials.json', timeout=10).split()[0]
        assert re.fullmatch('[a-f0-9]{64}', source_digest) and source_digest == device_digest
        gateway.succeed('rm -- ' + GATEWAY_INPUTS + '/vaultwarden-browser.json', timeout=10)
        results['checks']['vaultwardenNativeEncryptionAndAttachments'] = True
        results['checks']['vaultwardenSyntheticMasterPasswordsOnlyOnRetainedDevice'] = True

with subtest('simultaneous real native SSO, public/private HTTP and synthetic automation'):
    results['checks']['application'] = timed('simultaneousOriginalApplications', lambda: simultaneous_functional_checks('original'))
    for port in [80, 8123, 5000, 5001, 1984, 8554]:
        client.fail(f'nc -z -w 1 automation {port}')
    results['checks']['automation'] = True
    relay.succeed('curl --fail --silent --max-time 5 http://127.0.0.1:8080/health >/dev/null')
    if RELAY_PEERS_ENABLED:
        relay.succeed("headscale policy get | jq -e '.acls == [{action:\"accept\",src:[\"tag:rehearsal-application\"],dst:[\"tag:rehearsal-private:18444\"]}] and (.tagOwners | length == 3)' >/dev/null")
    else:
        relay.succeed("headscale policy get | jq -e '.acls == []' >/dev/null")
    client.succeed('curl --fail --silent --max-time 10 https://relay/health >/dev/null')
    results['checks']['relayControlPlane' if RELAY_PEERS_ENABLED else 'relayControlPlaneOnly'] = True
    results['services'] = json.loads(gateway.succeed('cat /etc/rehearsal-versions.json')) | {'web': json.loads(gateway.succeed('cat $(systemctl show terrace-web.service -p WorkingDirectory --value)/release.json'))['revision'],
                           'cms': json.loads(gateway.succeed('cat $(systemctl show terrace-cms.service -p WorkingDirectory --value)/release.json'))['revision'],
                           'k3s': gateway.succeed('k3s --version').splitlines()[0],
                           'headscale': relay.succeed('headscale version').splitlines()[0],
                           'postgresql': gateway.succeed('psql --version').strip()}
    if RELAY_PEERS_ENABLED:
        results['services']['tailscale'] = gateway.succeed('tailscale version').splitlines()[0]
    results['peakBytes'] = {unit: int(gateway.succeed('systemctl show ' + unit + '.service -p MemoryPeak --value').strip()) for unit in ['keycloak', 'terrace-cms', 'terrace-web', 'postgresql', 'k3s']}
    if DOCUMENSO_ENABLED:
        results['peakBytes']['terrace-documenso'] = int(gateway.succeed('systemctl show terrace-documenso.service -p MemoryPeak --value').strip())
    if APPLICATIONS_ENABLED:
        results['execution'] = {
            'nativeSystemd': ['web', 'cms', 'keycloak', 'postgresql', 'vaultwarden', 'headscale', 'home-assistant', 'frigate', 'go2rtc'],
            'managedPodman': ['documenso', 'windmill-server', 'windmill-worker'],
            'k3sWorkloads': ['synthetic persistent-volume probe'],
            'testGuestCount': 4,
        }
        results['containerMemory'] = {name: container_memory(name, limit) for name, limit in [
            ('terrace-documenso', 2147483648), ('terrace-windmill-server', 1610612736), ('terrace-windmill-worker', 1610612736)]}
        results['peakBytes']['vaultwarden'] = int(gateway.succeed('systemctl show vaultwarden.service -p MemoryPeak --value').strip())
    results['automationPeakBytes'] = {unit: int(automation.succeed('systemctl show ' + unit + '.service -p MemoryPeak --value').strip()) for unit in ['home-assistant', 'frigate', 'go2rtc', 'nginx']}
    results['relayPeakBytes'] = {unit: int(relay.succeed('systemctl show ' + unit + '.service -p MemoryPeak --value').strip()) for unit in ['headscale', 'nginx']}
    results['postgresConnectionsAfterConcurrentChecks'] = int(sql('select count(*) from pg_stat_activity', 'postgres'))

with subtest('quiesce every writer before all encrypted snapshots'):
    deployment_replicas = quiesce_writers()
    signature = app_signature()
    if APPLICATIONS_ENABLED:
        gateway.succeed('test ! -e ' + GATEWAY_INPUTS + '/vaultwarden-browser.json && test ! -L ' + GATEWAY_INPUTS + '/vaultwarden-browser.json', timeout=10)
    relay_keys = relay.succeed('cd /var/lib/headscale && sha256sum noise_private.key derp_server_private.key').strip()
    for machine in nodes:
        machine.succeed('install -d -m 0700 ' + BACKUP_SECRETS + ' && ssh-keygen -q -t ed25519 -N "" -f ' + BACKUP_SECRETS + '/id_ed25519 && chmod 0400 ' + BACKUP_SECRETS + '/id_ed25519')
        public = machine.succeed('cat ' + BACKUP_SECRETS + '/id_ed25519.pub').strip()
        assert re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+ [^\n]*', public)
        client.succeed('install -d -o restic -g users -m 0700 /home/restic/.ssh && echo ' + shlex.quote(public) + ' >>/home/restic/.ssh/authorized_keys && chown restic:users /home/restic/.ssh/authorized_keys && chmod 0600 /home/restic/.ssh/authorized_keys')
        machine.succeed('ssh-keyscan -t ed25519 client >' + BACKUP_SECRETS + '/known_hosts 2>/dev/null')
    client.succeed('install -d -m 0700 /root/escrow && head -c 32 /dev/urandom | base64 >/root/escrow/repository-password && chmod 0400 /root/escrow/repository-password')
    for machine in nodes:
        transfer(client, '/root/escrow/repository-password', machine, BACKUP_SECRETS + '/repository-password')
        transfer(machine, BACKUP_SECRETS, client, '/root/escrow/' + machine.name)
    gateway.succeed('terrace-backup init primary >/dev/null 2>&1')
    snapshots = {machine.name: timed('backup-' + machine.name, lambda machine=machine: backup(machine)) for machine in nodes}
    timed('fullEncryptedCheck', lambda: gateway.succeed('systemctl start terrace-backup-check.service', timeout=300))
    assert app_signature() == signature
    client.fail('jq . /home/restic/repository/config >/dev/null 2>&1')
    client.succeed('tar -C /home/restic -cf /root/encrypted-repository.tar repository && chmod 0600 /root/encrypted-repository.tar && tar -C /root -cf /root/recovery-escrow.tar escrow && chmod 0600 /root/recovery-escrow.tar')
    client.copy_from_machine('/root/encrypted-repository.tar', 'retained')
    client.copy_from_machine('/root/recovery-escrow.tar', 'escrow')
    # The driver output is private and outside the store; move only these named artifacts.
    for source_dir, name in [('retained', 'encrypted-repository.tar'), ('escrow', 'recovery-escrow.tar')]:
        source = client.out_dir / source_dir / name
        target = run / source_dir / name
        assert source.is_file() and not target.exists()
        source.rename(target)
        target.chmod(0o600)
        with target.open('rb') as stream:
            artifact_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        results.setdefault('artifacts', {})[name] = {'sha256': artifact_hash, 'bytes': target.stat().st_size}

    captured = run / 'evidence' / 'captured-artifacts.json'
    captured.write_text(json.dumps(results['artifacts'], indent=2) + '\n')
    captured.chmod(0o600)

    # No source writer is restarted until every source snapshot has completed.
    stop_units(gateway, ['postgresql'])
    retired = {machine.name: stopped_guest(machine) for machine in nodes}
    assert all(not Path('/proc/' + str(item['pid'])).exists() for item in results['originalGuests'])

with subtest('fresh empty disks restore the exact node identity and functional services'):
    application_paths = [GATEWAY_INPUTS, WEB_INPUTS, MEDIA, '/var/lib/rancher/k3s/server', '/var/lib/rancher/k3s/storage', '/etc/rancher/node']
    if APPLICATIONS_ENABLED:
        application_paths += [VAULT_INPUTS, VAULT_STATE, VAULT_RELEASE, WIND_INPUTS, WIND_STATE]
    if DOCUMENSO_ENABLED:
        application_paths += [DOCUMENSO_INPUTS, DOCUMENSO_STATE]
    if RELAY_PEERS_ENABLED:
        application_paths += [TAILNET_INPUTS, TAILNET_STATE]
    timed('restore-application', lambda: fresh_restore(gateway, snapshots['gateway'], application_paths))
    # Adopt the reconciled, stopped PG copy into its DECLARED native service.
    # The module replaces postgresql.conf with its pinned native HBA/config;
    # the isolated trust configuration is never the active runtime configuration.
    pg_dir = gateway.succeed('cat /etc/rehearsal-pg-directory').strip()
    assert re.fullmatch('/var/lib/postgresql/[0-9]+', pg_dir)
    gateway.succeed('test ! -L ' + pg_dir + ' && { test ! -e ' + pg_dir + ' || { entries="$(ls -A ' + pg_dir + ')" && test -z "$entries"; }; }')
    gateway.succeed('install -d -m 0700 -o postgres -g postgres ' + pg_dir + ' && cp -a ' + RESTORE + '/pgdata/. ' + pg_dir + '/ && chown -R postgres:postgres ' + pg_dir)
    gateway.succeed('systemctl start postgresql.target')
    # PG18 requires its bootstrap OID10 to remain superuser/catalog owner.
    # Retain no authentication or membership path to that internal identity.
    sql('alter role "terrace-restore" nologin password null', 'postgres')
    assert sql("select rolsuper and not rolcanlogin and rolpassword is null from pg_authid where oid=10 and rolname='terrace-restore'", 'postgres') == 't'
    assert sql('select count(*) from pg_auth_members where roleid=10 or member=10', 'postgres') == '0'
    managed_roles = ['terrace_cms', 'keycloak'] + (['terrace_documenso'] if DOCUMENSO_ENABLED else [])
    if APPLICATIONS_ENABLED:
        managed_roles += ['terrace_vaultwarden', 'terrace_windmill']
    names = ','.join("'" + role + "'" for role in managed_roles)
    assert sql("select count(*)=" + str(len(managed_roles)) + " and bool_and(not pg_has_role(oid,10::oid,'MEMBER') and not pg_has_role(oid,10::oid,'SET')) from pg_roles where rolname in (" + names + ')', 'postgres') == 't'
    assert sql("select count(*) from pg_stat_activity where usesysid=10 and backend_type='client backend'", 'postgres') == '0'
    gateway.fail('runuser -u terrace-restore -- psql -X -w -h /run/postgresql -U terrace-restore -d postgres -c \x27select 1\x27 >/dev/null 2>&1')
    cms_peer = 'runuser -u terrace_cms -- psql -X -w -v ON_ERROR_STOP=1 -h /run/postgresql -U terrace_cms -d terrace_cms'
    identity_password = 'PGPASSWORD="$(cat /var/lib/terrace-gateway-test/db-password)" psql -X -w -v ON_ERROR_STOP=1 -h 127.0.0.1 -U keycloak -d keycloak'
    app_credentials = [cms_peer, identity_password]
    if DOCUMENSO_ENABLED:
        app_credentials.append('PGPASSWORD="$(jq -r .databasePassword ' + DOCUMENSO_INPUTS + '/secrets.json 2>/dev/null)" psql -X -w -v ON_ERROR_STOP=1 -h /run/postgresql -U terrace_documenso -d terrace_documenso')
    if APPLICATIONS_ENABLED:
        app_credentials.append('runuser -u terrace_windmill -- psql -X -w -v ON_ERROR_STOP=1 -h /run/postgresql -U terrace_windmill -d terrace_windmill')
        app_credentials.append('runuser -u vaultwarden -- psql -X -w -v ON_ERROR_STOP=1 -h /run/postgresql -U terrace_vaultwarden -d terrace_vaultwarden')
    for credentials in app_credentials:
        gateway.succeed(credentials + ' -c \x27select 1\x27 >/dev/null 2>&1')
        gateway.fail(credentials + ' -c \x27set role "terrace-restore"\x27 >/dev/null 2>&1')
    if APPLICATIONS_ENABLED:
        assert windmill_role_policy()
        # Prove native credentials also enforce the role-membership boundary.
        for credentials in app_credentials:
            if credentials.startswith('runuser -u terrace_windmill '):
                for role in ['windmill_admin', 'windmill_user']:
                    gateway.succeed(credentials + ' -c ' + shlex.quote('set role ' + role + '; select 1') + ' >/dev/null 2>&1')
            else:
                for role in ['windmill_admin', 'windmill_user']:
                    gateway.fail(credentials + ' -c ' + shlex.quote('set role ' + role) + ' >/dev/null 2>&1')
        results['checks']['restoredWindmillDatabaseRoleIsolation'] = True
    results['checks']['bootstrapOwnerLoginAndAssumptionDenied'] = True
    assert sql('show hba_file', 'postgres').startswith('/nix/store/')
    assert sql('show fsync', 'postgres') == 'on'
    if APPLICATIONS_ENABLED:
        assert connection_limits() == results['postgresConnectionLimits']
        results['checks']['restoredDatabaseConnectionLimits'] = True
    assert app_signature() == signature
    timed('recoveredBootstrapApplication', bootstrap_app)
    assert gateway.succeed("k3s kubectl get node gateway -o jsonpath='{.metadata.uid}'").strip() == cluster_identity
    assert gateway.succeed("k3s kubectl get pvc rehearsal -o jsonpath='{.spec.volumeName}'").strip() == volume
    for namespace, name, replicas in deployment_replicas:
        gateway.succeed(f'k3s kubectl scale deployment {name} -n {namespace} --replicas={replicas} >/dev/null')
    gateway.succeed('k3s kubectl apply -f /etc/rehearsal-probe.yaml >/dev/null')
    gateway.wait_until_succeeds('k3s kubectl wait --for=condition=Ready pod/rehearsal --timeout=5s', timeout=360)
    gateway.succeed("k3s kubectl exec rehearsal -- /bin/busybox sh -c 'test $(cat /data/marker) = retained-synthetic && echo restored-write > /data/restored'")
    gateway.succeed("k3s kubectl exec rehearsal -- /bin/busybox grep -qx restored-write /data/restored")
    results['checks']['freshK3sRuntimeRetainedPVCReadWrite'] = True
    if not APPLICATIONS_ENABLED:
        results['checks']['restoredApplication'] = timed('recoveredFunctionalApplication', lambda: check_app('-restored'))
    if APPLICATIONS_ENABLED:
        for name, original in windmill_identities.items():
            restored = gateway.succeed("podman inspect --format '{{.Id}} {{.Image}}' " + name, timeout=10).strip().split()
            assert len(restored) == 2 and restored[0] != original[0] and restored[1] == original[1]
        assert client.succeed('sha256sum ' + WIND_BROWSER + '/roster-receipt.json', timeout=10).split()[0] == roster_receipt_digest
        timed('windmillFreshRestore', lambda: windmill_browser('verify'))
        results['checks']['windmillFreshContainersDataAndWorkerExecution'] = True
        results['checks']['windmillRestoredOriginalRosterContract'] = True
        gateway.succeed('test ! -e ' + GATEWAY_INPUTS + '/vaultwarden-browser.json && test ! -L ' + GATEWAY_INPUTS + '/vaultwarden-browser.json', timeout=10)
        timed('vaultwardenFreshRestore', lambda: vault_browser('restore'))
        vault_attachments('verify')
        results['checks']['vaultwardenFreshEncryptedVaultAndAttachments'] = True
    if DOCUMENSO_ENABLED:
        recovered_documenso = documenso_container_identity()
        assert recovered_documenso[0] != documenso_identity[0] and recovered_documenso[1] == documenso_identity[1]
        timed('documensoFreshVerify', lambda: documenso_browser('verify'))
        results['checks']['documensoFreshContainer'] = True
        results['checks']['documensoRestoredPdfAndEncrypted2FA'] = True
        if DOCUMENSO_SIGNING_ENABLED:
            sink = dict(line.split('=', 1) for line in gateway.succeed('systemctl show documenso-test-mailpit.service -p ActiveState,MainPID,ControlPID', timeout=10).splitlines())
            assert sink == {'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0'}
            assert sql('SELECT count(*) FROM "BackgroundJob" WHERE "jobId"=\'send.signing.requested.email\'', 'terrace_documenso') == '1'
            assert sql('SELECT count(*) FROM "BackgroundJob" WHERE "jobId"=\'send.signing.requested.email\' AND status=\'COMPLETED\'', 'terrace_documenso') == '1'
            results['checks']['documensoRestoredSignedEnvelopeAndCertificate'] = True
            results['checks']['documensoRestoredSigningRequestCompleteAndSinkOff'] = True
        results['checks']['documensoRestoredIdentityIndependentRecovery'] = timed('documensoFreshRecoveryAccess', documenso_recovery_access)
    automation_paths = ['/var/lib/terrace-demo-hass', '/var/lib/terrace-demo-frigate', '/var/lib/rehearsal-demo-token']
    if RELAY_PEERS_ENABLED:
        automation_paths += [TAILNET_INPUTS, TAILNET_STATE]
    timed('restore-automation', lambda: fresh_restore(automation, snapshots['automation'], automation_paths))
    automation.succeed('cp /var/lib/rehearsal-demo-token/token /run/demos-test-token')
    timed('recoveredBootstrapAutomation', bootstrap_automation)
    assert check_automation()
    assert json.loads(automation.succeed('demos-ha-test identity')) == automation_identity
    assert automation.succeed('sha256sum /var/lib/rehearsal-demo-token/token').strip() == automation_token
    results['checks']['restoredAutomation'] = True
    timed('restore-relay', lambda: fresh_restore(relay, snapshots['relay'], ['/var/lib/headscale']))
    relay.succeed('install -m 0600 -o headscale -g headscale ' + RESTORE + '/files/var/lib/terrace-backup/dumps/sqlite/headscale.sqlite /var/lib/headscale/db.sqlite')
    assert relay.succeed('cd /var/lib/headscale && sha256sum noise_private.key derp_server_private.key').strip() == relay_keys
    relay.succeed('systemctl start headscale.service nginx.service')
    assert json.loads(relay.succeed('headscale users list -o json')) == relay_identity
    relay.succeed('sqlite3 /var/lib/headscale/db.sqlite "PRAGMA integrity_check" | grep -qx ok')
    client.succeed('curl --fail --silent --max-time 10 https://relay/health >/dev/null')
    results['checks']['restoredRelayControlPlane' if RELAY_PEERS_ENABLED else 'restoredRelayControlPlaneOnly'] = True
    if RELAY_PEERS_ENABLED:
        for machine, _role in TAILNET_PEERS:
            machine.succeed('systemctl start tailscaled.service', timeout=30)
            machine.wait_until_succeeds('tailscale status --json --peers=false 2>/dev/null | jq -e \x27.BackendState == "Running"\x27 >/dev/null', timeout=90)
        assert {machine.name: tailnet_client_identity(machine) for machine, _role in TAILNET_PEERS} == tailnet_identities
        assert headscale_peer_identities() == tailnet_nodes
        results['checks']['restoredTailnetIdentitiesAndPrivateAccess'] = timed('restoredTailnetAccess', lambda: check_tailnet_access(tailnet_identities))
    if APPLICATIONS_ENABLED:
        results['checks']['simultaneousRestoredApplications'] = timed('simultaneousRestoredApplications', lambda: simultaneous_functional_checks('restored'))
        results['checks']['restoredApplication'] = results['checks']['simultaneousRestoredApplications']
        results['restoredContainerMemory'] = {name: container_memory(name, limit) for name, limit in [
            ('terrace-documenso', 2147483648), ('terrace-windmill-server', 1610612736), ('terrace-windmill-worker', 1610612736)]}
        results['restoredPostgresConnectionsAfterConcurrentChecks'] = int(sql('select count(*) from pg_stat_activity', 'postgres'))
    for machine in nodes:
        assert machine.succeed('cat /var/lib/unrelated-synthetic-sentinel').strip() == 'retained'

with subtest('verified recovery and final stop of every guest'):
    timed('finalQuiesceWriters', quiesce_writers)
    stop_units(gateway, ['postgresql'])
    final_guests = []
    for machine in [gateway, automation, relay, client]:
        assert machine.process is not None
        pid = machine.process.pid
        assert Path(f'/proc/{pid}/comm').read_text().startswith('qemu-system')
        start = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
        assert start.isascii() and start.isdigit()
        machine.execute('systemctl poweroff --no-block', check_return=False, check_output=False, timeout=10)
        machine.wait_for_shutdown(timeout=datetime.timedelta(seconds=90))
        assert machine.process.poll() is not None
        assert not Path(f'/proc/{pid}').exists()
        final_guests.append({'name': machine.name, 'pid': pid, 'start': start,
                             'role': 'retained-device' if machine.name == 'client' else 'replacement',
                             'absentAfterShutdown': True})
    results['finalGuests'] = final_guests
    assert len({(item['pid'], item['start']) for item in results['originalGuests'] + final_guests}) == 7
    results['checks']['allGuestsStopped'] = True
    results['timings']['scenarioTotal'] = round(time.monotonic() - started, 2)
    temporary = run / 'evidence' / 'scenario-result.json.new'
    temporary.write_text(json.dumps(results, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.rename(temporary.with_suffix(''))
    print('Established-service rehearsal passed: two bootstrap passes, native HTTPS SSO/privacy, synthetic automation, encrypted quiescent backup, original guests absent, fresh retained-PVC/app/relay recovery and all guests stopped.')
