{
  pkgs,
  contract,
}: let
  directory = "/var/lib/terrace-gateway-test";
  browserPython = pkgs.python3.withPackages (ps: [ps.playwright]);
  fixture = pkgs.runCommand "vaultwarden-functional-fixture" {} ''
    mkdir $out
    cp -R ${contract}/. $out/
    chmod -R u+w $out
    cp ${./vaultwarden/functional-fixture.ts} $out/functional-fixture.ts
    cp ${./vaultwarden/policy.ts} $out/policy.ts
  '';
  certificate = pkgs.runCommand "vaultwarden-functional-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir $out
    openssl req -x509 -nodes -newkey rsa:2048 -keyout $out/ca-key.pem -out $out/ca.pem -days 3650 \
      -subj /CN=TerraceVaultSyntheticCA -addext basicConstraints=critical,CA:TRUE \
      -addext keyUsage=critical,keyCertSign,cRLSign >/dev/null 2>&1
    openssl req -new -nodes -newkey rsa:2048 -keyout $out/key.pem -out $out/request.pem \
      -subj /CN=vault.terrace.invalid >/dev/null 2>&1
    cat >$out/extensions <<'EXTENSIONS'
    basicConstraints=critical,CA:FALSE
    keyUsage=critical,digitalSignature,keyEncipherment
    extendedKeyUsage=serverAuth
    subjectAltName=DNS:vault.terrace.invalid,DNS:identity.terrace.invalid
    EXTENSIONS
    openssl x509 -req -in $out/request.pem -CA $out/ca.pem -CAkey $out/ca-key.pem -CAcreateserial \
      -out $out/cert.pem -days 3650 -extfile $out/extensions >/dev/null 2>&1
    openssl verify -purpose sslserver -verify_hostname vault.terrace.invalid -CAfile $out/ca.pem $out/cert.pem >/dev/null
    openssl verify -purpose sslserver -verify_hostname identity.terrace.invalid -CAfile $out/ca.pem $out/cert.pem >/dev/null
  '';
  capture = pkgs.runCommand "vaultwarden-functional-capture" {} ''
    mkdir $out
    cp ${./windmill/capture.py} $out/capture.py
    cp ${./windmill/diagnostics.py} $out/diagnostics.py
    cp ${./vaultwarden/capture.py} $out/vaultwarden-capture.py
  '';
  tag = "name=opt/terrace-vaultwarden-functional,string=${builtins.hashFile "sha256" ./vaultwarden/browser.py}";
