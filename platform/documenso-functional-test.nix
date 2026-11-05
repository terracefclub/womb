{
  pkgs,
  contract,
}: let
  directory = "/var/lib/terrace-gateway-test";
  inputs = "/var/lib/terrace-documenso-inputs";
  fixture = pkgs.runCommand "documenso-functional-fixture" {} ''
    mkdir -p $out
    cp -R ${contract}/. $out/
    chmod -R u+w $out
    cp ${./documenso/functional-fixture.ts} $out/functional-fixture.ts
    cp ${./documenso/client.ts} $out/client.ts
    cp ${./documenso/inputs.ts} $out/inputs.ts
  '';
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
      systemd.services.documenso-functional-inputs = {
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
          cp ${certificate}/{cert,key}.pem ${directory}/
          chmod 0400 ${directory}/{cert,key}.pem
        '';
      };
      environment.systemPackages = [pkgs.curl pkgs.jq pkgs.python3 pkgs.postgresql_18];
      environment.etc."gateway-protocol.py".source = ./website-gateway/protocol.py;
      environment.etc."documenso-recovery.py".source = ./documenso/recovery-auth-test.py;
    };
    client = {nodes, ...}: {
      virtualisation = {
        memorySize = 2048;
        cores = 1;
      };
      security.pki.certificateFiles = ["${certificate}/cert.pem"];
      networking.hosts.${nodes.documents.networking.primaryIPAddress} = ["documents.terrace.invalid" "identity.terrace.invalid"];
      environment.systemPackages = [pkgs.curl pkgs.nssTools (pkgs.python3.withPackages (ps: [ps.playwright]))];
      environment.variables.TERRACE_BROWSER_BINARY = "${pkgs.chromium}/bin/chromium";
      environment.etc."documenso-functional-browser.py".source = ./documenso/functional-browser.py;
    };
  };
  testScript = ''
    import shlex, tempfile
    from pathlib import Path
    start_all()
    documents.wait_for_unit("keycloak.service", timeout=300)
    documents.wait_for_unit("nginx.service")
    def sql(query):
        return documents.succeed("runuser -u postgres -- psql -XAt -d terrace_documenso -c " + shlex.quote(query) + " 2>/dev/null").strip()
    def start():
        documents.succeed("systemctl reset-failed terrace-documenso.service && systemctl start terrace-documenso.service", timeout=180)
        documents.wait_until_succeeds("curl --fail --silent --max-time 5 http://127.0.0.1:13050/api/health | jq -e '.checks.database.status == \"ok\"' >/dev/null", timeout=90)
    def stop():
        documents.succeed("systemctl stop terrace-documenso.service")
        assert documents.succeed("systemctl show terrace-documenso.service -p ExecMainStatus --value").strip() in ["0", "143"]
        assert documents.succeed("systemctl show terrace-documenso.service -p ActiveState --value").strip() == "inactive"
    def browser(phase):
        try:
            assert client.succeed("python /etc/documenso-functional-browser.py " + phase, timeout=240).strip() == "Documenso functional browser phase " + phase + " passed."
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
                for name in ["failure-" + phase + "-labels.json", "failure-" + phase + ".png"]:
                    client.execute("test ! -f /root/documenso-functional/" + name + " || cp /root/documenso-functional/" + name + " /tmp/shared/" + private.name + "/", timeout=10)
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
        assert sql('SELECT count(*) FROM "User" WHERE source=\'terrace-approved-bootstrap-v1\' ') == "4"
        client.succeed("install -d -m 0700 /root/documenso-functional /root/.pki/nssdb && certutil -N -d sql:/root/.pki/nssdb --empty-password && certutil -A -d sql:/root/.pki/nssdb -n TerraceDocumensoTest -t 'C,,' -i ${certificate}/cert.pem")
        assert not str(documents.shared_dir).startswith("/nix/store/")
        with tempfile.TemporaryDirectory(prefix="documenso-private-", dir=documents.shared_dir) as private:
            name = private.rsplit("/", 1)[-1]
            documents.succeed(f"cp ${directory}/documenso-browser.json /tmp/shared/{name}/credentials.json")
            client.succeed(f"cp /tmp/shared/{name}/credentials.json /root/documenso-functional/credentials.json")
    with subtest("real approved and denied OIDC, encrypted data and native unsigned PDF"):
        browser("create")
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
    documents.succeed("systemctl stop nginx.service postgresql.target postgresql.service")
    assert documents.succeed("podman ps -q").strip() == ""
    for unit in ["terrace-documenso", "keycloak", "nginx", "postgresql"]:
        documents.fail(f"systemctl is-active --quiet {unit}.service")
    documents.fail("ss -H -lnt | grep -Eq ':(443|5432|8088|9000) '")
    documents.fail("curl --fail --silent --max-time 2 http://127.0.0.1:13050/api/health >/dev/null")
    client.succeed("rm /root/documenso-functional/{credentials,receipt}.json")
  '';
}
