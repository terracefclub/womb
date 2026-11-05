# Private raw diagnostics survive failed test teardown; no inference from
# absent filtered labels. The imported capture primitive is already fault-tested.
try:
    start_all()
    server.wait_for_unit("postgresql.service")
    server.succeed("install -d -m 0700 /var/lib/vaultwarden-test && cp /etc/vaultwarden-test-certificate/* /var/lib/vaultwarden-test/ && bun --no-env-file /etc/vaultwarden-test-fixture.ts")
    server.succeed("systemctl start nginx.service")
    def sql(query):
        import shlex
        return server.succeed("runuser -u postgres -- psql -XAtw -v ON_ERROR_STOP=1 -d terrace_vaultwarden -c " + shlex.quote(query)).strip()
    def status():
        return client.succeed("curl --silent --output /dev/null --write-out '%{http_code}' https://vault.terrace.invalid/alive").strip()
    with subtest("closed before explicit native migrations and private bootstrap"):
        assert status() == "503"
        server.fail("systemctl start vaultwarden.service")
        assert sql("SELECT count(*) FROM pg_tables WHERE schemaname='public'") == "0"
        assert status() == "503"
        server.succeed("systemctl reset-failed vaultwarden.service terrace-vaultwarden-inputs.service")
        server.succeed("systemd-run --unit=vaultwarden-test-sentinel --property=RuntimeMaxSec=120 --property=Type=exec sleep 120")
        sentinel = server.succeed("systemctl show vaultwarden-test-sentinel.service -p MainPID --value").strip()
        assert sentinel.isdigit() and int(sentinel) > 0
        server.succeed("install -d /run/systemd/system/terrace-vaultwarden-migration-server.service.d && printf '[Service]\\nType=oneshot\\nExecStart=\\nExecStart=/run/current-system/sw/bin/false\\n' >/run/systemd/system/terrace-vaultwarden-migration-server.service.d/failure.conf && systemctl daemon-reload")
        server.fail("systemctl start terrace-vaultwarden-migrate.service", timeout=60)
        server.succeed("test ! -e /var/lib/terrace-vaultwarden/migrated-release && jq -e '.original.ExecMainStatus == \"1\" and .final.ActiveState == \"inactive\"' /run/terrace-vaultwarden-inputs/migration-stop.json >/dev/null")
        assert server.succeed("systemctl show vaultwarden-test-sentinel.service -p MainPID --value").strip() == sentinel
        server.succeed("systemctl is-active --quiet vaultwarden-test-sentinel.service && systemctl stop vaultwarden-test-sentinel.service")
        server.succeed("rm /run/systemd/system/terrace-vaultwarden-migration-server.service.d/failure.conf && rmdir /run/systemd/system/terrace-vaultwarden-migration-server.service.d && systemctl daemon-reload && systemctl reset-failed terrace-vaultwarden-migrate.service")
        server.succeed("systemctl start terrace-vaultwarden-migrate.service", timeout=180)
        assert sql("SELECT count(*) FROM __diesel_schema_migrations") == "48"
        assert int(sql("SELECT count(*) FROM pg_tables WHERE schemaname='public'")) > 20
        assert status() == "503"
        client.fail("nc -z -w 1 server 18222")
        client.fail("nc -z -w 1 server 18224")
    with subtest("native start, idempotent approved-account bootstrap and private admin"):
        server.succeed("systemctl start vaultwarden.service && systemctl start terrace-vaultwarden-bootstrap.service", timeout=180)
        assert status() == "200"
        server.succeed("systemctl start terrace-vaultwarden-bootstrap.service")
        server.succeed("systemctl start terrace-vaultwarden-bootstrap.service")
        assert sql("SELECT count(*) FROM users WHERE enabled") == "1"
        assert sql("SELECT count(*) FROM invitations") == "1"
        for property in ['PrivateUsers', 'MemoryDenyWriteExecute', 'NoNewPrivileges', 'PrivateDevices']:
            assert server.succeed('systemctl show vaultwarden.service -p ' + property + ' --value').strip() == 'yes'
        assert server.succeed('systemctl show vaultwarden.service -p ProtectSystem --value').strip() == 'strict'
        assert server.succeed('systemctl show vaultwarden.service -p User --value').strip() == 'terrace_vaultwarden'
        assert client.succeed("curl --silent --output /dev/null --write-out '%{http_code}' https://vault.terrace.invalid/admin/").strip() == "403"
        client.succeed("curl --fail --silent --head https://vault.terrace.invalid/ | grep -qi 'cache-control: private, no-store'")
        server.fail("runuser -u nobody -- cat /var/lib/terrace-vaultwarden-inputs/input.json")
        server.fail("runuser -u terrace_vaultwarden -- touch /run/terrace-vaultwarden-state/ready")
        server.fail("runuser -u terrace_vaultwarden -- curl --silent --connect-timeout 1 --max-time 2 https://192.0.2.1/")
        assert sql("SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolbypassrls AND rolconnlimit=6 FROM pg_roles WHERE rolname='terrace_vaultwarden'") == "t"
        signature = server.succeed("sha256sum /var/lib/vaultwarden/rsa_key.pem").strip()
        account = sql("SELECT uuid FROM users WHERE email='approved@terrace.invalid'")
    with subtest("changed policy fails closed until stopped snapshot rotation"):
        server.succeed("bun --no-env-file /etc/vaultwarden-test-fixture.ts rotate")
        server.fail("systemctl start terrace-vaultwarden-bootstrap.service")
        assert status() == "503"
        server.succeed("systemctl stop vaultwarden.service && systemctl restart terrace-vaultwarden-inputs.service && systemctl reset-failed terrace-vaultwarden-bootstrap.service")
        server.succeed("systemctl start vaultwarden.service && systemctl start terrace-vaultwarden-bootstrap.service", timeout=180)
        assert status() == "200"
        assert sql("SELECT count(*) FROM users") == "2"
        assert sql("SELECT count(*) FROM users WHERE enabled") == "1"
        assert sql("SELECT enabled FROM users WHERE email='approved@terrace.invalid'") == "f"
        assert account == sql("SELECT uuid FROM users WHERE email='approved@terrace.invalid'")
        assert server.succeed("sha256sum /var/lib/vaultwarden/rsa_key.pem").strip() == signature
    with subtest("re-enable retains the same native account and RSA key"):
        server.succeed("systemctl stop vaultwarden.service && bun --no-env-file /etc/vaultwarden-test-fixture.ts restore && systemctl restart terrace-vaultwarden-inputs.service && systemctl start vaultwarden.service && systemctl start terrace-vaultwarden-bootstrap.service", timeout=180)
        assert status() == "200"
        assert sql("SELECT enabled FROM users WHERE email='approved@terrace.invalid'") == "t"
        assert sql("SELECT count(*) FROM users") == "2"
        assert account == sql("SELECT uuid FROM users WHERE email='approved@terrace.invalid'")
        assert server.succeed("sha256sum /var/lib/vaultwarden/rsa_key.pem").strip() == signature
    with subtest("nonregular and broadly readable private input fails promptly"):
        server.succeed("systemctl stop vaultwarden.service && mv /var/lib/terrace-vaultwarden-inputs/input.json /var/lib/terrace-vaultwarden-inputs/valid.json && mkfifo -m 0400 /var/lib/terrace-vaultwarden-inputs/input.json")
        result, _private = server.execute("timeout 10 systemctl restart terrace-vaultwarden-inputs.service", timeout=15)
        assert result == 1
        assert status() == "503"
        server.succeed("rm /var/lib/terrace-vaultwarden-inputs/input.json && mv /var/lib/terrace-vaultwarden-inputs/valid.json /var/lib/terrace-vaultwarden-inputs/input.json && chmod 0600 /var/lib/terrace-vaultwarden-inputs/input.json")
        result, _private = server.execute("timeout 10 systemctl restart terrace-vaultwarden-inputs.service", timeout=15)
        assert result == 1
        assert status() == "503"
        server.succeed("chmod 0400 /var/lib/terrace-vaultwarden-inputs/input.json && systemctl reset-failed terrace-vaultwarden-inputs.service && systemctl restart terrace-vaultwarden-inputs.service && systemctl start vaultwarden.service && systemctl start terrace-vaultwarden-bootstrap.service", timeout=180)
        assert status() == "200"
    with subtest("stop retains native state and ledger; restart is a no-op migration"):
        ledger = sql("SELECT version FROM __diesel_schema_migrations ORDER BY version")
        server.succeed("systemctl stop vaultwarden.service")
        assert status() == "503"
        assert sql("SELECT count(*) FROM users") == "2"
        server.succeed("test -s /var/lib/vaultwarden/rsa_key.pem && test -s /var/lib/terrace-vaultwarden-inputs/input.json")
        server.succeed("systemctl start vaultwarden.service && systemctl start terrace-vaultwarden-bootstrap.service", timeout=180)
        assert status() == "200"
        assert sql("SELECT version FROM __diesel_schema_migrations ORDER BY version") == ledger
        assert server.succeed("sha256sum /var/lib/vaultwarden/rsa_key.pem").strip() == signature
        server.succeed("systemctl stop vaultwarden.service nginx.service postgresql.target")
        assert server.succeed("systemctl show vaultwarden.service -p MainPID --value").strip() == "0"
        server.fail("ss -lnt | grep -E ':(18222|18224) '")

except Exception:
    try:
        import os
        from pathlib import Path
        import tempfile
        server.succeed('python3 /etc/vaultwarden-test-capture/vaultwarden-capture.py', timeout=40)
        with tempfile.TemporaryDirectory(prefix='vaultwarden-private-', dir=server.shared_dir) as temporary:
            name = Path(temporary).name
            server.succeed('cp /var/lib/vaultwarden-test-evidence/raw.json /tmp/shared/' + name + '/raw.json && chmod 0600 /tmp/shared/' + name + '/raw.json', timeout=10)
            destination = Path(server.shared_dir).parent / 'vaultwarden-private-raw.json'
            assert not destination.exists()
            os.rename(Path(temporary) / 'raw.json', destination)
            os.chmod(destination, 0o600)
        print('Protected Vaultwarden raw evidence retained outside the result; contents suppressed.')
    except Exception:
        print('Private Vaultwarden export unavailable; original failure preserved.')
    raise
