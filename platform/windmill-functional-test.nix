{
  pkgs,
  contract,
}: let
  fixture = import ./windmill-functional/functional-fixture-package.nix {inherit pkgs contract;};
  certificate = pkgs.runCommand "windmill-functional-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir $out
    openssl req -x509 -nodes -newkey rsa:2048 -keyout ca-key.pem -out $out/ca.pem -days 3650 \
      -subj /CN=TerraceWindmillSyntheticCA -addext basicConstraints=critical,CA:TRUE \
      -addext keyUsage=critical,keyCertSign,cRLSign >/dev/null 2>&1
    openssl req -new -nodes -newkey rsa:2048 -keyout $out/key.pem -out request.pem \
      -subj /CN=windmill.terrace.invalid >/dev/null 2>&1
    cat >extensions <<'EXTENSIONS'
    basicConstraints=critical,CA:FALSE
    keyUsage=critical,digitalSignature,keyEncipherment
    extendedKeyUsage=serverAuth
    subjectAltName=DNS:windmill.terrace.invalid,DNS:identity.terrace.invalid
    EXTENSIONS
    openssl x509 -req -in request.pem -CA $out/ca.pem -CAkey ca-key.pem -CAcreateserial \
      -out $out/cert.pem -days 3650 -extfile extensions >/dev/null 2>&1
    openssl verify -purpose sslserver -verify_hostname windmill.terrace.invalid -CAfile $out/ca.pem $out/cert.pem >/dev/null
    openssl verify -purpose sslserver -verify_hostname identity.terrace.invalid -CAfile $out/ca.pem $out/cert.pem >/dev/null
  '';
  base = import ./windmill-test.nix {
    inherit pkgs contract;
    fixtureProgram = "${fixture}/functional-fixture.ts";
    tlsFixture = certificate;
  };
  directory = "/var/lib/terrace-gateway-test";
  ownershipTag = "name=opt/terrace-windmill-functional,string=${builtins.hashFile "sha256" ./windmill-functional/functional-browser.py}";
  bootOptionsPresent = options:
    builtins.all (prefix: builtins.any (option: pkgs.lib.hasPrefix prefix option) options) ["-kernel " "-initrd " "-append " "-device virtio-net-pci," "-netdev vde,"]
    && builtins.any (option: pkgs.lib.hasInfix "vhost-user-fs-pci" option) options;
