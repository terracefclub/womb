{
  pkgs,
  module,
}: let
  certificate = pkgs.runCommand "terrace-identity-test-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir -p $out
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout $out/key.pem -out $out/cert.pem \
      -subj '/CN=identity' -addext 'subjectAltName=DNS:identity' 2>/dev/null
  '';
  # No people, client credentials or real contract roles in this hosting fixture.
  realm = pkgs.writeText "terrace-realm.json" (builtins.toJSON {
    realm = "terrace";
    enabled = true;
    displayName = "Hosting fixture";
    registrationAllowed = false;
    sslRequired = "external";
    clients = [
      {
        clientId = "hosting-fixture";
        enabled = true;
        publicClient = true;
        standardFlowEnabled = true;
        directAccessGrantsEnabled = false;
        implicitFlowEnabled = false;
        serviceAccountsEnabled = false;
        redirectUris = ["https://website.invalid/auth/callback"];
        webOrigins = ["https://website.invalid"];
      }
    ];
  });
  inputs = "/var/lib/identity-test";
in {
  name = "terrace-identity";
  nodes = {
    identity = {config, ...}: {
      imports = [module];
      virtualisation = {
        memorySize = 3584;
        cores = 2;
      };
      terrace.identity = {
        enable = true;
        hostname = "identity";
        realmFile = "${inputs}/terrace-realm.json";
        databasePasswordFile = "${inputs}/db-password";
        certificateFile = "${inputs}/cert.pem";
        certificateKeyFile = "${inputs}/key.pem";
      };
      systemd.services.identity-test-inputs = {
        requiredBy = ["keycloak.service" "keycloakPostgreSQLInit.service" "nginx.service"];
        before = ["keycloak.service" "keycloakPostgreSQLInit.service" "nginx.service"];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          UMask = "0077";
        };
        script = ''
          mkdir -m 0700 -p ${inputs}
          ${pkgs.openssl}/bin/openssl rand -base64 32 >${inputs}/db-password
          cp ${realm} ${inputs}/terrace-realm.json
          cp ${certificate}/{cert,key}.pem ${inputs}/
          chmod 0600 ${inputs}/*
        '';
      };
      environment.systemPackages = [pkgs.curl pkgs.jq pkgs.iproute2];
      # Deliberately pierce the firewall: remote failures must prove loopback binding.
      networking.firewall.allowedTCPPorts = [5432 8088 9000];
      environment.etc."identity-test-policy.jq".source = ./identity/realm-policy.jq;
      environment.etc."identity-test-db-dir".text = config.services.postgresql.dataDir;
    };
    client = {...}: {
      imports = [module]; # Importing the optional module must enable nothing.
      environment.systemPackages = [pkgs.curl pkgs.netcat-openbsd];
      security.pki.certificateFiles = ["${certificate}/cert.pem"];
    };
  };
  testScript = ''
    import json, shlex

    start_all()
    identity.wait_for_unit("postgresql.service")
    identity.wait_for_unit("keycloak.service", timeout=300)
    identity.wait_for_unit("nginx.service")
    def ready():
        identity.wait_until_succeeds("curl --fail --silent --max-time 5 http://127.0.0.1:9000/health/ready | jq -e '.status == \"UP\"' >/dev/null", timeout=120)
        identity.succeed("systemctl reset-failed terrace-identity-health.service && systemctl start terrace-identity-health.service")
    ready()
    health = json.loads(identity.succeed("curl --fail --silent --max-time 5 http://127.0.0.1:9000/health/ready"))
    assert any("database" in check["name"].lower() for check in health["checks"])
    identity.wait_for_unit("terrace-identity-health.timer")

    PG = "runuser -u postgres -- psql -X -A -t -d keycloak -c "
    def sql(query):
        return identity.succeed(PG + shlex.quote(query)).strip()

    with subtest("trusted HTTPS issuer and public-path boundary"):
        discovery = json.loads(client.succeed("curl --fail --silent --max-time 10 https://identity/realms/terrace/.well-known/openid-configuration"))
        assert discovery["issuer"] == "https://identity/realms/terrace"
        assert discovery["authorization_endpoint"].startswith("https://identity/")
        assert discovery["token_endpoint"].startswith("https://identity/")
        for path in ["/", "/admin/", "/admin/master/console/", "/realms/master/", "/.well-known/oauth-authorization-server/realms/master", "/health/ready", "/metrics"]:
            assert client.succeed(f"curl -s -o /dev/null -w '%{{http_code}}' https://identity{path}").strip() == "404"
        for path in ["/realms/terrace/clients-registrations", "/realms/terrace/clients-registrations/default", "/realms/terrace/clients-registrations;ignored/default"]:
            endpoint = shlex.quote(f"https://identity{path}")
            assert client.succeed(f"curl -s -X POST -H 'Content-Type: application/json' -d '{{}}' -o /dev/null -w '%{{http_code}}' {endpoint}").strip() == "404"
        spoofed = json.loads(client.succeed("curl --fail --silent https://identity/realms/terrace/.well-known/openid-configuration -H 'X-Forwarded-Host: attacker.invalid' -H 'X-Forwarded-Proto: http' -H 'X-Forwarded-Port: 81' -H 'Forwarded: host=attacker.invalid;proto=http'"))
        assert spoofed["issuer"] == discovery["issuer"]
        client.fail("nc -z -w 2 identity 80")
        for port in [5432, 8088, 9000]:
            client.fail(f"nc -z -w 2 identity {port}")
        listeners = identity.succeed("ss -H -lnt").splitlines()
        for port in [8088, 9000]:
            addresses = [line.split()[3].rsplit(":", 1)[0].strip("[]") for line in listeners if line.split()[3].endswith(f":{port}")]
            assert addresses and all(address in ["127.0.0.1", "::ffff:127.0.0.1", "::1"] for address in addresses)
        assert sql("select count(*) from user_entity") == "0"
        assert sql("select registration_allowed from realm where name='terrace'") == "f"
        identity.succeed("test -f /run/keycloak/keycloak.log")
        assert identity.succeed("stat -c %a /run/keycloak").strip() == "700"
        assert identity.succeed("stat -c %a /run/keycloak/keycloak.log").strip() == "600"
        identity.fail("runuser -u nobody -- test -r /run/keycloak/keycloak.log")
        identity.succeed("grep -qx 'log-file-rotation-max-file-size=5M' /run/keycloak/conf/keycloak.conf >/dev/null 2>&1")
        identity.succeed("grep -qx 'log-file-rotation-max-backup-index=2' /run/keycloak/conf/keycloak.conf >/dev/null 2>&1")

    with subtest("database and realm survive restart; import is create-only"):
        realm_id = sql("select id from realm where name='terrace'")
        assert realm_id
        sql("create table terrace_persistence (marker text); insert into terrace_persistence values ('synthetic-marker')")
        identity.succeed("jq '.displayName = \"Not reconciled\"' ${inputs}/terrace-realm.json >${inputs}/next.json && mv ${inputs}/next.json ${inputs}/terrace-realm.json")
        identity.succeed("systemctl restart keycloak.service")
        ready()
        assert sql("select id from realm where name='terrace'") == realm_id
        assert sql("select display_name from realm where name='terrace'") == "Hosting fixture"
        identity.succeed("systemctl stop keycloak.service && systemctl restart postgresql.service && systemctl start keycloak.service")
        ready()
        assert sql("select marker from terrace_persistence") == "synthetic-marker"
        assert sql("select id from realm where name='terrace'") == realm_id

    with subtest("unsafe runtime artifacts fail closed without exposing contents"):
        mutations = [
            {"users": [{"username": "forbidden-private-fixture"}]},
            {"registrationAllowed": True}, {"sslRequired": "none"},
            {"identityProviders": [{"alias": "unreviewed"}]},
            {"components": {"org.keycloak.storage.UserStorageProvider": [{"name": "ldap"}]}},
            {"userFederationProviders": [{"providerName": "ldap"}]},
            {"clients": [{"clientId": "bad", "directAccessGrantsEnabled": False, "redirectUris": ["http://127.0.0.1/callback"]}]},
            {"clients": [{"clientId": "bad", "directAccessGrantsEnabled": False, "webOrigins": ["*"]}]},
            {"clients": [{"clientId": "bad", "directAccessGrantsEnabled": True}]},
            {"clients": [{"clientId": "bad", "directAccessGrantsEnabled": False, "implicitFlowEnabled": True}]},
            {"clients": [{"clientId": "bad", "directAccessGrantsEnabled": False, "serviceAccountsEnabled": True}]},
        ]
        for mutation in mutations:
            bad = shlex.quote(json.dumps(mutation))
            identity.fail(f"jq -s '.[0] * .[1]' ${inputs}/terrace-realm.json <(echo {bad}) | jq -e -f /etc/identity-test-policy.jq >/dev/null 2>&1")
        # Exercise the actual service credential/pre-start path too, not only jq.
        identity.succeed("cp ${inputs}/terrace-realm.json ${inputs}/good.json && jq '.users = [{username: \"forbidden-private-fixture\"}]' ${inputs}/good.json >${inputs}/terrace-realm.json")
        identity.succeed("systemctl stop keycloak.service && systemctl reset-failed keycloak.service")
        identity.fail("systemctl start keycloak.service")
        identity.fail("systemctl start terrace-identity-health.service")
        identity.fail("journalctl -u keycloak.service --no-pager | grep -q forbidden-private-fixture")
        identity.succeed("cp ${inputs}/good.json ${inputs}/terrace-realm.json && systemctl reset-failed keycloak.service && systemctl start keycloak.service")
        ready()
        assert sql("select count(*) from user_entity") == "0"

    with subtest("resource limits and dependency failure are observable"):
        assert identity.succeed("systemctl show keycloak.service -p MemoryMax --value").strip() == "2147483648"
        assert identity.succeed("systemctl show keycloak.service -p TasksMax --value").strip() == "256"
        assert identity.succeed("systemctl show keycloak.service -p CPUQuotaPerSecUSec --value").strip() == "2s"
        identity.succeed("systemctl stop postgresql.service")
        identity.wait_until_fails("systemctl start terrace-identity-health.service", timeout=30)
        identity.succeed("systemctl start postgresql.service && systemctl reset-failed keycloak.service && systemctl restart keycloak.service")
        ready()
        assert sql("select marker from terrace_persistence") == "synthetic-marker"
        identity.fail("systemctl is-active k3s.service")
        for unit in ["keycloak.service", "postgresql.service", "nginx.service", "terrace-identity-health.timer"]:
            client.fail(f"systemctl is-enabled {unit}")
  '';
}
