{
  pkgs,
  release,
  contract,
  extraDnsNames ? [],
  fixtureProgram ? "${contract}/fixture.ts",
  tlsFixture ? null,
}: let
  directory = "/var/lib/terrace-gateway-test";
  media = "/var/lib/terrace-cms-media";
  # Public, disposable test certificates only. No production key or ACME account.
  certificate =
    if tlsFixture != null
    then tlsFixture
    else
      pkgs.runCommand "terrace-gateway-test-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
        mkdir -p $out
        openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
          -keyout $out/key.pem -out $out/cert.pem -subj '/CN=web.terrace.invalid' \
          -addext ${pkgs.lib.escapeShellArg ("subjectAltName=" + pkgs.lib.concatStringsSep "," (map (name: "DNS:${name}") (["web.terrace.invalid" "cms.terrace.invalid" "identity.terrace.invalid"] ++ extraDnsNames)))} 2>/dev/null
      '';
  trustedCertificate = "${certificate}/${
    if tlsFixture == null
    then "cert.pem"
    else "ca.pem"
  }";
in {
  name = "terrace-website-https";
  nodes = {
    gateway = {lib, ...}: {
      imports = [./website-gateway.nix ./identity.nix];
      virtualisation = {
        memorySize = 6144;
        cores = 2;
      };
      security.pki.certificateFiles = [trustedCertificate];
      networking.hosts."127.0.0.1" = ["web.terrace.invalid" "cms.terrace.invalid" "identity.terrace.invalid"] ++ extraDnsNames;
      terrace.website = {
        enable = true;
        webPackage = release.web;
        cmsPackage = release.cms;
        revision = release.revision;
      };
      terrace.websiteGateway = {
        enable = true;
        webHostname = "web.terrace.invalid";
        cmsHostname = "cms.terrace.invalid";
        certificateFile = "${directory}/cert.pem";
        certificateKeyFile = "${directory}/key.pem";
      };
      terrace.identity = {
        enable = true;
        hostname = "identity.terrace.invalid";
        realmFile = "${directory}/terrace-realm.json";
        databasePasswordFile = "${directory}/db-password";
        certificateFile = "${directory}/cert.pem";
        certificateKeyFile = "${directory}/key.pem";
      };
      # Test-only isolated master bootstrap; removed through the private API.
      # It is absent from the production realm and from module defaults.
      systemd.services.keycloak.serviceConfig.EnvironmentFile = "${directory}/keycloak.env";
      systemd.services.terrace-cms.environment.NODE_EXTRA_CA_CERTS = trustedCertificate;
      systemd.services.terrace-cms-migrate.environment.NODE_EXTRA_CA_CERTS = trustedCertificate;
      systemd.services.terrace-web.environment.NODE_EXTRA_CA_CERTS = trustedCertificate;
      systemd.services.terrace-cms.wantedBy = lib.mkForce [];
      systemd.services.terrace-web.wantedBy = lib.mkForce [];
      systemd.services.gateway-test-inputs = {
        requiredBy = ["terrace-website-inputs.service" "keycloak.service" "keycloakPostgreSQLInit.service" "nginx.service"];
        before = ["terrace-website-inputs.service" "keycloak.service" "keycloakPostgreSQLInit.service" "nginx.service"];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          UMask = "0077";
          StandardOutput = "null";
          StandardError = "null";
        };
        script = ''
          set -euo pipefail
          ${pkgs.bun}/bin/bun ${pkgs.lib.escapeShellArg fixtureProgram}
          cp ${certificate}/{cert,key}.pem ${directory}/
          chmod 0400 ${directory}/{cert,key}.pem${pkgs.lib.optionalString (tlsFixture != null) "\ncp ${certificate}/ca.pem ${directory}/ca.pem && chmod 0400 ${directory}/ca.pem"}
        '';
      };
      # Pierce the VM firewall to establish actual backend binding, not just filtering.
      networking.firewall.allowedTCPPorts = [5432 8088 9000 13048 14294];
      environment.systemPackages = [pkgs.curl pkgs.jq pkgs.netcat-openbsd pkgs.python3 pkgs.postgresql_18];
      environment.etc."gateway-protocol.py".source = ./website-gateway/protocol.py;
    };
    client = {nodes, ...}: {
      imports = [./website-gateway.nix ./identity.nix];
      virtualisation = {
        memorySize = 2048;
        cores = 1;
      };
      security.pki.certificateFiles = [trustedCertificate];
      networking.hosts.${nodes.gateway.networking.primaryIPAddress} = ["web.terrace.invalid" "cms.terrace.invalid" "identity.terrace.invalid"] ++ extraDnsNames;
      environment.systemPackages = [pkgs.curl pkgs.jq pkgs.netcat-openbsd pkgs.nssTools (pkgs.python3.withPackages (ps: [ps.playwright]))];
      environment.variables.TERRACE_BROWSER_BINARY = "${pkgs.chromium}/bin/chromium";
      environment.etc."gateway-browser.py".source = ./website-gateway/browser.py;
    };
  };
  testScript = ''
    import json, re, shlex, tempfile
    start_all()
    gateway.wait_for_unit("postgresql.target")
    gateway.wait_for_unit("keycloak.service", timeout=300)
    gateway.wait_for_unit("nginx.service")

    with subtest("trusted public TLS and denied direct/private infrastructure"):
        discovery = json.loads(client.succeed("curl --fail --silent --max-time 10 https://identity.terrace.invalid/realms/terrace/.well-known/openid-configuration"))
        assert discovery["issuer"] == "https://identity.terrace.invalid/realms/terrace"
        client.fail("curl --fail --silent --max-time 10 https://gateway/ >/dev/null 2>&1")
        assert client.succeed("curl --silent --max-time 10 -H 'Host: attacker.invalid' -o /dev/null -w '%{http_code}' https://web.terrace.invalid/").strip() == "421"
        assert client.succeed("curl --silent --max-time 10 -H 'Host: cms.terrace.invalid' -o /dev/null -w '%{http_code}' https://web.terrace.invalid/").strip() == "421"
        for route in ["/admin/", "/realms/master/", "/metrics", "/health/ready", "/realms/terrace/clients-registrations/default"]:
            assert client.succeed("curl --silent --max-time 10 -o /dev/null -w '%{http_code}' " + shlex.quote("https://identity.terrace.invalid" + route)).strip() == "404"
        for port in [80, 5432, 8088, 9000, 13048, 14294]:
            client.fail(f"nc -z -w 2 gateway {port}")
        gateway.succeed("python /etc/gateway-protocol.py seed")
        assert gateway.succeed("runuser -u postgres -- psql -XAt -d keycloak -c \"select count(*) from user_entity where username='gateway-fixture-bootstrap'\" 2>/dev/null").strip() == "0"

    with subtest("public activation guards and explicit migrations"):
        gateway.succeed("sed -i 's/^CMS_PUBLIC_DEPLOYMENT=1$/CMS_PUBLIC_DEPLOYMENT=0/' /var/lib/terrace-website-inputs/cms.env")
        gateway.fail("systemctl start terrace-cms-migrate.service")
        gateway.succeed("sed -i 's/^CMS_PUBLIC_DEPLOYMENT=0$/CMS_PUBLIC_DEPLOYMENT=1/' /var/lib/terrace-website-inputs/cms.env && systemctl reset-failed terrace-cms-migrate.service")
        gateway.succeed("systemctl start terrace-cms-migrate.service && systemctl start terrace-cms.service terrace-web.service")
        gateway.succeed("install -m 0600 /dev/null ${media}/private.png && echo 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGPgUbL4DwACCgFmeGgpMwAAAABJRU5ErkJggg==' | base64 -d >${media}/private.png && cp ${media}/private.png ${media}/public.png && chown terrace_cms:terrace_cms ${media}/*.png")
        gateway.succeed("runuser -u postgres -- psql -XAt -d terrace_cms -c \"insert into media(alt,public,filename,mime_type,filesize,width,height) values ('Synthetic private',false,'private.png','image/png',70,1,1),('Synthetic public',true,'public.png','image/png',70,1,1)\" >/dev/null 2>&1")
        headers = client.succeed("curl --fail --silent --max-time 10 -D - -o /dev/null https://web.terrace.invalid/events/").lower()
        assert "cache-control: private, no-store" in headers and "x-content-type-options: nosniff" in headers
        for route in ["/api/media/file/private.png", "/_next/image?url=%2Fapi%2Fmedia%2Ffile%2Fprivate.png&w=64&q=75", "/api/users/me"]:
            headers = client.succeed("curl --silent --max-time 10 -D - -o /dev/null " + shlex.quote("https://cms.terrace.invalid" + route)).lower()
            assert "cache-control: private, no-store" in headers

    with subtest("normal HTTPS code/PKCE flows and cache separation through gateway"):
        protocol = gateway.succeed("python /etc/gateway-protocol.py test").strip()
        assert re.fullmatch(r"Native HTTPS protocol checks passed: [0-9]+ bounded assertions; SSO, roles, state, CSRF, cookies, publication, private cache and logout\.", protocol)
        print(protocol)
        gateway.succeed("test -n \"$(find /var/cache/terrace-cms/images -type f -print -quit)\"")
        # Copy only inside the driver's disposable shared temp directory, never
        # its copy_from_machine helper (which is intended to write into $out).
        # No credential bytes enter a logged command, test result or Nix store.
        assert not str(gateway.shared_dir).startswith("/nix/store/")
        with tempfile.TemporaryDirectory(prefix="gateway-credentials-", dir=gateway.shared_dir) as private:
            name = private.rsplit("/", 1)[-1]
            gateway.succeed(f"cp ${directory}/credentials.json /tmp/shared/{name}/credentials.json")
            client.succeed(f"cp /tmp/shared/{name}/credentials.json /root/gateway-fixture.json")
        client.succeed("install -d -m 0700 /root/.pki/nssdb && certutil -N -d sql:/root/.pki/nssdb --empty-password && certutil -A -d sql:/root/.pki/nssdb -n TerraceGatewayTest -t 'C,,' -i ${trustedCertificate}")
        browser = client.succeed("test $(stat -c %a /root/gateway-fixture.json) = 400 && python /etc/gateway-browser.py").strip()
        assert browser == "Real Chromium denied-to-switch-to-approved checks passed: HTTPS trust, native POST Origin, approved page and secure cookie."
        print(browser)
        client.succeed("rm /root/gateway-fixture.json")
        for route in ["/members/", "/auth/callback?state=invalid&code=invalid"]:
            headers = client.succeed("curl --silent --max-time 10 -D - -o /dev/null " + shlex.quote("https://web.terrace.invalid" + route)).lower()
            assert "cache-control: private, no-store" in headers
        peaks = {unit: gateway.succeed(f"systemctl show {unit}.service -p MemoryPeak --value").strip() for unit in ["keycloak", "terrace-cms", "terrace-web", "postgresql", "nginx"]}

    with subtest("explicit task service stop and disabled optional import"):
        gateway.succeed("systemctl stop terrace-identity-health.timer terrace-identity-health.service terrace-web.service terrace-cms.service keycloak.service nginx.service postgresql.target postgresql.service")
        for unit in ["terrace-web", "terrace-cms", "keycloak", "nginx", "postgresql"]:
            gateway.fail(f"systemctl is-active --quiet {unit}.service")
        for port in [443, 5432, 8088, 9000, 13048, 14294]:
            gateway.fail(f"nc -z -w 1 127.0.0.1 {port}")
        for unit in ["terrace-web.service", "terrace-cms.service", "keycloak.service", "nginx.service"]:
            client.fail(f"systemctl is-enabled {unit}")
        print(f"HTTPS native gateway/SSO/cookie/cache boundary and owned-service stop checks passed; synthetic component peak bytes {peaks}")
  '';
}