in
  base
  // {
    name = "terrace-windmill-functional";
    nodes = {
      host = {...}: {
        imports = [base.nodes.host];
        # Append a distinct key; replacing the option list removes direct boot,
        # shared filesystem and test-network definitions from Nixpkgs.
        virtualisation.qemu.options = ["-fw_cfg" ownershipTag];
        # Private fixture bootstrap only; seed removes the temporary IdP admin.
        systemd.services.keycloak.serviceConfig.EnvironmentFile = "${directory}/keycloak.env";
        environment.etc."gateway-protocol.py".source = ./website-gateway/protocol.py;
      };
      client = {...}: {
        imports = [base.nodes.client];
        virtualisation = {
          memorySize = 2048;
          cores = 1;
          qemu.options = ["-fw_cfg" ownershipTag];
        };
        environment.systemPackages = [pkgs.nssTools (pkgs.python3.withPackages (ps: [ps.playwright]))];
        environment.variables.TERRACE_BROWSER_BINARY = "${pkgs.chromium}/bin/chromium";
        environment.etc."windmill-functional-browser.py".source = ./windmill-functional/functional-browser.py;
      };
    };
    testScript = {nodes}:
      assert pkgs.lib.assertMsg (bootOptionsPresent nodes.host.virtualisation.qemu.options && bootOptionsPresent nodes.client.virtualisation.qemu.options) "Windmill functional guests require native direct boot, shared filesystem and test network options."; ''
        import shlex, tempfile
        from pathlib import Path

        def sql(query):
            return host.succeed("runuser -u postgres -- psql -w -XAt -d terrace_windmill -c " + shlex.quote(query) + " 2>/dev/null", timeout=10).strip()

        def browser(phase):
            try:
                assert client.succeed("python /etc/windmill-functional-browser.py " + phase, timeout=240).strip() == "Windmill functional browser phase " + phase + " passed."
                print("Windmill functional browser phase " + phase + " passed.")
            except Exception:
                try:
                    assert not str(client.shared_dir).startswith("/nix/store/")
                    private = Path(client.shared_dir) / ("windmill-browser-failure-" + phase)
                    private.mkdir(mode=0o700)
                    for name in ["failure-" + phase + ".json", "failure-" + phase + "-dom.json", "failure-" + phase + ".png"]:
                        client.execute("test ! -f /root/windmill-functional/" + name + " || (cp /root/windmill-functional/" + name + " /tmp/shared/" + private.name + "/ && chmod 0600 /tmp/shared/" + private.name + "/" + name + ")", timeout=10)
                    assert all(p.is_file() and not p.is_symlink() and p.stat().st_mode & 0o777 == 0o600 for p in private.iterdir())
                    print("Private Windmill browser evidence retained; content suppressed.")
                except Exception:
                    print("Private Windmill browser evidence unavailable; original failure retained.")
                raise

        def stopped(unit):
            host.succeed("systemctl stop " + unit, timeout=60)
            values = dict(line.split("=", 1) for line in host.succeed("systemctl show " + unit + " -p ActiveState,MainPID,ControlPID", timeout=10).splitlines())
            assert values == {"ActiveState":"inactive", "MainPID":"0", "ControlPID":"0"}

        try:
            start_all()
            host.wait_for_unit("keycloak.service", timeout=300)
            host.wait_for_unit("nginx.service")
            with subtest("reviewed synthetic enrollment and explicit migration/bootstrap"):
                host.succeed("python /etc/gateway-protocol.py seed", timeout=120)
                assert host.succeed("runuser -u postgres -- psql -w -XAt -d keycloak -c \"SELECT count(*) FROM user_entity WHERE username='gateway-fixture-bootstrap'\" 2>/dev/null", timeout=10).strip() == "0"
                host.succeed("chmod 0600 ${directory}/keycloak.env && truncate -s 0 ${directory}/keycloak.env && chmod 0400 ${directory}/keycloak.env")
                assert host.succeed("curl --silent --max-time 5 -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid", timeout=10).strip() == "503"
                host.succeed("systemctl start terrace-windmill-migrate.service", timeout=300)
                host.succeed("touch /run/windmill-test-release && systemctl start podman-terrace-windmill-server.service && systemctl start podman-terrace-windmill-worker.service", timeout=300)
                host.wait_until_succeeds("curl --fail --silent --max-time 5 https://windmill.terrace.invalid/api/version >/dev/null", timeout=90)
                assert sql("SELECT count(*) FROM password WHERE email IN ('local-member@terrace.invalid','local-alumni@terrace.invalid','local-editorial-admin@terrace.invalid') AND disabled=false AND login_type='keycloak'") == "3"
                assert sql("SELECT count(*) FROM password WHERE email='local-unapproved@terrace.invalid'") == "0"
                client.succeed("install -d -m 0700 /root/windmill-functional /root/.pki/nssdb && certutil -N -d sql:/root/.pki/nssdb --empty-password && certutil -A -d sql:/root/.pki/nssdb -n TerraceWindmillTest -t 'C,,' -i ${certificate}/ca.pem")
                assert not str(host.shared_dir).startswith("/nix/store/")
                with tempfile.TemporaryDirectory(prefix="windmill-private-", dir=host.shared_dir) as private:
                    name = private.rsplit("/", 1)[-1]
                    host.succeed("cp --preserve=mode ${directory}/windmill-browser.json /tmp/shared/" + name + "/credentials.json", timeout=10)
                    client.succeed("cp --preserve=mode /tmp/shared/" + name + "/credentials.json /root/windmill-functional/", timeout=10)
                for port in [18001,18002,18003,5432]:
                    client.fail("nc -z -w 2 host " + str(port), timeout=5)
            with subtest("individual native OIDC, operator permissions/isolation and real worker job"):
                browser("create")
                assert sql("SELECT count(*) FROM password WHERE email='local-unapproved@terrace.invalid'") == "0"
                before = sql("SELECT md5(string_agg(row_to_json(p)::text, chr(124) ORDER BY email)) FROM password p")
                stopped("podman-terrace-windmill-worker.service")
                stopped("podman-terrace-windmill-server.service")
                stopped("keycloak.service")
                host.succeed("systemctl restart postgresql.service && systemctl start keycloak.service && systemctl start podman-terrace-windmill-server.service && systemctl start podman-terrace-windmill-worker.service", timeout=300)
                assert sql("SELECT md5(string_agg(row_to_json(p)::text, chr(124) ORDER BY email)) FROM password p") == before
                browser("verify")
            with subtest("approval removal rejects old session and fresh native OIDC"):
                host.succeed("python - <<'PY'\nimport json,os\np='/var/lib/terrace-windmill-inputs/bootstrap.json'\nv=json.load(open(p));v['approvedUsers']=[u for u in v['approvedUsers'] if u['email']!='local-alumni@terrace.invalid']\nos.chmod(p,0o600)\nwith open(p,'w') as f:json.dump(v,f)\nos.chmod(p,0o400)\nPY", timeout=10)
                host.succeed("systemctl start terrace-windmill-bootstrap.service", timeout=120)
                assert sql("SELECT count(*) FROM password WHERE email='local-alumni@terrace.invalid' AND disabled=true") == "1"
                assert sql("SELECT count(*) FROM token WHERE email='local-alumni@terrace.invalid'") == "0"
                browser("disabled")
                delay = client.succeed("python -c \"import json; print(json.load(open('/root/windmill-functional/revocation.json'))['deniedAfterMs'])\"", timeout=10).strip()
                assert delay.isascii() and delay.isdigit() and 0 <= int(delay) <= 130000
                print("Windmill retained warm session denied after " + delay + " milliseconds.")
                assert sql("SELECT count(*) FROM password WHERE email='local-unapproved@terrace.invalid'") == "0"
            with subtest("final owned writers stopped and retained state"):
                for unit in ["podman-terrace-windmill-worker.service", "podman-terrace-windmill-server.service", "keycloak.service", "nginx.service", "postgresql.service"]:
                    stopped(unit)
                host.succeed("test -s /var/lib/terrace-windmill-inputs/bootstrap.json && test -s /var/lib/terrace-windmill/migrated-image")
                host.fail("podman container exists terrace-windmill-server")
                host.fail("podman container exists terrace-windmill-worker")
            host.succeed("listeners=$(ss -H -ltn '( sport = :443 or sport = :18001 or sport = :18002 or sport = :18003 or sport = :5432 )') && test -z \"$listeners\"", timeout=10)
            print("Windmill individual SSO, native isolation, real worker and retained restart checks passed.")
        except Exception:
            # Native module failures keep the existing bounded private evidence path.
            try:
                status, _ = host.execute("python /etc/windmill-evidence/capture.py >/dev/null 2>&1", timeout=45)
                if status == 0:
                    assert not str(host.shared_dir).startswith("/nix/store/")
                    private = Path(host.shared_dir) / "windmill-functional-runtime-failure"
                    private.mkdir(mode=0o700)
                    host.succeed("cp --preserve=mode /var/lib/windmill-test-evidence/raw.json /tmp/shared/" + private.name + "/raw.json", timeout=10)
                    artifact = private / "raw.json"
                    assert artifact.is_file() and not artifact.is_symlink() and artifact.stat().st_mode & 0o777 == 0o600
                    print("Private bounded Windmill runtime evidence retained; content suppressed.")
            except Exception:
                print("Private Windmill runtime evidence unavailable; original failure retained.")
            raise
      '';
  }
