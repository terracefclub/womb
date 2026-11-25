# VM proof of the opt-in deploy path (docs/host-updates.md, "Deploy path"): the production app role at the
# PREVIOUS website release (680ad56) is adopted and populated; an operator guest standing in for the build
# machine signs and copies a second system (the pinned release, one more CMS migration) as `operator`; the host
# refuses what it must; activation survives a dropped SSH session; a generation rollback before the migration
# brings the old site back; the upgrade migration runs once on the populated data; a rollback after it leaves the
# old CMS refusing to start. Synthetic `.invalid` names; every key (SSH, escrow, deploy signing) is made at build
# time by this file and never committed.
{
  pkgs,
  release,
  previous,
  contract,
  sopsModule,
}: let
  lib = pkgs.lib;
  names = ["web.terrace.invalid" "cms.terrace.invalid" "identity.terrace.invalid"];
  tool = pkgs.callPackage ./package.nix {};
  ca = pkgs.runCommand "terrace-deploy-proof-ca" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir $out
    openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 3650 \
      -keyout $out/ca-key.pem -out $out/ca.pem -subj /CN=TerraceDeployRehearsalCA \
      -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign >/dev/null 2>&1
  '';
  keys = pkgs.runCommand "terrace-deploy-proof-ssh-keys" {nativeBuildInputs = [pkgs.openssh];} ''
    mkdir $out
    ssh-keygen -q -t ed25519 -N "" -C rehearsal-operator -f $out/operator
  '';
  publicKey = file: lib.removeSuffix "\n" (builtins.readFile "${keys}/${file}.pub");
  escrow = pkgs.runCommand "terrace-deploy-proof-escrow" {nativeBuildInputs = [pkgs.age];} ''
    mkdir $out
    age-keygen -o $out/escrow.age 2>/dev/null
    age-keygen -y $out/escrow.age >$out/escrow.pub
  '';
  escrowRecipient = lib.removeSuffix "\n" (builtins.readFile "${escrow}/escrow.pub");
  # Three Nix signing keys: the trusted deploy key, a foreign one the host does not trust, and one the host trusts
  # through plain Nix settings but not as a deploy key (as it trusts the nixpkgs cache key).
  signing = pkgs.runCommand "terrace-deploy-proof-signing-keys" {nativeBuildInputs = [pkgs.nix];} ''
    mkdir $out
    export HOME=$TMPDIR NIX_STATE_DIR=$TMPDIR/state NIX_CONF_DIR=$TMPDIR/conf
    for name in deploy foreign cachelike; do
      nix --extra-experimental-features nix-command key generate-secret --key-name terrace-rehearsal-$name-1 >$out/$name.sec
      nix --extra-experimental-features nix-command key convert-secret-to-public <$out/$name.sec >$out/$name.pub
    done
  '';
  deployPublic = lib.removeSuffix "\n" (builtins.readFile "${signing}/deploy.pub");
  cachelikePublic = lib.removeSuffix "\n" (builtins.readFile "${signing}/cachelike.pub");
  users = pkgs.runCommand "terrace-deploy-proof-users" {} ''
    mkdir $out
    cp -r ${contract}/. $out/
    cp ${./rehearsal-users.ts} $out/rehearsal-users.ts
  '';
  extension = pkgs.writeText "terrace-deploy-leaf.ext" ''
    basicConstraints=critical,CA:FALSE
    keyUsage=critical,digitalSignature,keyEncipherment
    extendedKeyUsage=serverAuth
    subjectAltName=${lib.concatMapStringsSep "," (name: "DNS:${name}") names}
  '';
  deploy = pkgs.writeShellApplication {
    name = "terrace-deploy";
    runtimeInputs = [pkgs.nix pkgs.openssh pkgs.coreutils pkgs.gnugrep];
    text = builtins.readFile ./terrace-deploy.sh;
  };
  revisions = {
    old = lib.concatStrings (lib.replicate 40 "a");
    next = lib.concatStrings (lib.replicate 40 "b");
  };
  # The second system revision: the pinned release (its migration list), a new Womb revision, and a probe that
  # holds the activation for 20 seconds so the test can drop the SSH session in the middle of it.
  nextModule = {
    terrace.website = {
      webPackage = lib.mkForce release.web;
      cmsPackage = lib.mkForce release.cms;
      revision = lib.mkForce release.revision;
    };
    terrace.production.app.releaseMigrations = lib.mkForce release.migrations;
    terrace.production.wombRevision = lib.mkForce revisions.next;
    system.activationScripts.terraceDeployProbe.text = ''
      touch /run/terrace-deploy-probe-started
      sleep 20
      touch /run/terrace-deploy-probe-done
    '';
  };
