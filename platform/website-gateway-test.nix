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
  names = ["web.terrace.invalid" "cms.terrace.invalid" "identity.terrace.invalid"] ++ extraDnsNames;
  # Public, disposable test certificates only. No production key or ACME account.
  # The key is random and made at build time, so a rebuild makes a different
  # certificate under the same path: every client references this path (the
  # /etc/terrace-test-ca.pem symlink, NODE_EXTRA_CA_CERTS, the browser's NSS
  # database) and nothing copies it into another derivation (no security.pki).
  certificate =
    if tlsFixture != null
    then tlsFixture
    else
      pkgs.runCommand "terrace-gateway-proof-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
        mkdir -p $out
        openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
          -keyout $out/key.pem -out $out/cert.pem -subj '/CN=web.terrace.invalid' \
          -addext ${pkgs.lib.escapeShellArg ("subjectAltName=" + pkgs.lib.concatStringsSep "," (map (name: "DNS:${name}") names))} 2>/dev/null
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
      environment.etc."terrace-test-ca.pem".source = trustedCertificate;
      networking.hosts."127.0.0.1" = names;
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
      environment.etc."gateway-content.sql".source = ./website-gateway/content.sql;
    };
    client = {nodes, ...}: {
      imports = [./website-gateway.nix ./identity.nix];
      virtualisation = {
        memorySize = 2048;
        cores = 1;
      };
      environment.etc."terrace-test-ca.pem".source = trustedCertificate;
      networking.hosts.${nodes.gateway.networking.primaryIPAddress} = names;
      environment.systemPackages = [pkgs.curl pkgs.jq pkgs.netcat-openbsd pkgs.nssTools (pkgs.python3.withPackages (ps: [ps.playwright]))];
      environment.variables.TERRACE_BROWSER_BINARY = "${pkgs.chromium}/bin/chromium";
      environment.etc."gateway-browser.py".source = ./website-gateway/browser.py;
      # A second real client address for the per-address application limit.
      environment.etc."gateway-protocol.py".source = ./website-gateway/protocol.py;
    };
  };
  testScript = ''
    ${import ./test-trust.nix {inherit pkgs;}}
    import json, re, shlex, tempfile
    # Every HTTPS client trusts the test certificate by store-path reference only.
    CA = "/etc/terrace-test-ca.pem"
    CURL = "curl --cacert " + CA + " "
    PROTOCOL = "SSL_CERT_FILE=" + CA + " python3 /etc/gateway-protocol.py "
    start_all()
    gateway.wait_for_unit("postgresql.target")
    gateway.wait_for_unit("keycloak.service", timeout=300)
    gateway.wait_for_unit("nginx.service")
    client.wait_for_unit("multi-user.target")
    trust_consistent(gateway, CA, "127.0.0.1:443", ${builtins.toJSON names}, "gateway")
    trust_consistent(client, CA, "web.terrace.invalid:443", ${builtins.toJSON names}, "client")

    with subtest("trusted public TLS and denied direct/private infrastructure"):
        discovery = json.loads(client.succeed(CURL + "--fail --silent --max-time 10 https://identity.terrace.invalid/realms/terrace/.well-known/openid-configuration"))
        assert discovery["issuer"] == "https://identity.terrace.invalid/realms/terrace"
        client.fail(CURL + "--fail --silent --max-time 10 https://gateway/ >/dev/null 2>&1")
        assert client.succeed(CURL + "--silent --max-time 10 -H 'Host: attacker.invalid' -o /dev/null -w '%{http_code}' https://web.terrace.invalid/").strip() == "421"
        assert client.succeed(CURL + "--silent --max-time 10 -H 'Host: cms.terrace.invalid' -o /dev/null -w '%{http_code}' https://web.terrace.invalid/").strip() == "421"
        for route in ["/admin/", "/realms/master/", "/metrics", "/health/ready", "/realms/terrace/clients-registrations/default"]:
            assert client.succeed(CURL + "--silent --max-time 10 -o /dev/null -w '%{http_code}' " + shlex.quote("https://identity.terrace.invalid" + route)).strip() == "404"
        for port in [80, 5432, 8088, 9000, 13048, 14294]:
            client.fail(f"nc -z -w 2 gateway {port}")
        gateway.succeed(PROTOCOL + "seed")
        assert gateway.succeed("runuser -u postgres -- psql -XAt -d keycloak -c \"select count(*) from user_entity where username='gateway-fixture-bootstrap'\" 2>/dev/null").strip() == "0"

    with subtest("public activation guards and explicit migrations"):
        gateway.succeed("sed -i 's/^CMS_PUBLIC_DEPLOYMENT=1$/CMS_PUBLIC_DEPLOYMENT=0/' /var/lib/terrace-website-inputs/cms.env")
        gateway.fail("systemctl start terrace-cms-migrate.service")
        gateway.succeed("sed -i 's/^CMS_PUBLIC_DEPLOYMENT=0$/CMS_PUBLIC_DEPLOYMENT=1/' /var/lib/terrace-website-inputs/cms.env && systemctl reset-failed terrace-cms-migrate.service")
        gateway.succeed("systemctl start terrace-cms-migrate.service && systemctl start terrace-cms.service terrace-web.service")
        gateway.succeed("install -m 0600 /dev/null ${media}/private.png && echo 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGPgUbL4DwACCgFmeGgpMwAAAABJRU5ErkJggg==' | base64 -d >${media}/private.png && cp ${media}/private.png ${media}/public.png && chown terrace_cms:terrace_cms ${media}/*.png")
        gateway.succeed("runuser -u postgres -- psql -XAt -d terrace_cms -c \"insert into media(alt,public,filename,mime_type,filesize,width,height) values ('Synthetic private',false,'private.png','image/png',70,1,1),('Synthetic public',true,'public.png','image/png',70,1,1)\" >/dev/null 2>&1")
        gateway.succeed("runuser -u postgres -- psql -XAt -v ON_ERROR_STOP=1 -d terrace_cms -f /etc/gateway-content.sql >/dev/null 2>&1")
        # The gateway supplies every lifecycle setting the public bundles require.
        for unit, name, value in [("terrace-cms", "LIFECYCLE_OUTBOX_DIR", "/var/lib/terrace-cms-outbox"), ("terrace-cms", "LIFECYCLE_PUBLIC_ORIGIN", "https://web.terrace.invalid"),
                                  ("terrace-web", "CMS_PUBLIC_ORIGIN", "https://cms.terrace.invalid"), ("terrace-web", "ADDRESS_HEADER", "X-Forwarded-For"), ("terrace-web", "XFF_DEPTH", "1")]:
            gateway.succeed(f"pid=$(systemctl show {unit}.service -p MainPID --value) && tr '\\0' '\\n' </proc/$pid/environ | grep -qx {name}={value}")
        assert gateway.succeed("stat -c %U:%G:%a /var/lib/terrace-cms-outbox").strip() == "terrace_cms:terrace_cms:700"
        headers = client.succeed(CURL + "--fail --silent --max-time 10 -D - -o /dev/null https://web.terrace.invalid/events/").lower()
        assert "cache-control: private, no-store" in headers and "x-content-type-options: nosniff" in headers
        for route in ["/api/media/file/private.png", "/_next/image?url=%2Fapi%2Fmedia%2Ffile%2Fprivate.png&w=64&q=75", "/api/users/me"]:
            headers = client.succeed(CURL + "--silent --max-time 10 -D - -o /dev/null " + shlex.quote("https://cms.terrace.invalid" + route)).lower()
            assert "cache-control: private, no-store" in headers

    with subtest("normal HTTPS code/PKCE flows and cache separation through gateway"):
        protocol = gateway.succeed(PROTOCOL + "test").strip()
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
            headers = client.succeed(CURL + "--silent --max-time 10 -D - -o /dev/null " + shlex.quote("https://web.terrace.invalid" + route)).lower()
            assert "cache-control: private, no-store" in headers
        peaks = {unit: gateway.succeed(f"systemctl show {unit}.service -p MemoryPeak --value").strip() for unit in ["keycloak", "terrace-cms", "terrace-web", "postgresql", "nginx"]}

    with subtest("imported content, application limit, invitation outbox, sweep timer and erasure through the gateway"):
        def lifecycle(machine, action, label):
            output = machine.succeed(PROTOCOL + action, timeout=180).strip()
            assert re.fullmatch(label + r"[^:]*: [0-9]+ bounded assertions(;.*)?\.", output), output
            print(output)

        def applications():
            return int(gateway.succeed("runuser -u postgres -- psql -XAt -d terrace_cms -c 'select count(*) from member_applications' 2>/dev/null").strip())

        mirror = "/var/lib/terrace-cms-outbox/*-invitation.json"
        lifecycle(gateway, "content", "Imported-content checks passed")
        assert applications() == 0
        lifecycle(gateway, "apply-burst", "Application rate-limit checks passed")
        assert applications() == 5
        lifecycle(client, "apply-other", "Separate-address application checks passed")
        assert applications() == 10
        lifecycle(gateway, "approve local-alumni", "Approval checks passed")
        assert gateway.succeed("stat -c %U:%a " + mirror).strip() == "terrace_cms:600"
        lifecycle(gateway, "redeem local-alumni", "Redemption checks passed")
        lifecycle(gateway, "sign-in local-alumni", "Member sign-in checks passed")
        lifecycle(gateway, "link local-alumni", "Application link checks passed")
        lifecycle(gateway, "erase local-alumni", "Erasure checks passed")
        # An administrator erasure cannot rewrite files; the mirror still names the applicant until the sweep runs.
        gateway.succeed("jq -e '.to == \"local-alumni@terrace.invalid\"' " + mirror + " >/dev/null")
        gateway.succeed("systemctl is-enabled terrace-cms-sweep.timer && systemctl is-active terrace-cms-sweep.timer")
        before = gateway.succeed("systemctl show terrace-cms-sweep.service -p InvocationID --value").strip()
        gateway.succeed("systemctl stop terrace-cms-sweep.timer && mkdir -p /var/lib/systemd/timers && touch -d '3 days ago' /var/lib/systemd/timers/stamp-terrace-cms-sweep.timer && systemctl start terrace-cms-sweep.timer")
        gateway.wait_until_succeeds(f"test \"$(systemctl show terrace-cms-sweep.service -p InvocationID --value)\" != '{before}' && systemctl show terrace-cms-sweep.service -p ActiveState --value | grep -qx inactive", timeout=180)
        assert dict(line.split("=", 1) for line in gateway.succeed("systemctl show terrace-cms-sweep.service -p Result,ExecMainStatus,User").splitlines()) == {"Result": "success", "ExecMainStatus": "0", "User": "terrace_cms"}
        gateway.succeed("jq -e '.to == \"Former member\" and .body == \"Redacted after erasure.\"' " + mirror + " >/dev/null")
        gateway.fail("grep -rl local-alumni@terrace.invalid /var/lib/terrace-cms-outbox")
        # Platform-level negative case: the packaged sweep refuses a public deployment without its outbox,
        # while the identical invocation with the module's outbox succeeds (the harness itself is sound).
        manual = ("systemd-run --quiet --wait --pipe --collect -p User=terrace_cms -p Group=terrace_cms -p EnvironmentFile=/var/lib/terrace-website-inputs/cms.env "
                  "-p WorkingDirectory=/ -p Environment=PATH=${pkgs.nodejs_24}/bin:${pkgs.bash}/bin:${pkgs.coreutils}/bin "
                  "-p Environment=CMS_MEDIA_DIR=/var/lib/terrace-cms-media -p 'Environment=DATABASE_URI=postgresql://terrace_cms@/terrace_cms?host=/run/postgresql&port=5432' "
                  "-p Environment=NODE_EXTRA_CA_CERTS=${trustedCertificate} -p Environment=LIFECYCLE_PUBLIC_ORIGIN=https://web.terrace.invalid ")
        sweep = "$(systemctl show terrace-cms.service -p WorkingDirectory --value)/bin/sweep"
        refused = gateway.fail(manual + sweep + " 2>&1 >/dev/null")
        assert "CMS lifecycle sweep failed" in refused, refused
        gateway.succeed(manual + "-p Environment=LIFECYCLE_OUTBOX_DIR=/var/lib/terrace-cms-outbox " + sweep + " >/dev/null 2>&1")

    with subtest("explicit task service stop and disabled optional import"):
        gateway.succeed("systemctl stop terrace-identity-health.timer terrace-identity-health.service terrace-cms-sweep.timer terrace-cms-sweep.service terrace-web.service terrace-cms.service keycloak.service nginx.service postgresql.target postgresql.service")
        for unit in ["terrace-web", "terrace-cms", "keycloak", "nginx", "postgresql"]:
            gateway.fail(f"systemctl is-active --quiet {unit}.service")
        for port in [443, 5432, 8088, 9000, 13048, 14294]:
            gateway.fail(f"nc -z -w 1 127.0.0.1 {port}")
        gateway.fail("systemctl is-active --quiet terrace-cms-sweep.timer")
        for unit in ["terrace-web.service", "terrace-cms.service", "keycloak.service", "nginx.service"]:
            client.fail(f"systemctl is-enabled {unit}")
        print(f"HTTPS native gateway/SSO/cookie/cache boundary and owned-service stop checks passed; synthetic component peak bytes {peaks}")
  '';
}
