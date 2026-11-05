{
  pkgs,
  contract,
}: let
  directory = "/var/lib/terrace-gateway-test";
  inputs = "/var/lib/terrace-documenso-inputs";
  fixture = import ./documenso/fixture-package.nix {inherit pkgs contract;};
  certificate = pkgs.runCommand "documenso-functional-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir -p $out
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout $out/key.pem -out $out/cert.pem -subj '/CN=documents.terrace.invalid' \
      -addext 'subjectAltName=DNS:documents.terrace.invalid,DNS:identity.terrace.invalid' 2>/dev/null
  '';
in {
  name = "terrace-documenso-functional";
  nodes = {
    documents = {lib, ...}: {
      imports = [./documenso.nix ./identity.nix];
      virtualisation = {
        memorySize = 6144;
        cores = 2;
        diskSize = 12288;
      };
      security.pki.certificateFiles = ["${certificate}/cert.pem"];
      networking.hosts."127.0.0.1" = ["documents.terrace.invalid" "identity.terrace.invalid"];
      terrace.documenso = {
        enable = true;
        enableSigning = true;
        hostname = "documents.terrace.invalid";
        identityHostname = "identity.terrace.invalid";
        certificateFile = "${directory}/cert.pem";
        certificateKeyFile = "${directory}/key.pem";
        identityCaFile = "${directory}/cert.pem";
      };
      terrace.identity = {
        enable = true;
        hostname = "identity.terrace.invalid";
        realmFile = "${directory}/terrace-realm.json";
        databasePasswordFile = "${directory}/db-password";
        certificateFile = "${directory}/cert.pem";
        certificateKeyFile = "${directory}/key.pem";
      };
      # Test-only bootstrap exists only on the private interface and is removed
      # after enrolling synthetic people; production input remains user-free.
      systemd.services.keycloak.serviceConfig.EnvironmentFile = "${directory}/keycloak.env";
      systemd.services.terrace-documenso.wantedBy = lib.mkForce [];
      # Test-only bounded private errors; the runtime module keeps logging off.
      virtualisation.oci-containers.containers.terrace-documenso = {
        log-driver = lib.mkForce "k8s-file";
        extraOptions = lib.mkAfter [
          "--log-opt=path=/var/lib/documenso-test-evidence/app.log"
          "--log-opt=max-size=4194304"
        ];
      };
      systemd.services.documenso-functional-inputs = {
        path = [pkgs.openssl];
        requiredBy = ["terrace-documenso-inputs.service" "keycloak.service" "keycloakPostgreSQLInit.service" "nginx.service"];
        before = ["terrace-documenso-inputs.service" "keycloak.service" "keycloakPostgreSQLInit.service" "nginx.service"];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          UMask = "0077";
          StandardOutput = "null";
          StandardError = "null";
        };
        script = ''
          ${pkgs.bun}/bin/bun ${fixture}/functional-fixture.ts
          install -d -m 0700 /var/lib/documenso-test-evidence
          install -m 0600 /dev/null /var/lib/documenso-test-evidence/app.log
          cp ${certificate}/{cert,key}.pem ${directory}/
          chmod 0400 ${directory}/{cert,key}.pem
        '';
      };
      # Disposable capture only: no relay/forward configuration, no provider.
      # The native app reaches the host through the private Podman bridge.
      systemd.services.documenso-test-mailpit = {
        serviceConfig = {
          ExecStart = "${pkgs.mailpit}/bin/mailpit --smtp 10.88.0.1:1025 --listen 127.0.0.1:8025 --database /var/lib/documenso-test-mailpit/mail.db --max 0 --disable-version-check --smtp-disable-rdns --smtp-allowed-recipients ^local-member@terrace[.]invalid$ --smtp-max-recipients 1 --quiet";
          StateDirectory = "documenso-test-mailpit";
          StateDirectoryMode = "0700";
          UMask = "0077";
          MemoryMax = "128M";
          CPUQuota = "100%";
          TasksMax = 64;
          TimeoutStartSec = 30;
          TimeoutStopSec = 10;
          NoNewPrivileges = true;
          StandardOutput = "null";
          StandardError = "null";
        };
      };
      environment.systemPackages = [pkgs.curl pkgs.jq pkgs.python3 pkgs.postgresql_18];
      environment.etc."gateway-protocol.py".source = ./website-gateway/protocol.py;
      environment.etc."documenso-recovery.py".source = ./documenso/recovery-auth-test.py;
      environment.etc."documenso-signing-proof.py".source = ./documenso/signing-proof.py;
      environment.etc."documenso-smtp-probe.cjs".source = ./documenso/smtp-probe.cjs;
    };
    client = {nodes, ...}: {
      virtualisation = {
        memorySize = 2048;
        cores = 1;
      };
      security.pki.certificateFiles = ["${certificate}/cert.pem"];
      networking.hosts.${nodes.documents.networking.primaryIPAddress} = ["documents.terrace.invalid" "identity.terrace.invalid"];
      environment.systemPackages = [pkgs.curl pkgs.netcat-openbsd pkgs.nssTools pkgs.openssl (pkgs.python3.withPackages (ps: [ps.playwright]))];
      environment.variables.TERRACE_BROWSER_BINARY = "${pkgs.chromium}/bin/chromium";
      environment.etc."documenso-functional-browser.py".source = ./documenso/functional-browser.py;
      environment.etc."documenso-signing-proof.py".source = ./documenso/signing-proof.py;
    };
  };
  testScript = ''
    import shlex, tempfile
    from pathlib import Path
    start_all()
    documents.wait_for_unit("keycloak.service", timeout=300)
    documents.wait_for_unit("nginx.service")
    def sql(query):
        return documents.succeed("runuser -u postgres -- psql -w -XAt -d terrace_documenso -c " + shlex.quote(query) + " 2>/dev/null", timeout=10).strip()
    def server_failure_evidence():
        try:
            for state in ["PENDING", "PROCESSING", "COMPLETED", "FAILED"]:
                count = sql(f"SELECT count(*) FROM \"BackgroundJob\" WHERE \"jobId\"='send.signing.requested.email' AND status='{state}'")
                assert count.isascii() and count.isdigit() and len(count) <= 10
                print("Synthetic signing-email job " + state + " count:", int(count))
            for task in ["send-signing-email", "update-recipient"]:
                for state in ["PENDING", "COMPLETED", "FAILED"]:
                    count = sql(f"SELECT count(*) FROM \"BackgroundJobTask\" WHERE name='{task}' AND status='{state}' AND \"jobId\" IN (SELECT id FROM \"BackgroundJob\" WHERE \"jobId\"='send.signing.requested.email')")
                    assert count.isascii() and count.isdigit() and len(count) <= 10
                    print("Synthetic signing-email task " + task + " " + state + " count:", int(count))
            assert documents.succeed("python /etc/documenso-signing-proof.py evidence", timeout=10).strip() == "Private bounded signing runtime evidence retained."
            assert not str(documents.shared_dir).startswith("/nix/store/")
            private = Path(documents.shared_dir) / "documenso-server-failure"
            private.mkdir(mode=0o700)
            documents.succeed("cp --preserve=mode /var/lib/documenso-test-evidence/runtime-copy.log /tmp/shared/" + private.name + "/runtime-copy.log", timeout=10)
            artifact = private / "runtime-copy.log"
            assert artifact.is_file() and not artifact.is_symlink() and artifact.stat().st_mode & 0o777 == 0o600
            assert artifact.stat().st_size <= 4 * 1024 * 1024
            print("Private bounded signing runtime log exported; contents suppressed.")
        except Exception:
            print("Private signing server evidence unavailable; original failure preserved.")
    def start():
        documents.succeed("systemctl reset-failed terrace-documenso.service && systemctl start terrace-documenso.service", timeout=180)
        documents.wait_until_succeeds("curl --fail --silent --max-time 5 http://127.0.0.1:13050/api/health | jq -e '.checks.database.status == \"ok\"' >/dev/null", timeout=90)
    def stop():
        documents.succeed("systemctl stop terrace-documenso.service")
        assert documents.succeed("systemctl show terrace-documenso.service -p ExecMainStatus --value").strip() in ["0", "143"]
        assert documents.succeed("systemctl show terrace-documenso.service -p ActiveState --value").strip() == "inactive"
    def browser(phase):
        try:
            assert client.succeed("python /etc/documenso-functional-browser.py " + phase + (" --signing" if phase in ["create", "verify"] else ""), timeout=240).strip() == "Documenso functional browser phase " + phase + " passed."
        except Exception:
            try:
                for label, query in [("all team rows", 'SELECT count(*) FROM "Team"'), ("expected team rows", 'SELECT count(*) FROM "Team" WHERE url=\'rehearsal-team\' ')]:
                    count = sql(query)
                    assert count.isdigit()
                    print("Synthetic Documenso " + label + ": " + count)
                # --keep-failed retains this temporary build directory. Never use
                # copy_from_vm, which writes into the publicly substitutable $out.
                assert not str(client.shared_dir).startswith("/nix/store/")
                private = Path(client.shared_dir) / ("documenso-failure-" + phase)
                private.mkdir(mode=0o700)
                for name in ["failure-" + phase + "-labels.json", "failure-" + phase + ".png", "signed.pdf", "signing.crt"]:
                    client.execute("test ! -f /root/documenso-functional/" + name + " || (cp /root/documenso-functional/" + name + " /tmp/shared/" + private.name + "/ && chmod 0600 /tmp/shared/" + private.name + "/" + name + ")", timeout=10)
                artifacts = list(private.iterdir())
                assert all(path.is_file() and path.stat().st_mode & 0o777 == 0o600 for path in artifacts)
                if artifacts:
                    print("Private Documenso failure artifacts retained in failed-build shared directory; content suppressed.")
                else:
                    print("Private Documenso failure artifacts unavailable; no raw diagnostics emitted.")
            except Exception:
                print("Private Documenso failure evidence unavailable; original failure preserved.")
            raise
    with subtest("reviewed user-free realm, explicit provisioning and real HTTPS"):
        documents.succeed("python /etc/gateway-protocol.py seed", timeout=120)
        assert documents.succeed("runuser -u postgres -- psql -XAt -d keycloak -c \"select count(*) from user_entity where username='gateway-fixture-bootstrap'\" 2>/dev/null").strip() == "0"
        assert documents.succeed("runuser -u postgres -- psql -XAt -d keycloak -c \"select count(*) from user_entity where email='recovery@terrace.invalid'\" 2>/dev/null").strip() == "0"
        documents.succeed("chmod 0600 ${directory}/keycloak.env && truncate -s 0 ${directory}/keycloak.env && chmod 0400 ${directory}/keycloak.env")
        documents.succeed("systemctl start terrace-documenso-migrate.service && systemctl start terrace-documenso-bootstrap.service", timeout=240)
        start()
        assert documents.succeed("podman network inspect podman | jq -er '.[0].subnets[0].gateway'").strip() == "10.88.0.1"
        documents.succeed("systemctl start documenso-test-mailpit.service")
        documents.wait_until_succeeds("curl --fail --silent --max-time 2 http://127.0.0.1:8025/api/v1/info >/dev/null", timeout=15)
        try:
            assert documents.succeed("podman exec --interactive terrace-documenso node - </etc/documenso-smtp-probe.cjs", timeout=12).strip() == "Native container SMTP alias, protocol and image mailer verified without sending mail."
        except Exception:
            server_failure_evidence()
            raise
        assert sql('SELECT count(*) FROM "User" WHERE source=\'terrace-approved-bootstrap-v1\' ') == "4"
        client.succeed("install -d -m 0700 /root/documenso-functional /root/.pki/nssdb && certutil -N -d sql:/root/.pki/nssdb --empty-password && certutil -A -d sql:/root/.pki/nssdb -n TerraceDocumensoTest -t 'C,,' -i ${certificate}/cert.pem")
        assert not str(documents.shared_dir).startswith("/nix/store/")
        with tempfile.TemporaryDirectory(prefix="documenso-private-", dir=documents.shared_dir) as private:
            name = private.rsplit("/", 1)[-1]
            documents.succeed(f"cp ${directory}/documenso-browser.json /tmp/shared/{name}/credentials.json")
            client.succeed(f"cp /tmp/shared/{name}/credentials.json /root/documenso-functional/credentials.json")
            documents.succeed(f"cp --preserve=mode ${inputs}/signing.crt /tmp/shared/{name}/signing.crt")
            client.succeed(f"cp --preserve=mode /tmp/shared/{name}/signing.crt /root/documenso-functional/signing.crt")
        client.fail("nc -z -w 2 documents 1025")
        client.fail("nc -z -w 2 documents 8025")
    with subtest("real approved and denied OIDC, encrypted data and native unsigned PDF"):
        browser("create")
        try:
            assert documents.succeed("python /etc/documenso-signing-proof.py mail", timeout=90).strip() == "Synthetic signing request captured in the local mail sink."
            documents.wait_until_succeeds("runuser -u postgres -- psql -w -XAt -d terrace_documenso -c \"SELECT count(*) FROM \\\"BackgroundJob\\\" WHERE \\\"jobId\\\"='send.signing.requested.email' AND status='COMPLETED'\" 2>/dev/null | grep -qx 1", timeout=10)
            assert sql('SELECT count(*) FROM "BackgroundJob" WHERE "jobId"=\'send.signing.requested.email\' ') == "1"
        except Exception:
            server_failure_evidence()
            raise
        with tempfile.TemporaryDirectory(prefix="documenso-recipient-", dir=documents.shared_dir) as private:
            name = private.rsplit("/", 1)[-1]
            documents.succeed(f"cp --preserve=mode /var/lib/terrace-documenso-mail-test/recipient.json /tmp/shared/{name}/recipient.json")
            client.succeed(f"cp --preserve=mode /tmp/shared/{name}/recipient.json /root/documenso-functional/recipient.json")
        browser("sign")
        assert sql('SELECT count(*) FROM "Envelope" WHERE status=\'COMPLETED\' ') == "1"
        assert sql('SELECT count(*) FROM "User" WHERE email IN (\'local-unapproved@terrace.invalid\',\'local-editorial-editor@terrace.invalid\')') == "0"
        assert sql('SELECT count(*) FROM "User" WHERE "twoFactorEnabled" AND length("twoFactorSecret") > 80 AND length("twoFactorBackupCodes") > 80') == "1"
        before = sql('SELECT md5(string_agg(row_to_json(u)::text,\'\' ORDER BY id)) FROM "User" u')
        stop()
        documents.succeed("systemctl stop keycloak.service && systemctl restart postgresql.service && systemctl start keycloak.service && systemctl start terrace-documenso-bootstrap.service", timeout=300)
        assert documents.succeed("runuser -u postgres -- psql -XAt -d keycloak -c \"select count(*) from user_entity where username='gateway-fixture-bootstrap'\" 2>/dev/null").strip() == "0"
        assert sql('SELECT md5(string_agg(row_to_json(u)::text,\'\' ORDER BY id)) FROM "User" u') == before
        start()
        browser("verify")
    with subtest("scoped offboarding rejects live session and fresh OIDC"):
        stop()
        documents.succeed("jq '.approvedUsers |= map(select(.email != \"local-alumni@terrace.invalid\"))' ${inputs}/secrets.json 2>/dev/null >${inputs}/next.json && chmod 0400 ${inputs}/next.json && mv ${inputs}/next.json ${inputs}/secrets.json && systemctl restart terrace-documenso-inputs.service && systemctl start terrace-documenso-bootstrap.service", timeout=180)
        assert sql('SELECT count(*) FROM "User" WHERE email=\'local-alumni@terrace.invalid\' AND disabled') == "1"
        assert sql('SELECT count(*) FROM "Session" WHERE "userId"=(SELECT id FROM "User" WHERE email=\'local-alumni@terrace.invalid\')') == "0"
        start()
        browser("disabled")
        browser("verify")
    with subtest("identity-independent local recovery and public password denial"):
        documents.succeed("systemctl stop terrace-identity-health.timer terrace-identity-health.service keycloak.service")
        documents.fail("systemctl is-active --quiet keycloak.service")
        assert documents.succeed("python /etc/documenso-recovery.py").strip() == "Native loopback recovery administrator authenticated without identity."
        assert client.succeed("curl --silent --max-time 5 -X POST -o /dev/null -w '%{http_code}' https://documents.terrace.invalid/api/auth/email-password/authorize").strip() == "403"
    stop()
    documents.succeed("systemctl stop documenso-test-mailpit.service nginx.service postgresql.target postgresql.service")
    assert documents.succeed("podman ps -q").strip() == ""
    for unit in ["terrace-documenso", "keycloak", "nginx", "postgresql"]:
        documents.fail(f"systemctl is-active --quiet {unit}.service")
    documents.fail("ss -H -lnt | grep -Eq ':(443|5432|8088|9000|1025|8025) '")
    documents.fail("curl --fail --silent --max-time 2 http://127.0.0.1:13050/api/health >/dev/null")
    client.succeed("rm /root/documenso-functional/{credentials,receipt}.json")
  '';
}
