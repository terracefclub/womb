# VM proof of ACME issuance and renewal for the production roles (docs/tls.md):
# the SAME app and relay role modules with `instance = "rehearsal"`, synthetic
# .test names and a LOCAL ACME server (pebble) on the test network. Its CA is
# trusted only inside these guests, through the rehearsal harness
# (tls-rehearsal.nix). No request leaves the test network: no public CA, no
# public DNS. Every key is generated at build or test time, never committed.
{
  pkgs,
  release,
  contract,
  sopsModule,
}: let
  lib = pkgs.lib;
  names = ["web.terrace.test" "cms.terrace.test" "identity.terrace.test"];
  relayName = "relay.terrace.test";
  tool = pkgs.callPackage ./package.nix {};
  # Test-only SSH identities, as in test.nix: the operator's client key and the
  # synthetic backup repository's host key (pinned by the hosts).
  keys = pkgs.runCommand "terrace-tls-rehearsal-keys" {nativeBuildInputs = [pkgs.openssh];} ''
    mkdir $out
    ssh-keygen -q -t ed25519 -N "" -C rehearsal-operator -f $out/operator
    ssh-keygen -q -t ed25519 -N "" -C rehearsal-repository -f $out/repository-host
  '';
  publicKey = file: lib.removeSuffix "\n" (builtins.readFile "${keys}/${file}.pub");
  escrow = pkgs.runCommand "terrace-tls-rehearsal-escrow" {nativeBuildInputs = [pkgs.age];} ''
    mkdir $out
    age-keygen -o $out/escrow.age 2>/dev/null
    age-keygen -y $out/escrow.age >$out/escrow.pub
  '';
  escrowRecipient = lib.removeSuffix "\n" (builtins.readFile "${escrow}/escrow.pub");
  tlsSettings = {
    mode = "acme";
    acme = {
      directory = "https://acme.test:14000/dir";
      acceptTerms = true;
      contact = "tls-rehearsal@terrace.test";
    };
  };
  # Pebble's own HTTPS (directory) certificate is made at boot inside the acme
  # guest; its issuing roots are new on every pebble start and are fetched from
  # the management API by the test.
  pebbleConfig = pkgs.writeText "terrace-pebble.json" (builtins.toJSON {
    pebble = {
      listenAddress = "0.0.0.0:14000";
      managementListenAddress = "0.0.0.0:15000";
      certificate = "/run/pebble-tls/cert.pem";
      privateKey = "/run/pebble-tls/key.pem";
      httpPort = 80;
      tlsPort = 443;
      ocspResponderURL = "";
      externalAccountBindingRequired = false;
      # 90 days, like the public CA's default profile.
      certificateValidityPeriod = 7776000;
    };
  });
  operatorKeys = [(publicKey "operator")];
