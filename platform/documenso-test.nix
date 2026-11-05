{
  pkgs,
  module,
}: let
  inputs = "/var/lib/terrace-documenso-inputs";
  certificate = pkgs.runCommand "documenso-synthetic-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir -p $out
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout $out/key.pem -out $out/cert.pem -subj '/CN=documents.terrace.invalid' \
      -addext 'subjectAltName=DNS:documents.terrace.invalid' 2>/dev/null
  '';
  prepare = pkgs.writeText "documenso-synthetic-inputs.py" ''
    import json, os, secrets
    destination = "${inputs}"
    os.makedirs(destination, mode=0o700, exist_ok=True)
    value = {key: secrets.token_urlsafe(40) for key in ["authSecret", "encryptionKey", "encryptionSecondaryKey", "oidcClientSecret", "databasePassword"]}
    value["recoveryUser"] = {"email": "recovery@terrace.invalid", "name": "Synthetic Recovery", "password": secrets.token_urlsafe(40)}
    value["approvedUsers"] = [{"email": "approved@terrace.invalid", "name": "Synthetic Approved", "role": "USER"}]
    with open(destination + "/secrets.json", "x") as output: json.dump(value, output)
    os.chmod(destination + "/secrets.json", 0o400)
  '';
in {
  name = "terrace-documenso";
  nodes = {
    documents = {lib, ...}: {
      imports = [module];
      virtualisation = {
        memorySize = 4096;
        cores = 2;
        diskSize = 12288;
      };
      terrace.documenso = {
        enable = true;
        hostname = "documents.terrace.invalid";
        identityHostname = "identity.terrace.invalid";
        certificateFile = "${inputs}/cert.pem";
        certificateKeyFile = "${inputs}/key.pem";
        identityCaFile = "${inputs}/cert.pem";
      };
      # Initialization is deliberately explicit: never migrate on app start.
      systemd.services.terrace-documenso.wantedBy = lib.mkForce [];
      virtualisation.oci-containers.containers.terrace-documenso = {
        volumes = lib.mkAfter ["${./documenso/admin-loader-test.cjs}:/run/admin-loader-test.cjs:ro"];
        extraOptions = lib.mkAfter ["--add-host=documents.terrace.invalid:host-gateway"];
      };
      systemd.services.documenso-test-inputs = {
        requiredBy = ["terrace-documenso-inputs.service" "nginx.service"];
        before = ["terrace-documenso-inputs.service" "nginx.service"];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          UMask = "0077";
          StandardOutput = "null";
          StandardError = "null";
        };
        script = ''
          ${pkgs.python3}/bin/python ${prepare}
          cp ${certificate}/{cert,key}.pem ${inputs}/
          chmod 0600 ${inputs}/*.pem
        '';
      };
      environment.systemPackages = [pkgs.curl pkgs.jq pkgs.python3 pkgs.iproute2];
      environment.etc."documenso-reserved-auth-test.py".source = ./documenso/reserved-auth-test.py;
      networking.firewall.allowedTCPPorts = [13050 5432];
    };
    client = {nodes, ...}: {
      networking.hosts.${nodes.documents.networking.primaryIPAddress} = ["documents.terrace.invalid"];
      imports = [module];
      environment.systemPackages = [pkgs.curl pkgs.netcat-openbsd];
      security.pki.certificateFiles = ["${certificate}/cert.pem"];
    };
  };
  testScript = ''
    import shlex
    start_all()
    documents.wait_for_unit("postgresql.service")
    documents.wait_for_unit("nginx.service", timeout=30)
    def sql(query):
        return documents.succeed("runuser -u postgres -- psql -X -A -t -d terrace_documenso -c " + shlex.quote(query)).strip()
    def start():
        documents.succeed("systemctl reset-failed terrace-documenso.service && systemctl start terrace-documenso.service", timeout=180)
        documents.wait_until_succeeds("curl --fail --silent --max-time 5 http://127.0.0.1:13050/api/health | jq -e '.checks.database.status == \"ok\"' >/dev/null", timeout=90)
    def stop():
        documents.succeed("systemctl stop terrace-documenso.service")
        assert documents.succeed("systemctl show terrace-documenso.service -p ExecMainStatus --value").strip() in ["0", "143"]
        assert documents.succeed("systemctl show terrace-documenso.service -p ActiveState --value").strip() == "inactive"
    def mutate(expression):
        documents.succeed("chmod 0600 ${inputs}/secrets.json && jq " + shlex.quote(expression) + " ${inputs}/secrets.json >${inputs}/next.json && chmod 0400 ${inputs}/next.json && mv ${inputs}/next.json ${inputs}/secrets.json")
    with subtest("no implicit migrations or bootstrap"):
        documents.fail("systemctl start terrace-documenso.service")
        documents.fail("test -f /var/lib/terrace-documenso/bootstrapped-image")
        documents.succeed("systemctl start terrace-documenso-image.service", timeout=180)
        # Credential-free runtime probe gives useful OCI diagnostics without
        # handing error-reporting code any private environment values.
        documents.succeed("podman run --rm --name terrace-documenso-probe --pull=never --network=none --user 1001:1001 --cap-drop=ALL --security-opt=no-new-privileges --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m --memory=1g --cpus=1 --pids-limit=128 --log-driver=none --volume /run/postgresql:/run/postgresql:ro --volume /run/terrace-documenso-inputs/credentials:/run/terrace-inputs:ro --workdir /app/apps/remix --entrypoint node ghcr.io/documenso/documenso:v2.21.1 --version", timeout=60)
        documents.succeed("systemctl start terrace-documenso-migrate.service", timeout=180)
        documents.fail("systemctl start terrace-documenso.service")
        documents.succeed("systemctl start terrace-documenso-bootstrap.service", timeout=180)
        start()
        assert sql('SELECT count(*) FROM "User"') == "4"
        assert sql('SELECT count(*) FROM "User" WHERE source=\'terrace-approved-bootstrap-v1\' ') == "2"
        assert sql('SELECT count(*) FROM "User" WHERE email NOT IN (\'serviceaccount@documents.terrace.invalid\',\'deleted-account@documents.terrace.invalid\',\'approved@terrace.invalid\',\'recovery@terrace.invalid\')') == "0"
        assert sql('SELECT count(*) FROM "User" WHERE email IN (\'serviceaccount@documents.terrace.invalid\',\'deleted-account@documents.terrace.invalid\') AND password IS NULL') == "2"
        documents.succeed("python /etc/documenso-reserved-auth-test.py")
        assert sql('SELECT count(*) FROM "User" WHERE password IS NOT NULL AND roles=ARRAY[\'ADMIN\']::"Role"[]') == "1"
        assert sql('SELECT count(*) FROM "User" WHERE source=\'terrace-approved-bootstrap-v1\' AND password IS NULL AND roles=ARRAY[\'USER\']::"Role"[]') == "1"
        assert sql("SELECT rolconnlimit FROM pg_roles WHERE rolname='terrace_documenso'") == "6"
        documents.succeed("test $(stat -c %a /run/terrace-documenso-inputs/runtime.env) = 600 && test $(stat -c %a ${inputs}/secrets.json) = 400")
        documents.fail("runuser -u nobody -- test -r /run/terrace-documenso-inputs/runtime.env")
        documents.succeed("podman exec terrace-documenso node -e 'require(\"node:fs\").accessSync(\"/run/terrace-oidc-ca.pem\", 4)'")
    with subtest("idempotent account provisioning and retained database"):
        before = sql('SELECT md5(string_agg(row_to_json(u)::text,\'\' ORDER BY id)) FROM "User" u')
        documents.succeed("systemctl start terrace-documenso-bootstrap.service", timeout=180)
        assert sql('SELECT md5(string_agg(row_to_json(u)::text,\'\' ORDER BY id)) FROM "User" u') == before
        start()
        stop()
        documents.succeed("systemctl restart postgresql.service")
        start()
        assert sql('SELECT md5(string_agg(row_to_json(u)::text,\'\' ORDER BY id)) FROM "User" u') == before
    with subtest("failed reconciliation cannot authorize stale inputs or adopt unrelated accounts"):
        stop()
        sql('INSERT INTO "User" (name,email,"updatedAt") VALUES (\'Unmanaged Fixture\',\'unmanaged@terrace.invalid\',now())')
        mutate('.approvedUsers += [{email:"unmanaged@terrace.invalid",name:"Unmanaged Fixture",role:"USER"}]')
        documents.succeed("systemctl restart terrace-documenso-inputs.service")
        documents.fail("systemctl start terrace-documenso-bootstrap.service", timeout=180)
        documents.fail("systemctl start terrace-documenso.service")
        assert sql('SELECT count(*) FROM "User" WHERE email=\'unmanaged@terrace.invalid\' AND source IS NULL AND NOT disabled') == "1"
        mutate('.approvedUsers |= map(select(.email != "unmanaged@terrace.invalid"))')
        documents.succeed("systemctl restart terrace-documenso-inputs.service && systemctl reset-failed terrace-documenso-bootstrap.service && systemctl start terrace-documenso-bootstrap.service", timeout=180)
        start()
    with subtest("public password routes and unauthenticated admin loader are denied"):
        assert client.succeed("curl --silent --max-time 5 -H 'Host: other.terrace.invalid' -o /dev/null -w '%{http_code}' https://documents.terrace.invalid/api/health").strip() == "404"
        for path in ["/api/auth/email-password", "/api/auth/email-password/authorize", "/api/auth/email-password/forgot-password", "/api/auth/email-password;ignored/authorize", "/forgot-password", "/forgot-password.data", "/forgot-password;ignored", "/reset-password", "/reset-password.data", "/signup", "/signup.data"]:
            for method in ["GET", "POST"]:
                assert client.succeed("curl --silent --max-time 5 -X " + method + " -o /dev/null -w '%{http_code}' " + shlex.quote("https://documents.terrace.invalid" + path)).strip() == "403"
        assert documents.succeed("podman exec terrace-documenso node /run/admin-loader-test.cjs").strip() == "Admin page and selected data loader denied access without user data."
        for port in [13050, 5432]:
            client.fail(f"nc -z -w 2 documents {port}")
        assert documents.succeed("podman port terrace-documenso 3000/tcp").strip() == "127.0.0.1:13050"
        assert documents.succeed("systemctl show terrace-documenso.service -p MemoryMax --value").strip() == "2415919104"
        client.fail("systemctl is-enabled terrace-documenso.service")
    with subtest("changed inputs cannot reuse a stale or failed bootstrap receipt"):
        stop()
        mutate('.approvedUsers[0].name = "Changed Synthetic Approval"')
        documents.fail("systemctl start terrace-documenso.service")
        documents.succeed("systemctl restart terrace-documenso-inputs.service")
        documents.fail("systemctl start terrace-documenso.service")
        documents.succeed("systemctl start terrace-documenso-bootstrap.service", timeout=180)
        start()
    with subtest("scoped offboarding revokes sessions and retains user data"):
        stop()
        sql('INSERT INTO "Session" (id,"sessionToken","userId","expiresAt","updatedAt") SELECT \'fixture-session\',\'fixture-token\',id,now()+interval \'1 day\',now() FROM "User" WHERE password IS NULL AND source IS NOT NULL')
        mutate('.approvedUsers = []')
        documents.succeed("systemctl restart terrace-documenso-inputs.service && systemctl start terrace-documenso-bootstrap.service", timeout=180)
        assert sql('SELECT count(*) FROM "User"') == "5"
        assert sql('SELECT count(*) FROM "User" WHERE disabled AND password IS NULL') == "1"
        assert sql('SELECT count(*) FROM "Session"') == "0"
        start()
    stop()
    documents.succeed("systemctl stop nginx.service postgresql.service")
    assert documents.succeed("podman ps -q").strip() == ""
    documents.fail("ss -H -lnt | grep -Eq ':(443|5432|13050) '")
    documents.fail("curl --silent --fail --connect-timeout 1 --max-time 2 http://127.0.0.1:13050/api/health >/dev/null")
  '';
}
