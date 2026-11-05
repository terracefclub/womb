# Runs outside the Nix store under the independent host deadline/cgroup.
# All credential-bearing guest command output is redirected into private files.
import concurrent.futures
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
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
BLANK_WRITERS = {
    'gateway': ['postgresql.service', 'keycloak.service', 'nginx.service', 'terrace-cms.service', 'terrace-web.service', 'k3s.service'],
    'automation': ['home-assistant.target', 'home-assistant.service', 'terrace-demo-ha.socket', 'terrace-demo-ha.service', 'terrace-demo-ha-network.service', 'frigate.service', 'go2rtc.service', 'nginx.service'],
    'relay': ['headscale.service', 'nginx.service'],
}


def assert_blank_writers(machine):
    for unit in BLANK_WRITERS[machine.name]:
        assert machine.succeed('systemctl show ' + unit + ' -p ActiveState --value', timeout=10).strip() == 'inactive'


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
    gateway.succeed('systemctl start terrace-cms-migrate.service && systemctl start terrace-cms.service terrace-web.service', timeout=300)
    gateway.succeed('systemctl start k3s.service')
    gateway.wait_until_succeeds("k3s kubectl get nodes --no-headers | grep ' Ready '", timeout=360)
    gateway.wait_until_succeeds("k3s kubectl get storageclass local-retain -o jsonpath='{.reclaimPolicy}' | grep -qx Retain", timeout=180)


def app_signature():
    return {'realm': sql("select id from realm where name='terrace'", 'keycloak'),
            'users': sql('select md5(string_agg(id||username,chr(10) order by id)) from user_entity', 'keycloak'),
            'migrations': sql('select name||batch from payload_migrations order by id'),
            'inputs': gateway.succeed('cd ' + WEB_INPUTS + ' && sha256sum *.env').strip()}


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
    return True


def check_app(phase=''):
    output = gateway.succeed('TERRACE_SYNTHETIC_PHASE=' + shlex.quote(phase) + ' python /etc/gateway-protocol.py test', timeout=180).strip()
    assert re.fullmatch(r'Native HTTPS protocol checks passed: [0-9]+ bounded assertions; SSO, roles, state, CSRF, cookies, publication, private cache and logout\.', output)
    transfer(gateway, GATEWAY_INPUTS + '/credentials.json', client, '/root/gateway-fixture.json')
    browser = client.succeed('rehearsal-browser', timeout=90).strip()
    assert browser == 'Real Chromium denied-to-switch-to-approved checks passed: HTTPS trust, native POST Origin, approved page and secure cookie.'
    client.succeed('rm /root/gateway-fixture.json')
    for port in [5432, 6443, 8088, 9000, 13048, 14294]:
        client.fail(f'nc -z -w 1 gateway {port}')
    count = re.search(r': ([0-9]+) ', output)
    assert count is not None
    return {'protocolAssertions': int(count[1]), 'browserSwitch': True}


def stop_units(machine, units):
    machine.succeed('systemctl stop ' + ' '.join(unit + '.service' for unit in units), timeout=180)
    for unit in units:
        machine.fail('systemctl is-active --quiet ' + unit + '.service')


def quiesce_writers():
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
    machine.succeed('umask 077; terrace-backup-restore --target primary --snapshot ' + snapshot + ' --dest ' + RESTORE + ' >/var/tmp/rehearsal-restore.log 2>&1', timeout=300)
    machine.succeed("grep -qx 'RECONCILIATION OK' " + RESTORE + '/reconciliation.txt')
    for path in paths:
        # NixOS tmpfiles installs this one immutable manifest before K3s starts.
        # Permit only that exact skeleton and the same restored store symlink;
        # any mutable or unexpected file still rejects before the copy.
        if path == '/var/lib/rancher/k3s/server':
            expected = shlex.quote('d manifests\nl manifests/local-retain.yaml')
            machine.succeed('test "$(find ' + path + ' -mindepth 1 -printf \x27%y %P\\n\x27 | LC_ALL=C sort)" = ' + expected)
            manifest = path + '/manifests/local-retain.yaml'
            machine.succeed('test -L ' + manifest + ' && test -f ' + manifest + ' && readlink ' + manifest + ' | grep -q \x27^/nix/store/\x27')
            machine.succeed('test "$(readlink ' + manifest + ')" = "$(readlink ' + RESTORE + '/files' + manifest + ')"')
        else:
            machine.succeed('test ! -e ' + shlex.quote(path) + ' || test -z "$(ls -A ' + shlex.quote(path) + ')"')
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

with subtest('simultaneous real native SSO, public/private HTTP and synthetic automation'):
    # Both components remain active concurrently, not isolated passing closures.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        application = pool.submit(check_app)
        automation_checks = pool.submit(check_automation)
        results['checks']['application'] = application.result()
        assert automation_checks.result()
    for port in [80, 8123, 5000, 5001, 1984, 8554]:
        client.fail(f'nc -z -w 1 automation {port}')
    results['checks']['automation'] = True
    relay.succeed("curl --fail --silent --max-time 5 http://127.0.0.1:8080/health >/dev/null && headscale policy get | jq -e '.acls == []' >/dev/null")
    client.succeed('curl --fail --silent --max-time 10 https://relay/health >/dev/null')
    results['checks']['relayControlPlaneOnly'] = True
    results['services'] = json.loads(gateway.succeed('cat /etc/rehearsal-versions.json')) | {'web': json.loads(gateway.succeed('cat $(systemctl show terrace-web.service -p WorkingDirectory --value)/release.json'))['revision'],
                           'cms': json.loads(gateway.succeed('cat $(systemctl show terrace-cms.service -p WorkingDirectory --value)/release.json'))['revision'],
                           'k3s': gateway.succeed('k3s --version').splitlines()[0],
                           'headscale': relay.succeed('headscale version').splitlines()[0],
                           'postgresql': gateway.succeed('psql --version').strip()}
    results['peakBytes'] = {unit: int(gateway.succeed('systemctl show ' + unit + '.service -p MemoryPeak --value').strip()) for unit in ['keycloak', 'terrace-cms', 'terrace-web', 'postgresql', 'k3s']}
    results['automationPeakBytes'] = {unit: int(automation.succeed('systemctl show ' + unit + '.service -p MemoryPeak --value').strip()) for unit in ['home-assistant', 'frigate', 'go2rtc', 'nginx']}
    results['relayPeakBytes'] = {unit: int(relay.succeed('systemctl show ' + unit + '.service -p MemoryPeak --value').strip()) for unit in ['headscale', 'nginx']}
    results['postgresConnectionsAfterConcurrentChecks'] = int(sql('select count(*) from pg_stat_activity', 'postgres'))