in {
  name = "terrace-production-tls";
  nodes = {
    # The local ACME server. `acme.test` resolves to it on every guest (test network hosts).
    acme = {nodes, ...}: {
      virtualisation = {
        memorySize = 1024;
        cores = 1;
      };
      networking.domain = "test";
      # The CA validates http-01 by connecting to the names' hosts on port 80.
      networking.hosts = {
        ${nodes.app.networking.primaryIPAddress} = names;
        ${nodes.relay.networking.primaryIPAddress} = [relayName];
      };
      networking.firewall.allowedTCPPorts = [14000 15000];
      environment.systemPackages = [pkgs.curl pkgs.openssl pkgs.iptables];
      systemd.services.pebble-tls = {
        description = "Rehearsal: make the local ACME server's HTTPS certificate";
        requiredBy = ["pebble.service"];
        before = ["pebble.service"];
        path = [pkgs.openssl pkgs.coreutils];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          RuntimeDirectory = "pebble-tls";
          UMask = "0077";
        };
        script = ''
          cd /run/pebble-tls
          openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 -sha256 -days 30 \
            -subj /CN=terrace-rehearsal-acme-server-ca -keyout ca-key.pem -out ca.pem \
            -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign 2>/dev/null
          openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 -subj /CN=acme.test -keyout key.pem -out req.pem 2>/dev/null
          printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:acme.test\n' >ext
          openssl x509 -req -in req.pem -CA ca.pem -CAkey ca-key.pem -set_serial "0x$(openssl rand -hex 8)" -days 30 -sha256 -extfile ext -out cert.pem 2>/dev/null
          rm ca-key.pem req.pem ext
          chmod 0644 ca.pem cert.pem
        '';
      };
      systemd.services.pebble = {
        description = "Rehearsal: local ACME server (pebble)";
        wantedBy = ["multi-user.target"];
        after = ["network.target"];
        environment = {
          PEBBLE_VA_NOSLEEP = "1";
          PEBBLE_WFE_NONCEREJECT = "0";
          # Every order is validated afresh: the proof sees each http-01 challenge.
          PEBBLE_AUTHZREUSE = "0";
        };
        serviceConfig.ExecStart = "${pkgs.pebble}/bin/pebble -config ${pebbleConfig}";
      };
    };
    # Operator workstation, HTTPS client and synthetic SFTP backup repository.
    operator = {...}: {
      virtualisation = {
        memorySize = 1536;
        cores = 1;
      };
      environment.systemPackages = [tool pkgs.sops pkgs.age pkgs.ssh-to-age pkgs.openssl pkgs.jq pkgs.curl pkgs.netcat-openbsd];
      services.openssh = {
        enable = true;
        hostKeys = [
          {
            type = "ed25519";
            path = "/etc/ssh/ssh_host_ed25519_key";
          }
        ];
        extraConfig = ''
          Match User restic
            ForceCommand internal-sftp
            AllowTcpForwarding no
            PermitTTY no
        '';
      };
      users.users.restic.isNormalUser = true;
      system.activationScripts.rehearsalRepositoryHostKey = ''
        install -d -m 0755 /etc/ssh
        install -m 0600 ${keys}/repository-host /etc/ssh/ssh_host_ed25519_key
        install -m 0644 ${keys}/repository-host.pub /etc/ssh/ssh_host_ed25519_key.pub
      '';
    };
    app = {...}: {
      imports = [sopsModule ./app.nix ./tls-rehearsal.nix];
      virtualisation = {
        memorySize = 6144;
        cores = 2;
        diskSize = 4096;
      };
      terrace.production = {
        instance = "rehearsal";
        memoryMiB = 9216;
        inherit escrowRecipient operatorKeys;
        tls = tlsSettings;
        app = {
          webHostname = "web.terrace.test";
          cmsHostname = "cms.terrace.test";
          identityHostname = "identity.terrace.test";
          identityContract = contract;
          backup.primary = {
            repository = "sftp:restic@operator:repository";
            knownHosts = "operator ${publicKey "repository-host"}";
          };
        };
      };
      terrace.website = {
        webPackage = release.web;
        cmsPackage = release.cms;
        inherit (release) revision;
      };
    };
    relay = {...}: {
      imports = [sopsModule ./relay.nix ./tls-rehearsal.nix];
      virtualisation = {
        memorySize = 1536;
        cores = 1;
      };
      terrace.production = {
        instance = "rehearsal";
        memoryMiB = 2048;
        inherit escrowRecipient operatorKeys;
        tls = tlsSettings;
        relay = {
          hostname = relayName;
          publicIPv4 = "192.0.2.10";
          backup.primary = {
            repository = "sftp:restic@operator:relay-repository";
            knownHosts = "operator ${publicKey "repository-host"}";
          };
        };
      };
    };
  };
  testScript = ''
    import base64, json, os, re, shlex, time

    checks = []
    def ok(condition, label):
        assert condition, label
        checks.append(label)

    STORE = "/root/store"
    ESCROW = "/root/escrow/escrow.age"
    ENV = f"export TERRACE_SECRETS_STORE={STORE} SOPS_AGE_KEY_FILE={ESCROW} TERRACE_SECRETS_SCRATCH=/dev/shm TERRACE_ESCROW_RECIPIENT=${escrowRecipient}; "
    NAMES = ${builtins.toJSON names}
    RELAY = "${relayName}"
    HARNESS = "/var/lib/terrace-tls-rehearsal"
    RESTORE = "/var/lib/terrace-restore"
    MEDIA = "/var/lib/terrace-cms-media"
    OUTBOX = "/var/lib/terrace-cms-outbox"
    WRITERS = ["postgresql", "keycloak", "nginx", "terrace-cms", "terrace-web"]
    CHALLENGE = "/var/lib/acme/acme-challenge/.well-known/acme-challenge"
    OIDC = "https://identity.terrace.test/realms/terrace/.well-known/openid-configuration"

    def live(cert):
        return "/var/lib/acme/" + cert

    def order(cert):
        return "acme-order-renew-" + cert + ".service"

    def op(command, **kwargs):
        return operator.succeed(ENV + command, **kwargs)

    def put(machine, path, text, mode="0644"):
        # Test-time files only (the local CA's public certificates, synthetic test leaves).
        data = base64.b64encode(text.encode()).decode()
        machine.succeed(f"{{ test -d $(dirname {path}) || install -d -m 0755 $(dirname {path}); }} && echo {data} | base64 -d >{path}.new && chmod {mode} {path}.new && mv {path}.new {path}")

    def state(machine, unit, prop):
        return machine.succeed(f"systemctl show {unit} -p {prop} --value").strip()

    def failed_units(machine):
        return machine.succeed("systemctl list-units --failed --plain --no-legend --no-pager | cut -d' ' -f1").split()

    def address(host):
        return operator.succeed(f"getent hosts {host} | cut -d' ' -f1").strip()

    def fingerprint(machine, path):
        return machine.succeed(f"openssl x509 -noout -fingerprint -sha256 -in {path}").strip()

    def served(host, name, verify=True):
        # What a client sees: chain verified against the local CA's roots, name checked.
        flags = f"-verify_return_error -verify_hostname {name} -CAfile /root/roots.pem" if verify else ""
        return operator.succeed(f"echo | openssl s_client -connect {host}:443 -servername {name} {flags} 2>/dev/null | openssl x509 -noout -fingerprint -sha256").strip()

    def account_hash(machine):
        return machine.succeed("find /var/lib/acme/.lego/accounts -type f -name '*.key' -print0 | sort -z | xargs -0 cat | sha256sum").strip()

    def http(host, name, path, extra=""):
        return operator.succeed(f"curl -s -o /dev/null -w '%{{http_code}} %{{redirect_url}}' {extra} --resolve {name}:80:{address(host)} 'http://{name}{path}'").strip()

    def harness(machine):
        # Rehearsal trust only: the ACME client trusts the local server's HTTPS, the expiry check its roots.
        put(machine, HARNESS + "/acme-server-ca.pem", server_ca)
        put(machine, HARNESS + "/roots.pem", roots)

    def stage(host):
        source = "/dev/shm/terrace-stage-" + host
        operator.succeed(f"rm -rf /tmp/shared/stage/{host} && install -d -m 0700 /tmp/shared/stage/{host}")
        operator.succeed(f"cp {source}/ssh_host_ed25519_key {source}/ssh_host_ed25519_key.pub /tmp/shared/stage/{host}/")

    def unstage(host):
        operator.succeed(f"rm -rf /tmp/shared/stage/{host}")
        op("terrace-secrets host unstage " + host)

    def push(host, count):
        out = op(f"terrace-secrets push {host} --address {host} --identity /root/.ssh/operator --rehearsal")
        ok(f"PASS {count} secrets" in out and f"Installed {count} encrypted secrets." in out, f"{host}: push of {count} secrets (no tls/*)")

    def enrol_repository(host, directory):
        public = op(f"terrace-secrets public {host} backup/primary/sftp-key").strip()
        operator.succeed(f"install -d -o restic -g users -m 0700 /home/restic/.ssh /home/restic/{directory} && echo {shlex.quote(public)} >>/home/restic/.ssh/authorized_keys && chown restic:users /home/restic/.ssh/authorized_keys && chmod 0600 /home/restic/.ssh/authorized_keys")

    def issued(machine, cert):
        machine.wait_until_succeeds(f"test -e {live(cert)}/acme-success && test \"$(systemctl show {order(cert)} -p ActiveState --value)\" = inactive", timeout=300)
        ok(state(machine, order(cert), "Result") == "success", f"{machine.name}: order unit succeeded")

    def tls_check(machine, expect_ok, label):
        if expect_ok:
            machine.succeed("systemctl start terrace-tls-check.service", timeout=120)
        else:
            machine.fail("systemctl start terrace-tls-check.service", timeout=120)
        output = machine.succeed("journalctl -u terrace-tls-check.service -o cat --no-pager -n 20")
        failed = machine.succeed("systemctl is-failed terrace-tls-check.service || true").strip() == "failed"
        ok(failed != expect_ok and (expect_ok or "terrace-tls-check.service" in failed_units(machine)), label)
        return output

    def no_failed_units(machine, label):
        units = failed_units(machine)
        ok(units == [], f"{label}: no failed units {units}")

    started_at = time.monotonic()

    with subtest("local ACME server on the test network"):
        acme.start()
        acme.wait_for_unit("pebble.service")
        acme.wait_for_open_port(14000)
        acme.wait_for_open_port(15000)
        server_ca = acme.succeed("cat /run/pebble-tls/ca.pem")
        acme.succeed("curl -fsS --cacert /run/pebble-tls/ca.pem https://acme.test:14000/dir >/dev/null")
        roots = acme.wait_until_succeeds("curl -fsS --cacert /run/pebble-tls/ca.pem https://acme.test:15000/roots/0")
        ok("BEGIN CERTIFICATE" in roots, "local CA root fetched at test time")

    with subtest("operator: an ACME host receives no tls/* secrets"):
        operator.start()
        operator.wait_for_unit("sshd.service")
        operator.succeed("install -d -m 0700 /root/escrow /root/.ssh " + STORE + " && install -m 0600 ${keys}/operator /root/.ssh/operator")
        operator.succeed("install -m 0600 ${escrow}/escrow.age " + ESCROW + " && install -m 0644 ${escrow}/escrow.pub " + STORE + "/escrow.recipient")
        put(operator, "/root/roots.pem", roots)
        op("terrace-secrets host create app --role app --feature acme-tls")
        out = op("terrace-secrets generate app")
        ok("Generated 10 secrets for app. Supply with `set`: none." in out, out.strip())
        ok("app: 10 secrets encrypted to exactly" in op("terrace-secrets check app"), "app store complete without a certificate")
        refused = operator.fail(ENV + "terrace-secrets set app tls/key </dev/null 2>&1")
        ok("is not a secret of this host" in refused, "tls/key refused for an ACME host")
        enrol_repository("app", "repository")
        stage("app")

    with subtest("first boot: nothing of ACME runs and port 80 is closed before adoption"):
        app.start()
        app.wait_for_unit("multi-user.target")
        unstage("app")
        push("app", 9)
        ok(app.succeed("terrace-secrets verify").startswith("terrace-secrets verify: PASS 9 secrets"), "app secrets verify")
        app.fail("test -e /run/secrets/tls")
        harness(app)
        for unit in ["acme-web.terrace.test.service", order("web.terrace.test"), "nginx.service", "acme-renew-web.terrace.test.timer", "terrace-tls-check.timer"]:
            ok(state(app, unit, "ActiveState") == "inactive", f"{unit} inactive before adoption")
        ok(state(app, order("web.terrace.test"), "ExecMainStartTimestampMonotonic") == "0", "no certificate order before adoption")
        app.fail("test -e " + live("web.terrace.test"))
        for port in [80, 443]:
            operator.fail(f"nc -z -w 2 app {port}")
        ok(True, "ports 80 and 443 closed before adoption")

    with subtest("adoption: placeholder first, then issuance by http-01 from the local CA"):
        app.succeed("terrace-production adopt --new")
        app.wait_for_unit("nginx.service")
        issued(app, "web.terrace.test")
        ok(app.succeed("journalctl -u acme-web.terrace.test.service -o cat --no-pager | grep -c minica").strip() != "0" and app.succeed("test -e /var/lib/acme/.minica/cert.pem && echo yes").strip() == "yes", "placeholder generated before nginx started")
        order_log = app.succeed("journalctl -u " + order("web.terrace.test") + " -o cat --no-pager")
        ok("acme.test:14000" in order_log and "HTTP-01" in order_log, "lego ordered from the local CA by http-01")
        script = app.succeed("systemctl show " + order("web.terrace.test") + " -p ExecStart --value | grep -o '/nix/store/[^ ;]*' | head -n 1").strip()
        ok(app.succeed(f"grep -c 'https://acme.test:14000/dir' {script} || true").strip() != "0" and app.succeed(f"grep -c 'letsencrypt\\|api.pki\\|zerossl' {script} || true").strip() == "0", "the order unit names only the local CA")
        issuer = app.succeed("openssl x509 -noout -issuer -in " + live("web.terrace.test") + "/cert.pem")
        ok("Pebble Intermediate" in issuer, f"issued by the local CA: {issuer.strip()}")
        sans = sorted(re.findall(r"DNS:([a-z0-9.-]+)", app.succeed("openssl x509 -noout -ext subjectAltName -in " + live("web.terrace.test") + "/cert.pem")))
        ok(sans == sorted(NAMES), f"one certificate names exactly the three public hostnames {sans}")
        current = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        app.wait_until_succeeds("test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername web.terrace.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(current), timeout=60)
        for name in NAMES:
            ok(served("app", name) == current, f"{name}: issued chain served and verified")
        ok(state(app, "nginx.service", "NRestarts") == "0", "nginx picked up the certificate by reload, no restart")
        ok(app.succeed("journalctl -u nginx.service -o cat --no-pager | grep -c 'Reloaded\\|Reloading' || true").strip() != "0", "nginx reloaded after issuance")
        app.wait_for_unit("keycloak.service", timeout=360)
        ok(operator.succeed(f"curl -s -o /dev/null -w '%{{http_code}}' --cacert /root/roots.pem --resolve identity.terrace.test:443:{address('app')} {OIDC}").strip() == "200", "HTTPS identity endpoint with the issued certificate")

    with subtest("port 80: only the challenge and the redirect"):
        for name in NAMES:
            ok(http("app", name, "/members/x?y=1") == f"301 https://{name}/members/x?y=1", f"{name}: port 80 redirects to https")
        ok(http("app", "web.terrace.test", "/api/lifecycle/redeem", "-X POST --data x=1") == "301 https://web.terrace.test/api/lifecycle/redeem", "a POST on port 80 is redirected, never proxied")
        probe = "terrace-probe-" + os.urandom(8).hex()
        app.succeed(f"echo {probe} >{CHALLENGE}/{probe}")
        body = operator.succeed(f"curl -fsS -D /tmp/probe-headers --resolve web.terrace.test:80:{address('app')} http://web.terrace.test/.well-known/acme-challenge/{probe}").strip()
        ok(body == probe and "text/plain" in operator.succeed("cat /tmp/probe-headers").lower(), "challenge files served on port 80")
        app.succeed(f"rm {CHALLENGE}/{probe}")
        ok(http("app", "cms.terrace.test", "/.well-known/acme-challenge/missing").startswith("404"), "unknown challenge token is 404, not a redirect")
        ok(http("app", "other.terrace.test", "/").startswith("421") and http("app", "other.terrace.test", f"/.well-known/acme-challenge/{probe}").startswith("421"), "unknown host on port 80 gets 421")
        listeners = app.succeed("ss -H -ltn | awk '{print $4}'").split()
        public = sorted({line for line in listeners if not re.search(r"^(127\.0\.0\.1|\[::1\]|\[::ffff:127\.0\.0\.1\])(%[a-z0-9]+)?:[0-9]+$", line)})
        ok(public == sorted(["0.0.0.0:22", "0.0.0.0:443", "0.0.0.0:80", "[::]:22", "[::]:443", "[::]:80"]), f"public TCP listeners {public}")

    with subtest("owners and modes; keys never leave /var/lib/acme"):
        directory = live("web.terrace.test")
        ok(app.succeed(f"stat -c '%a %U %G' {directory}").strip() == "750 acme nginx", "certificate directory 0750 acme:nginx")
        modes = app.succeed(f"find {directory} -type f -printf '%m %u %g %f\\n' | sort").strip().splitlines()
        ok(all(line.startswith("640 acme nginx ") for line in modes) and any(line.endswith(" key.pem") for line in modes), f"certificate files 0640 acme:nginx {modes}")
        stored = app.succeed("find /var/lib/acme/.lego/web.terrace.test -type f -printf '%m %u %g\\n' | sort -u").strip().splitlines()
        ok(stored == ["640 acme nginx"], f"client copies 0640 acme:nginx {stored}")
        accounts = app.succeed("find /var/lib/acme/.lego/accounts -type f -printf '%m %u\\n' | sort -u").strip().splitlines()
        ok(accounts == ["600 acme"], f"account key 0600 acme {accounts}")
        ok(app.succeed("stat -c '%a %U' /var/lib/acme/.lego/accounts").strip() == "700 acme", "accounts directory 0700 acme")
        for user in ["terrace_cms", "terrace_web", "keycloak", "nobody"]:
            app.fail(f"runuser -u {user} -- cat {directory}/key.pem >/dev/null 2>&1")
        ok(True, "service accounts cannot read the TLS key")

    with subtest("expiry check passes on the fresh certificate"):
        output = tls_check(app, True, "expiry check passes on a fresh certificate")
        ok(all(f"{name}: OK: expires" in output for name in NAMES), "expiry check reports every name")
        status = app.succeed("terrace-tls status")
        ok("mode: acme" in status and "Pebble Intermediate" in status and "PLACEHOLDER" not in status, "terrace-tls status")
        ok(state(app, "acme-renew-web.terrace.test.timer", "ActiveState") == "active" and state(app, "terrace-tls-check.timer", "ActiveState") == "active", "renewal and expiry timers armed by adoption")

    with subtest("a routine renewal run is a no-op; a forced renewal reloads nginx without dropping connections"):
        before = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        account = account_hash(app)
        app.succeed("systemctl start " + order("web.terrace.test"), timeout=300)
        ok(fingerprint(app, live("web.terrace.test") + "/cert.pem") == before, "renewal not yet due: certificate unchanged")
        pid, restarts = state(app, "nginx.service", "MainPID"), state(app, "nginx.service", "NRestarts")
        probe = f"for i in $(seq 1 80); do curl -s -o /dev/null -w '%{{http_code}}\\n' --max-time 3 --cacert /root/roots.pem --resolve identity.terrace.test:443:{address('app')} {OIDC} >>/root/probe.log; sleep 0.25; done"
        operator.succeed("rm -f /root/probe.log && systemd-run --unit=tls-probe-loop ${pkgs.bash}/bin/bash -c " + shlex.quote("PATH=${lib.makeBinPath [pkgs.curl pkgs.coreutils]}; " + probe))
        operator.wait_until_succeeds("test $(wc -l </root/probe.log) -ge 8", timeout=60)
        out = app.succeed("terrace-tls renew --force", timeout=300)
        ok("succeeded" in out, "terrace-tls renew --force")
        after = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        ok(after != before, "a new certificate was issued")
        app.wait_until_succeeds("test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername web.terrace.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(after), timeout=60)
        operator.wait_until_succeeds("test \"$(systemctl show tls-probe-loop.service -p ActiveState --value)\" != active", timeout=120)
        codes = operator.succeed("cat /root/probe.log").split()
        ok(len(codes) == 80 and set(codes) == {"200"}, f"80 HTTPS requests across the renewal, all 200 ({sorted(set(codes))})")
        ok((state(app, "nginx.service", "MainPID"), state(app, "nginx.service", "NRestarts")) == (pid, restarts), "same nginx process: reloaded, not restarted")
        for name in NAMES:
            ok(served("app", name) == after, f"{name}: renewed chain served")
        ok(account_hash(app) == account, "the same ACME account renewed")
        no_failed_units(app, "after the forced renewal")

    with subtest("expiry check fails on a short-lived or wrong-name certificate"):
        # Test leaves signed at test time by the local CA's intermediate (management API).
        acme.succeed("install -d -m 0700 /root/leaf && cd /root/leaf && curl -fsS --cacert /run/pebble-tls/ca.pem https://acme.test:15000/intermediates/0 >int.pem && curl -fsS --cacert /run/pebble-tls/ca.pem https://acme.test:15000/intermediate-keys/0 >int-key.pem")
        def leaf(label, sans, days):
            acme.succeed(f"cd /root/leaf && openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 -subj /CN={sans[0]} -keyout {label}-key.pem -out {label}.csr 2>/dev/null && "
                         f"printf 'basicConstraints=critical,CA:FALSE\\nkeyUsage=critical,digitalSignature\\nextendedKeyUsage=serverAuth\\nsubjectAltName=%s\\n' {','.join('DNS:' + s for s in sans)} >{label}.ext && "
                         f"openssl x509 -req -in {label}.csr -CA int.pem -CAkey int-key.pem -set_serial 0x$(openssl rand -hex 8) -days {days} -sha256 -extfile {label}.ext -out {label}.pem 2>/dev/null && cat int.pem >>{label}.pem")
            return acme.succeed(f"cat /root/leaf/{label}.pem"), acme.succeed(f"cat /root/leaf/{label}-key.pem"), fingerprint(acme, f"/root/leaf/{label}.pem")
        directory = live("web.terrace.test")
        app.succeed(f"install -d -m 0700 /run/tls-held && cp -p {directory}/fullchain.pem {directory}/key.pem /run/tls-held/")
        for label, sans, days, expected in [("short", NAMES, 3, "fewer than 14 days left"), ("wrong", ["other.terrace.test"], 60, "FAIL: no verified TLS session")]:
            chain, key, test_leaf = leaf(label, sans, days)
            put(app, directory + "/fullchain.pem", chain, "0640")
            put(app, directory + "/key.pem", key, "0640")
            app.succeed(f"chown acme:nginx {directory}/fullchain.pem {directory}/key.pem && systemctl reload nginx.service")
            app.wait_until_succeeds("test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername web.terrace.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(test_leaf), timeout=60)
            output = tls_check(app, False, f"expiry check fails visibly on the {label} certificate")
            ok(expected in output, f"{label}: {expected}")
        app.succeed(f"cp -p /run/tls-held/fullchain.pem /run/tls-held/key.pem {directory}/ && rm -rf /run/tls-held && systemctl reload nginx.service")
        acme.succeed("rm -rf /root/leaf")
        app.wait_until_succeeds("test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername web.terrace.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(after), timeout=60)
        tls_check(app, True, "expiry check passes again on the real certificate")
        no_failed_units(app, "after restoring the certificate")

    with subtest("CA unreachable: the order fails as a unit, the site keeps the previous certificate"):
        block = "iptables -I INPUT -p tcp --dport 14000 -j REJECT && ip6tables -I INPUT -p tcp --dport 14000 -j REJECT"
        unblock = "iptables -D INPUT -p tcp --dport 14000 -j REJECT && ip6tables -D INPUT -p tcp --dport 14000 -j REJECT"
        previous = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        acme.succeed(block)
        refused = app.fail("terrace-tls renew --force 2>&1", timeout=600)
        ok("FAILED; the previous certificate is still served" in refused, "terrace-tls reports the failed order")
        ok(state(app, order("web.terrace.test"), "ActiveState") == "failed" and order("web.terrace.test") in failed_units(app), "failed issuance is a failed unit (systemctl --failed)")
        ok(fingerprint(app, live("web.terrace.test") + "/cert.pem") == previous and all(served("app", name) == previous for name in NAMES), "previous certificate still served and valid")
        tls_check(app, True, "expiry check still passes on the previous certificate")
        acme.succeed(unblock)
        app.succeed("systemctl start " + order("web.terrace.test"), timeout=300)
        recovered = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        ok(recovered != previous, "the next order run issued a new certificate")
        app.wait_until_succeeds("test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername web.terrace.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(recovered), timeout=60)
        no_failed_units(app, "after the CA came back")

    with subtest("reboot keeps the certificate and the account key"):
        certificate, account = fingerprint(app, live("web.terrace.test") + "/cert.pem"), account_hash(app)
        app.shutdown()
        app.start()
        app.wait_for_unit("multi-user.target")
        app.wait_for_unit("nginx.service")
        app.wait_until_succeeds(f"test \"$(systemctl show {order('web.terrace.test')} -p ExecMainStartTimestampMonotonic --value)\" != 0 && test \"$(systemctl show {order('web.terrace.test')} -p ActiveState --value)\" = inactive", timeout=300)
        ok(state(app, order("web.terrace.test"), "Result") == "success", "order unit ran at boot and succeeded (not due)")
        ok(fingerprint(app, live("web.terrace.test") + "/cert.pem") == certificate and account_hash(app) == account, "certificate and account key survive the reboot")
        ok(all(served("app", name) == certificate for name in NAMES), "same chain served after the reboot")
        app.wait_for_unit("keycloak.service", timeout=360)
        no_failed_units(app, "after the reboot")

    with subtest("relay: first boot with the CA unreachable is safe and visible; issuance once it is back"):
        op("terrace-secrets host create relay --role relay --feature acme-tls")
        ok("Generated 2 secrets for relay. Supply with `set`: none." in op("terrace-secrets generate relay"), "relay store needs no certificate")
        enrol_repository("relay", "relay-repository")
        stage("relay")
        relay.start()
        relay.wait_for_unit("multi-user.target")
        unstage("relay")
        push("relay", 2)
        harness(relay)
        acme.succeed(block)
        relay.succeed("terrace-production adopt --new")
        relay.wait_for_unit("nginx.service")
        relay.wait_until_succeeds(f"systemctl is-failed {order(RELAY)}", timeout=600)
        ok(order(RELAY) in failed_units(relay), "relay: failed first issuance is a failed unit")
        placeholder = operator.succeed(f"echo | openssl s_client -connect relay:443 -servername {RELAY} 2>/dev/null | openssl x509 -noout -issuer")
        ok("minica" in placeholder, f"relay: nginx up with the placeholder {placeholder.strip()}")
        operator.fail(f"echo | openssl s_client -connect relay:443 -servername {RELAY} -verify_return_error -verify_hostname {RELAY} -CAfile /root/roots.pem >/dev/null 2>&1")
        ok(True, "relay: the placeholder is not trusted")
        tls_check(relay, False, "relay: expiry check fails on the placeholder")
        ok("PLACEHOLDER" in relay.succeed("terrace-tls status"), "relay: status names the placeholder")
        ok(http("relay", RELAY, "/x") == f"301 https://{RELAY}/x", "relay: port 80 redirect")
        acme.succeed(unblock)
        relay.succeed("systemctl start " + order(RELAY), timeout=300)
        issued(relay, RELAY)
        relay_cert = fingerprint(relay, live(RELAY) + "/cert.pem")
        relay.wait_until_succeeds(f"test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername {RELAY} 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(relay_cert), timeout=60)
        ok(served("relay", RELAY) == relay_cert, "relay: issued chain served and verified")
        ok(operator.succeed(f"curl -s -o /dev/null -w '%{{http_code}}' --cacert /root/roots.pem --resolve {RELAY}:443:{address('relay')} https://{RELAY}/health").strip() == "200", "relay: Headscale over the issued certificate")
        tls_check(relay, True, "relay: expiry check passes after issuance")
        no_failed_units(relay, "relay after issuance")
        relay.shutdown()

    with subtest("backup leaves ACME state out; a replacement host re-issues with a new account"):
        old_certificate, old_account = fingerprint(app, live("web.terrace.test") + "/cert.pem"), account_hash(app)
        app.succeed("systemctl stop terrace-cms-sweep.timer terrace-web.service terrace-cms.service keycloak.service nginx.service", timeout=120)
        app.succeed("terrace-backup init primary >/dev/null 2>&1")
        app.succeed("systemctl start terrace-backup.service", timeout=600)
        snapshot = json.loads(app.succeed("terrace-backup restic primary snapshots --json 2>/dev/null"))[-1]["id"]
        listing = app.succeed(f"terrace-backup restic primary ls {snapshot} 2>/dev/null").splitlines()
        ok(len(listing) > 3 and not [path for path in listing if "/acme" in path], f"snapshot of {len(listing)} paths holds no ACME key, account or certificate")
        app.succeed("systemctl stop postgresql.target postgresql.service")
        app.shutdown()
        disks = list(app.state_dir.glob("*.qcow2"))
        ok(len(disks) == 1, "one original disk")
        disks[0].unlink()
        out = op("terrace-secrets host create app --role app --feature acme-tls --replace")
        ok("Run `rekey` before pushing." in out, "replacement host key")
        op("terrace-secrets rekey app")
        stage("app")
        app.start()
        app.wait_for_unit("multi-user.target")
        unstage("app")
        push("app", 9)
        harness(app)
        app.fail("test -e /var/lib/acme/.lego")
        app.succeed("umask 077; terrace-backup-restore --target primary --snapshot " + snapshot + " --dest " + RESTORE + " >/var/tmp/restore.log 2>&1", timeout=600)
        app.succeed("grep -qx 'RECONCILIATION OK' " + RESTORE + "/reconciliation.txt")
        ok(app.succeed(f"find {RESTORE} -path '*acme*' | wc -l").strip() == "0", "the restore brings no ACME state")
        for path in [MEDIA, OUTBOX]:
            app.succeed("mkdir -p " + path + " && if test -d " + RESTORE + "/files" + path + "; then cp -a " + RESTORE + "/files" + path + "/. " + path + "/; fi")
        app.succeed("install -d -m 0700 -o postgres -g postgres /var/lib/postgresql/18 && cp -a " + RESTORE + "/pgdata/. /var/lib/postgresql/18/ && chown -R postgres:postgres /var/lib/postgresql/18")
        app.succeed("terrace-production adopt --restored")
        app.wait_for_unit("nginx.service")
        issued(app, "web.terrace.test")
        new_certificate = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        ok(new_certificate != old_certificate and account_hash(app) != old_account, "replacement: new account and new certificate")
        app.wait_until_succeeds("test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername web.terrace.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(new_certificate), timeout=60)
        for name in NAMES:
            ok(served("app", name) == new_certificate, f"replacement: {name} served and verified")
        app.wait_for_unit("keycloak.service", timeout=360)
        tls_check(app, True, "replacement: expiry check passes")
        no_failed_units(app, "replacement")

    with subtest("stop owned guests"):
        app.shutdown()
        operator.shutdown()
        acme.shutdown()
    # Labels carry names and counts only, never key material.
    print("TLS-CHECKS " + json.dumps(checks))
    print("TLS-PROOF " + json.dumps({"assertions": len(checks), "seconds": round(time.monotonic() - started_at, 2)}))
  '';
}