in {
  name = "terrace-production-deploy";
  nodes = {
    # The build machine and operator workstation: holds the signing keys, signs and copies.
    operator = {nodes, ...}: {
      virtualisation = {
        memorySize = 3072;
        cores = 2;
        diskSize = 4096;
        # Registered in this guest's store, so it can sign and copy them like a builder its own outputs.
        additionalPaths = with nodes.app.system.build; [nodes.app.system.build.toplevel deployNext deployOtherHost deployUnsigned deployForeign deployCachelike deployBoot deployBroken nodes.relay.system.build.toplevel];
      };
      environment.systemPackages = [tool deploy pkgs.sops pkgs.age pkgs.ssh-to-age pkgs.openssl pkgs.bun pkgs.jq];
    };
    app = {extendModules, ...}: let
      variant = modules: (extendModules {inherit modules;}).config.system.build.toplevel;
      etcVariant = text: variant [{environment.etc."terrace-deploy-variant".text = text;}];
    in {
      imports = [sopsModule ./app.nix (import ./rehearsal.nix {inherit ca;})];
      virtualisation = {
        memorySize = 9216;
        cores = 2;
        diskSize = 8192;
        # Copied closures land on the guest's disk, not in its memory.
        writableStoreUseTmpfs = false;
      };
      # The test framework turns switching off for its nodes; an installed production host keeps NixOS's default (on).
      system.switch.enable = true;
      # The test VM's disk cannot take GRUB (`switch` installs the boot loader first and would fail there): write the
      # menu only. Priority 5 beats qemu-vm.nix's mkVMOverride (10). A real host's hardware module declares its own boot loader.
      boot.loader.grub.device = lib.mkOverride 5 "nodev";
      terrace.production = {
        instance = "rehearsal";
        memoryMiB = 9216;
        inherit escrowRecipient;
        operatorKeys = [(publicKey "operator")];
        deploy.trustedKeys = [deployPublic];
        wombRevision = revisions.old;
        app = {
          webHostname = "web.terrace.invalid";
          cmsHostname = "cms.terrace.invalid";
          identityHostname = "identity.terrace.invalid";
          identityContract = contract;
          releaseMigrations = previous.migrations;
          backup.primary = {
            repository = "sftp:restic@operator:repository";
            knownHosts = "operator ${publicKey "operator"}";
          };
        };
      };
      # A key Nix trusts on this host that is NOT a deploy key: activate must not accept its signatures.
      nix.settings.extra-trusted-public-keys = [cachelikePublic];
      terrace.website = {
        webPackage = previous.web;
        cmsPackage = previous.cms;
        inherit (previous) revision;
      };
      system.build = {
        deployNext = variant [nextModule];
        deployOtherHost = variant [{networking.hostName = lib.mkForce "app-other";}];
        deployUnsigned = etcVariant "unsigned";
        deployForeign = etcVariant "foreign";
        deployCachelike = etcVariant "cachelike";
        # The next release once more, activated with --boot and cancelled before any reboot.
        deployBoot = variant [nextModule {environment.etc."terrace-deploy-variant".text = "boot";}];
        # The previous release plus a unit that fails and restarts: a gated switch that ALSO breaks something else.
        deployBroken = variant [
          {
            systemd.services.terrace-deploy-probe-broken = {
              wantedBy = ["multi-user.target"];
              serviceConfig = {
                ExecStart = "${pkgs.coreutils}/bin/false";
                Restart = "on-failure";
                RestartSec = 2;
              };
            };
          }
        ];
      };
    };
    # Built for its toplevel only (a closure for the other role); never started.
    relay = {...}: {
      imports = [sopsModule ./relay.nix ./rehearsal-relay.nix];
      system.switch.enable = true;
      boot.loader.grub.device = lib.mkOverride 5 "nodev";
      terrace.production = {
        instance = "rehearsal";
        memoryMiB = 2048;
        inherit escrowRecipient;
        operatorKeys = [(publicKey "operator")];
        deploy.trustedKeys = [deployPublic];
        relay = {
          hostname = "relay.terrace.invalid";
          publicIPv4 = "192.0.2.10";
          backup.primary = {
            repository = "sftp:restic@operator:relay-repository";
            knownHosts = "operator ${publicKey "operator"}";
          };
        };
      };
    };
  };
  testScript = {nodes, ...}: let
    base = nodes.app.system.build.toplevel;
    inherit (nodes.app.system.build) deployNext deployOtherHost deployUnsigned deployForeign deployCachelike deployBoot deployBroken;
    relaySystem = nodes.relay.system.build.toplevel;
  in ''
    import json, shlex, time

    checks = []
    def ok(condition, label):
        assert condition, label
        checks.append(label)

    STORE = "/root/store"
    ESCROW = "/root/escrow/escrow.age"
    ENV = f"export TERRACE_SECRETS_STORE={STORE} SOPS_AGE_KEY_FILE={ESCROW} TERRACE_SECRETS_SCRATCH=/dev/shm TERRACE_ESCROW_RECIPIENT=${escrowRecipient}; "
    STAGE = "/dev/shm/terrace-stage-app"
    MEDIA = "/var/lib/terrace-cms-media"
    MARKER = "/var/lib/terrace-cms/migrated-revision"
    SSH = "ssh -i /root/.ssh/operator -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/app_known_hosts operator@app "
    NIX = "nix --extra-experimental-features nix-command "
    BASE, NEXT, OTHER, UNSIGNED, FOREIGN, RELAY = "${base}", "${deployNext}", "${deployOtherHost}", "${deployUnsigned}", "${deployForeign}", "${relaySystem}"
    CACHELIKE, BOOT, BROKEN = "${deployCachelike}", "${deployBoot}", "${deployBroken}"
    DEPLOY = "terrace-deploy --key /root/signing/deploy.sec --address app --identity /root/.ssh/operator --known-hosts /root/app_known_hosts "
    OLD_PIN, NEW_PIN = "${previous.revision}", "${release.revision}"
    OLD_MIGRATIONS, NEW_MIGRATIONS = ${builtins.toJSON previous.migrations}, ${builtins.toJSON release.migrations}
    GATED = ["terrace-cms.service", "terrace-web.service"]

    def failed_units():
        return app.succeed("systemctl list-units --state=failed --type=service --plain --no-legend | cut -d' ' -f1").split()

    def gated_record(record, label):
        # The two legitimate outcomes when the switch returns: the website already failed, or still retrying.
        outcomes = [(GATED, []), (["terrace-cms.service"], ["terrace-web.service"])]
        ok((record["failedUnits"], record["startingUnits"]) in outcomes and record["result"] == "switched; down until the CMS migration: " + " ".join(record["failedUnits"]),
           f"{label}: switched with only the gated CMS down and the website failed or still starting: {record}")
    BACKUP_HELD = ["terrace-backup.timer", "terrace-backup-check.timer", "terrace-backup-freshness.timer"]
    SERVICE_HELD = ["terrace-cms-sweep.timer"]
    timings = {}

    def op(command, **kwargs):
        return operator.succeed(ENV + command, **kwargs)

    def sql(query, database="terrace_cms"):
        return app.succeed("runuser -u postgres -- psql -XAt -d " + database + " -c " + shlex.quote(query) + " 2>/dev/null").strip()

    def unit_state(unit, prop):
        return app.succeed(f"systemctl show {unit} -p {prop} --value").strip()

    def running():
        return app.succeed("readlink -f /run/current-system").strip()

    def generation():
        return app.succeed("readlink /nix/var/nix/profiles/system").strip()

    def records():
        return [json.loads(line) for line in app.succeed("cat /var/lib/terrace-production/deployments.log 2>/dev/null || true").splitlines() if line]

    def ledger():
        return sql("select name from payload_migrations order by id").splitlines()

    CONTENT = ["posts", "_posts_v", "redirects", "media", "users"]

    def tables():
        return sql("select tablename from pg_tables where schemaname = 'public' order by 1").splitlines()

    def data():
        # The tables the previous release's data was written into, row by row, plus the media files: what the upgrade must keep.
        rows = {t: sql(f"select count(*) || ':' || md5(coalesce(string_agg(x::text, chr(10) order by x::text), ''')) from \"{t}\" x") for t in CONTENT}
        rows["media files"] = app.succeed("cd " + MEDIA + " && find . -type f -print0 | LC_ALL=C sort -z | xargs -0r sha256sum").strip()
        return rows

    def cms_serves(label):
        body = json.loads(app.succeed("curl -sf --max-time 20 'http://127.0.0.1:13048/api/posts?limit=1'"))
        ok(body.get("totalDocs", 0) > 0, label + ": the CMS serves the synthetic post")
        ok(app.execute("curl -sf --max-time 20 -o /dev/null http://127.0.0.1:14294/events/")[0] == 0, label + ": the website answers")

    def refused(command, needle, label, machine=None):
        machine = machine or app
        before = (running(), generation(), len(records()))
        out = machine.fail(command + " 2>&1")
        ok(needle in out, f"refused: {label} ({needle!r} in {out.strip()[-300:]!r})")
        ok((running(), generation(), len(records())) == before, f"refused: {label}: nothing changed")
        return out

    def quiesce():
        # Step 6's first two lines (docs/host-updates.md); the backup itself is not part of this proof.
        app.succeed("systemctl stop terrace-backup.timer terrace-backup-check.timer terrace-cms-sweep.timer")
        app.succeed("systemctl stop terrace-cms-sweep.service terrace-web.service terrace-cms.service")

    def restart_after_quiesce():
        app.succeed("systemctl reset-failed terrace-cms.service terrace-web.service")
        app.succeed("systemctl start terrace-cms.service terrace-web.service", timeout=300)

    def held(label):
        for unit in BACKUP_HELD + SERVICE_HELD:
            ok((unit_state(unit, "ActiveState"), unit_state(unit, "ConditionResult")) == ("inactive", "no"), f"{label}: {unit} held (inactive, start condition not met)")
        status = app.succeed("terrace-production status")
        ok("timers held after a restored adoption" in status and "timers held until the replacement is declared in service" in status, f"{label}: status names the held timers")

    started_at = time.time()
    operator.start()
    operator.wait_for_unit("multi-user.target")
    operator.succeed("install -d -m 0700 /root/escrow /root/.ssh /root/signing " + STORE + " && install -m 0600 ${keys}/operator /root/.ssh/operator")
    operator.succeed("install -m 0600 ${escrow}/escrow.age " + ESCROW + " && install -m 0644 ${escrow}/escrow.pub " + STORE + "/escrow.recipient")
    operator.succeed("install -m 0600 ${signing}/deploy.sec ${signing}/foreign.sec ${signing}/cachelike.sec /root/signing/")

    with subtest("operator creates the app host key and secrets"):
        op("terrace-secrets host create app --role app")
        op("terrace-secrets generate app")
        op("sh -ec 'umask 077; d=$(mktemp -d /dev/shm/tls.XXXXXX); "
           "openssl req -new -nodes -newkey rsa:2048 -sha256 -keyout $d/key.pem -out $d/req.pem -subj /CN=web.terrace.invalid 2>/dev/null; "
           "openssl x509 -req -in $d/req.pem -CA ${ca}/ca.pem -CAkey ${ca}/ca-key.pem -set_serial 0x$(openssl rand -hex 8) -days 30 -sha256 -extfile ${extension} -out $d/cert.pem 2>/dev/null; "
           "terrace-secrets set app tls/key <$d/key.pem; terrace-secrets set app tls/certificate <$d/cert.pem; rm -rf $d'")
        op("bun --no-env-file ${users}/rehearsal-users.ts | terrace-secrets set app rehearsal/users-json --rehearsal")
        ok("app: 13 secrets encrypted to exactly the host key and the pinned escrow key." in op("terrace-secrets check app --rehearsal"), "store complete")
        # The host key the operator made is the one the copies and the activation commands pin.
        operator.succeed("echo \"app $(cat " + STAGE + "/ssh_host_ed25519_key.pub)\" >/root/app_known_hosts")
        operator.succeed("rm -rf /tmp/shared/stage/app && install -d -m 0700 /tmp/shared/stage/app && cp " + STAGE + "/ssh_host_ed25519_key " + STAGE + "/ssh_host_ed25519_key.pub /tmp/shared/stage/app/")

    with subtest("the previous release: adopted, migrated, populated"):
        app.start()
        app.wait_for_unit("multi-user.target")
        operator.succeed("rm -rf /tmp/shared/stage/app")
        op("terrace-secrets host unstage app")
        out = op("terrace-secrets push app --address app --identity /root/.ssh/operator --rehearsal")
        ok("Installed 13 encrypted secrets." in out, "secrets pushed")
        # A NixOS install registers the installed system as generation 1; the test VM boots its toplevel directly.
        installed = app.succeed("test -L /nix/var/nix/profiles/system && echo yes || echo no").strip()
        if installed == "no":
            app.succeed("nix-env -p /nix/var/nix/profiles/system --set $(readlink -f /run/current-system)")
        ok(generation() == "system-1-link" and running() == BASE, f"generation 1 is the installed system (profile existed before: {installed})")
        out = app.succeed("terrace-production adopt --new --first-deployment-without-repository app 2>&1")
        ok("Adopted" in out, "adopted as new")
        app.wait_for_unit("keycloak.service", timeout=360)
        app.succeed("systemctl start terrace-cms-migrate.service", timeout=300)
        ok(app.succeed("cat " + MARKER).strip() == OLD_PIN, "previous release migrated: marker")
        ok(ledger() == OLD_MIGRATIONS, f"previous release ledger: {len(OLD_MIGRATIONS)} migrations")
        app.succeed("systemctl start terrace-cms.service terrace-web.service", timeout=300)
        app.succeed("install -m 0600 /dev/null " + MEDIA + "/private.png && echo 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGPgUbL4DwACCgFmeGgpMwAAAABJRU5ErkJggg==' | base64 -d >" + MEDIA + "/private.png && cp " + MEDIA + "/private.png " + MEDIA + "/public.png && chown terrace_cms:terrace_cms " + MEDIA + "/*.png")
        sql("insert into media(alt,public,filename,mime_type,filesize,width,height) values ('Synthetic private',false,'private.png','image/png',70,1,1),('Synthetic public',true,'public.png','image/png',70,1,1)")
        app.succeed("runuser -u postgres -- psql -XAt -v ON_ERROR_STOP=1 -d terrace_cms -f /etc/gateway-content.sql >/dev/null 2>&1")
        sql("insert into users(display_name,role,email) values ('Synthetic editor','admin','editor@terrace.invalid')")
        cms_serves("previous release")
        before_upgrade = data()
        tables_before = tables()
        ok(all(not value.startswith("0:") for key, value in before_upgrade.items() if key != "media files"), f"populated: {sorted(before_upgrade)}")
        ok(json.loads(app.succeed("cat /etc/terrace/system.json"))["website"] == {"revision": OLD_PIN, "migrations": OLD_MIGRATIONS}, "running manifest names the previous release")

    with subtest("Nix on the host: only root trusted, signatures required, the deploy key trusted"):
        ok(app.succeed(NIX + "config show trusted-users").strip() == "root", "trusted-users is root only")
        ok(app.succeed(NIX + "config show require-sigs").strip() == "true", "signatures required")
        keys = app.succeed(NIX + "config show trusted-public-keys").split()
        ok("${deployPublic}" in keys and not any(k.startswith("terrace-rehearsal-foreign-1:") for k in keys), "the deploy key is trusted, the foreign key is not")
        ok("${cachelikePublic}" in keys, "a second key is trusted through plain Nix settings (not a deploy key)")
        ok("deploy path: on, 1 trusted deploy key(s)" in app.succeed("terrace-production status"), "status: deploy path on")

    with subtest("the host refuses unsigned and foreign-signed copies from operator"):
        copy = "NIX_SSHOPTS='-i /root/.ssh/operator -o IdentitiesOnly=yes -o UserKnownHostsFile=/root/app_known_hosts' " + NIX + "copy --to ssh-ng://operator@app "
        out = operator.fail(copy + UNSIGNED + " 2>&1")
        ok("lacks a signature by a trusted key" in out, "unsigned closure refused by the host's Nix daemon")
        app.fail("nix-store --check-validity " + UNSIGNED)
        operator.succeed(NIX + "store sign --key-file /root/signing/foreign.sec --recursive " + FOREIGN)
        out = operator.fail(copy + FOREIGN + " 2>&1")
        ok("lacks a signature by a trusted key" in out, "closure signed by an untrusted key refused by the host's Nix daemon")
        ok(all(app.execute("nix-store --check-validity " + path)[0] != 0 for path in [UNSIGNED, FOREIGN]), "neither refused closure is in the host's store")

    with subtest("signed copies of systems for another host and the other role pass the copy, not the activation"):
        for path in [OTHER, RELAY]:
            out = op(DEPLOY + path)
            ok(out.strip().endswith("ssh -t operator@app sudo terrace-production activate " + path), "helper prints the activation command")
            ok(app.execute("nix-store --check-validity " + path)[0] == 0, "signed copy accepted as operator (control for the refused copies)")
        sigs = app.succeed(NIX + "path-info --sigs " + OTHER)
        ok("terrace-rehearsal-deploy-1:" in sigs, "the copied toplevel carries the deploy signature on the host")
        refused("terrace-production activate " + OTHER, "this host is '1 app app rehearsal'", "a system built for another host name")
        refused("terrace-production activate " + RELAY, "was built for (schema role host instance) '1 relay relay rehearsal'", "a system built for the relay role")

    with subtest("activation refuses closures put in the store by root without a trusted signature"):
        # Root on the host can add paths without signatures; the command still asks for the deploy key's.
        operator.succeed("nix-store -qR " + BASE + " >/tmp/base.paths")
        for name, path in [("unsigned", UNSIGNED), ("foreign", FOREIGN)]:
            # Only the paths the host lacks, in the closure's (topological) order.
            operator.succeed(f"nix-store -qR {path} | grep -vxF -f /tmp/base.paths >/tmp/{name}.paths && nix-store --export $(cat /tmp/{name}.paths) >/tmp/shared/{name}.export")
            app.succeed(f"nix-store --import </tmp/shared/{name}.export >/dev/null && rm /tmp/shared/{name}.export")
            app.succeed("nix-store --check-validity " + path)
        # The export format carries no signatures: root signs the foreign closure's new paths with the foreign key, on the host.
        operator.succeed("cp /root/signing/foreign.sec /tmp/shared/foreign.sec && cp /tmp/foreign.paths /tmp/shared/foreign.paths")
        app.succeed(NIX + "store sign --key-file /tmp/shared/foreign.sec $(cat /tmp/shared/foreign.paths) && rm /tmp/shared/foreign.sec /tmp/shared/foreign.paths")
        ok("terrace-rehearsal-foreign-1:" in app.succeed(NIX + "path-info --sigs " + FOREIGN), "foreign closure present, signed by the untrusted key only")
        refused("terrace-production activate " + UNSIGNED, "not all carry a valid signature by a trusted deploy key", "an unsigned closure")
        refused("terrace-production activate " + FOREIGN, "not all carry a valid signature by a trusted deploy key", "a closure signed by an untrusted key")

    with subtest("a key Nix trusts but that is not a deploy key does not count"):
        operator.succeed(NIX + "store sign --key-file /root/signing/cachelike.sec --recursive " + CACHELIKE)
        operator.succeed(copy + CACHELIKE)
        ok(app.execute("nix-store --check-validity " + CACHELIKE)[0] == 0, "the host's Nix daemon accepts a copy signed by the non-deploy key it trusts")
        refused("terrace-production activate " + CACHELIKE, "not all carry a valid signature by a trusted deploy key", "a closure signed only by a non-deploy trusted key")
        # Control: the same closure's new paths also signed with the deploy key pass every check (dry run: nothing changes).
        operator.succeed("nix-store -qR " + CACHELIKE + " | grep -vxF -f /tmp/base.paths >/tmp/shared/cachelike.paths && cp /root/signing/deploy.sec /tmp/shared/deploy.sec")
        app.succeed(NIX + "store sign --key-file /tmp/shared/deploy.sec $(cat /tmp/shared/cachelike.paths) && rm /tmp/shared/deploy.sec /tmp/shared/cachelike.paths")
        before = (running(), generation(), len(records()))
        out = app.succeed("terrace-production activate " + CACHELIKE + " --dry-run")
        ok("Signatures: " in out and "Dry run: nothing changed." in out and (running(), generation(), len(records())) == before, "control: with the deploy key's signature added, the checks pass (dry run, nothing changed)")

    with subtest("refused: the running system, a non-root caller, a switch in progress, a release change with the CMS up"):
        out = op(DEPLOY + NEXT)
        ok("Signed the closure of " + NEXT in out and "Copied to operator@app." in out, "the next system signed and copied as operator")
        refused("terrace-production activate " + BASE, "is already the running system", "the running system")
        refused(SSH + "terrace-production activate " + NEXT, "terrace-production: run as root", "operator without sudo", machine=operator)
        refused("runuser -u nobody -- terrace-production activate " + NEXT, "terrace-production: run as root", "a non-root caller")
        app.succeed("systemd-run --collect --unit=terrace-switch ${pkgs.coreutils}/bin/sleep 600")
        refused("terrace-production activate " + NEXT, "terrace-switch.service is active; wait until it has finished", "another terrace-switch running")
        app.succeed("systemctl stop terrace-switch.service")
        refused("terrace-production activate " + NEXT, "the release changes and terrace-cms.service is active; quiesce first", "a release change with the CMS running")

    with subtest("upgrade: activation of the signed system survives a dropped SSH session; held timers stay held"):
        quiesce()
        # The markers `adopt --restored` writes, placed by hand on this adopted host: a restored host's hold.
        app.succeed("install -m 0600 /dev/null /var/lib/terrace-production/timers-held && install -m 0600 /dev/null /var/lib/terrace-production/service-timers-held")
        app.succeed("systemctl stop terrace-backup-freshness.timer")
        # A start of a held timer (as the target's at boot) is skipped while its marker exists.
        app.succeed("systemctl start " + " ".join(BACKUP_HELD + SERVICE_HELD))
        held("before the update")
        # `-tt`: a terminal, as `ssh -t` gives an operator, so a dropped connection hangs up the remote side.
        operator.succeed("(" + SSH.replace("ssh ", "ssh -tt ", 1) + "sudo terrace-production activate " + NEXT + " >/tmp/activate.log 2>&1 </dev/null; echo $? >/tmp/activate.rc) >/dev/null 2>&1 </dev/null & echo $! >/tmp/activate.pid")
        app.wait_until_succeeds("test -e /run/terrace-deploy-probe-started", timeout=180)
        ok(app.succeed("systemctl is-active terrace-switch.service").strip() == "active", "the switch runs in the terrace-switch unit")
        # Drop the session: kill the SSH client mid-switch.
        operator.succeed("pkill -KILL -P $(cat /tmp/activate.pid) ssh; kill -KILL $(cat /tmp/activate.pid) 2>/dev/null || true")
        app.wait_until_succeeds("! pgrep -f '[t]errace-production activate'", timeout=60)
        ok(not app.succeed("test -e /run/terrace-deploy-probe-done && echo done || true").strip(), "the client side is gone before the switch finished")
        app.wait_until_succeeds("test -e /run/terrace-deploy-probe-done && ! systemctl is-active terrace-switch.service", timeout=300)
        ok(running() == NEXT and generation() == "system-2-link", "the new generation runs after the dropped session")
        record = records()[-1]
        # The switch starts the stopped CMS and website (the active terrace-production.target wants them): the CMS fails its gate and
        # switch-to-configuration exits 4. The website has no gate; it fails its health check against the stopped CMS and restarts
        # (Restart=on-failure), so when the switch returns it is either failed or still retrying.
        ok((record["action"], record["generationBefore"], record["generationAfter"], record["to"], record["wombRevision"], record["websiteRevision"]) ==
           ("activate", "1", "2", NEXT, "${revisions.next}", NEW_PIN), f"the unit recorded the activation: {record}")
        gated_record(record, "upgrade activation")
        # Left alone, the website gives up at its start limit: then exactly the CMS and the website are failed.
        app.wait_until_succeeds("test $(systemctl show terrace-web.service -p ActiveState --value) = failed", timeout=600)
        ok(unit_state("terrace-web.service", "Result") == "start-limit-hit", "the ungated website, started by the switch, failed at its start limit (H7)")
        ok(failed_units() == GATED, "then exactly the CMS and the website are failed")
        status = app.succeed("terrace-production status")
        ok(f"running system: {NEXT} (Womb ${revisions.next}, website {NEW_PIN})" in status and f"previous generation: 1 {BASE} (Womb ${revisions.old}, website {OLD_PIN})" in status, "status shows both generations")
        held("after the update")
        ok(app.succeed("cat " + MARKER).strip() == OLD_PIN, "the marker still holds the previous release")

    with subtest("the gate: the new CMS refuses until the migration"):
        app.succeed("journalctl --namespace=terrace-apps -u terrace-cms.service -o cat --no-pager | grep -q 'CMS migration gate is closed'")
        ok(unit_state("terrace-cms.service", "Result") == "start-limit-hit", "the switch's starts of the new CMS hit its start limit on the gate")
        app.succeed("systemctl reset-failed terrace-cms.service")
        ok(app.execute("systemctl start terrace-cms.service", timeout=120)[0] != 0, "the new CMS refuses to start (its gate)")
        app.succeed("systemctl reset-failed terrace-cms.service terrace-web.service")

    with subtest("case A: rollback before the migration brings the previous site back"):
        # After the gated activation the CMS (and the website) can still be retrying: the rollback refuses until they are stopped.
        app.succeed("systemctl start terrace-cms.service || true")
        out = app.fail("terrace-production rollback 2>&1")
        ok("quiesce first: systemctl stop terrace-cms.service terrace-web.service" in out, f"rollback refused while the CMS retries, naming the command: {out.strip()[-200:]}")
        app.succeed("systemctl stop terrace-cms.service terrace-web.service && systemctl reset-failed terrace-cms.service terrace-web.service")
        out = app.succeed("terrace-production rollback")
        ok("Running generation 1" in out, "rollback accepted before the migration")
        ok(running() == BASE and generation() == "system-1-link", "generation 1 runs again")
        ok(records()[-1]["action"] == "rollback" and records()[-1]["result"] == "success" and records()[-1]["failedUnits"] == [], "rollback recorded, no unit failed (the old CMS's gate is open)")
        restart_after_quiesce()
        cms_serves("after case A")
        ok(data() == before_upgrade and ledger() == OLD_MIGRATIONS, "case A: data and ledger unchanged")
        held("after case A")

    with subtest("the upgrade migration on the populated data runs once"):
        quiesce()
        operator.succeed(SSH + "sudo terrace-production activate " + NEXT)
        # nix-env --set reuses the newest generation when it already points to the same path (Nix's createGeneration): 2, not 3.
        ok(running() == NEXT and generation() == "system-2-link" and (records()[-1]["generationBefore"], records()[-1]["generationAfter"]) == ("1", "2"),
           f"the signed system activated again, as its existing generation 2: {records()[-1]}")
        gated_record(records()[-1], "re-activation")
        # Step 7's instruction: stop the website; then only the CMS is failed.
        app.succeed("systemctl stop terrace-web.service")
        ok(failed_units() in [["terrace-cms.service"], GATED], f"after stopping the website the CMS is failed: {failed_units()}")
        app.wait_until_succeeds("test -e /run/terrace-deploy-probe-done")
        t0 = time.monotonic()
        app.succeed("systemctl start terrace-cms-migrate.service", timeout=300)
        timings["migration"] = round(time.monotonic() - t0, 2)
        ok(app.succeed("cat " + MARKER).strip() == NEW_PIN, "marker holds the new release")
        after = ledger()
        ok(after == NEW_MIGRATIONS, f"ledger equals the new pin's list ({len(after)})")
        ok([name for name in after if name not in OLD_MIGRATIONS] == [name for name in NEW_MIGRATIONS if name not in OLD_MIGRATIONS], "only the new release's added migrations ran")
        app.succeed("systemctl start terrace-cms-migrate.service", timeout=300)
        ok(ledger() == after, "a second migration run applies nothing")
        restart_after_quiesce()
        app.succeed("systemctl start terrace-cms-sweep.timer")
        cms_serves("after the upgrade")
        migrated = data()
        changed = sorted(key for key in before_upgrade if migrated.get(key) != before_upgrade[key])
        ok(changed == [], f"every table of the previous release and the media files unchanged by the migration: changed {changed}")
        added = sorted(set(tables()) - set(tables_before))
        ok(len(added) > 0 and set(tables_before) <= set(tables()), f"the migration added tables and dropped none: {added}")

    with subtest("rollback after the migration: refused by default; forced, the old CMS refuses to start"):
        quiesce()
        out = refused("terrace-production rollback", "the CMS database has migrations the target release does not have", "a rollback over a newer schema")
        ok("--cms-stays-down" in out and "restore onto a replacement" in out, "the refusal names the flag and the way back")
        ciphertext = app.succeed("sha256sum </var/lib/terrace-secrets/secrets.sops.json").strip()
        out = app.succeed("terrace-production rollback --cms-stays-down 2>&1")
        ok("WARNING: the CMS stays down" in out and running() == BASE, "forced rollback ran generation 1 with a warning")
        gated_record(records()[-1], "forced rollback")
        app.succeed("systemctl stop terrace-web.service")
        app.succeed("systemctl reset-failed terrace-cms.service terrace-web.service")
        gate_messages = lambda: int(app.succeed("journalctl --namespace=terrace-apps -u terrace-cms.service -o cat --no-pager | grep -c 'CMS migration gate is closed' || true").strip())
        seen = gate_messages()
        ok(app.execute("systemctl start terrace-cms.service", timeout=120)[0] != 0 and gate_messages() > seen, "forward-only: the previous CMS does not start on the migrated database, refused by its gate")
        # What a generation rollback reverted (docs/host-updates.md table 4.1) ...
        ok(json.loads(app.succeed("cat /etc/terrace/system.json"))["website"]["revision"] == OLD_PIN, "reverted: the pinned revision the gate expects")
        ok("${previous.cms}" in app.succeed("systemctl cat terrace-cms.service"), "reverted: the unit definitions name the previous bundle")
        # ... and what it did not.
        ok(app.succeed("cat " + MARKER).strip() == NEW_PIN, "not reverted: the migration marker")
        ok(ledger() == NEW_MIGRATIONS, "not reverted: the database schema and its ledger")
        ok(data()["media files"] == migrated["media files"], "not reverted: media files")
        ok(app.succeed("sha256sum </var/lib/terrace-secrets/secrets.sops.json").strip() == ciphertext, "not reverted: the host ciphertext")
        ok(app.execute("test -e /var/lib/terrace-production/adopted")[0] == 0, "not reverted: the adopted marker")
        held("after the forced rollback")

    with subtest("the way forward: the new generation serves again"):
        app.succeed("systemctl stop terrace-cms.service terrace-web.service && systemctl reset-failed terrace-cms.service terrace-web.service")
        app.succeed("terrace-production activate " + NEXT)
        ok(running() == NEXT and records()[-1]["result"] == "success" and records()[-1]["failedUnits"] == [], "the migrated release's system activated again (no flag: the ledger is its own), no unit failed")
        restart_after_quiesce()
        cms_serves("after the way forward")
        ok(data() == migrated, "data unchanged across rollback and re-activation")
        out = app.succeed("terrace-production arm-timers && terrace-production arm-timers --in-service")
        ok("Every writer timer is active now and at every boot." in out, "the held timers armed once the update is done")

    with subtest("activate --boot, then rollback cancels it before any reboot"):
        op(DEPLOY + BOOT)
        current = generation()
        out = app.succeed("terrace-production activate " + BOOT + " --boot")
        ok("is the boot default." in out and "cancel it with terrace-production rollback" in out and running() == NEXT and generation() != current, f"--boot registered {generation()} as the boot default, the running system unchanged")
        ok("WARNING: the boot default" in app.succeed("terrace-production status"), "status warns that the boot default is not the running system")
        out = app.succeed("terrace-production rollback")
        ok("the boot default goes back to generation" in out and generation() == current and running() == NEXT, f"rollback made the running system's generation the boot default again: {generation()}")
        ok(records()[-1]["action"] == "cancel" and records()[-1]["mode"] == "boot" and records()[-1]["result"] == "success", f"the cancel is recorded: {records()[-1]}")
        ok("WARNING" not in app.succeed("terrace-production status"), "status is clean again")
        ok(app.succeed("readlink -f /nix/var/nix/profiles/system").strip() == NEXT, "the boot default is the running system")

    with subtest("a gated switch that also breaks another unit is a failed switch"):
        quiesce()
        op(DEPLOY + BROKEN)
        out = app.fail("terrace-production activate " + BROKEN + " --cms-stays-down 2>&1")
        record = records()[-1]
        ok("activate FAILED (exit 4)" in out and record["result"] == "failed (exit 4)" and "terrace-deploy-probe-broken.service" in record["unexpectedUnits"]
           and "terrace-cms.service" in record["failedUnits"], f"recorded FAILED, naming the extra failing unit next to the gated CMS: {record}")
        # Back to the migrated release (control: the same command succeeds once nothing else fails).
        app.succeed("systemctl stop terrace-cms.service terrace-web.service terrace-deploy-probe-broken.service; systemctl reset-failed")
        app.succeed("terrace-production activate " + NEXT)
        ok(running() == NEXT and records()[-1]["result"] == "success", "the migrated release activated again, nothing failed")
        restart_after_quiesce()
        cms_serves("after the failed switch")

    with subtest("stop owned guests"):
        app.shutdown()
        operator.shutdown()
    print("DEPLOY-CHECKS " + json.dumps(checks))
    print("DEPLOY-PROOF " + json.dumps({"assertions": len(checks), "seconds": round(time.time() - started_at, 2), "timings": timings}))
  '';
}