in {
  name = "terrace-vaultwarden-functional";
  nodes = {
    server = {lib, ...}: {
      imports = [./vaultwarden.nix ./identity.nix];
      virtualisation = {
        memorySize = 4096;
        cores = 2;
        diskSize = 6144;
        qemu.options = ["-fw_cfg" tag];
      };
      security.pki.certificateFiles = ["${certificate}/ca.pem"];
      networking.hosts."127.0.0.1" = ["vault.terrace.invalid" "identity.terrace.invalid"];
      services.postgresql.package = pkgs.postgresql_18;
      terrace.vaultwarden = {
        enable = true;
        hostname = "vault.terrace.invalid";
        identityHostname = "identity.terrace.invalid";
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
      systemd.services.keycloak.serviceConfig.EnvironmentFile = "${directory}/keycloak.env";
      systemd.services.vaultwarden.wantedBy = lib.mkForce [];
      systemd.services.vaultwarden-functional-inputs = {
        requiredBy = ["terrace-vaultwarden-inputs.service" "keycloak.service" "keycloakPostgreSQLInit.service" "nginx.service"];
        before = ["terrace-vaultwarden-inputs.service" "keycloak.service" "keycloakPostgreSQLInit.service" "nginx.service"];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          UMask = "0077";
          StandardOutput = "null";
          StandardError = "null";
          TimeoutStartSec = 30;
        };
        script = ''
          ${pkgs.bun}/bin/bun --no-env-file ${fixture}/functional-fixture.ts
          cp ${certificate}/{cert,key}.pem ${directory}/
          chmod 0400 ${directory}/{cert,key}.pem
        '';
      };
      environment.systemPackages = [pkgs.bun pkgs.curl pkgs.jq pkgs.python3 pkgs.postgresql_18];
      environment.etc."gateway-protocol.py".source = ./website-gateway/protocol.py;
      environment.etc."vaultwarden-functional-fixture".source = fixture;
      environment.etc."vaultwarden-functional-capture".source = capture;
    };
    client = {nodes, ...}: {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        qemu.options = ["-fw_cfg" tag];
      };
      security.pki.certificateFiles = ["${certificate}/ca.pem"];
      networking.hosts.${nodes.server.networking.primaryIPAddress} = ["vault.terrace.invalid" "identity.terrace.invalid"];
      environment.systemPackages = [browserPython pkgs.chromium pkgs.nssTools pkgs.curl];
      environment.variables.TERRACE_BROWSER_BINARY = "${pkgs.chromium}/bin/chromium";
      environment.etc."vaultwarden-browser.py".source = ./vaultwarden/browser.py;
    };
  };
  testScript = ''
    import tempfile
    from pathlib import Path
    start_all()
    server.wait_for_unit("keycloak.service")
    server.wait_for_unit("nginx.service")
    with subtest("user-free initial realm, private synthetic enrollment, trusted discovery"):
        assert server.succeed("runuser -u postgres -- psql -XAtw -d keycloak -c \"select count(*) from user_entity where realm_id=(select id from realm where name='terrace')\" 2>/dev/null").strip() == "0"
        server.succeed("python3 /etc/gateway-protocol.py seed", timeout=120)
        assert server.succeed("runuser -u postgres -- psql -XAtw -d keycloak -c \"select count(*) from user_entity where username='gateway-fixture-bootstrap'\" 2>/dev/null").strip()=="0"
        server.succeed("chmod 0600 ${directory}/keycloak.env && truncate -s 0 ${directory}/keycloak.env && chmod 0400 ${directory}/keycloak.env")
        server.succeed("systemctl start terrace-vaultwarden-migrate.service && systemctl start vaultwarden.service && systemctl start terrace-vaultwarden-bootstrap.service",timeout=180)
        server.succeed("runuser -u terrace_vaultwarden -- curl --fail --silent --max-time 5 https://identity.terrace.invalid/realms/terrace/.well-known/openid-configuration >/dev/null")
        client.succeed("install -d -m 0700 /root/vaultwarden-functional /root/.pki/nssdb && certutil -N -d sql:/root/.pki/nssdb --empty-password && certutil -A -d sql:/root/.pki/nssdb -n TerraceVaultTest -t 'C,,' -i ${certificate}/ca.pem")
        with tempfile.TemporaryDirectory(prefix="vaultwarden-browser-",dir=server.shared_dir) as temporary:
            name=Path(temporary).name
            server.succeed("cp ${directory}/vaultwarden-browser.json /tmp/shared/"+name+"/credentials.json")
            client.succeed("cp /tmp/shared/"+name+"/credentials.json /root/vaultwarden-functional/credentials.json && chmod 0600 /root/vaultwarden-functional/credentials.json")
    def browser(mode):
        try:
            assert client.succeed("${browserPython}/bin/python3 /etc/vaultwarden-browser.py "+mode,timeout=240).strip()=="Vaultwarden browser phase "+mode+" passed."
            print("Vaultwarden browser phase "+mode+" passed.")
        except Exception:
            try:
                server.succeed("python3 /etc/vaultwarden-functional-capture/vaultwarden-capture.py",timeout=60)
                assert not Path(server.shared_dir).resolve().is_relative_to(Path("/nix/store"))
                private_server=Path(server.shared_dir)/("vaultwarden-functional-server-"+mode)
                private_server.mkdir(mode=0o700)
                server.succeed("cp /var/lib/vaultwarden-test-evidence/raw.json /tmp/shared/"+private_server.name+"/raw.json && chmod 0600 /tmp/shared/"+private_server.name+"/raw.json",timeout=10)
                print("Bounded private Vaultwarden unit/PID/log evidence retained; contents suppressed.")
            except Exception:
                print("Private Vaultwarden unit capture unavailable; original failure preserved.")
            try:
                assert not Path(client.shared_dir).resolve().is_relative_to(Path("/nix/store"))
                private=Path(client.shared_dir)/("vaultwarden-browser-failure-"+mode)
                private.mkdir(mode=0o700)
                for name in ["failure.png","failure-labels.json"]:
                    client.execute("test ! -f /root/vaultwarden-functional/"+name+" || (cp /root/vaultwarden-functional/"+name+" /tmp/shared/"+private.name+"/ && chmod 0600 /tmp/shared/"+private.name+"/"+name+")",timeout=10)
                print("Private Vaultwarden browser artifacts retained; contents suppressed.")
            except Exception:
                print("Private Vaultwarden browser export unavailable; original failure preserved.")
            raise
    with subtest("approved native browser SSO, separate master password and encrypted item"):
        browser("create")
    with subtest("fresh browser decrypts retained data and denies unapproved identity"):
        server.succeed("systemctl restart vaultwarden.service && systemctl start terrace-vaultwarden-bootstrap.service",timeout=180)
        browser("verify")
    with subtest("revoked existing and fresh sessions denied while encrypted data retained"):
        server.succeed("systemctl stop vaultwarden.service && bun --no-env-file /etc/vaultwarden-functional-fixture/functional-fixture.ts revoke && systemctl restart terrace-vaultwarden-inputs.service && systemctl start vaultwarden.service && systemctl start terrace-vaultwarden-bootstrap.service",timeout=180)
        browser("disabled")
        assert server.succeed("runuser -u postgres -- psql -XAtw -d terrace_vaultwarden -c 'SELECT count(*) FROM ciphers' 2>/dev/null").strip()=="2"
    with subtest("reenabled person decrypts the same retained encrypted vault"):
        # Four deliberate operator starts in this short test exceed the native
        # three-per-five-minutes restart guard. Keep that production guard and
        # reset only this explicitly stopped, process-free test-owned unit.
        server.succeed("systemctl stop vaultwarden.service",timeout=60)
        assert server.succeed("systemctl show vaultwarden.service -p ActiveState --value").strip()=="inactive"
        for property in ["MainPID","ControlPID"]:
            assert server.succeed("systemctl show vaultwarden.service -p "+property+" --value").strip()=="0"
        assert server.succeed("systemctl show vaultwarden.service -p ControlGroup --value").strip()==""
        server.succeed("systemctl reset-failed vaultwarden.service && bun --no-env-file /etc/vaultwarden-functional-fixture/functional-fixture.ts restore && systemctl restart terrace-vaultwarden-inputs.service && systemctl start vaultwarden.service && systemctl start terrace-vaultwarden-bootstrap.service",timeout=180)
        browser("reenabled")
        server.succeed("systemctl stop terrace-identity-health.timer vaultwarden.service nginx.service keycloak.service postgresql.service postgresql.target",timeout=90)
        for unit in ["vaultwarden","nginx","keycloak","postgresql","terrace-vaultwarden-migration-server","terrace-vaultwarden-migrate","terrace-vaultwarden-bootstrap"]:
            assert server.succeed("systemctl show "+unit+".service -p ActiveState --value").strip()=="inactive"
            assert server.succeed("systemctl show "+unit+".service -p MainPID --value").strip()=="0"
            assert server.succeed("systemctl show "+unit+".service -p ControlPID --value").strip()=="0"
        inspect_status,listeners=server.execute("ss -H -lnt")
        assert inspect_status==0
        import re
        assert not re.search(r':(?:443|5432|8088|9000|18222|18224)(?:\s|$)',listeners)
  '';
}
