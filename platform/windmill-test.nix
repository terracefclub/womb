{
  pkgs,
  contract,
}: let
  directory = "/var/lib/terrace-windmill-inputs";
  certificate = pkgs.runCommand "windmill-public-test-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir -p $out
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes -keyout $out/key.pem -out $out/cert.pem \
      -subj '/CN=windmill.terrace.invalid' -addext 'subjectAltName=DNS:windmill.terrace.invalid,DNS:identity.terrace.invalid' 2>/dev/null
  '';
  fixture = pkgs.runCommand "windmill-runtime-fixture" {} ''
    mkdir -p $out
    cp -R ${contract}/. $out/
    cp ${./windmill/fixture.ts} $out/windmill-fixture.ts
    cp ${./windmill/policy.ts} $out/policy.ts
  '';
in {
  name = "terrace-windmill-runtime";
  nodes = {
    host = {lib, ...}: {
      imports = [./windmill.nix ./identity.nix];
      virtualisation = {
        memorySize = 6144;
        cores = 2;
        diskSize = 16384;
        qemu.options = ["-fw_cfg" "name=opt/terrace-workload,string=windmill-runtime"];
      };
      security.pki.certificateFiles = ["${certificate}/cert.pem"];
      networking.hosts."127.0.0.1" = ["windmill.terrace.invalid" "identity.terrace.invalid"];
      terrace.identity = {
        enable = true;
        hostname = "identity.terrace.invalid";
        realmFile = "${directory}/terrace-realm.json";
        databasePasswordFile = "${directory}/db-password";
        certificateFile = "${directory}/cert.pem";
        certificateKeyFile = "${directory}/key.pem";
      };
      terrace.windmill = {
        enable = true;
        hostname = "windmill.terrace.invalid";
        certificateFile = "${directory}/cert.pem";
        certificateKeyFile = "${directory}/key.pem";
      };
      systemd.services.windmill-test-inputs = {
        requiredBy = ["nginx.service" "keycloak.service" "keycloakPostgreSQLInit.service"];
        before = ["nginx.service" "keycloak.service" "keycloakPostgreSQLInit.service"];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          UMask = "0077";
          StandardOutput = "null";
          StandardError = "null";
        };
        script = ''
          ${pkgs.bun}/bin/bun --no-env-file ${fixture}/windmill-fixture.ts
          cp ${certificate}/{cert,key}.pem ${directory}/
          chmod 0400 ${directory}/{cert,key}.pem
          install -m 0600 /dev/null /var/lib/windmill-test-launcher.log
        '';
      };
      # Hold the test's first bootstrap until the driver observes closed ingress.
      # This avoids racing a fixed sleep against cold image startup.
      systemd.services.podman-terrace-windmill-server.serviceConfig.ExecStartPost = lib.mkBefore [
        "${pkgs.writeShellScript "windmill-test-bootstrap-release" ''
          ${pkgs.coreutils}/bin/timeout 120 ${pkgs.bash}/bin/bash -c 'until test -e /run/windmill-test-release; do sleep 0.2; done'
        ''}"
      ];
      # Test-only protected capture also observes failures before Bun's handler.
      systemd.services.podman-terrace-windmill-server.serviceConfig = {
        StandardOutput = "append:/var/lib/windmill-test-launcher.log";
        StandardError = "append:/var/lib/windmill-test-launcher.log";
      };
      environment.systemPackages = [pkgs.curl pkgs.jq pkgs.python3 pkgs.postgresql_18];
      environment.etc."windmill-probe.py".source = ./windmill/probe.py;
      environment.etc."windmill-lock-probe.py".text = ''
        import os, pathlib, re
        path=pathlib.Path("/run/terrace-windmill-bootstrap.lock")
        owners=[]
        if path.exists():
            status=path.stat()
            for line in pathlib.Path("/proc/locks").read_text().splitlines():
                parts=line.split()
                if "->" in parts or "FLOCK" not in parts: continue
                for index,part in enumerate(parts):
                    match=re.fullmatch(r"([0-9a-f]+):([0-9a-f]+):([0-9]+)",part)
                    if match and (int(match[1],16),int(match[2],16),int(match[3]))==(os.major(status.st_dev),os.minor(status.st_dev),status.st_ino):
                        pid=parts[index-1]
                        if pid.isdigit(): owners.append(int(pid))
        print("Windmill exact lock holder PIDs:", sorted(set(owners)))
      '';
      environment.etc."windmill-resource-probe.py".text = ''
        import re, subprocess
        unit="podman-terrace-windmill-server.service"
        for key in ["MemoryPeak", "MemoryCurrent", "TasksCurrent"]:
            value=subprocess.run(["${pkgs.systemd}/bin/systemctl","show",unit,"-p",key,"--value"],capture_output=True,text=True,timeout=5).stdout.strip()
            if value.isdigit(): print("Windmill resource "+key+": "+value)
        group=subprocess.run(["${pkgs.systemd}/bin/systemctl","show",unit,"-p","ControlGroup","--value"],capture_output=True,text=True,timeout=5).stdout.strip()
        if re.fullmatch(r"/system.slice/[a-zA-Z0-9_.-]+",group):
            import pathlib
            for name in ["memory.events", "pids.events"]:
                path=pathlib.Path("/sys/fs/cgroup"+group)/name
                if path.is_file():
                    for line in path.read_text().splitlines():
                        parts=line.split()
                        if len(parts)==2 and parts[0] in ["low","high","max","oom","oom_kill","oom_group_kill","sock_throttled"] and parts[1].isdigit():print("Windmill resource "+name+" "+parts[0]+": "+parts[1])
      '';
      systemd.services.podman-terrace-windmill-server.serviceConfig.ExecStopPost = lib.mkBefore ["${pkgs.python3}/bin/python /etc/windmill-resource-probe.py"];
      environment.etc."windmill-evidence".source = ./windmill;
      networking.firewall.allowedTCPPorts = [18001 18002 18003 5432];
    };
    client = {nodes, ...}: {
      networking.hosts.${nodes.host.networking.primaryIPAddress} = ["windmill.terrace.invalid" "identity.terrace.invalid"];
      security.pki.certificateFiles = ["${certificate}/cert.pem"];
      environment.systemPackages = [pkgs.curl pkgs.netcat-openbsd];
      networking.firewall.allowedTCPPorts = [18080];
      systemd.services.synthetic-peer = {
        wantedBy = ["multi-user.target"];
        serviceConfig = {
          ExecStart = "${pkgs.python3}/bin/python -m http.server 18080 --bind 0.0.0.0 --directory /tmp";
          User = "nobody";
          MemoryMax = "64M";
          TimeoutStopSec = 5;
          StandardOutput = "null";
          StandardError = "null";
        };
      };
    };
  };
  testScript = ''
    def stop_test_blocker(unit):
        assert unit in ["windmill-test-positive.service", "windmill-test-db-block.service", "windmill-test-cancel-block.service"]
        status, _ = host.execute("systemctl stop " + unit + " >/dev/null 2>&1", timeout=10)
        print("Windmill test blocker:", unit)
        print("Windmill test blocker original stop status:", status)
        def stopped_properties(label):
            text = host.succeed("systemctl show " + unit + " -p LoadState,ActiveState,MainPID,ControlPID,ControlGroup,ExecMainStatus", timeout=10)
            values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            for key in ["MainPID", "ControlPID", "ExecMainStatus"]:
                assert values[key].isascii() and values[key].isdigit()
                print("Windmill test blocker " + label + " " + key + ":", int(values[key]))
            active = values["ActiveState"]
            print("Windmill test blocker " + label + " state:", active if active in ["inactive", "failed", "active", "activating", "deactivating"] else "unrecognized")
            assert values["LoadState"] in ["loaded", "not-found"]
            assert values["MainPID"] == "0" and values["ControlPID"] == "0"
            group = values.get("ControlGroup", "")
            if group:
                assert group == "/system.slice/" + unit
                path = "/sys/fs/cgroup" + group
                host.succeed("test ! -d " + path + " || { pids=$(find " + path + " -name cgroup.procs -exec cat {} +) && test -z \"$pids\"; }", timeout=10)
            return values
        original = stopped_properties("post-stop")
        assert original["ActiveState"] in ["inactive", "failed"]
        if status != 0:
            # A completed transient unit may already have been collected. Do
            # not ignore other stop failures or accept a still-loaded unit.
            assert status == 5
            assert original["LoadState"] == "not-found" and original["ActiveState"] == "inactive"
        elif original["ActiveState"] == "failed":
            # Measured systemd261 behavior: a successful stop retains failed
            # state. Only reset this exact stopped, empty test-owned unit.
            host.succeed("systemctl reset-failed " + unit + " >/dev/null 2>&1", timeout=10)
        assert stopped_properties("final")["ActiveState"] == "inactive"

    try:
        start_all()
        host.wait_for_unit("nginx.service")
        host.wait_for_unit("postgresql.target")
        host.wait_for_unit("keycloak.service", timeout=300)
        with subtest("real user-free identity issuer and trusted loopback discovery"):
            host.succeed("curl --fail --silent --max-time 10 https://identity.terrace.invalid/realms/terrace/.well-known/openid-configuration | jq -e '.issuer == \"https://identity.terrace.invalid/realms/terrace\"' >/dev/null")
            host.succeed("runuser -u terrace_windmill -- curl --fail --silent --max-time 10 https://identity.terrace.invalid/realms/terrace/.well-known/openid-configuration >/dev/null")
            host.succeed("runuser -u postgres -- psql -XAt -d keycloak -c 'SELECT count(*) FROM user_entity' | grep -qx 0")
        status, _ = host.execute("umask 077; ${pkgs.bun}/bin/bun --no-env-file --eval 'console.log(1)' >/var/lib/windmill-test-bun.log 2>&1")
        print("Bun bootstrap interpreter preflight exit status:", status)
        assert status == 0
        host.fail("runuser -u nobody -- test -r /var/lib/windmill-test-launcher.log")
        with subtest("closed before explicit migration and bootstrap"):
            assert client.succeed("curl -s -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid/").strip() == "503"
            host.fail("systemctl start podman-terrace-windmill-server.service")
            host.succeed("systemctl reset-failed podman-terrace-windmill-server.service")
        with subtest("actual unchanged image migrations and closed delayed bootstrap"):
            status, _ = host.execute("systemctl start terrace-windmill-migrate.service", timeout=300)
            if status != 0:
                raise Exception("Windmill migration failed; filtered diagnostic classifications above.")
            host.succeed("systemctl start --no-block podman-terrace-windmill-server.service")
            host.wait_for_open_port(18001, timeout=120)
            assert client.succeed("curl -s -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid/").strip() == "503"
            host.succeed("touch /run/windmill-test-release")
            host.wait_until_succeeds("grep -Fxq 'Windmill launcher stage: waiting for ownership' /var/lib/windmill-test-launcher.log", timeout=30)
            print(host.succeed("python /etc/windmill-lock-probe.py"))
            host.wait_for_unit("podman-terrace-windmill-server.service", timeout=180)
            host.succeed("systemctl start podman-terrace-windmill-worker.service")
            host.succeed("systemctl start terrace-windmill-bootstrap.service")
            assert client.succeed("curl -s -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid/").strip() == "200"
            assert client.succeed("curl -s -X POST -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid/api/auth/login").strip() == "403"
            client.fail("nc -z -w 2 host 18001")
            client.fail("nc -z -w 2 host 5432")
        with subtest("factory rejected and actual bounded worker execution"):
            host.succeed("python /etc/windmill-probe.py", timeout=90)
            host.succeed("runuser -u terrace_windmill -- psql -XAt -d terrace_windmill -c 'select 1' >/dev/null")
            host.fail("runuser -u terrace_windmill -- psql -XAt -d postgres -c 'select rolpassword from pg_authid' >/dev/null 2>&1")
        with subtest("UID-scoped egress isolation and unmodified nonprivileged image"):
            host.succeed("curl --fail --silent --max-time 3 http://client:18080/ >/dev/null")
            host.fail("runuser -u terrace_windmill -- curl --fail --silent --max-time 3 http://client:18080/ >/dev/null 2>&1")
            host.succeed("curl --fail --silent --max-time 3 http://client:18080/ >/dev/null")
            host.succeed("podman inspect terrace-windmill-server | jq -e '.[0] | .Config.User == \"44123:44123\" and .HostConfig.Privileged == false and .HostConfig.ReadonlyRootfs == true' >/dev/null")
        with subtest("changed input during concurrent bootstrap cannot publish stale readiness"):
            host.succeed("systemd-run --unit=windmill-test-db-block --property=RuntimeMaxSec=15 --uid=postgres ${pkgs.postgresql_18}/bin/psql -X -d terrace_windmill -c 'BEGIN; LOCK TABLE global_settings IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(8); COMMIT' >/dev/null 2>&1")
            host.wait_until_succeeds("runuser -u postgres -- psql -XAt -d terrace_windmill -c \"SELECT EXISTS(SELECT FROM pg_locks l JOIN pg_class c ON l.relation=c.oid WHERE c.relname='global_settings' AND l.mode='AccessExclusiveLock' AND l.granted)\" | grep -qx t")
            host.succeed("systemd-run --unit=windmill-test-positive --property=RuntimeMaxSec=90 --property=LogNamespace=terrace-apps /run/current-system/sw/bin/terrace-windmill-bootstrap >/dev/null 2>&1")
            host.wait_until_succeeds("journalctl --namespace=terrace-apps -u windmill-test-positive.service --no-pager -o cat | grep -Fxq 'Windmill bootstrap stage: protected input validated.'")
            host.succeed("chmod 0600 /var/lib/terrace-windmill-inputs/bootstrap.json && systemctl start --no-block terrace-windmill-bootstrap.service")
            host.wait_until_succeeds("systemctl is-failed --quiet windmill-test-positive.service")
            host.wait_until_succeeds("systemctl is-failed --quiet terrace-windmill-bootstrap.service")
            assert client.succeed("curl -s -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid/").strip() == "503"
            stop_test_blocker("windmill-test-positive.service")
            stop_test_blocker("windmill-test-db-block.service")
            host.succeed("chmod 0400 /var/lib/terrace-windmill-inputs/bootstrap.json && systemctl reset-failed terrace-windmill-bootstrap.service && systemctl start terrace-windmill-bootstrap.service")
        with subtest("cancelled owning bootstrap leaves ingress closed"):
            host.succeed("systemd-run --unit=windmill-test-cancel-block --property=RuntimeMaxSec=15 --uid=postgres ${pkgs.postgresql_18}/bin/psql -X -d terrace_windmill -c 'BEGIN; LOCK TABLE global_settings IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(8); COMMIT' >/dev/null 2>&1")
            host.wait_until_succeeds("runuser -u postgres -- psql -XAt -d terrace_windmill -c \"SELECT EXISTS(SELECT FROM pg_locks l JOIN pg_class c ON l.relation=c.oid WHERE c.relname='global_settings' AND l.mode='AccessExclusiveLock' AND l.granted)\" | grep -qx t")
            host.succeed("systemctl start --no-block terrace-windmill-bootstrap.service")
            host.wait_until_succeeds("test \"$(systemctl show terrace-windmill-bootstrap.service -p ActiveState --value)\" = activating && test ! -e /run/terrace-windmill-ready")
            host.succeed("systemctl kill --kill-whom=main --signal=SIGTERM terrace-windmill-bootstrap.service")
            host.wait_until_succeeds("systemctl is-failed --quiet terrace-windmill-bootstrap.service")
            assert client.succeed("curl -s -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid/").strip() == "503"
            stop_test_blocker("windmill-test-cancel-block.service")
            host.succeed("systemctl reset-failed terrace-windmill-bootstrap.service && systemctl start terrace-windmill-bootstrap.service")
        with subtest("failed rebootstrap closes previously ready ingress"):
            host.succeed("chmod 0600 /var/lib/terrace-windmill-inputs/bootstrap.json")
            host.fail("systemctl start terrace-windmill-bootstrap.service")
            assert client.succeed("curl -s -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid/").strip() == "503"
            host.succeed("chmod 0400 /var/lib/terrace-windmill-inputs/bootstrap.json && systemctl reset-failed terrace-windmill-bootstrap.service && systemctl start terrace-windmill-bootstrap.service")
            assert client.succeed("curl -s -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid/").strip() == "200"
        with subtest("explicit service stop leaves persistent schema and runtime inputs"):
            host.succeed("systemctl stop podman-terrace-windmill-worker.service podman-terrace-windmill-server.service")
            assert client.succeed("curl -s -o /dev/null -w '%{http_code}' https://windmill.terrace.invalid/").strip() == "503"
            host.succeed("runuser -u postgres -- psql -XAt -d terrace_windmill -c 'select count(*) from password' >/dev/null && test -s /var/lib/terrace-windmill-inputs/bootstrap.json")
            host.succeed("test -z \"$(podman ps -q)\"")
            host.succeed("systemctl stop keycloak.service nginx.service postgresql.target postgresql.service")
        print("Actual Windmill runtime, explicit migration, repeated approved bootstrap and closed factory ingress checks passed.")
    except Exception:
        try:
            capture_status, summary = host.execute("python /etc/windmill-evidence/capture.py 2>/dev/null", timeout=30)
            if capture_status == 0:
                print(summary)
            else:
                print("Windmill private capture command did not complete; raw output suppressed.")
            # --keep-failed retains this private build/shared directory. Never
            # export raw evidence with copy_from_vm: that targets public $out.
            from pathlib import Path
            assert not str(host.shared_dir).startswith("/nix/store/")
            private = Path(host.shared_dir) / "windmill-failure"
            private.mkdir(mode=0o700)
            host.succeed("cp --preserve=mode /var/lib/windmill-test-evidence/raw.json /tmp/shared/windmill-failure/raw.json 2>/dev/null")
            artifact = private / "raw.json"
            assert artifact.is_file() and not artifact.is_symlink() and artifact.stat().st_mode & 0o777 == 0o600
            print("Windmill protected raw evidence exported privately before teardown; content suppressed.")
        except Exception:
            print("Windmill private failure evidence unavailable; original failure preserved.")
        raise

  '';
}
