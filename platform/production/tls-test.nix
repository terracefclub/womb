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
      # Short retries so the proof sees the bounded retry (production: at least 900 s).
      retrySeconds = 20;
      retries = 3;
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
      # The same relay with the operator-delivered certificate, for the live
      # switch from ACME to manual and back.
      specialisation.manual.configuration.terrace.production.tls.mode = lib.mkForce "manual";
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

    # The local CA (pebble) sets notBefore to the moment of issue; a public CA backdates it
    # (Let's Encrypt by an hour). Guest clocks start from the RTC (whole seconds), so they can
    # trail the CA's guest by a second or two. Each guest is set to the host clock when it
    # boots. After that sync every guest is within milliseconds of the CA's guest, so a
    # verifying client whose clock has not reached the served certificate's notBefore points
    # at a broken sync or a wrong issuance: the proof fails then, it does not wait.
    clock_offsets = {}

    def sync_clock(machine):
        offset = float(machine.succeed("date +%s.%N").strip()) - time.time()
        clock_offsets.setdefault(machine.name, []).append(round(offset, 3))
        machine.succeed(f"date -u -s @{time.time():.3f} >/dev/null")

    def assert_valid_now(machine, target, name):
        # Only fetching the served certificate is retried (a connection can race a reload);
        # the clock comparison is made once.
        started = f"echo | openssl s_client -connect {target}:443 -servername {name} 2>/dev/null | openssl x509 -noout -startdate | cut -d= -f2"
        before = machine.wait_until_succeeds(f"nb=$({started}) && test -n \"$nb\" && date -d \"$nb\" +%s", timeout=30).strip()
        now = machine.succeed("date +%s.%N").strip()
        if float(now) < int(before):
            raise AssertionError(f"{machine.name} {name}: after the boot clock sync the clock ({now}) is still {int(before) - float(now):.3f} s before the served certificate's notBefore ({before}); offsets before the syncs {clock_offsets}")

    def served(host, name, verify=True):
        # What a client sees: chain verified against the local CA's roots, name checked.
        base = f"echo | openssl s_client -connect {host}:443 -servername {name}"
        if verify:
            assert_valid_now(operator, host, name)
            status, output = operator.execute(base + f" -verify_return_error -verify_hostname {name} -CAfile /root/roots.pem 2>&1")
            if status != 0:
                diagnosis = [line for line in output.splitlines() if re.search(r"verif|error|depth|connect", line, re.I)][:12]
                journal = {"app": app, "relay": relay}[host].execute("journalctl -u nginx.service -o cat --no-pager -n 15")[1]
                raise AssertionError(f"{host} {name}: no verified TLS session: {diagnosis} nginx: {journal}")
        return operator.succeed(base + " 2>/dev/null | openssl x509 -noout -fingerprint -sha256").strip()

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

    def push(host, count, declared):
        # The store also holds the escrow-only bootstrap password, which the host does not declare.
        out = op(f"terrace-secrets push {host} --address {host} --identity /root/.ssh/operator --rehearsal")
        ok(f"PASS {declared} secrets" in out and f"Installed {count} encrypted secrets." in out and "tls/" not in out, f"{host}: push of {count} secrets, {declared} declared, no tls/* ({out.strip()})")

    def enrol_repository(host, directory):
        public = op(f"terrace-secrets public {host} backup/primary/sftp-key").strip()
        operator.succeed(f"install -d -o restic -g users -m 0700 /home/restic/.ssh /home/restic/{directory} && echo {shlex.quote(public)} >>/home/restic/.ssh/authorized_keys && chown restic:users /home/restic/.ssh/authorized_keys && chmod 0600 /home/restic/.ssh/authorized_keys")

    def leaf(label, sans, days, starts_in_days=0):
        # Test leaves signed at test time by the local CA's intermediate (management API).
        # starts_in_days > 0: notBefore that far in the future (a host clock behind its certificate).
        validity = f"-days {days}"
        if starts_in_days:
            stamp = lambda offset: time.strftime("%Y%m%d%H%M%SZ", time.gmtime(time.time() + offset * 86400))
            validity = f"-not_before {stamp(starts_in_days)} -not_after {stamp(starts_in_days + days)}"
        acme.succeed(f"cd /root/leaf && openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 -subj /CN={sans[0]} -keyout {label}-key.pem -out {label}.csr 2>/dev/null && "
                     f"printf 'basicConstraints=critical,CA:FALSE\\nkeyUsage=critical,digitalSignature\\nextendedKeyUsage=serverAuth\\nsubjectAltName=%s\\n' {','.join('DNS:' + s for s in sans)} >{label}.ext && "
                     f"openssl x509 -req -in {label}.csr -CA int.pem -CAkey int-key.pem -set_serial 0x$(openssl rand -hex 8) {validity} -sha256 -extfile {label}.ext -out {label}.pem 2>/dev/null && cat int.pem >>{label}.pem")
        return acme.succeed(f"cat /root/leaf/{label}.pem"), acme.succeed(f"cat /root/leaf/{label}-key.pem"), fingerprint(acme, f"/root/leaf/{label}.pem")

    def served_locally(machine, name, fp):
        machine.wait_until_succeeds(f"test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername {name} 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(fp), timeout=120)

    def issued_by_acme(machine, cert):
        # lego's own copy exists only after a real order (not for the placeholder or a staged manual certificate).
        machine.wait_until_succeeds(f"test -e /var/lib/acme/.lego/{cert}/*/{cert}.crt && test \"$(systemctl show {order(cert)} -p ActiveState --value)\" = inactive", timeout=600)
        ok(state(machine, order(cert), "Result") == "success", f"{machine.name}: ACME order succeeded")

    def issued(machine, cert):
        machine.wait_until_succeeds(f"test -e {live(cert)}/acme-success && test \"$(systemctl show {order(cert)} -p ActiveState --value)\" = inactive", timeout=300)
        ok(state(machine, order(cert), "Result") == "success", f"{machine.name}: order unit succeeded")

    def tls_check(machine, expect_ok, label):
        if expect_ok:
            for name in (NAMES if machine.name == "app" else [RELAY]):
                assert_valid_now(machine, "127.0.0.1", name)
            machine.succeed("systemctl start terrace-tls-check.service", timeout=120)
        else:
            machine.fail("systemctl start terrace-tls-check.service", timeout=120)
        output = machine.succeed("journalctl -u terrace-tls-check.service -o cat --no-pager -n 20")
        failed = machine.succeed("systemctl is-failed terrace-tls-check.service || true").strip() == "failed"
        ok(failed != expect_ok and (expect_ok or "terrace-tls-check.service" in failed_units(machine)), label)
        return output

    FRESHNESS = "terrace-backup-freshness.service"
    FRESHNESS_STAMP = "/var/lib/systemd/timers/stamp-terrace-backup-freshness.timer"

    def first_backup(machine, label, init=True):
        # Adoption arms the hourly, Persistent backup freshness timer. Until a backup is
        # recorded its unit fails (correct on a real host); whenever the proof's clock crosses
        # the hour it would fire and trip the no-failed-units checks. So every adopted host
        # here takes its backup at once, and the freshness unit is run (as its next hourly
        # run would) and must pass, which also clears a failure from that short window.
        if init:
            machine.succeed("terrace-backup init primary >/dev/null 2>&1")
        machine.succeed("systemctl start terrace-backup.service", timeout=600)
        ok(state(machine, "terrace-backup.service", "Result") == "success", f"{label}: backup recorded")
        machine.succeed(f"systemctl start {FRESHNESS}", timeout=120)
        ok(state(machine, FRESHNESS, "Result") == "success" and FRESHNESS not in failed_units(machine), f"{label}: backup freshness check passes")

    def no_failed_units(machine, label):
        units = failed_units(machine)
        ok(units == [], f"{label}: no failed units {units}")

    started_at = time.monotonic()

    with subtest("local ACME server on the test network"):
        acme.start()
        acme.wait_for_unit("pebble.service")
        sync_clock(acme)
        acme.wait_for_open_port(14000)
        acme.wait_for_open_port(15000)
        server_ca = acme.succeed("cat /run/pebble-tls/ca.pem")
        acme.succeed("curl -fsS --cacert /run/pebble-tls/ca.pem https://acme.test:14000/dir >/dev/null")
        roots = acme.wait_until_succeeds("curl -fsS --cacert /run/pebble-tls/ca.pem https://acme.test:15000/roots/0")
        ok("BEGIN CERTIFICATE" in roots, "local CA root fetched at test time")
        acme.succeed("install -d -m 0700 /root/leaf && cd /root/leaf && curl -fsS --cacert /run/pebble-tls/ca.pem https://acme.test:15000/intermediates/0 >int.pem && curl -fsS --cacert /run/pebble-tls/ca.pem https://acme.test:15000/intermediate-keys/0 >int-key.pem")

    with subtest("operator: an ACME host receives no tls/* secrets"):
        operator.start()
        operator.wait_for_unit("sshd.service")
        sync_clock(operator)
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
        sync_clock(app)
        unstage("app")
        push("app", 10, 9)
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

    with subtest("first backup: the freshness check fails without one, passes after it"):
        ok(state(app, "terrace-backup-freshness.timer", "ActiveState") == "active", "hourly backup freshness timer armed by adoption")
        app.fail(f"systemctl start {FRESHNESS}", timeout=120)
        journal = app.succeed(f"journalctl -u {FRESHNESS} -o cat --no-pager -n 20")
        ok("no successful backup recorded" in journal and FRESHNESS in failed_units(app), "without a backup the freshness check fails (a failed unit)")
        first_backup(app, "app")

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
        ok(app.succeed(f"stat -c '%a %U %G' {directory}").strip() == "750 acme acme", "certificate directory 0750 acme:acme")
        modes = app.succeed(f"find {directory} -type f -printf '%m %u %g %f\\n' | sort").strip().splitlines()
        ok(all(line.startswith("640 acme acme ") for line in modes) and any(line.endswith(" key.pem") for line in modes), f"certificate files 0640 acme:acme {modes}")
        stored = app.succeed("find /var/lib/acme/.lego/web.terrace.test -type f -printf '%m %u %g\\n' | sort -u").strip().splitlines()
        ok(stored == ["640 acme acme"], f"client copies 0640 acme:acme {stored}")
        ok(app.succeed("id -nG nginx").split() == ["nginx"], "the nginx user is in no other group")
        for path in [f"{directory}/key.pem", f"{directory}/full.pem", "/var/lib/acme/.lego/web.terrace.test/*/web.terrace.test.key"]:
            app.succeed(f"cat {path} >/dev/null")
            app.fail(f"runuser -u nginx -- sh -c 'cat {path}' >/dev/null 2>&1")
        ok(True, "the nginx user cannot read the TLS key (ACME directory and lego copy)")
        unit = app.succeed("systemctl cat nginx.service")
        loads = [line for line in unit.splitlines() if line.startswith("LoadCredential=")]
        ok(any(line.endswith(f"{directory}/key.pem") for line in loads) and any(line.endswith(f"{directory}/fullchain.pem") for line in loads) and "RefreshOnReload=credentials" in unit.splitlines(), f"nginx gets the pair as credentials refreshed on reload {loads}")
        ok(app.succeed("cmp -s /run/credentials/nginx.service/terrace-website-key " + directory + "/key.pem && echo same").strip() == "same", "nginx's credential copy is the ACME key")
        accounts = app.succeed("find /var/lib/acme/.lego/accounts -type f -printf '%m %u\\n' | sort -u").strip().splitlines()
        ok(accounts == ["600 acme"], f"account key 0600 acme {accounts}")
        account_dirs = app.succeed("find /var/lib/acme/.lego/accounts -type d -printf '%m %u %g\\n' | sort -u").strip().splitlines()
        # The upstream module sets these directory modes; the key files themselves are 0600 acme (above).
        ok(all(re.fullmatch(r"7[0145][0145] acme (acme|nginx)", line) for line in account_dirs), f"account directories owned by acme, writable by nobody else {account_dirs}")
        account_key = app.succeed("find /var/lib/acme/.lego/accounts -type f -name '*.key' | head -n 1").strip()
        ok(account_key != "", "account key present")
        app.succeed(f"cat {account_key} >/dev/null")
        for user in ["nginx", "nobody"]:
            app.fail(f"runuser -u {user} -- cat {account_key} >/dev/null 2>&1")
        ok(True, "account key unreadable by nginx and others")
        for user in ["nginx", "terrace_cms", "terrace_web", "keycloak", "nobody"]:
            app.fail(f"runuser -u {user} -- cat {directory}/key.pem >/dev/null 2>&1")
        ok(True, "service accounts cannot read the TLS key")

    with subtest("expiry check passes on the fresh certificate"):
        output = tls_check(app, True, "expiry check passes on a fresh certificate")
        ok(all(f"{name}: OK: expires" in output for name in NAMES), "expiry check reports every name")
        status = app.succeed("terrace-tls status")
        ok("mode: acme" in status and "Pebble Intermediate" in status and "PLACEHOLDER" not in status, "terrace-tls status")
        ok(state(app, "acme-renew-web.terrace.test.timer", "ActiveState") == "active" and state(app, "terrace-tls-check.timer", "ActiveState") == "active", "renewal and expiry timers armed by adoption")
        for timer in ["acme-renew-web.terrace.test.timer", "terrace-tls-check.timer"]:
            ok(state(app, timer, "NextElapseUSecRealtime") not in ["", "0", "n/a"], f"{timer} has a next run")
        ok("run as root" in app.fail("runuser -u nobody -- terrace-tls status 2>&1"), "terrace-tls status says to run as root")

    with subtest("a routine renewal run is a no-op; a forced renewal reloads nginx without dropping connections"):
        before = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        account = account_hash(app)
        app.succeed("systemctl start " + order("web.terrace.test"), timeout=300)
        ok(fingerprint(app, live("web.terrace.test") + "/cert.pem") == before, "renewal not yet due: certificate unchanged")
        pid, restarts = state(app, "nginx.service", "MainPID"), state(app, "nginx.service", "NRestarts")
        probe = f"for i in $(seq 1 80); do printf '%s ' $(date +%s.%N) >>/root/probe.log; curl -s -o /dev/null -w '%{{http_code}}\\n' --max-time 3 --cacert /root/roots.pem --resolve identity.terrace.test:443:{address('app')} {OIDC} >>/root/probe.log; sleep 0.25; done"
        operator.succeed("rm -f /root/probe.log && systemd-run --unit=tls-probe-loop ${pkgs.bash}/bin/bash -c " + shlex.quote("PATH=${lib.makeBinPath [pkgs.curl pkgs.coreutils]}; " + probe))
        operator.wait_until_succeeds("test $(wc -l </root/probe.log) -ge 8", timeout=60)
        ok("repeat with --force --yes" in app.fail("terrace-tls renew --force 2>&1"), "a forced renewal needs --yes")
        out = app.succeed("terrace-tls renew --force --yes", timeout=300)
        ok("succeeded" in out, "terrace-tls renew --force --yes")
        after = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        ok(after != before, "a new certificate was issued")
        app.wait_until_succeeds("test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername web.terrace.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(after), timeout=60)
        operator.wait_until_succeeds("test \"$(systemctl show tls-probe-loop.service -p ActiveState --value)\" != active", timeout=120)
        lines = [line.split() for line in operator.succeed("cat /root/probe.log").splitlines()]
        codes = [line[1] for line in lines if len(line) == 2]
        ok(len(codes) == 80 and set(codes) == {"200"}, f"80 new HTTPS connections across the renewal, all 200 ({sorted(set(codes))})")
        reloaded = float(app.succeed("journalctl -u nginx.service -o short-unix --no-pager | grep 'Reloaded' | tail -n 1 | cut -d' ' -f1").strip())
        stamps = [float(line[0]) for line in lines]
        ok(min(stamps) < reloaded < max(stamps), f"the probe window ({min(stamps):.1f} to {max(stamps):.1f}) straddles the reload ({reloaded:.1f})")
        ok((state(app, "nginx.service", "MainPID"), state(app, "nginx.service", "NRestarts")) == (pid, restarts), "same nginx process: reloaded, not restarted")
        for name in NAMES:
            ok(served("app", name) == after, f"{name}: renewed chain served")
        ok(account_hash(app) == account, "the same ACME account renewed")
        no_failed_units(app, "after the forced renewal")

    with subtest("expiry check fails on a short-lived, wrong-name or not yet valid certificate, naming the cause"):
        directory = live("web.terrace.test")
        app.succeed(f"install -d -m 0700 /run/tls-held && cp -p {directory}/fullchain.pem {directory}/key.pem /run/tls-held/")
        # "future": a certificate whose notBefore is a day ahead, what a host clock behind its CA sees.
        for label, sans, days, starts, expected in [("short", NAMES, 3, 0, "fewer than 14 days left"), ("wrong", ["other.terrace.test"], 60, 0, "the served certificate does not name"),
                                                    ("future", NAMES, 60, 1, "web.terrace.test: FAIL: no verified TLS session: verify error 9: certificate is not yet valid")]:
            chain, key, test_leaf = leaf(label, sans, days, starts)
            put(app, directory + "/fullchain.pem", chain, "0640")
            put(app, directory + "/key.pem", key, "0640")
            app.succeed(f"chown acme:acme {directory}/fullchain.pem {directory}/key.pem && systemctl reload nginx.service")
            app.wait_until_succeeds("test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername web.terrace.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(test_leaf), timeout=60)
            output = tls_check(app, False, f"expiry check fails visibly on the {label} certificate")
            ok(expected in output and (label in ["short", "future"] or "no verified TLS session" not in output), f"{label}: {expected}")
            if label == "future":
                status = app.succeed("terrace-tls status")
                ok("served now (5 s per name):" in status and "verify error 9: certificate is not yet valid" in status, "terrace-tls status names the verification failure reason")
                try:
                    assert_valid_now(operator, "app", "web.terrace.test")
                    refused = ""
                except AssertionError as error:
                    refused = str(error)
                ok("before the served certificate's notBefore" in refused, "a verifying client still before notBefore after the clock sync fails the proof instead of waiting")
        app.succeed(f"cp -p /run/tls-held/fullchain.pem /run/tls-held/key.pem {directory}/ && rm -rf /run/tls-held && systemctl reload nginx.service")
        app.wait_until_succeeds("test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername web.terrace.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(after), timeout=60)
        tls_check(app, True, "expiry check passes again on the real certificate")
        no_failed_units(app, "after restoring the certificate")

    with subtest("CA unreachable: the previous certificate stays; the order retries on its own and recovers"):
        block = "iptables -I INPUT -p tcp --dport 14000 -j REJECT && ip6tables -I INPUT -p tcp --dport 14000 -j REJECT"
        unblock = "iptables -D INPUT -p tcp --dport 14000 -j REJECT && ip6tables -D INPUT -p tcp --dport 14000 -j REJECT"
        previous = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        acme.succeed(block)
        # The client may return when the first attempt fails or wait longer; the unit's own retries continue either way.
        status, out = app.execute("timeout 60 terrace-tls renew --force --yes 2>&1", timeout=90)
        print(f"renew with the CA unreachable: exit {status}: {out.strip()}")
        ok(status != 0, "the forced renewal does not succeed while the CA is unreachable")
        ok(state(app, order("web.terrace.test"), "ActiveState") == "activating" and state(app, order("web.terrace.test"), "Result") != "success", f"the order unit is retrying ({state(app, order('web.terrace.test'), 'SubState')})")
        ok(fingerprint(app, live("web.terrace.test") + "/cert.pem") == previous and all(served("app", name) == previous for name in NAMES), "previous certificate still served and valid")
        acme.succeed(unblock)
        issued_by_acme(app, "web.terrace.test")
        ok(int(state(app, order("web.terrace.test"), "NRestarts")) >= 1, "recovered by the unit's own retry, no operator action")
        recovered = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        ok(recovered != previous, "the retry issued a new certificate")
        served_locally(app, "web.terrace.test", recovered)
        tls_check(app, True, "expiry check passes after the recovery")
        no_failed_units(app, "after the CA came back")

    with subtest("a failed nginx reload after a renewal is retried by the next run"):
        previous = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        dropin = "/run/systemd/system/nginx.service.d/terrace-test-reload-fails.conf"
        app.succeed(f"install -d /run/systemd/system/nginx.service.d && printf '[Service]\\nExecReload=\\nExecReload=${pkgs.coreutils}/bin/false\\n' >{dropin} && systemctl daemon-reload")
        status, out = app.execute("terrace-tls renew --force --yes 2>&1", timeout=300)
        ok(status != 0, f"a renewal whose nginx reload fails is a failed run (exit {status})")
        renewed = fingerprint(app, live("web.terrace.test") + "/cert.pem")
        ok(renewed != previous, "the new certificate is installed on disk")
        ok(app.succeed("test -e " + live("web.terrace.test") + "/renewed && echo kept").strip() == "kept", "the renewed marker is kept for the retry")
        ok(all(served("app", name) == previous for name in NAMES), "nginx still serves the previous certificate")
        app.succeed(f"rm {dropin} && systemctl daemon-reload")
        app.wait_until_succeeds(f"test \"$(systemctl show {order('web.terrace.test')} -p ActiveState --value)\" = inactive", timeout=180)
        ok(state(app, order("web.terrace.test"), "Result") == "success" and int(state(app, order("web.terrace.test"), "NRestarts")) >= 1, "the unit's own retry succeeded")
        served_locally(app, "web.terrace.test", renewed)
        ok(all(served("app", name) == renewed for name in NAMES), "the retry reloaded nginx: the new chain is served")
        app.fail("test -e " + live("web.terrace.test") + "/renewed")
        no_failed_units(app, "after the reload retry")

    with subtest("reboot keeps the certificate and the account key"):
        certificate, account = fingerprint(app, live("web.terrace.test") + "/cert.pem"), account_hash(app)
        # As if the reboot crossed the top of the hour (the 2026-11-23 wall-clock flake): the
        # freshness timer's last run is two hours back, so the Persistent timer must fire at boot.
        app.succeed(f"test -f {FRESHNESS_STAMP} && touch -d @$(( $(date +%s) - 7200 )) {FRESHNESS_STAMP}")
        app.shutdown()
        app.start()
        app.wait_for_unit("multi-user.target")
        sync_clock(app)
        app.wait_for_unit("nginx.service")
        app.wait_until_succeeds(f"test \"$(systemctl show {order('web.terrace.test')} -p ExecMainStartTimestampMonotonic --value)\" != 0 && test \"$(systemctl show {order('web.terrace.test')} -p ActiveState --value)\" = inactive", timeout=300)
        ok(state(app, order("web.terrace.test"), "Result") == "success", "order unit ran at boot and succeeded (not due)")
        ok(fingerprint(app, live("web.terrace.test") + "/cert.pem") == certificate and account_hash(app) == account, "certificate and account key survive the reboot")
        ok(all(served("app", name) == certificate for name in NAMES), "same chain served after the reboot")
        app.wait_for_unit("keycloak.service", timeout=360)
        app.wait_until_succeeds(f"test \"$(systemctl show {FRESHNESS} -p ExecMainStartTimestampMonotonic --value)\" != 0 && test \"$(systemctl show {FRESHNESS} -p ActiveState --value)\" != activating", timeout=300)
        ok(state(app, FRESHNESS, "Result") == "success", "the freshness timer fired at boot for the missed hour and passed on the recorded backup")
        no_failed_units(app, "after the reboot")

    with subtest("relay: first boot with the CA unreachable is safe and visible; issuance once it is back"):
        op("terrace-secrets host create relay --role relay --feature acme-tls")
        ok("Generated 2 secrets for relay. Supply with `set`: none." in op("terrace-secrets generate relay"), "relay store needs no certificate")
        enrol_repository("relay", "relay-repository")
        stage("relay")
        relay.start()
        relay.wait_for_unit("multi-user.target")
        sync_clock(relay)
        unstage("relay")
        push("relay", 2, 2)
        harness(relay)
        acme.succeed(block)
        relay.succeed("terrace-production adopt --new")
        relay.wait_for_unit("nginx.service")
        relay.wait_until_succeeds(f"systemctl is-failed {order(RELAY)}", timeout=900)
        ok(order(RELAY) in failed_units(relay) and state(relay, order(RELAY), "Result") == "start-limit-hit", f"relay: after its bounded retries the first issuance is a failed unit ({state(relay, order(RELAY), 'Result')})")
        placeholder = operator.succeed(f"echo | openssl s_client -connect relay:443 -servername {RELAY} 2>/dev/null | openssl x509 -noout -issuer")
        ok("minica" in placeholder, f"relay: nginx up with the placeholder {placeholder.strip()}")
        operator.fail(f"echo | openssl s_client -connect relay:443 -servername {RELAY} -verify_return_error -verify_hostname {RELAY} -CAfile /root/roots.pem >/dev/null 2>&1")
        ok(True, "relay: the placeholder is not trusted")
        output = tls_check(relay, False, "relay: expiry check fails on the placeholder")
        found = re.search(r"no verified TLS session: (verify error [0-9]+: [^\n]+)", output)
        assert found is not None, f"relay: no verification reason in the check output: {output}"
        reason = found.group(1)
        ok("not yet valid" not in reason, f"relay: the check names OpenSSL's reason for the placeholder ({reason})")
        status = relay.succeed("terrace-tls status")
        ok("PLACEHOLDER" in status and reason in status, "relay: status names the placeholder and the reason")
        ok(http("relay", RELAY, "/x") == f"301 https://{RELAY}/x", "relay: port 80 redirect")
        acme.succeed(unblock)
        # The first run could not register; the retry must register afresh, not "recover" it.
        relay.succeed("terrace-tls renew", timeout=300)
        ok("never registered" in relay.succeed("journalctl -u " + order(RELAY) + " -o cat --no-pager"), "relay: the never-registered account is replaced, not recovered")
        issued(relay, RELAY)
        relay_cert = fingerprint(relay, live(RELAY) + "/cert.pem")
        relay.wait_until_succeeds(f"test \"$(echo | openssl s_client -connect 127.0.0.1:443 -servername {RELAY} 2>/dev/null | openssl x509 -noout -fingerprint -sha256)\" = " + shlex.quote(relay_cert), timeout=60)
        ok(served("relay", RELAY) == relay_cert, "relay: issued chain served and verified")
        ok(operator.succeed(f"curl -s -o /dev/null -w '%{{http_code}}' --cacert /root/roots.pem --resolve {RELAY}:443:{address('relay')} https://{RELAY}/health").strip() == "200", "relay: Headscale over the issued certificate")
        tls_check(relay, True, "relay: expiry check passes after issuance")
        first_backup(relay, "relay")
        no_failed_units(relay, "relay after issuance")
        relay_listing = relay.succeed("terrace-backup restic primary ls latest 2>/dev/null").splitlines()
        ok(len(relay_listing) > 1 and not [path for path in relay_listing if "/acme" in path], f"relay: snapshot of {len(relay_listing)} paths holds no ACME state")

    with subtest("relay: live switch from ACME to the manual certificate and back, never the placeholder"):
        system = relay.succeed("readlink -f /run/current-system").strip()
        short_chain, short_key, short_fp = leaf("relay-short", [RELAY], 3)
        good_chain, good_key, good_fp = leaf("relay-manual", [RELAY], 60)
        op("terrace-secrets host tls relay manual")
        put(operator, "/dev/shm/relay-cert.pem", short_chain, "0600")
        put(operator, "/dev/shm/relay-key.pem", short_key, "0600")
        op("terrace-secrets set relay tls/certificate </dev/shm/relay-cert.pem && terrace-secrets set relay tls/key </dev/shm/relay-key.pem")
        out = op("terrace-secrets push relay --address relay --identity /root/.ssh/operator --rehearsal")
        ok("Installed 4 encrypted secrets." in out and "WARNING: this host gets its certificate by ACME but its ciphertext still holds tls/certificate, tls/key" in out, "the ACME relay accepts the manual pair and reports it")
        relay.succeed(f"{system}/specialisation/manual/bin/switch-to-configuration test", timeout=300)
        served_locally(relay, RELAY, short_fp)
        ok(served("relay", RELAY) == short_fp, "manual mode: the delivered certificate is served")
        ok(http("relay", RELAY, "/x") == f"301 https://{RELAY}/x", "manual mode: port 80 still redirects")
        output = tls_check(relay, False, "manual mode: expiry check fails on a 3-day certificate")
        ok("fewer than 14 days left" in output, "manual mode: the check names the expiry")
        put(operator, "/dev/shm/relay-cert.pem", good_chain, "0600")
        put(operator, "/dev/shm/relay-key.pem", good_key, "0600")
        op("terrace-secrets set relay tls/certificate </dev/shm/relay-cert.pem && terrace-secrets set relay tls/key </dev/shm/relay-key.pem && rm /dev/shm/relay-cert.pem /dev/shm/relay-key.pem")
        push("relay", 4, 4)
        served_locally(relay, RELAY, good_fp)
        tls_check(relay, True, "manual mode: expiry check passes on the delivered certificate")
        refused = relay.fail("terrace-tls prepare-acme 2>&1")
        ok("already holds a valid ACME certificate" in refused, "prepare-acme does not overwrite a valid ACME certificate without --overwrite")
        # Back to ACME on a host that never ran it: no lego state, no account.
        relay.succeed("rm -rf /var/lib/acme")
        ok("staged" in relay.succeed("terrace-tls prepare-acme"), "the served certificate is staged before the switch")
        watch = f"for i in $(seq 1 150); do echo | timeout 3 openssl s_client -connect relay:443 -servername {RELAY} 2>/dev/null | openssl x509 -noout -fingerprint -sha256 -issuer >>/root/switch.log 2>/dev/null; sleep 0.3; done"
        operator.succeed("rm -f /root/switch.log && systemd-run --unit=tls-switch-watch ${pkgs.bash}/bin/bash -c " + shlex.quote("PATH=${lib.makeBinPath [pkgs.openssl pkgs.coreutils]}; " + watch))
        relay.succeed(f"{system}/bin/switch-to-configuration test", timeout=300)
        issued_by_acme(relay, RELAY)
        switched = fingerprint(relay, live(RELAY) + "/cert.pem")
        ok(switched not in [good_fp, short_fp, relay_cert], "a new ACME certificate after the switch")
        served_locally(relay, RELAY, switched)
        ok(served("relay", RELAY) == switched, "ACME certificate served and verified after the switch")
        operator.wait_until_succeeds("test \"$(systemctl show tls-switch-watch.service -p ActiveState --value)\" != active", timeout=180)
        seen = operator.succeed("cat /root/switch.log")
        fingerprints = set(line.strip() for line in seen.splitlines() if "Fingerprint" in line)
        ok("minica" not in seen and fingerprints <= {good_fp, switched} and good_fp in fingerprints and switched in fingerprints, f"clients saw only the manual certificate, then the ACME one, never the placeholder ({len(fingerprints)} distinct)")
        refused = operator.fail(ENV + "terrace-secrets host tls relay acme 2>&1")
        ok("this removes tls/certificate, tls/key from the store" in refused, "dropping the manual pair needs --yes")
        op("terrace-secrets host tls relay acme --yes")
        push("relay", 2, 2)
        tls_check(relay, True, "after the switch: expiry check passes")
        no_failed_units(relay, "relay after the switches")
        relay.shutdown()

    with subtest("backup leaves ACME state out; a replacement host re-issues with a new account"):
        old_certificate, old_account = fingerprint(app, live("web.terrace.test") + "/cert.pem"), account_hash(app)
        app.succeed("systemctl stop terrace-cms-sweep.timer terrace-web.service terrace-cms.service keycloak.service nginx.service", timeout=120)
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
        sync_clock(app)
        unstage("app")
        push("app", 10, 9)
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
        # The restore does not bring the old host's backup record: back up from the replacement.
        first_backup(app, "replacement", init=False)
        no_failed_units(app, "replacement")

    with subtest("stop owned guests"):
        app.shutdown()
        operator.shutdown()
        acme.shutdown()
    # Labels carry names and counts only, never key material.
    print("TLS-CHECKS " + json.dumps(checks))
    print("TLS-PROOF " + json.dumps({"assertions": len(checks), "clockOffsetsBeforeSync": clock_offsets, "seconds": round(time.monotonic() - started_at, 2)}))
  '';
}
