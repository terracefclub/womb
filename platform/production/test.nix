# VM proof of the production app role (docs/production-profile.md): the SAME
# role module with `instance = "rehearsal"`, synthetic .invalid names and secrets
# that terrace-secrets generates inside the operator guest at test time.
{
  pkgs,
  release,
  contract,
  sopsModule,
}: let
  lib = pkgs.lib;
  names = ["web.terrace.invalid" "cms.terrace.invalid" "identity.terrace.invalid"];
  tool = pkgs.callPackage ./package.nix {};
  # Synthetic CA only. Its key signs a leaf at test time inside the operator
  # guest; the leaf key exists only in the encrypted store and on ramfs.
  ca = pkgs.runCommand "terrace-production-rehearsal-ca" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir $out
    openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 3650 \
      -keyout $out/ca-key.pem -out $out/ca.pem -subj /CN=TerraceProductionRehearsalCA \
      -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign >/dev/null 2>&1
  '';
  # Test-only SSH identities: the operator's client key and the synthetic
  # repository's host key (the app pins it, as it would a Storage Box key).
  keys = pkgs.runCommand "terrace-production-rehearsal-keys" {nativeBuildInputs = [pkgs.openssh];} ''
    mkdir $out
    ssh-keygen -q -t ed25519 -N "" -C rehearsal-operator -f $out/operator
    ssh-keygen -q -t ed25519 -N "" -C rehearsal-repository -f $out/repository-host
  '';
  publicKey = file: lib.removeSuffix "\n" (builtins.readFile "${keys}/${file}.pub");
  users = pkgs.runCommand "terrace-production-rehearsal-users" {} ''
    mkdir $out
    cp -r ${contract}/. $out/
    cp ${./rehearsal-users.ts} $out/rehearsal-users.ts
  '';
  patterns = pkgs.writers.writePython3Bin "rehearsal-patterns" {flakeIgnore = ["E501"];} (builtins.readFile ./rehearsal-patterns.py);
  extension = pkgs.writeText "terrace-rehearsal-leaf.ext" ''
    basicConstraints=critical,CA:FALSE
    keyUsage=critical,digitalSignature,keyEncipherment
    extendedKeyUsage=serverAuth
    subjectAltName=${lib.concatMapStringsSep "," (name: "DNS:${name}") names}
  '';