with subtest('quiesce every writer before all encrypted snapshots'):
    deployment_replicas = quiesce_writers()
    signature = app_signature()
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
    timed('restore-application', lambda: fresh_restore(gateway, snapshots['gateway'], [GATEWAY_INPUTS, WEB_INPUTS, MEDIA, '/var/lib/rancher/k3s/server', '/var/lib/rancher/k3s/storage', '/etc/rancher/node']))
    # Adopt the reconciled, stopped PG copy into its DECLARED native service.
    # The module replaces postgresql.conf with its pinned native HBA/config;
    # the isolated trust configuration is never the active runtime configuration.
    pg_dir = gateway.succeed('cat /etc/rehearsal-pg-directory').strip()
    assert re.fullmatch('/var/lib/postgresql/[0-9]+', pg_dir)
    gateway.succeed('test ! -e ' + pg_dir + ' || test -z "$(ls -A ' + pg_dir + ')"')
    gateway.succeed('install -d -m 0700 -o postgres -g postgres ' + pg_dir + ' && cp -a ' + RESTORE + '/pgdata/. ' + pg_dir + '/ && chown -R postgres:postgres ' + pg_dir)
    gateway.succeed('systemctl start postgresql.target')
    # PG18 requires its bootstrap OID10 to remain superuser/catalog owner.
    # Retain no authentication or membership path to that internal identity.
    sql('alter role "terrace-restore" nologin password null', 'postgres')
    assert sql("select rolsuper and not rolcanlogin and rolpassword is null from pg_authid where oid=10 and rolname='terrace-restore'", 'postgres') == 't'
    assert sql('select count(*) from pg_auth_members where roleid=10 or member=10', 'postgres') == '0'
    assert sql("select count(*)=2 and bool_and(not pg_has_role(oid,10::oid,'MEMBER') and not pg_has_role(oid,10::oid,'SET')) from pg_roles where rolname in ('terrace_cms','keycloak')", 'postgres') == 't'
    assert sql("select count(*) from pg_stat_activity where usesysid=10 and backend_type='client backend'", 'postgres') == '0'
    gateway.fail('runuser -u terrace-restore -- psql -X -w -h /run/postgresql -U terrace-restore -d postgres -c \x27select 1\x27 >/dev/null 2>&1')
    cms_peer = 'runuser -u terrace_cms -- psql -X -w -v ON_ERROR_STOP=1 -h /run/postgresql -U terrace_cms -d terrace_cms'
    identity_password = 'PGPASSWORD="$(cat /var/lib/terrace-gateway-test/db-password)" psql -X -w -v ON_ERROR_STOP=1 -h 127.0.0.1 -U keycloak -d keycloak'
    for credentials in [cms_peer, identity_password]:
        gateway.succeed(credentials + ' -c \x27select 1\x27 >/dev/null 2>&1')
        gateway.fail(credentials + ' -c \x27set role "terrace-restore"\x27 >/dev/null 2>&1')
    results['checks']['bootstrapOwnerLoginAndAssumptionDenied'] = True
    assert sql('show hba_file', 'postgres').startswith('/nix/store/')
    assert sql('show fsync', 'postgres') == 'on'
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
    results['checks']['restoredApplication'] = timed('recoveredFunctionalApplication', lambda: check_app('-restored'))
    timed('restore-automation', lambda: fresh_restore(automation, snapshots['automation'], ['/var/lib/terrace-demo-hass', '/var/lib/terrace-demo-frigate', '/var/lib/rehearsal-demo-token']))
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
    results['checks']['restoredRelayControlPlaneOnly'] = True
    for machine in nodes:
        assert machine.succeed('cat /var/lib/unrelated-synthetic-sentinel').strip() == 'retained'

with subtest('verified recovery and final stop of every guest'):
    timed('finalQuiesceWriters', quiesce_writers)
    stop_units(gateway, ['postgresql'])
    for machine in [gateway, automation, relay, client]:
        assert machine.process is not None
        pid = machine.process.pid
        machine.execute('systemctl poweroff --no-block', check_return=False, check_output=False, timeout=10)
        machine.wait_for_shutdown(timeout=datetime.timedelta(seconds=90))
        assert not Path(f'/proc/{pid}').exists()
    results['checks']['allGuestsStopped'] = True
    results['timings']['scenarioTotal'] = round(time.monotonic() - started, 2)
    temporary = run / 'evidence' / 'scenario-result.json.new'
    temporary.write_text(json.dumps(results, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.rename(temporary.with_suffix(''))
    print('Established-service rehearsal passed: two bootstrap passes, native HTTPS SSO/privacy, synthetic automation, encrypted quiescent backup, original guests absent, fresh retained-PVC/app/relay recovery and all guests stopped.')
