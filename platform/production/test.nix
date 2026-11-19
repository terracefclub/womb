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
  # The rehearsal's throwaway escrow key, made at build time so its PUBLIC recipient
  # can be pinned as a build parameter, exactly as a production host pins the owner's.
  escrow = pkgs.runCommand "terrace-production-rehearsal-escrow" {nativeBuildInputs = [pkgs.age];} ''
    mkdir $out
    age-keygen -o $out/escrow.age 2>/dev/null
    age-keygen -y $out/escrow.age >$out/escrow.pub
  '';
  escrowRecipient = lib.removeSuffix "\n" (builtins.readFile "${escrow}/escrow.pub");
  users = pkgs.runCommand "terrace-production-rehearsal-users" {} ''
    mkdir $out
    cp -r ${contract}/. $out/
    cp ${./rehearsal-users.ts} $out/rehearsal-users.ts
  '';
  patterns = pkgs.writers.writePython3Bin "rehearsal-patterns" {flakeIgnore = ["E501"];} (builtins.readFile ./rehearsal-patterns.py);
  leafExtension = hostnames:
    pkgs.writeText "terrace-rehearsal-leaf.ext" ''
      basicConstraints=critical,CA:FALSE
      keyUsage=critical,digitalSignature,keyEncipherment
      extendedKeyUsage=serverAuth
      subjectAltName=${lib.concatMapStringsSep "," (name: "DNS:${name}") hostnames}
    '';
  extension = leafExtension names;
  relayExtension = leafExtension ["relay.terrace.invalid"];
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
      environment.systemPackages = [tool pkgs.sops pkgs.age pkgs.ssh-to-age pkgs.openssl pkgs.bun pkgs.jq pkgs.curl pkgs.netcat-openbsd patterns];
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
        memorySize = 9216;
        cores = 2;
        diskSize = 4096;
      };
      terrace.production = {
        instance = "rehearsal";
        # 25 percent headroom over the writers' 6912 MiB of ceilings (runbook 6.22).
        memoryMiB = 9216;
        inherit escrowRecipient;
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
    # The production relay role, booted once: secrets delivered, adopted, its own service checked.
    relay = {...}: {
      imports = [sopsModule ./relay.nix ./rehearsal-relay.nix];
      virtualisation = {
        memorySize = 2048;
        cores = 1;
      };
      terrace.production = {
        instance = "rehearsal";
        memoryMiB = 2048;
        inherit escrowRecipient;
        operatorKeys = [(publicKey "operator")];
        relay = {
          hostname = "relay.terrace.invalid";
          # A documentation address: accepted by the rehearsal instance only.
          publicIPv4 = "192.0.2.10";
          backup.primary = {
            repository = "sftp:restic@operator:relay-repository";
            knownHosts = "operator ${publicKey "repository-host"}";
          };
        };
      };
    };
  };
  testScript = {nodes, ...}: let
    closure = pkgs.closureInfo {rootPaths = [nodes.app.system.build.toplevel];};
  in
    builtins.readFile ../website-gateway/lifecycle_steps.py
    + ''
      import json, os, re, shlex, time

      checks = []
      def ok(condition, label):
          assert condition, label
          checks.append(label)

      STORE = "/root/store"
      ESCROW = "/root/escrow/escrow.age"
      # Exported, so every command of a pipeline sees the operator environment.
      ENV = f"export TERRACE_SECRETS_STORE={STORE} SOPS_AGE_KEY_FILE={ESCROW} TERRACE_SECRETS_SCRATCH=/dev/shm TERRACE_ESCROW_RECIPIENT=${escrowRecipient}; "
      STAGE = "/dev/shm/terrace-stage-app"
      PUSH = " --address app --identity /root/.ssh/operator --rehearsal"
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

      def verify(machine, counts="13 secrets, 5 templates"):
          output = machine.succeed("terrace-secrets verify").strip()
          ok(output == f"terrace-secrets verify: PASS {counts}, root-only modes, valid formats, ramfs.", machine.name + " secrets verify " + output)

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

      def counts(machine, patterns, disk_patterns, closure_scan):
          found = {}
          if closure_scan:
              found["closure"] = files_with_values(machine, "xargs -r -a ${closure}/store-paths -I{} find {} -type f -print0", patterns, timeout=1800)
          found["unit files"] = files_with_values(machine, "{ find -L /etc/systemd /run/systemd -type f -print0 2>/dev/null; true; }", patterns)
          found["unit properties"] = machine.succeed("systemctl show '*' --all --no-pager 2>/dev/null | grep -cF -f " + patterns + " || true").strip()
          found["journals"] = machine.succeed("journalctl --namespace='*' -o export --no-pager 2>/dev/null | grep -acF -f " + patterns + " || true").strip()
          found["process arguments"] = machine.succeed("cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' '\\n' | grep -cF -f " + patterns + " || true").strip()
          machine.succeed("sync")
          found["root-disk files"] = files_with_values(machine, "find / -xdev \\( -path /nix -o -path /var/lib/postgresql -o -path /var/lib/terrace-backup -o -path " + RESTORE + " \\) -prune -o -type f -print0", patterns)
          found["raw disk"] = machine.succeed("grep -acF -f " + disk_patterns + " /dev/vda || true", timeout=600).strip()
          return found

      def scan(machine, label, closure_scan=True):
          # Patterns come from this host's own ramfs generation, into private tmpfs.
          machine.succeed("rm -rf /run/terrace-scan && install -d -m 0700 /run/terrace-scan")
          machine.succeed("rehearsal-patterns /run/terrace-scan/all --directory \"$(readlink /run/secrets)\" >/dev/null")
          machine.succeed("rehearsal-patterns /run/terrace-scan/disk --exclude-database --directory \"$(readlink /run/secrets)\" >/dev/null")
          # The host's age identity, derived from its SSH key, must appear nowhere either.
          machine.succeed("${pkgs.ssh-to-age}/bin/ssh-to-age -private-key -i /etc/ssh/ssh_host_ed25519_key | tee -a /run/terrace-scan/all >>/run/terrace-scan/disk")
          count = int(machine.succeed("wc -l </run/terrace-scan/all").strip())
          ok(count >= 20, f"{label}: {count} value patterns")
          # Positive control: a non-secret canary planted in a unit description,
          # the journal, a process argument and a disk file must be found by the
          # same scans, so a zero below means absence, not a blind scan.
          canary = "terrace-scan-canary-" + os.urandom(24).hex()
          machine.succeed(f"echo {canary} >/run/terrace-scan/canary && echo {canary} >/var/tmp/terrace-scan-canary && sync")
          # The closure cannot be written to; its control is a known string the harness built into it.
          machine.succeed("install -m 0600 /run/terrace-scan/canary /run/terrace-scan/closure-canary && echo terrace-closure-canary-5b1d0c7e9a3f4c28b6e1d2a7f0c9e8b4 >>/run/terrace-scan/closure-canary")
          # Absolute sleep: transient units get systemd's default PATH. The trailing `true` keeps the shell, and its canary argument, alive.
          machine.succeed(f"systemd-run --unit=terrace-scan-canary -p Description={canary} sh -c '${pkgs.coreutils}/bin/sleep 900; true' {canary}")
          control = counts(machine, "/run/terrace-scan/closure-canary", "/run/terrace-scan/canary", closure_scan)
          ok(all(int(value) > 0 for value in control.values()), f"{label}: canary found by every scan {control}")
          found = counts(machine, "/run/terrace-scan/all", "/run/terrace-scan/disk", closure_scan)
          for where, hits in found.items():
              ok(hits == "0", f"{label}: no secret value in {where}")
          machine.succeed("systemctl stop terrace-scan-canary.service; rm -f /var/tmp/terrace-scan-canary; rm -rf /run/terrace-scan")

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

      def stage(host="app"):
          # What the installer stages: the operator-generated host key, nothing else.
          source = "/dev/shm/terrace-stage-" + host
          operator.succeed(f"rm -rf /tmp/shared/stage/{host} && install -d -m 0700 /tmp/shared/stage/{host}")
          operator.succeed(f"cp {source}/ssh_host_ed25519_key {source}/ssh_host_ed25519_key.pub /tmp/shared/stage/{host}/")

      def unstage(host="app"):
          # The installer's and the operator's copies of the host key go once the host runs it.
          operator.succeed(f"rm -rf /tmp/shared/stage/{host}")
          op("terrace-secrets host unstage " + host)
          operator.fail("test -e /dev/shm/terrace-stage-" + host)

      def pids(units):
          return {unit: app.succeed(f"systemctl show {unit}.service -p MainPID --value").strip() for unit in units}

      def unit_state(unit, prop):
          return app.succeed(f"systemctl show {unit} -p {prop} --value").strip()

      def first_boot_without_secrets(label):
          app.start()
          app.wait_for_unit("multi-user.target")
          ok(app.succeed("ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2").strip() == operator.succeed(f"ssh-keygen -lf {STAGE}/ssh_host_ed25519_key.pub | cut -d' ' -f2").strip(), label + ": host runs the operator-generated key")
          unstage()
          # The installer stages no ciphertext, so the first boot has none and stays gated.
          ok(unit_state("sops-install-secrets.service", "Result") != "success", label + ": no secrets before the first push")
          app.fail("terrace-secrets verify")
          writers_inactive(app)
          ok(unit_state("terrace-production-boot.service", "ConditionResult") == "no", label + ": boot gate closed")

      def push(label, host="app", count=13):
          out = op(f"terrace-secrets push {host} --address {host} --identity /root/.ssh/operator --rehearsal")
          ok(f"PASS {count} secrets" in out and f"Installed {count} encrypted secrets." in out, label + ": push over SSH pinned to the operator-generated host key")

      def journal_hits(machine, needle_file):
          return machine.succeed("journalctl --namespace='*' -o export --no-pager 2>/dev/null | grep -acF -f " + needle_file + " || true").strip()

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
      operator.succeed("install -d -m 0700 /root/escrow /root/.ssh " + STORE + " && install -m 0600 ${keys}/operator /root/.ssh/operator")
      # The rehearsal escrow was made at build time so its public recipient is pinned in the host build.
      operator.succeed("install -m 0600 ${escrow}/escrow.age " + ESCROW + " && install -m 0644 ${escrow}/escrow.pub " + STORE + "/escrow.recipient")

      with subtest("operator creates the host key and the secrets; validation and refusals"):
          out = operator.succeed("export TERRACE_SECRETS_STORE=/root/throwaway-store; terrace-secrets escrow create /dev/shm/throwaway-escrow.age && rm -rf /root/throwaway-store /dev/shm/throwaway-escrow.age")
          ok(re.search(r"Escrow recipient age1[02-9ac-hj-np-z]{58}\.", out) is not None, "escrow create (separate throwaway store)")
          out = op("terrace-secrets host create app --role app")
          ok(re.search(r"Host app: ssh-ed25519 key staged in /dev/shm/terrace-stage-app; age recipient age1", out) is not None and "WARNING" not in out, "host key created before install, staged on tmpfs")
          out = op("terrace-secrets generate app")
          ok("Generated 10 secrets for app. Supply with `set`: tls/certificate, tls/key." in out, out.strip())
          # A leaf for the three synthetic names, signed in tmpfs scratch by the synthetic CA.
          op("sh -ec 'umask 077; d=$(mktemp -d /dev/shm/tls.XXXXXX); "
             "openssl req -new -nodes -newkey rsa:2048 -sha256 -keyout $d/key.pem -out $d/req.pem -subj /CN=web.terrace.invalid 2>/dev/null; "
             "openssl x509 -req -in $d/req.pem -CA ${ca}/ca.pem -CAkey ${ca}/ca-key.pem -set_serial 0x$(openssl rand -hex 8) -days 30 -sha256 -extfile ${extension} -out $d/cert.pem 2>/dev/null; "
             "terrace-secrets set app tls/key <$d/key.pem; terrace-secrets set app tls/certificate <$d/cert.pem; rm -rf $d'")
          op("bun --no-env-file ${users}/rehearsal-users.ts | terrace-secrets set app rehearsal/users-json --rehearsal")
          out = op("terrace-secrets check app --rehearsal")
          ok("app: 13 secrets encrypted to exactly the host key and the pinned escrow key." in out, out.strip())
          operator.fail(ENV + "terrace-secrets check app")
          operator.fail(ENV + "terrace-secrets rotate app identity/database-password")
          operator.fail(ENV + "terrace-secrets generate app")
          operator.fail(ENV + "printf '%s\\nCMS_LOCAL_BOOTSTRAP=1\\n' \"$(openssl rand -hex 32)\" | terrace-secrets set app website/payload-secret")
          ok(True, "tool refuses unlisted names, manual rotations, regeneration and a newline-injected value")
          public = op("terrace-secrets public app backup/primary/sftp-key").strip()
          ok(public.startswith("ssh-ed25519 "), "sftp public half")
          operator.succeed("install -d -o restic -g users -m 0700 /home/restic/.ssh /home/restic/repository && echo " + shlex.quote(public) + " >/home/restic/.ssh/authorized_keys && chown restic:users /home/restic/.ssh/authorized_keys && chmod 0600 /home/restic/.ssh/authorized_keys")
          # The revocation check later needs this first host key: keep a copy in memory only.
          operator.succeed("install -m 0600 " + STAGE + "/ssh_host_ed25519_key /dev/shm/retired-app-key && cp " + STAGE + "/ssh_host_ed25519_key.pub /dev/shm/retired-app-key.pub")
          stage()

      with subtest("brand-new host: key staged, no secrets until the first push, foreign ciphertext refused"):
          first_boot_without_secrets("new host")
          foreign = "/tmp/shared/foreign.sops.json"
          op("age-keygen -o /dev/shm/foreign.age 2>/dev/null && sops decrypt --output-type json " + STORE + "/hosts/app/secrets.sops.json | "
             "sops encrypt --age \"$(cat " + STORE + "/hosts/app/recipient),$(age-keygen -y /dev/shm/foreign.age)\" --input-type json --output-type json --filename-override x >" + foreign + "; rm -f /dev/shm/foreign.age")
          refused = app.fail("terrace-secrets install <" + foreign + " 2>&1")
          ok("recipients differ from exactly this host and the pinned escrow" in refused, "install refuses a foreign recipient")
          app.fail("test -e /var/lib/terrace-secrets/secrets.sops.json")
          operator.succeed("rm " + foreign)
          push("new host")
          verify(app)
          writers_inactive(app)
          app.fail("test -e /var/lib/terrace-production/adopted")

      with subtest("an unadopted host stays gated across a reboot; adopt --restored refused"):
          app.shutdown()
          app.start()
          app.wait_for_unit("multi-user.target")
          verify(app)
          writers_inactive(app)
          ok(unit_state("terrace-production-boot.service", "ConditionResult") == "no", "boot gate stays closed after a reboot")
          refused = app.fail("terrace-production adopt --restored 2>&1")
          ok("no reconciled restore" in refused, "adopt --restored refused without a restore")
          app.fail("test -e /var/lib/terrace-production/adopted")

      with subtest("adopt --new, enrol, migrate and pass the shared protocol through the gateway"):
          # The temporary administrator exists only for the first Keycloak start.
          app.succeed("install -d -m 0700 /run/terrace-rehearsal && install -m 0400 /run/secrets/rendered/rehearsal/keycloak-bootstrap.env /run/terrace-rehearsal/keycloak-bootstrap.env")
          # A2: the first adoption fails after PostgreSQL is initialised (nginx cannot bind 443) ...
          app.succeed("systemd-run --unit=rehearsal-block-443 ${pkgs.python3}/bin/python3 -m http.server 443 --bind 0.0.0.0")
          app.wait_until_succeeds("ss -ltn | grep -q ':443 '")
          refused = app.fail("terrace-production adopt --new 2>&1")
          ok("not adopted yet" in refused and "nginx.service" in refused, "a failed first adoption names the unit")
          app.succeed("test -e /var/lib/postgresql/18/PG_VERSION && test -e /var/lib/terrace-production/adopting && test ! -e /var/lib/terrace-production/adopted")
          ok("adoption as new not finished" in app.succeed("terrace-production status"), "status reports the unfinished adoption")
          ok("no reconciled restore" in app.fail("terrace-production adopt --restored 2>&1"), "adopt --restored still refused")
          # ... and the same command resumes once the cause is gone.
          app.succeed("systemctl stop rehearsal-block-443.service")
          app.succeed("terrace-production adopt --new")
          app.succeed("test -e /var/lib/terrace-production/adopted && test ! -e /var/lib/terrace-production/adopting")
          ok(True, "adopt --new resumed after the failure")
          ok(app.succeed("terrace-production adopt --new").strip() == "Already adopted.", "adopt on an adopted host says so")
          app.wait_for_unit("keycloak.service", timeout=360)
          app.wait_for_unit("nginx.service")
          for unit in ["terrace-cms", "terrace-web"]:
              ok((unit_state(unit + ".service", "ActiveState"), unit_state(unit + ".service", "ConditionResult")) == ("inactive", "no"), f"{unit} skipped until the first migration")
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
          ok(app.succeed("systemctl is-enabled terrace-cms-sweep.timer terrace-backup.timer").split() == ["enabled", "enabled"], "sweep and backup timers enabled")
          ok(app.succeed("systemctl is-active terrace-cms-sweep.timer terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer").split() == ["active"] * 4, "sweep and backup timers active")

      with subtest("first backup: fails visibly until the repository is initialised, then succeeds"):
          app.fail("systemctl start terrace-backup.service", timeout=300)
          ok(unit_state("terrace-backup.service", "Result") != "success", "backup fails before `terrace-backup init`")
          app.succeed("systemctl reset-failed terrace-backup.service && terrace-backup init primary >/dev/null 2>&1")
          app.succeed("systemctl start terrace-backup.service", timeout=300)
          ok(unit_state("terrace-backup.service", "Result") == "success", "first backup after init")

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

      with subtest("rotate the intake secret: both sides pick it up, the old value stops working, nothing else restarts"):
          app.succeed("rm -rf /run/terrace-rotation && install -d -m 0700 /run/terrace-rotation && install -m 0400 /run/secrets/website/lifecycle-intake /run/terrace-rotation/old")
          intake_header("/run/terrace-rotation/old", "/run/terrace-rotation/old-header")
          ok(intake_status("/run/terrace-rotation/old-header") == "404", "old value accepted before rotation")
          ok(join_bogus() == ("failure", 404), "website reaches the CMS before rotation")
          apps = pids(["terrace-cms", "terrace-web"])
          others = pids(["postgresql", "keycloak", "nginx"])
          out = op("terrace-secrets rotate app website/lifecycle-intake")
          ok("Rotated website/lifecycle-intake on app. `push` delivers it and restarts: terrace-cms.service, terrace-web.service." in out, out.strip())
          push("intake rotation")
          for unit, pid in apps.items():
              app.wait_until_succeeds(f"now=$(systemctl show {unit}.service -p MainPID --value) && test \"$now\" != 0 && test \"$now\" != {pid} && systemctl is-active {unit}.service", timeout=240)
          ok(all(a != b for a, b in zip(apps.values(), pids(["terrace-cms", "terrace-web"]).values())), "CMS and website restarted by sops-install-secrets")
          ok(pids(["postgresql", "keycloak", "nginx"]) == others, "PostgreSQL, Keycloak and nginx not restarted by an intake rotation")
          app.fail("cmp -s /run/terrace-rotation/old /run/secrets/website/lifecycle-intake")
          ok(files_with_values(app, "find /run/secrets.d -type f -print0", "/run/terrace-rotation/old") == "0", "old value gone from every secrets generation")
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

      with subtest("manual secrets: database password and backup repository password via `apply`"):
          before = pids(["keycloak"])
          op("terrace-secrets rotate app identity/database-password --manual")
          push("database password")
          ok(pids(["keycloak"]) == before, "push alone does not restart Keycloak")
          out = op("terrace-secrets apply app identity/database-password" + PUSH)
          ok("Database role keycloak updated with a SCRAM verifier and Keycloak restarted." in out, "database password applied on the host as a SCRAM verifier")
          app.wait_for_unit("keycloak.service", timeout=360)
          ok(pids(["keycloak"]) != before, "Keycloak restarted onto the new database password")
          app.succeed("PGPASSWORD=\"$(cat /run/secrets/identity/database-password)\" psql -X -h 127.0.0.1 -U keycloak -d keycloak -c 'select 1' >/dev/null")
          ok(True, "the delivered database password opens the Keycloak role")
          op("terrace-secrets rotate app backup/primary/repository-password --manual")
          out = op("terrace-secrets apply app backup/primary/repository-password" + PUSH)
          ok("Added the new repository key" in out, "new repository key added over the pinned channel")
          push("repository password")
          out = op("terrace-secrets apply app backup/primary/repository-password --finish --only-current" + PUSH)
          ok("Removed 1 old repository key(s)" in out, "old repository key removed")
          ok(len(json.loads(app.succeed("terrace-backup restic primary key list --json 2>/dev/null"))) == 1, "one repository key left")

      with subtest("a failing database-password apply logs neither the cleartext nor the verifier"):
          app.succeed("rm -rf /run/terrace-db && install -d -m 0700 /run/terrace-db && install -m 0400 /run/secrets/identity/database-password /run/terrace-db/patterns && echo 'SCRAM-SHA-256$' >>/run/terrace-db/patterns && echo 'role \"terrace_missing_role\" does not exist' >/run/terrace-db/control")
          ok(journal_hits(app, "/run/terrace-db/control") == "0", "control text absent before the failure")
          refused = app.fail("terrace-secrets host-apply identity/database-password --role terrace_missing_role 2>&1")
          ok("ALTER ROLE failed (statement text is not logged)" in refused, "apply against a missing role fails")
          app.wait_until_succeeds("test $(journalctl --namespace='*' -o export --no-pager | grep -acF -f /run/terrace-db/control) -gt 0", timeout=30)
          ok(True, "PostgreSQL logged the error itself (control)")
          ok(journal_hits(app, "/run/terrace-db/patterns") == "0", "no cleartext and no SCRAM verifier in any journal")
          ok(app.succeed("find /var/lib/postgresql -name '*.log' -type f | wc -l").strip() == "0", "PostgreSQL keeps no log file of its own")
          app.succeed("rm -rf /run/terrace-db")

      with subtest("the upstream Keycloak database init logs no cleartext when its CREATE ROLE fails"):
          app.succeed("systemctl stop keycloak.service")
          sql("ALTER ROLE keycloak RENAME TO keycloak_held", "postgres")
          sql("ALTER SYSTEM SET default_transaction_read_only = on", "postgres")
          app.succeed("systemctl reload postgresql.service")
          app.succeed("rm -rf /run/terrace-db && install -d -m 0700 /run/terrace-db && install -m 0400 /run/secrets/identity/database-password /run/terrace-db/patterns && echo 'CREATE ROLE keycloak WITH LOGIN PASSWORD' >>/run/terrace-db/patterns && echo 'cannot execute CREATE ROLE in a read-only transaction' >/run/terrace-db/control")
          app.fail("systemctl restart keycloakPostgreSQLInit.service")
          app.wait_until_succeeds("test $(journalctl --namespace='*' -o export --no-pager | grep -acF -f /run/terrace-db/control) -gt 0", timeout=30)
          ok(True, "the init's CREATE ROLE failed and PostgreSQL logged the error (control)")
          ok(journal_hits(app, "/run/terrace-db/patterns") == "0", "no cleartext database password and no CREATE ROLE statement text in any journal")
          app.succeed("PGOPTIONS='-c default_transaction_read_only=off' runuser -u postgres -- psql -XAt -v ON_ERROR_STOP=1 -d postgres -c 'ALTER SYSTEM RESET default_transaction_read_only' >/dev/null && systemctl reload postgresql.service")
          app.succeed("PGOPTIONS='-c default_transaction_read_only=off' runuser -u postgres -- psql -XAt -v ON_ERROR_STOP=1 -d postgres -c 'ALTER ROLE keycloak_held RENAME TO keycloak' >/dev/null")
          app.succeed("systemctl reset-failed keycloakPostgreSQLInit.service && systemctl start keycloak.service", timeout=360)
          app.succeed("rm -rf /run/terrace-db")

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
          ok(unit_state("terrace-production-boot.service", "Result") == "success", "boot gate started the writers")
          ok(app.succeed("sha256sum </run/secrets/website/lifecycle-intake").strip() == rotated, "rotated value survives a reboot (from the host ciphertext)")
          ok(app.succeed("python3 /etc/gateway-protocol.py content", timeout=120).strip().startswith("Imported-content checks passed"), "service after reboot")

      with subtest("a crashing writer leaves no core dump"):
          app.succeed("touch /run/terrace-core-marker")
          restarts = int(unit_state("terrace-cms.service", "NRestarts"))
          pid = pids(["terrace-cms"])["terrace-cms"]
          app.succeed(f"kill -ABRT {pid}")
          app.wait_until_succeeds(f"test \"$(systemctl show terrace-cms.service -p NRestarts --value)\" -gt {restarts} && now=$(systemctl show terrace-cms.service -p MainPID --value) && test \"$now\" != 0 && test \"$now\" != {pid} && systemctl is-active terrace-cms.service", timeout=240)
          ok(True, "the CMS was aborted and restarted")
          ok(app.succeed("cat /proc/sys/kernel/core_pattern").strip() == "|${pkgs.coreutils}/bin/false", "kernel core pattern discards dumps")
          ok(app.succeed("find /var/lib/systemd/coredump -type f 2>/dev/null | wc -l").strip() == "0", "no systemd-coredump file")
          ok(app.succeed("find / -xdev -newer /run/terrace-core-marker -type f \\( -name core -o -name 'core.*' \\) 2>/dev/null | wc -l").strip() == "0", "no core file on the root disk")
          ok(app.succeed("journalctl --namespace='*' -o export --no-pager 2>/dev/null | grep -ac '^SYSLOG_IDENTIFIER=systemd-coredump$' || true").strip() == "0", "no coredump journal entry")
          # The kernel backstop alone: a unit that does NOT limit core size.
          app.succeed("systemd-run --unit=rehearsal-core-probe -p LimitCORE=infinity -p WorkingDirectory=/var/tmp ${pkgs.coreutils}/bin/sleep 600")
          probe = pids(["rehearsal-core-probe"])["rehearsal-core-probe"]
          ok(app.succeed(f"grep '^Max core file size' /proc/{probe}/limits | tr -s ' ' | cut -d' ' -f5").strip() == "unlimited", "probe allows core dumps")
          app.succeed(f"kill -ABRT {probe}")
          app.wait_until_succeeds("systemctl show rehearsal-core-probe.service -p ActiveState --value | grep -qvx active", timeout=30)
          ok(app.succeed("find / -xdev -newer /run/terrace-core-marker -type f \\( -name core -o -name 'core.*' \\) 2>/dev/null | wc -l").strip() == "0", "kernel core pattern: still no core file")
          ok(app.succeed("journalctl --namespace='*' -o export --no-pager 2>/dev/null | grep -ac '^SYSLOG_IDENTIFIER=systemd-coredump$' || true").strip() == "0", "kernel core pattern: still no coredump entry")
          app.succeed("systemctl reset-failed rehearsal-core-probe.service || true")

      with subtest("owners, modes and ramfs; values absent from store, units, journals, arguments and disk"):
          verify(app)
          modes(app)
          timed("scanOriginal", lambda: scan(app, "original"))

      with subtest("production relay role: key staged, secrets pushed, adopted, Headscale and HTTPS up, backup"):
          op("terrace-secrets host create relay --role relay && terrace-secrets generate relay")
          op("sh -ec 'umask 077; d=$(mktemp -d /dev/shm/tls.XXXXXX); "
             "openssl req -new -nodes -newkey rsa:2048 -sha256 -keyout $d/key.pem -out $d/req.pem -subj /CN=relay.terrace.invalid 2>/dev/null; "
             "openssl x509 -req -in $d/req.pem -CA ${ca}/ca.pem -CAkey ${ca}/ca-key.pem -set_serial 0x$(openssl rand -hex 8) -days 30 -sha256 -extfile ${relayExtension} -out $d/cert.pem 2>/dev/null; "
             "terrace-secrets set relay tls/key <$d/key.pem; terrace-secrets set relay tls/certificate <$d/cert.pem; rm -rf $d'")
          ok("relay: 4 secrets encrypted to exactly" in op("terrace-secrets check relay"), "relay store complete")
          relay_public = op("terrace-secrets public relay backup/primary/sftp-key").strip()
          operator.succeed("install -d -o restic -g users -m 0700 /home/restic/relay-repository && echo " + shlex.quote(relay_public) + " >>/home/restic/.ssh/authorized_keys")
          stage("relay")
          relay.start()
          relay.wait_for_unit("multi-user.target")
          unstage("relay")
          relay.fail("terrace-secrets verify")
          push("relay", host="relay", count=4)
          verify(relay, "4 secrets, 0 templates")
          for unit in ["headscale", "nginx"]:
              ok(relay.succeed(f"systemctl show {unit}.service -p ActiveState --value").strip() == "inactive", f"relay {unit} off before adoption")
          # An unreachable repository refuses adoption; only the named first-deployment override passes it.
          block = "${pkgs.iptables}/bin/iptables -I OUTPUT -p tcp --dport 22 -j REJECT && ${pkgs.iptables}/bin/ip6tables -I OUTPUT -p tcp --dport 22 -j REJECT"
          unblock = "${pkgs.iptables}/bin/iptables -D OUTPUT -p tcp --dport 22 -j REJECT && ${pkgs.iptables}/bin/ip6tables -D OUTPUT -p tcp --dport 22 -j REJECT"
          relay.succeed(block)
          ok("backup repository primary unreachable" in relay.fail("terrace-production adopt --new 2>&1"), "relay: unreachable repository refuses adopt --new")
          ok("backup repository primary unreachable" in relay.fail("terrace-production adopt --new --first-deployment-without-repository app 2>&1"), "relay: override naming another host refused")
          out = relay.succeed("terrace-production adopt --new --first-deployment-without-repository relay 2>&1")
          ok("WARNING: adopting as new without seeing backup repository primary" in out and "Adopted" in out, "relay: named first-deployment override accepted with a warning")
          relay.succeed(unblock)
          relay.wait_for_unit("headscale.service")
          relay.wait_for_unit("nginx.service")
          relay.succeed("headscale users create rehearsal-operator >/dev/null && headscale users list -o json | ${pkgs.jq}/bin/jq -e 'length == 1' >/dev/null")
          address = operator.succeed("getent hosts relay | cut -d' ' -f1").strip()
          ok(operator.succeed(f"curl -s -o /dev/null -w '%{{http_code}}' --resolve relay.terrace.invalid:443:{address} https://relay.terrace.invalid/health").strip() == "200", "relay HTTPS with the delivered certificate")
          ok(operator.succeed(f"curl -s -o /dev/null -w '%{{http_code}}' --resolve relay.terrace.invalid:80:{address} http://relay.terrace.invalid/").strip() == "301", "relay port 80 only redirects")
          relay_listeners = sorted({line for line in relay.succeed("ss -H -ltnu | awk '{print $1\" \"$5}'").split("\n") if line and not re.search(r" (127\.0\.0\.1|\[::1\])(%[a-z0-9]+)?:[0-9]+$", line)})
          relay_tcp = sorted(line for line in relay_listeners if line.startswith("tcp "))
          ok(relay_tcp == sorted(["tcp 0.0.0.0:22", "tcp 0.0.0.0:443", "tcp 0.0.0.0:80", "tcp [::]:22", "tcp [::]:443", "tcp [::]:80"]), f"relay public TCP listeners {relay_tcp}")
          ok(any(line.endswith(":3478") for line in relay_listeners if line.startswith("udp ")), "relay STUN listener")
          relay.succeed("terrace-backup init primary >/dev/null 2>&1 && systemctl start terrace-backup.service", timeout=300)
          ok(relay.succeed("systemctl show terrace-backup.service -p Result --value").strip() == "success", "relay backup")
          relay.shutdown()

      with subtest("back up, destroy the host, re-key from escrow for a new host key"):
          app.succeed("systemctl stop terrace-cms-sweep.timer terrace-cms-sweep.service terrace-web.service terrace-cms.service keycloak.service nginx.service", timeout=120)
          before = signature()
          app.succeed("systemctl start terrace-backup.service", timeout=600)
          snapshots = sorted((s for s in json.loads(app.succeed("terrace-backup restic primary snapshots --json 2>/dev/null")) if s["hostname"] == "app"), key=lambda s: s["time"])
          ok(len(snapshots) == 2, "first and quiesced snapshots")
          snapshot = snapshots[-1]["id"]
          app.succeed("systemctl start terrace-backup-check.service", timeout=600)
          ok(unit_state("terrace-backup-check.service", "Result") == "success", "repository check")
          operator.fail("jq . /home/restic/repository/config >/dev/null 2>&1")
          app.succeed("systemctl stop postgresql.target postgresql.service")
          app.shutdown()
          disks = list(app.state_dir.glob("*.qcow2"))
          ok(len(disks) == 1, "one original disk")
          disks[0].unlink()
          ok(not disks[0].exists(), "original disk destroyed")
          plain = op("sops decrypt --output-type json " + STORE + "/hosts/app/secrets.sops.json | jq -S -c . | sha256sum").strip()
          old_recipient = operator.succeed("cat " + STORE + "/hosts/app/recipient").strip()
          out = op("terrace-secrets host create app --role app --replace")
          ok("Run `rekey` before pushing." in out, "replacement key pending re-key")
          operator.fail(ENV + "terrace-secrets push app" + PUSH)
          op("terrace-secrets rekey app")
          op("terrace-secrets check app --rehearsal")
          new_recipient = operator.succeed("cat " + STORE + "/hosts/app/recipient").strip()
          recipients = sorted(json.loads(operator.succeed("jq -c '[.sops.age[].recipient]' " + STORE + "/hosts/app/secrets.sops.json")))
          ok(new_recipient != old_recipient and recipients == sorted([new_recipient, "${escrowRecipient}"]), "re-keyed to the new host and the escrow only")
          ok(op("sops decrypt --output-type json " + STORE + "/hosts/app/secrets.sops.json | jq -S -c . | sha256sum").strip() == plain, "same values after re-key")
          # Decrypt as each host would: sops-nix converts the ed25519 host key to an
          # age identity (sops' own SSH option only matches ssh-ed25519 recipients).
          revoked = ("umask 077; ssh-to-age -private-key -i {} -o /dev/shm/host.age && "
                     "HOME=/var/empty env -u SOPS_AGE_KEY_FILE SOPS_AGE_KEY_FILE=/dev/shm/host.age sops decrypt --output-type json " + STORE + "/hosts/app/secrets.sops.json >/dev/null 2>&1; "
                     "rc=$?; rm -f /dev/shm/host.age; exit $rc")
          operator.fail(revoked.format("/dev/shm/retired-app-key"))
          operator.succeed(revoked.format(STAGE + "/ssh_host_ed25519_key"))
          ok(True, "retired host key cannot decrypt; the new one can")
          operator.succeed("rm -f /dev/shm/retired-app-key /dev/shm/retired-app-key.pub")
          stage()

      with subtest("replacement: new key, push, guards, restore, adopt --restored, same data, lifecycle continues"):
          first_boot_without_secrets("replacement")
          push("replacement")
          verify(app)
          writers_inactive(app)
          # A1(a): the push said this host is a replacement, so it cannot be adopted as new.
          app.succeed("test -e /var/lib/terrace-secrets/replacement")
          ok("delivered as a replacement" in app.fail("terrace-production adopt --new 2>&1"), "adopt --new refused: replacement marker")
          # A1(b), independently: without the marker the repository still says this host has backups.
          app.succeed("mv /var/lib/terrace-secrets/replacement /run/replacement-held")
          ok("already holds snapshots; this is a replacement" in app.fail("terrace-production adopt --new 2>&1"), "adopt --new refused: snapshots exist")
          app.succeed("mv /run/replacement-held /var/lib/terrace-secrets/replacement")
          app.fail("test -e /var/lib/postgresql/18/PG_VERSION")
          ok(True, "the refused adoptions initialised nothing")
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
          refused = app.fail("terrace-production adopt --new 2>&1")
          ok("delivered as a replacement" in refused, "adopt --new refused on a restored host (replacement marker)")
          app.succeed("terrace-production adopt --restored")
          app.fail("test -e /var/lib/terrace-secrets/replacement")
          # The next push learns the host is adopted and clears the operator's replacement marker.
          ok("replacement marker is cleared" in op("terrace-secrets push app" + PUSH), "push after adoption clears the store's replacement marker")
          operator.fail("test -e " + STORE + "/hosts/app/replacement")
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
          canary = "terrace-scan-canary-" + os.urandom(24).hex()
          operator.succeed(f"echo {canary} >/dev/shm/scan/canary && echo {canary} >/var/tmp/terrace-scan-canary && sync")
          ok(files_with_values(operator, "find / -xdev -path /nix -prune -o -type f -print0", "/dev/shm/scan/canary") != "0" and
             operator.succeed("grep -acF -f /dev/shm/scan/canary /dev/vda || true", timeout=600).strip() != "0", "operator: canary found by both scans")
          operator.succeed("rm -f /var/tmp/terrace-scan-canary && sync")
          ok(files_with_values(operator, "find / -xdev -path /nix -prune -o -type f -print0", "/dev/shm/scan/all") == "0", "operator: no value in a file")
          ok(operator.succeed("grep -acF -f /dev/shm/scan/all /dev/vda || true", timeout=600).strip() == "0", "operator: no value on the raw disk")
          ok(operator.succeed("journalctl --namespace='*' -o export --no-pager 2>/dev/null | grep -acF -f /dev/shm/scan/all || true").strip() == "0", "operator: no value in the journal")
          operator.succeed("rm -rf /dev/shm/scan")

      with subtest("stop owned guests"):
          app.shutdown()
          operator.shutdown()
      # Labels carry counts and names only, never values.
      print("PRODUCTION-CHECKS " + json.dumps(checks))
      print("PRODUCTION-PROOF " + json.dumps({"assertions": len(checks), "protocolAssertions": assertions, "seconds": round(time.time() - started_at, 2), "timings": timings}))
    '';
}