in {
  name = "terrace-production-profile";
  nodes = {
    # Operator workstation, HTTPS client and synthetic SFTP backup repository.
    operator = {...}: {
      virtualisation = {
        memorySize = 2048;
        cores = 1;
      };
      security.pki.certificateFiles = ["${ca}/ca.pem"];
      environment.systemPackages = [tool pkgs.sops pkgs.age pkgs.openssl pkgs.bun pkgs.jq pkgs.curl pkgs.netcat-openbsd patterns];
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
      imports = [sopsModule ./app.nix (import ./rehearsal.nix {inherit ca;})];
      virtualisation = {
        memorySize = 7168;
        cores = 2;
        diskSize = 4096;
      };
      terrace.production = {
        instance = "rehearsal";
        memoryMiB = 7168;
        operatorKeys = [(publicKey "operator")];
        app = {
          webHostname = "web.terrace.invalid";
          cmsHostname = "cms.terrace.invalid";
          identityHostname = "identity.terrace.invalid";
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
  };
  testScript = {nodes, ...}: let
    closure = pkgs.closureInfo {rootPaths = [nodes.app.system.build.toplevel];};
  in
    builtins.readFile ../website-gateway/lifecycle_steps.py
    + ''
      import json, re, shlex, time

      checks = []
      def ok(condition, label):
          assert condition, label
          checks.append(label)

      STORE = "/root/store"
      ESCROW = "/root/escrow/escrow.age"
      # Exported, so every command of a pipeline sees the operator environment.
      ENV = f"export TERRACE_SECRETS_STORE={STORE} SOPS_AGE_KEY_FILE={ESCROW} TERRACE_SECRETS_SCRATCH=/dev/shm; "
      CA = "/etc/terrace-rehearsal-ca.pem"
      MEDIA = "/var/lib/terrace-cms-media"
      OUTBOX = "/var/lib/terrace-cms-outbox"
      RESTORE = "/var/lib/terrace-restore"
      WRITERS = ["postgresql", "keycloak", "nginx", "terrace-cms", "terrace-web"]
      timings = {}

      def op(command, **kwargs):
          return operator.succeed(ENV + command, **kwargs)

      def timed(label, function):
          started = time.monotonic()
          value = function()
          timings[label] = round(time.monotonic() - started, 2)
          return value

      def sql(query, database="terrace_cms"):
          return app.succeed("runuser -u postgres -- psql -XAt -d " + database + " -c " + shlex.quote(query) + " 2>/dev/null").strip()

      def protocol(action, expected):
          output = app.succeed("python3 /etc/gateway-protocol.py " + action, timeout=240).strip()
          ok(output.startswith(expected), "protocol " + action)
          return output

      def lifecycle_steps(phase, steps):
          for label, ceiling, script in steps:
              started = time.monotonic()
              app.succeed("bash -ceu " + shlex.quote("set -o pipefail\n" + LIFECYCLE_PROLOGUE + script) + " lifecycle " + CA, timeout=ceiling)
              timings[phase + "-" + label] = round(time.monotonic() - started, 2)
              ok(True, "lifecycle " + phase + " " + label)

      def writers_inactive(machine):
          for unit in WRITERS:
              ok(machine.succeed("systemctl show " + unit + ".service -p ActiveState --value").strip() in ["inactive", "failed"], "writer off before adoption: " + unit)
          ok(machine.succeed("systemctl show terrace-production.target -p ActiveState --value").strip() == "inactive", "production target off before adoption")
          for port in [443, 5432, 8088, 13048, 14294]:
              machine.fail(f"nc -z -w 1 127.0.0.1 {port}")

      def verify(machine):
          output = machine.succeed("terrace-secrets verify").strip()
          ok(re.fullmatch(r"terrace-secrets verify: PASS 13 secrets, 5 templates, root-only modes, ramfs\.", output) is not None, "secrets verify " + output)

      def modes(machine):
          generation = machine.succeed("readlink /run/secrets").strip()
          ok(re.fullmatch(r"/run/secrets\.d/[0-9]+", generation) is not None, "secrets link to a generation")
          ok(machine.succeed("findmnt -no FSTYPE /run/secrets.d").strip() == "ramfs", "secrets on ramfs")
          files = machine.succeed(f"find {generation} -type f -printf '%u:%g:%m %P\\n' | sort").strip().splitlines()
          ok(len(files) == 18 and all(line.startswith("root:root:400 ") for line in files), f"18 root:root 0400 files: {len(files)}")
          for path in ["/run/secrets/website/lifecycle-intake", "/run/secrets/rendered/terrace-website/cms.env", "/run/secrets/tls/key"]:
              for user in ["terrace_cms", "terrace_web", "nobody"]:
                  machine.fail(f"runuser -u {user} -- cat {path} >/dev/null 2>&1")
          ok(True, "service accounts cannot read secrets")
          units = machine.succeed("systemctl show terrace-cms.service terrace-web.service -p EnvironmentFiles --value").split()
          ok([unit for unit in units if unit.startswith("/")] == ["/run/secrets/rendered/terrace-website/cms.env", "/run/secrets/rendered/terrace-website/web.env"], "env files come from sops templates")
          machine.fail("test -e /var/lib/terrace-website-inputs")
          ok(machine.succeed("find /var/lib/terrace-gateway-test -mindepth 1 -printf '%y %P\\n'").strip() == "l credentials.json", "only the harness credentials link outside ramfs")

      # grep exits 1 when nothing matches; only a real error (2) may fail a scan.
      GREP = "sh -c 'grep -lF -D skip -f \"$0\" -- \"$@\"; test $? -le 1 || exit 255'"

      def files_with_values(machine, producer, patterns, timeout=600):
          # producer prints NUL-separated paths; the hit list stays inside the guest.
          machine.succeed(producer + " | xargs -0r " + GREP + " " + patterns + " >/run/terrace-scan-hits", timeout=timeout)
          return machine.succeed("wc -l </run/terrace-scan-hits && rm /run/terrace-scan-hits").strip()

      def scan(machine, label, closure_scan=True):
          # Patterns come from this host's own ramfs generation, into private tmpfs.
          machine.succeed("rm -rf /run/terrace-scan && install -d -m 0700 /run/terrace-scan")
          machine.succeed("rehearsal-patterns /run/terrace-scan/all --directory \"$(readlink /run/secrets)\" >/dev/null")
          machine.succeed("rehearsal-patterns /run/terrace-scan/disk --exclude-database --directory \"$(readlink /run/secrets)\" >/dev/null")
          count = int(machine.succeed("wc -l </run/terrace-scan/all").strip())
          ok(count >= 20, f"{label}: {count} value patterns")
          if closure_scan:
              hits = files_with_values(machine, "xargs -r -a ${closure}/store-paths -I{} find {} -type f -print0", "/run/terrace-scan/all", timeout=1800)
              ok(hits == "0", f"{label}: no value in the system closure")
          ok(files_with_values(machine, "{ find -L /etc/systemd /run/systemd -type f -print0 2>/dev/null; true; }", "/run/terrace-scan/all") == "0", f"{label}: no value in unit files")
          ok(machine.succeed("systemctl show '*' --all --no-pager 2>/dev/null | grep -cF -f /run/terrace-scan/all || true").strip() == "0", f"{label}: no value in unit properties")
          ok(machine.succeed("journalctl --namespace='*' -o export --no-pager 2>/dev/null | grep -acF -f /run/terrace-scan/all || true").strip() == "0", f"{label}: no value in any journal")
          ok(machine.succeed("cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' '\\n' | grep -cF -f /run/terrace-scan/all || true").strip() == "0", f"{label}: no value in process arguments")
          machine.succeed("sync")
          files = files_with_values(machine, "find / -xdev \\( -path /nix -o -path /var/lib/postgresql -o -path /var/lib/terrace-backup -o -path " + RESTORE + " \\) -prune -o -type f -print0", "/run/terrace-scan/all")
          ok(files == "0", f"{label}: no value in a file on the root disk outside the databases")
          raw = machine.succeed("grep -acF -f /run/terrace-scan/disk /dev/vda || true", timeout=600).strip()
          ok(raw == "0", f"{label}: no non-database value anywhere on the raw disk")
          machine.succeed("rm -rf /run/terrace-scan")

      def signature():
          return {
              "realm": sql("select id from realm where name='terrace'", "keycloak"),
              "users": sql("select md5(string_agg(id||username,chr(10) order by id)) from user_entity", "keycloak"),
              "migrations": sql("select name||batch from payload_migrations order by id"),
              "outbox": app.succeed("cd " + OUTBOX + " && stat -c '%U %a' . && find . -type f -printf '%P %U %m\\n' | LC_ALL=C sort && find . -type f -print0 | LC_ALL=C sort -z | xargs -0r sha256sum").strip(),
              "applications": sql("select md5(coalesce(string_agg(row_to_json(a)::text,chr(10) order by a.id),''')) from member_applications a"),
              "roles": sql("select md5(coalesce(string_agg(rolname||coalesce(rolpassword,'''),chr(10) order by rolname),''')) from pg_authid", "postgres"),
              "media": app.succeed("cd " + MEDIA + " && find . -type f -print0 | LC_ALL=C sort -z | xargs -0r sha256sum").strip(),
          }

      def stage(host_dir):
          operator.succeed("rm -rf /tmp/shared/stage/app && install -d -m 0700 /tmp/shared/stage/app")
          operator.succeed(f"cp {host_dir}/ssh_host_ed25519_key {host_dir}/ssh_host_ed25519_key.pub /tmp/shared/stage/app/")
          operator.succeed(f"cp {STORE}/hosts/app/secrets.sops.json /tmp/shared/stage/app/")

      def unstage():
          # The installer's copy of the host key is deleted once the host verified.
          operator.succeed("rm -rf /tmp/shared/stage/app")

      def intake_header(source, out):
          # printf is a shell builtin: the value never becomes a process argument.
          app.succeed(f"umask 077; printf 'x-terrace-intake: %s\\n' \"$(cat {source})\" >{out}")

      def intake_status(header):
          return app.succeed(f"curl -s -o /dev/null -w '%{{http_code}}' -X POST -H @{header} -H 'Content-Type: application/json' --data '{{\"token\":\"x\"}}' http://127.0.0.1:13048/api/lifecycle/redeem").strip()

      def join_bogus():
          body = app.succeed("curl -s -X POST -H 'Accept: application/json' -H 'x-sveltekit-action: true' -H 'Origin: https://web.terrace.invalid' --data 'token=" + "A" * 43 + "' https://web.terrace.invalid/join/")
          result = json.loads(body)
          return result.get("type"), result.get("status")

      def env_matches(unit, secret):
          return app.succeed(f"pid=$(systemctl show {unit}.service -p MainPID --value); a=$(tr '\\0' '\\n' </proc/$pid/environ | grep '^LIFECYCLE_INTAKE_SECRET=' | sha256sum); b=$(printf 'LIFECYCLE_INTAKE_SECRET=%s\\n' \"$(cat {secret})\" | sha256sum); test \"$a\" = \"$b\" && echo same || echo different").strip()

      started_at = time.time()
      operator.start()
      operator.wait_for_unit("multi-user.target")
      operator.wait_for_unit("sshd.service")
      operator.succeed("install -d -m 0700 /root/escrow /root/.ssh && install -m 0600 ${keys}/operator /root/.ssh/operator")

      with subtest("operator creates escrow, host key and secrets; nothing in plaintext on disk"):
          out = op("terrace-secrets escrow create " + ESCROW)
          ok(re.search(r"Escrow recipient age1[02-9ac-hj-np-z]{58}\.", out) is not None, "escrow recipient")
          out = op("terrace-secrets host create app --role app --stage /root/stage/app")
          ok(re.search(r"Host app: ssh-ed25519 key staged; age recipient age1", out) is not None, "host key created before install")
          out = op("terrace-secrets generate app")
          ok("Generated 10 secrets for app. Supply with `set`: tls/certificate, tls/key." in out, out.strip())
          # A leaf for the three synthetic names, signed in tmpfs scratch by the synthetic CA.
          op("sh -ec 'umask 077; d=$(mktemp -d /dev/shm/tls.XXXXXX); "
             "openssl req -new -nodes -newkey rsa:2048 -sha256 -keyout $d/key.pem -out $d/req.pem -subj /CN=web.terrace.invalid 2>/dev/null; "
             "openssl x509 -req -in $d/req.pem -CA ${ca}/ca.pem -CAkey ${ca}/ca-key.pem -set_serial 0x$(openssl rand -hex 8) -days 30 -sha256 -extfile ${extension} -out $d/cert.pem 2>/dev/null; "
             "terrace-secrets set app tls/key <$d/key.pem; terrace-secrets set app tls/certificate <$d/cert.pem; rm -rf $d'")
          op("bun --no-env-file ${users}/rehearsal-users.ts | terrace-secrets set app rehearsal/users-json --rehearsal")
          out = op("terrace-secrets check app --rehearsal")
          ok("app: 13 secrets encrypted to the host key and the escrow key." in out, out.strip())
          operator.fail(ENV + "terrace-secrets check app")
          operator.fail(ENV + "terrace-secrets rotate app identity/database-password")
          operator.fail(ENV + "terrace-secrets generate app")
          ok(True, "tool refuses unlisted names, manual rotations and regeneration")
          public = op("terrace-secrets public app backup/primary/sftp-key").strip()
          ok(public.startswith("ssh-ed25519 "), "sftp public half")
          operator.succeed("install -d -o restic -g users -m 0700 /home/restic/.ssh /home/restic/repository && echo " + shlex.quote(public) + " >/home/restic/.ssh/authorized_keys && chown restic:users /home/restic/.ssh/authorized_keys && chmod 0600 /home/restic/.ssh/authorized_keys")
          stage("/root/stage/app")

      with subtest("brand-new host: staged key, secrets on ramfs, no writer before adoption"):
          app.start()
          app.wait_for_unit("multi-user.target")
          app.wait_for_unit("sshd.service")
          unstage()
          ok(app.succeed("ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2").strip() == operator.succeed("ssh-keygen -lf /root/stage/app/ssh_host_ed25519_key.pub | cut -d' ' -f2").strip(), "host runs the operator-generated key")
          ok(app.succeed("systemctl show sops-install-secrets.service -p Result --value").strip() == "success", "sops-install-secrets succeeded")
          verify(app)
          writers_inactive(app)
          app.fail("test -e /var/lib/terrace-production/adopted")

      with subtest("adopt, enrol, migrate and pass the shared protocol through the gateway"):
          # The temporary administrator exists only for the first Keycloak start.
          app.succeed("install -d -m 0700 /run/terrace-rehearsal && install -m 0400 /run/secrets/rendered/rehearsal/keycloak-bootstrap.env /run/terrace-rehearsal/keycloak-bootstrap.env")
          app.succeed("terrace-production adopt")
          app.wait_for_unit("keycloak.service", timeout=360)
          app.wait_for_unit("nginx.service")
          for unit in ["terrace-cms", "terrace-web"]:
              state = app.succeed(f"systemctl show {unit}.service -p ActiveState --value").strip()
              condition = app.succeed(f"systemctl show {unit}.service -p ConditionResult --value").strip()
              ok((state, condition) == ("inactive", "no"), f"{unit} skipped until the first migration")
          protocol("seed", "Disposable identity enrollment passed; temporary administrator removed.")
          app.succeed("rm -rf /run/terrace-rehearsal")
          ok(sql("select count(*) from user_entity where username='gateway-fixture-bootstrap'", "keycloak") == "0", "bootstrap administrator removed")
          app.succeed("systemctl start terrace-cms-migrate.service && systemctl start terrace-cms.service terrace-web.service", timeout=300)
          app.succeed("install -m 0600 /dev/null " + MEDIA + "/private.png && echo 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGPgUbL4DwACCgFmeGgpMwAAAABJRU5ErkJggg==' | base64 -d >" + MEDIA + "/private.png && cp " + MEDIA + "/private.png " + MEDIA + "/public.png && chown terrace_cms:terrace_cms " + MEDIA + "/*.png")
          sql("insert into media(alt,public,filename,mime_type,filesize,width,height) values ('Synthetic private',false,'private.png','image/png',70,1,1),('Synthetic public',true,'public.png','image/png',70,1,1)")
          app.succeed("runuser -u postgres -- psql -XAt -v ON_ERROR_STOP=1 -d terrace_cms -f /etc/gateway-content.sql >/dev/null 2>&1")
          output = protocol("test", "Native HTTPS protocol checks passed: ")
          found = re.search(r"passed: ([0-9]+) bounded", output)
          assert found is not None, output
          assertions = int(found.group(1))
          ok(assertions == 198, f"198 protocol assertions ({assertions})")
          protocol("content", "Imported-content checks passed")
          for unit, name, value in [("terrace-cms", "LIFECYCLE_OUTBOX_DIR", OUTBOX), ("terrace-cms", "LIFECYCLE_PUBLIC_ORIGIN", "https://web.terrace.invalid"),
                                    ("terrace-web", "CMS_PUBLIC_ORIGIN", "https://cms.terrace.invalid"), ("terrace-web", "ADDRESS_HEADER", "X-Forwarded-For")]:
              app.succeed(f"pid=$(systemctl show {unit}.service -p MainPID --value) && tr '\\0' '\\n' </proc/$pid/environ | grep -qx {name}={value}")
          ok(True, "lifecycle settings forced by the module")
          lifecycle_steps("original", LIFECYCLE_ORIGINAL_STEPS)
          app.succeed("terrace-backup init primary >/dev/null 2>&1")
          ok(app.succeed("systemctl is-enabled terrace-cms-sweep.timer terrace-backup.timer").split() == ["enabled", "enabled"], "sweep and backup timers enabled")
          ok(app.succeed("systemctl is-active terrace-cms-sweep.timer terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer").split() == ["active"] * 4, "sweep and backup timers active")

      with subtest("only SSH and HTTPS are reachable from outside; everything else on loopback"):
          for port in [22, 443]:
              operator.succeed(f"nc -z -w 2 app {port}")
          for port in [80, 5432, 8088, 9000, 13048, 14294]:
              operator.fail(f"nc -z -w 2 app {port}")
          listeners = app.succeed("ss -H -ltnu | awk '{print $1\" \"$5}'").split("\n")
          public = sorted({line for line in listeners if line and not re.search(r" (127\.0\.0\.1|\[::1\]|\[::ffff:127\.0\.0\.1\])(%[a-z0-9]+)?:[0-9]+$", line)})
          tcp = [line for line in public if line.startswith("tcp ")]
          udp = [line for line in public if line.startswith("udp ")]
          ok(tcp == ["tcp 0.0.0.0:22", "tcp 0.0.0.0:443", "tcp [::]:22", "tcp [::]:443"], f"public TCP listeners {tcp}")
          # Only address-configuration clients may hold a non-loopback UDP socket.
          ok(all(re.search(r":(68|546)$", line) for line in udp), f"public UDP sockets {udp}")
          operator.succeed("curl --fail --silent --max-time 10 --resolve identity.terrace.invalid:443:$(getent hosts app | cut -d' ' -f1) https://identity.terrace.invalid/realms/terrace/.well-known/openid-configuration >/dev/null")
          ok(True, "trusted HTTPS from another host")

      with subtest("owners, modes and ramfs; values absent from store, units, journals, arguments and disk"):
          verify(app)
          modes(app)
          timed("scanOriginal", lambda: scan(app, "original"))

      with subtest("rotate the intake secret: both sides pick it up, the old value stops working"):
          app.succeed("rm -rf /run/terrace-rotation && install -d -m 0700 /run/terrace-rotation && install -m 0400 /run/secrets/website/lifecycle-intake /run/terrace-rotation/old")
          intake_header("/run/terrace-rotation/old", "/run/terrace-rotation/old-header")
          ok(intake_status("/run/terrace-rotation/old-header") == "404", "old value accepted before rotation")
          ok(join_bogus() == ("failure", 404), "website reaches the CMS before rotation")
          pids = app.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value").split()
          out = op("terrace-secrets rotate app website/lifecycle-intake")
          ok("Rotated website/lifecycle-intake on app. `push` delivers it and restarts: terrace-cms.service, terrace-web.service." in out, out.strip())
          out = op("terrace-secrets push app --address app --identity /root/.ssh/operator --rehearsal")
          ok("Installed 13 encrypted secrets." in out and "PASS 13 secrets" in out, "push over SSH pinned to the operator-generated host key")
          for unit, pid in zip(["terrace-cms", "terrace-web"], pids):
              app.wait_until_succeeds(f"now=$(systemctl show {unit}.service -p MainPID --value) && test \"$now\" != 0 && test \"$now\" != {pid} && systemctl is-active {unit}.service", timeout=240)
          new_pids = app.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value").split()
          ok(all(a != b for a, b in zip(pids, new_pids)), "CMS and website restarted by sops-install-secrets")
          app.fail("cmp -s /run/terrace-rotation/old /run/secrets/website/lifecycle-intake")
          ok(app.succeed("grep -rlF -f /run/terrace-rotation/old /run/secrets.d/ | wc -l").strip() == "0", "old value gone from every secrets generation")
          intake_header("/run/secrets/website/lifecycle-intake", "/run/terrace-rotation/new-header")
          ok(intake_status("/run/terrace-rotation/old-header") == "403", "old value refused after rotation")
          ok(intake_status("/run/terrace-rotation/new-header") == "404", "new value accepted after rotation")
          for unit in ["terrace-cms", "terrace-web"]:
              ok(env_matches(unit, "/run/secrets/website/lifecycle-intake") == "same", f"{unit} runs with the new value")
              ok(env_matches(unit, "/run/terrace-rotation/old") == "different", f"{unit} no longer has the old value")
          app.wait_until_succeeds("curl --fail --silent --max-time 5 https://web.terrace.invalid/events/ >/dev/null", timeout=60)
          ok(join_bogus() == ("failure", 404), "website and CMS agree after rotation")
          rotated = app.succeed("sha256sum </run/secrets/website/lifecycle-intake").strip()
          app.succeed("rm -rf /run/terrace-rotation")

      with subtest("reboot: ramfs vanishes, secrets return from ciphertext, adopted writers start alone"):
          app.succeed("touch /run/secrets.d/terrace-vanish-marker")
          app.shutdown()
          app.start()
          app.wait_for_unit("multi-user.target")
          app.fail("test -e /run/secrets.d/terrace-vanish-marker")
          ok(True, "ramfs contents gone after shutdown")
          verify(app)
          app.wait_for_unit("keycloak.service", timeout=360)
          app.wait_for_unit("terrace-cms.service", timeout=240)
          app.wait_for_unit("terrace-web.service", timeout=240)
          ok(app.succeed("systemctl show terrace-production-boot.service -p Result --value").strip() == "success", "boot gate started the writers")
          ok(app.succeed("sha256sum </run/secrets/website/lifecycle-intake").strip() == rotated, "rotated value survives a reboot (from the host ciphertext)")
          lifecycle_reboot = app.succeed("python3 /etc/gateway-protocol.py content", timeout=120).strip()
          ok(lifecycle_reboot.startswith("Imported-content checks passed"), "service after reboot")

      with subtest("back up, destroy the host, re-key from escrow for a new host key"):
          app.succeed("systemctl stop terrace-cms-sweep.timer terrace-cms-sweep.service terrace-web.service terrace-cms.service keycloak.service nginx.service", timeout=120)
          before = signature()
          app.succeed("systemctl start terrace-backup.service", timeout=600)
          snapshots = [s for s in json.loads(app.succeed("terrace-backup restic primary snapshots --json 2>/dev/null")) if s["hostname"] == "app"]
          ok(len(snapshots) == 1, "one snapshot")
          snapshot = snapshots[0]["id"]
          app.succeed("systemctl start terrace-backup-check.service", timeout=600)
          ok(app.succeed("systemctl show terrace-backup-check.service -p Result --value").strip() == "success", "repository check")
          operator.fail("jq . /home/restic/repository/config >/dev/null 2>&1")
          app.succeed("systemctl stop postgresql.target postgresql.service")
          app.shutdown()
          disks = list(app.state_dir.glob("*.qcow2"))
          ok(len(disks) == 1, "one original disk")
          disks[0].unlink()
          ok(not disks[0].exists(), "original disk destroyed")
          plain = op("sops decrypt --output-type json " + STORE + "/hosts/app/secrets.sops.json | jq -S -c . | sha256sum").strip()
          old_recipient = operator.succeed("cat " + STORE + "/hosts/app/recipient").strip()
          out = op("terrace-secrets host create app --role app --stage /root/stage/app-2 --replace")
          ok("Run `rekey` before pushing." in out, "replacement key pending re-key")
          operator.fail(ENV + "terrace-secrets push app --address app --identity /root/.ssh/operator --rehearsal")
          op("terrace-secrets rekey app")
          op("terrace-secrets check app --rehearsal")
          new_recipient = operator.succeed("cat " + STORE + "/hosts/app/recipient").strip()
          recipients = sorted(json.loads(operator.succeed("jq -c '[.sops.age[].recipient]' " + STORE + "/hosts/app/secrets.sops.json")))
          escrow = operator.succeed("cat " + STORE + "/escrow.recipient").strip()
          ok(new_recipient != old_recipient and recipients == sorted([new_recipient, escrow]), "re-keyed to the new host and the escrow only")
          ok(op("sops decrypt --output-type json " + STORE + "/hosts/app/secrets.sops.json | jq -S -c . | sha256sum").strip() == plain, "same values after re-key")
          revoked = "HOME=/var/empty env -u SOPS_AGE_KEY_FILE SOPS_AGE_SSH_PRIVATE_KEY_FILE={} sops decrypt --output-type json " + STORE + "/hosts/app/secrets.sops.json >/dev/null 2>&1"
          operator.fail(revoked.format("/root/stage/app/ssh_host_ed25519_key"))
          operator.succeed(revoked.format("/root/stage/app-2/ssh_host_ed25519_key"))
          ok(True, "retired host key cannot decrypt; the new one can")
          stage("/root/stage/app-2")

      with subtest("replacement: new key, restore from backup, same data and the lifecycle continues"):
          app.start()
          app.wait_for_unit("multi-user.target")
          unstage()
          ok(app.succeed("ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2").strip() == operator.succeed("ssh-keygen -lf /root/stage/app-2/ssh_host_ed25519_key.pub | cut -d' ' -f2").strip(), "replacement runs the new host key")
          verify(app)
          writers_inactive(app)
          app.succeed("umask 077; terrace-backup-restore --target primary --snapshot " + snapshot + " --dest " + RESTORE + " >/var/tmp/restore.log 2>&1", timeout=600)
          app.succeed("grep -qx 'RECONCILIATION OK' " + RESTORE + "/reconciliation.txt")
          ok(True, "restore reconciliation")
          for path in [MEDIA, OUTBOX]:
              app.succeed("test ! -L " + path + " && { test ! -e " + path + " || test -z \"$(ls -A " + path + ")\"; }")
              app.succeed("mkdir -p " + path + " && cp -a " + RESTORE + "/files" + path + "/. " + path + "/")
          pg_dir = app.succeed("cat /etc/rehearsal-pg-directory").strip()
          ok(re.fullmatch("/var/lib/postgresql/18", pg_dir) is not None, "PostgreSQL 18 data directory")
          app.succeed("test ! -L " + pg_dir + " && { test ! -e " + pg_dir + " || test -z \"$(ls -A " + pg_dir + ")\"; }")
          app.succeed("install -d -m 0700 -o postgres -g postgres " + pg_dir + " && cp -a " + RESTORE + "/pgdata/. " + pg_dir + "/ && chown -R postgres:postgres " + pg_dir)
          app.succeed("terrace-production adopt")
          app.wait_for_unit("keycloak.service", timeout=360)
          ok(sql("select count(*) from user_entity where username='gateway-fixture-bootstrap'", "keycloak") == "0", "no bootstrap administrator on the replacement")
          app.succeed("systemctl start terrace-cms-migrate.service && systemctl start terrace-cms.service terrace-web.service", timeout=300)
          app.succeed("systemctl stop terrace-cms-sweep.timer")
          after = signature()
          for key in before:
              ok(after[key] == before[key], "restored " + key)
          ok(app.succeed("sha256sum </run/secrets/website/lifecycle-intake").strip() == rotated, "the rotated intake value survived the re-key")
          for unit in ["terrace-cms", "terrace-web"]:
              ok(env_matches(unit, "/run/secrets/website/lifecycle-intake") == "same", f"replacement {unit} runs with the delivered value")
          app.succeed("systemctl start terrace-cms-sweep.timer")
          lifecycle_steps("replacement", LIFECYCLE_REPLACEMENT_STEPS)
          verify(app)
          modes(app)
          timed("scanReplacement", lambda: scan(app, "replacement", closure_scan=False))

      with subtest("operator disk never held a plaintext value"):
          operator.succeed("rm -rf /dev/shm/scan && install -d -m 0700 /dev/shm/scan")
          op("sops decrypt --output-type json " + STORE + "/hosts/app/secrets.sops.json | rehearsal-patterns /dev/shm/scan/all >/dev/null")
          operator.succeed("sync")
          ok(files_with_values(operator, "find / -xdev -path /nix -prune -o -type f -print0", "/dev/shm/scan/all") == "0", "operator: no value in a file")
          ok(operator.succeed("grep -acF -f /dev/shm/scan/all /dev/vda || true", timeout=600).strip() == "0", "operator: no value on the raw disk")
          ok(operator.succeed("journalctl --namespace='*' -o export --no-pager 2>/dev/null | grep -acF -f /dev/shm/scan/all || true").strip() == "0", "operator: no value in the journal")
          operator.succeed("rm -rf /dev/shm/scan")

      with subtest("stop owned guests"):
          app.shutdown()
          operator.shutdown()
      print("PRODUCTION-PROOF " + json.dumps({"assertions": len(checks), "protocolAssertions": assertions, "seconds": round(time.time() - started_at, 2), "timings": timings}))
    '';
}
