# VM drills of the host-update steps the deploy proof (deploy-test.nix) leaves out (docs/host-updates.md): the
# relay's update through the deploy path; a real rollback past a failed and an unfinished generation and
# `rollback --generation N`; a switch that fails before the new system runs, with the boot default set back
# automatically; and, on the app role, a migration that fails, one killed at its time limit before its commit and
# one killed after it (H9), each with what `status`, `activate` and `rollback` then allow, and the way out by fixing
# forward with a corrected build. Synthetic `.invalid` names; every key is made at build time by this
# file and never committed. What the drill changes to make a failure happen is said where it happens.
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
  ca = pkgs.runCommand "terrace-update-drill-ca" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir $out
    openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 3650 \
      -keyout $out/ca-key.pem -out $out/ca.pem -subj /CN=TerraceUpdateDrillCA \
      -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign >/dev/null 2>&1
  '';
  keys = pkgs.runCommand "terrace-update-drill-ssh-keys" {nativeBuildInputs = [pkgs.openssh];} ''
    mkdir $out
    ssh-keygen -q -t ed25519 -N "" -C drill-operator -f $out/operator
  '';
  publicKey = file: lib.removeSuffix "\n" (builtins.readFile "${keys}/${file}.pub");
  escrow = pkgs.runCommand "terrace-update-drill-escrow" {nativeBuildInputs = [pkgs.age];} ''
    mkdir $out
    age-keygen -o $out/escrow.age 2>/dev/null
    age-keygen -y $out/escrow.age >$out/escrow.pub
  '';
  escrowRecipient = lib.removeSuffix "\n" (builtins.readFile "${escrow}/escrow.pub");
  signing = pkgs.runCommand "terrace-update-drill-signing-key" {nativeBuildInputs = [pkgs.nix];} ''
    mkdir $out
    export HOME=$TMPDIR NIX_STATE_DIR=$TMPDIR/state NIX_CONF_DIR=$TMPDIR/conf
    # The working key and the offline spare, both trusted by the app from its installation.
    for name in deploy spare; do
      nix --extra-experimental-features nix-command key generate-secret --key-name terrace-drill-$name-1 >$out/$name.sec
      nix --extra-experimental-features nix-command key convert-secret-to-public <$out/$name.sec >$out/$name.pub
    done
  '';
  deployPublic = lib.removeSuffix "\n" (builtins.readFile "${signing}/deploy.pub");
  sparePublic = lib.removeSuffix "\n" (builtins.readFile "${signing}/spare.pub");
  users = pkgs.runCommand "terrace-update-drill-users" {} ''
    mkdir $out
    cp -r ${contract}/. $out/
    cp ${./rehearsal-users.ts} $out/rehearsal-users.ts
  '';
  leafExtension = hostnames:
    pkgs.writeText "terrace-drill-leaf.ext" ''
      basicConstraints=critical,CA:FALSE
      keyUsage=critical,digitalSignature,keyEncipherment
      extendedKeyUsage=serverAuth
      subjectAltName=${lib.concatMapStringsSep "," (name: "DNS:${name}") hostnames}
    '';
  appExtension = leafExtension names;
  relayExtension = leafExtension ["relay.terrace.invalid"];
  deploy = pkgs.writeShellApplication {
    name = "terrace-deploy";
    runtimeInputs = [pkgs.nix pkgs.openssh pkgs.coreutils pkgs.gnugrep];
    text = builtins.readFile ./terrace-deploy.sh;
  };
  revisions = {
    old = lib.concatStrings (lib.replicate 40 "a");
    next = lib.concatStrings (lib.replicate 40 "b");
    fixed = lib.concatStrings (lib.replicate 40 "c");
  };
  # A unit that fails and restarts: a switch that breaks something (as in deploy-test.nix).
  brokenUnit = {
    systemd.services.terrace-drill-broken = {
      wantedBy = ["multi-user.target"];
      serviceConfig = {
        ExecStart = "${pkgs.coreutils}/bin/false";
        Restart = "on-failure";
        RestartSec = 30;
      };
    };
  };
  variantFile = text: {environment.etc."terrace-drill-variant".text = text;};
  # The pinned release (one more CMS migration than the previous one) at a new Womb revision.
  nextRelease = womb: {
    terrace.website = {
      webPackage = lib.mkForce release.web;
      cmsPackage = lib.mkForce release.cms;
      revision = lib.mkForce release.revision;
    };
    terrace.production.app.releaseMigrations = lib.mkForce release.migrations;
    terrace.production.wombRevision = lib.mkForce womb;
  };
  relayHost = {
    hostname = "relay.terrace.invalid";
    publicIPv4 = "192.0.2.10";
    backup.primary = {
      repository = "sftp:restic@operator:relay-repository";
      knownHosts = "operator ${publicKey "operator"}";
    };
  };
in {
  name = "terrace-production-update-drill";
  nodes = {
    # The build machine and operator workstation: holds the signing key, signs and copies.
    operator = {nodes, ...}: {
      virtualisation = {
        memorySize = 3072;
        cores = 2;
        diskSize = 4096;
        # Registered in this guest's store, so it can sign and copy them like a builder its own outputs.
        additionalPaths = with nodes.app.system.build;
          [nodes.app.system.build.toplevel drillGood drillProbe drillBroken drillGood2 drillGrubFail drillNext drillFixed]
          ++ (with nodes.relay.system.build; [nodes.relay.system.build.toplevel drillRelayNext drillRelayBroken]);
      };
      environment.systemPackages = [tool deploy pkgs.sops pkgs.age pkgs.ssh-to-age pkgs.openssl pkgs.bun pkgs.jq pkgs.curl];
    };
    app = {extendModules, ...}: let
      variant = modules: (extendModules {inherit modules;}).config.system.build.toplevel;
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
      # The VM disk cannot take GRUB: write the menu only (priority 5 beats qemu-vm.nix's mkVMOverride), as deploy-test.nix.
      boot.loader.grub.device = lib.mkOverride 5 "nodev";
      terrace.production = {
        instance = "rehearsal";
        memoryMiB = 9216;
        inherit escrowRecipient;
        operatorKeys = [(publicKey "operator")];
        deploy.trustedKeys = [deployPublic sparePublic];
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
      terrace.website = {
        webPackage = previous.web;
        cmsPackage = previous.cms;
        inherit (previous) revision;
      };
      system.build = {
        # Same release as the installed system: switches that touch no CMS state.
        drillGood = variant [(variantFile "good")];
        drillGood2 = variant [(variantFile "good2")];
        drillBroken = variant [brokenUnit];
        # A switch that fails before it changes the running system: GRUB installed on the VM disk, which cannot take it
        # (the failure the deploy proof met while it was built; priority 4 beats the node's own "nodev" at 5).
        drillGrubFail = variant [{boot.loader.grub.device = lib.mkOverride 4 "/dev/disk/by-id/virtio-root";}];
        # Holds its activation for 20 seconds, so the drill can stop the switch unit in the middle of it.
        drillProbe = variant [
          (variantFile "probe")
          {
            system.activationScripts.terraceDrillProbe.text = ''
              touch /run/terrace-drill-probe-started
              sleep 20
              touch /run/terrace-drill-probe-done
            '';
          }
        ];
        # The pinned release, built with a migration time limit of 60 s instead of the module's 180 s: the drill's
        # only change to the failing build, so that a migration killed at its limit costs one minute, not three.
        drillNext = variant [
          (nextRelease revisions.next)
          {systemd.services.terrace-cms-migrate.serviceConfig.TimeoutStartSec = lib.mkForce 60;}
        ];
        # The corrected build: the same release with the module's own limit (180 s) at a new Womb revision.
        drillFixed = variant [(nextRelease revisions.fixed)];
      };
    };
    # The production relay role, updated through the deploy path.
    relay = {extendModules, ...}: let
      variant = modules: (extendModules {inherit modules;}).config.system.build.toplevel;
      # A new Womb revision whose relay system changes a Headscale setting (its log level), so the switch restarts Headscale.
      relayNext = {
        terrace.production.wombRevision = lib.mkForce revisions.next;
        services.headscale.settings.log.level = lib.mkForce "info";
      };
    in {
      imports = [sopsModule ./relay.nix ./rehearsal-relay.nix];
      virtualisation = {
        memorySize = 2048;
        cores = 1;
        diskSize = 4096;
        writableStoreUseTmpfs = false;
      };
      system.switch.enable = true;
      boot.loader.grub.device = lib.mkOverride 5 "nodev";
      terrace.production = {
        instance = "rehearsal";
        memoryMiB = 2048;
        inherit escrowRecipient;
        operatorKeys = [(publicKey "operator")];
        deploy.trustedKeys = [deployPublic];
        wombRevision = revisions.old;
        relay = relayHost;
      };
      system.build = {
        drillRelayNext = variant [relayNext];
        drillRelayBroken = variant [relayNext brokenUnit];
      };
    };
  };
  testScript = {nodes, ...}: let
    base = nodes.app.system.build.toplevel;
    relayBase = nodes.relay.system.build.toplevel;
    inherit (nodes.app.system.build) drillGood drillGood2 drillBroken drillProbe drillGrubFail drillNext drillFixed;
    inherit (nodes.relay.system.build) drillRelayNext drillRelayBroken;
  in ''
    import json, shlex, time

    # Every assertion is a real condition on what a command returned or printed, or on a state the drill waited for.
    checks: list[str] = []
    def ok(condition, label):
        assert condition, label
        checks.append(label)

    STORE = "/root/store"
    ESCROW = "/root/escrow/escrow.age"
    ENV = f"export TERRACE_SECRETS_STORE={STORE} SOPS_AGE_KEY_FILE={ESCROW} TERRACE_SECRETS_SCRATCH=/dev/shm TERRACE_ESCROW_RECIPIENT=${escrowRecipient}; "
    MEDIA = "/var/lib/terrace-cms-media"
    MARKER = "/var/lib/terrace-cms/migrated-revision"
    RECORDS = "/var/lib/terrace-production/deployments.log"
    HOLD = "/var/lib/terrace-production/timers-held"
    SERVICE_HOLD = "/var/lib/terrace-production/service-timers-held"
    BASE, GOOD, GOOD2, BROKEN, PROBE = "${base}", "${drillGood}", "${drillGood2}", "${drillBroken}", "${drillProbe}"
    NEXT, FIXED, GRUBFAIL = "${drillNext}", "${drillFixed}", "${drillGrubFail}"
    RELAY_BASE, RELAY_NEXT, RELAY_BROKEN = "${relayBase}", "${drillRelayNext}", "${drillRelayBroken}"
    OLD_REV, NEXT_REV, FIXED_REV = "${revisions.old}", "${revisions.next}", "${revisions.fixed}"
    OLD_PIN, NEW_PIN = "${previous.revision}", "${release.revision}"
    OLD_MIGRATIONS, NEW_MIGRATIONS = ${builtins.toJSON previous.migrations}, ${builtins.toJSON release.migrations}
    ADDED = [name for name in NEW_MIGRATIONS if name not in OLD_MIGRATIONS]
    GATED = ["terrace-cms.service", "terrace-web.service"]
    BACKUP_TIMERS = ["terrace-backup.timer", "terrace-backup-check.timer", "terrace-backup-freshness.timer"]
    CONTENT = ["posts", "_posts_v", "redirects", "media", "users"]
    H9 = "(a failed or partial migration, H9): its CMS would start on a newer schema. Not allowed by any flag"

    def ssh(host):
        return f"ssh -i /root/.ssh/operator -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/{host}_known_hosts operator@{host} "

    def user_manager_gone(machine):
        # Every SSH login as operator (the copy, an activation over SSH) starts operator's systemd user manager, which
        # stops about 10 s after the last session. A switch that runs while it is stopping fails its user activation
        # ("warning: user activation for operator failed") and exits 4 with no failed unit: seen in the fourth run of this
        # drill, recorded `failed (exit 4)` (the tool is right not to explain it away). So a switch waits for that state.
        machine.wait_until_succeeds("test \"$(systemctl show user@$(id -u operator).service -p ActiveState --value)\" = inactive", timeout=120)

    def deploy(host, path, key="deploy"):
        out = op(f"terrace-deploy --key /root/signing/{key}.sec --address {host} --identity /root/.ssh/operator --known-hosts /root/{host}_known_hosts {path}")
        ok(f"Signed the closure of {path}" in out and f"Copied to operator@{host}." in out and out.strip().endswith(f"ssh -t operator@{host} sudo terrace-production activate {path}"),
           f"{host}: {path} signed and copied as operator, the activation command printed")
        user_manager_gone(relay if host == "relay" else app)

    def op(command, **kwargs):
        return operator.succeed(ENV + command, **kwargs)

    def state(machine, unit, prop):
        return machine.succeed(f"systemctl show {unit} -p {prop} --value").strip()

    def running(machine):
        return machine.succeed("readlink -f /run/current-system").strip()

    def generation(machine):
        return machine.succeed("readlink /nix/var/nix/profiles/system").strip().removeprefix("system-").removesuffix("-link")

    def boot_default(machine):
        return machine.succeed("readlink -f /nix/var/nix/profiles/system").strip()

    def records(machine):
        return [json.loads(line) for line in machine.succeed(f"cat {RECORDS} 2>/dev/null || true").splitlines() if line]

    def snapshot(machine):
        return (running(machine), generation(machine), len(records(machine)))

    def refused(machine, command, needle, label):
        before = snapshot(machine)
        out = machine.fail(command + " 2>&1")
        ok(needle in out and "terrace-production: nothing changed:" in out, f"refused: {label} ({needle!r} in {out.strip()[-400:]!r})")
        ok(snapshot(machine) == before, f"refused: {label}: nothing changed")
        return out

    def dry_run(machine, command, label, host=None):
        # Exit 0, every check passed, nothing changed on the host (`machine` runs the command, `host` is the host it reaches).
        host = host or machine
        before = snapshot(host)
        rc, out = machine.execute(command + " --dry-run 2>&1")
        ok(rc == 0 and "Checks passed; " in out and "Dry run: nothing changed." in out and snapshot(host) == before, f"{label} (exit {rc}): {out.strip()[-400:]!r}")
        return out

    def finished(record, action, before, after, to, result):
        ok(record["phase"] == "finished" and (record["action"], record["generationBefore"], record["generationAfter"], record["to"], record["result"]) == (action, before, after, to, result),
           f"recorded: {action} {before} -> {after}, {result!r}: {record}")

    def gated_record(record, label):
        # The release's CMS and website failed their migration gate; the record is written once they stopped retrying.
        ok(record["failedUnits"] == GATED and record["unexpectedUnits"] == [] and record["notStartedUnits"] == [] and record["waitTimedOut"] is False
           and record["stillStarting"] == [] and record["result"] == "switched; down until the CMS migration: " + " ".join(GATED),
           f"{label}: switched with exactly the gated CMS and website down: {record}")

    def sql(query, database="terrace_cms"):
        return app.succeed("runuser -u postgres -- psql -XAt -v ON_ERROR_STOP=1 -d " + database + " -c " + shlex.quote(query) + " 2>/dev/null").strip()

    def ledger():
        return sql("select name from payload_migrations order by id").splitlines()

    def tables():
        return sql("select tablename from pg_tables where schemaname = 'public' order by 1").splitlines()

    def data():
        rows = {t: sql(f"select count(*) || ':' || md5(coalesce(string_agg(x::text, chr(10) order by x::text), ''')) from \"{t}\" x") for t in CONTENT}
        rows["media files"] = app.succeed("cd " + MEDIA + " && find . -type f -print0 | LC_ALL=C sort -z | xargs -0r sha256sum").strip()
        return rows

    def cms_backends(where=""):
        # The CMS role's database sessions other than this query's own (the CMS, the website and the sweep are down).
        return int(sql(f"select count(*) from pg_stat_activity where usename = 'terrace_cms' and pid <> pg_backend_pid() {where}", "postgres"))

    def marker():
        return app.succeed("cat " + MARKER).strip()

    def cms_serves(label):
        body = json.loads(app.succeed("curl -sf --max-time 20 'http://127.0.0.1:13048/api/posts?limit=1'"))
        ok(body.get("totalDocs", 0) > 0, label + ": the CMS serves the synthetic post")
        ok(app.execute("curl -sf --max-time 20 -o /dev/null http://127.0.0.1:14294/events/")[0] == 0, label + ": the website answers")

    def site_down(label):
        for unit in GATED + ["terrace-cms-sweep.service"]:
            ok(state(app, unit, "ActiveState") in ["failed", "inactive"], f"{label}: {unit} not running ({state(app, unit, 'ActiveState')})")

    def migrate_invocation(label, block):
        # Start the migration unit; returns (exit status of a blocking start or None, the new InvocationID).
        previous = state(app, "terrace-cms-migrate.service", "InvocationID")
        rc = None
        if block:
            rc = app.execute("systemctl start terrace-cms-migrate.service", timeout=600)[0]
        else:
            app.succeed("systemctl start --no-block terrace-cms-migrate.service")
            app.wait_until_succeeds(f"test \"$(systemctl show terrace-cms-migrate.service -p InvocationID --value)\" != {shlex.quote(previous)}", timeout=120)
        invocation = state(app, "terrace-cms-migrate.service", "InvocationID")
        ok(invocation not in ["", previous], f"{label}: a new run of the migration unit")
        return rc, invocation

    def lock_holder(table):
        # A second session holding an exclusive lock on one table, as a long query or a forgotten session would.
        app.succeed("systemd-run --unit=terrace-drill-lock --collect -p User=postgres --setenv=PGAPPNAME=terrace-drill-lock $(command -v psql) -XAt -d terrace_cms -c "
                    + shlex.quote(f"begin; lock table {table} in access exclusive mode; select pg_sleep(3600)"))
        app.wait_until_succeeds("test \"$(runuser -u postgres -- psql -XAt -d terrace_cms -c " + shlex.quote(
            f"select count(*) from pg_locks l join pg_class c on c.oid = l.relation where c.relname = '{table}' and l.mode = 'AccessExclusiveLock' and l.granted") + ")\" = 1", timeout=60)

    def release_lock():
        sql("select pg_terminate_backend(pid) from pg_stat_activity where application_name = 'terrace-drill-lock'", "postgres")
        app.wait_until_succeeds("! systemctl is-active --quiet terrace-drill-lock.service", timeout=60)

    def wait_timed_out(label):
        app.wait_until_succeeds("test \"$(systemctl show terrace-cms-migrate.service -p Result --value)\" = timeout && test \"$(systemctl show terrace-cms-migrate.service -p ActiveState --value)\" = failed", timeout=600)
        ok(state(app, "terrace-cms-migrate.service", "Result") == "timeout", f"{label}: the migration unit was killed at its time limit (Result timeout)")

    started_at = time.time()
    operator.start()
    operator.wait_for_unit("multi-user.target")
    operator.succeed("install -d -m 0700 /root/escrow /root/.ssh /root/signing " + STORE + " && install -m 0600 ${keys}/operator /root/.ssh/operator")
    operator.succeed("install -m 0600 ${escrow}/escrow.age " + ESCROW + " && install -m 0644 ${escrow}/escrow.pub " + STORE + "/escrow.recipient")
    operator.succeed("install -m 0600 ${signing}/deploy.sec ${signing}/spare.sec /root/signing/")

    with subtest("operator creates both hosts' keys and secrets"):
        for host, extension, cn, extra in [("app", "${appExtension}", "web.terrace.invalid", " --rehearsal"), ("relay", "${relayExtension}", "relay.terrace.invalid", "")]:
            op(f"terrace-secrets host create {host} --role {host} && terrace-secrets generate {host}")
            op("sh -ec 'umask 077; d=$(mktemp -d /dev/shm/tls.XXXXXX); "
               f"openssl req -new -nodes -newkey rsa:2048 -sha256 -keyout $d/key.pem -out $d/req.pem -subj /CN={cn} 2>/dev/null; "
               f"openssl x509 -req -in $d/req.pem -CA ${ca}/ca.pem -CAkey ${ca}/ca-key.pem -set_serial 0x$(openssl rand -hex 8) -days 30 -sha256 -extfile {extension} -out $d/cert.pem 2>/dev/null; "
               f"terrace-secrets set {host} tls/key <$d/key.pem; terrace-secrets set {host} tls/certificate <$d/cert.pem; rm -rf $d'")
            if host == "app":
                op("bun --no-env-file ${users}/rehearsal-users.ts | terrace-secrets set app rehearsal/users-json --rehearsal")
            ok("secrets encrypted to exactly the host key and the pinned escrow key." in op(f"terrace-secrets check {host}{extra}"), f"{host}: store complete")
            stage = f"/dev/shm/terrace-stage-{host}"
            operator.succeed(f"echo \"{host} $(cat {stage}/ssh_host_ed25519_key.pub)\" >/root/{host}_known_hosts")
            operator.succeed(f"rm -rf /tmp/shared/stage/{host} && install -d -m 0700 /tmp/shared/stage/{host} && cp {stage}/ssh_host_ed25519_key {stage}/ssh_host_ed25519_key.pub /tmp/shared/stage/{host}/")

    # ---- C. The relay's update through the deploy path (docs/host-updates.md section 3) ----
    with subtest("relay: adopted at its first system, a Headscale user, its timers held"):
        relay.start()
        relay.wait_for_unit("multi-user.target")
        operator.succeed("rm -rf /tmp/shared/stage/relay")
        op("terrace-secrets host unstage relay")
        ok("Installed 4 encrypted secrets." in op("terrace-secrets push relay --address relay --identity /root/.ssh/operator"), "relay: secrets pushed")
        if relay.execute("test -L /nix/var/nix/profiles/system")[0] != 0:
            relay.succeed("nix-env -p /nix/var/nix/profiles/system --set $(readlink -f /run/current-system)")
        ok(generation(relay) == "1" and running(relay) == RELAY_BASE, "relay: generation 1 is the installed system")
        ok("Adopted" in relay.succeed("terrace-production adopt --new --first-deployment-without-repository relay 2>&1"), "relay: adopted as new")
        # The hold marker `adopt --restored` writes, placed by hand: no backup repository exists in this drill, so no
        # scheduled backup, check or hourly freshness check may run (the freshness check would fail and fail the switches).
        relay.succeed(f"install -m 0600 /dev/null {HOLD} && systemctl stop " + " ".join(BACKUP_TIMERS))
        relay.wait_for_unit("headscale.service")
        relay.wait_for_unit("nginx.service")
        relay.succeed("headscale users create drill-alice >/dev/null")
        relay_keys = relay.succeed("cd /var/lib/headscale && sha256sum noise_private.key derp_server_private.key").strip()
        relay_users = sorted(user["name"] for user in json.loads(relay.succeed("headscale users list -o json")))
        ok(relay_users == ["drill-alice"] and len(relay_keys.splitlines()) == 2, f"relay: one Headscale user and both private keys: {relay_users}")
        address = operator.succeed("getent ahostsv4 relay | head -n 1 | cut -d' ' -f1").strip()

    def relay_serves(label):
        code = operator.succeed(f"curl --cacert ${ca}/ca.pem -s -o /dev/null -w '%{{http_code}}' --resolve relay.terrace.invalid:443:{address} https://relay.terrace.invalid/health").strip()
        ok(code == "200", f"{label}: https://relay.terrace.invalid/health answers 200 from the operator guest ({code})")
        ok([state(relay, unit, "ActiveState") for unit in ["headscale.service", "nginx.service"]] == ["active", "active"], f"{label}: Headscale and nginx active")
        ok(relay.succeed("cd /var/lib/headscale && sha256sum noise_private.key derp_server_private.key").strip() == relay_keys, f"{label}: the Headscale key hashes are R1's")
        ok(sorted(user["name"] for user in json.loads(relay.succeed("headscale users list -o json"))) == relay_users, f"{label}: the Headscale user is still there")
        ok(relay.succeed("systemctl list-units --state=failed --plain --no-legend").strip() == "", f"{label}: no failed unit")

    with subtest("relay R1 and R2: record, sign, copy, dry run, activate over SSH"):
        # R1's record (the backup of R1 is not part of this drill: no repository).
        ok(relay.succeed("systemctl list-units --state=failed --plain --no-legend").strip() == "", "relay R1: no failed unit")
        relay.succeed("systemctl stop terrace-backup.timer terrace-backup-check.timer")
        relay_serves("relay before the update")
        deploy("relay", RELAY_NEXT)
        # The relay has no CMS gate: no quiesce check, so the dry run is a clean pass (exit 0) with everything running.
        out = dry_run(operator, ssh("relay") + "sudo terrace-production activate " + RELAY_NEXT, "relay: dry run of the update with Headscale and nginx running is a clean pass", host=relay)
        ok(f"Checks passed; activate {RELAY_NEXT} (Womb {NEXT_REV}, website none) with switch-to-configuration switch in unit terrace-switch." in out and "Signatures: " in out,
           "relay: the dry run names the target, its revision and no website, and checked the signatures")
        headscale_before = state(relay, "headscale.service", "InvocationID")
        nginx_before = state(relay, "nginx.service", "InvocationID")
        out = operator.succeed(ssh("relay") + "sudo terrace-production activate " + RELAY_NEXT)
        ok(f"Running generation 2: {RELAY_NEXT}." in out and "THE SITE IS DOWN" not in out, f"relay: activated as generation 2: {out.strip()[-300:]!r}")
        ok(running(relay) == RELAY_NEXT and generation(relay) == "2", "relay: generation 2 runs")
        record = records(relay)[-1]
        finished(record, "activate", "1", "2", RELAY_NEXT, "success")
        ok(record["switchExit"] == 0 and record["failedUnits"] == [] and record["unexpectedUnits"] == [] and record["notStartedUnits"] == [] and record["waitTimedOut"] is False
           and record["stillStarting"] == [] and record["wombRevision"] == NEXT_REV and record["websiteRevision"] == "",
           f"relay: the record has the switch's exit 0, no unit lists, no website revision: {record}")
        ok(records(relay)[-2]["phase"] == "started" and records(relay)[-2]["failedBefore"] == [], "relay: a started record before it, with no CMS or website to clear")
        ok(state(relay, "headscale.service", "InvocationID") != headscale_before and state(relay, "nginx.service", "InvocationID") == nginx_before,
           "relay: the switch restarted Headscale (its settings changed) and left nginx running")
        relay_serves("relay after the update")
        status = relay.succeed("terrace-production status")
        ok(f"running system: {RELAY_NEXT} (Womb {NEXT_REV}, website none)" in status and f"previous generation: 1 {RELAY_BASE} (Womb {OLD_REV}, website none)" in status
           and "WARNING" not in status and "deploy path: on, 1 trusted deploy key(s)" in status, f"relay: status after the update: {status}")

    with subtest("relay: no CMS gate, so a switch that breaks a unit is a plain failed switch"):
        # terrace-production.sh for the relay: quiesce and cms_marker are empty (base.nix), so check_cms returns at once,
        # the gate is never closed, the wait has only terrace-production.target and terrace-production-boot jobs to wait
        # for, and a switch is judged only after exit 4 (or a timed-out wait): never "down until the CMS migration".
        deploy("relay", RELAY_BROKEN)
        out = relay.fail("terrace-production activate " + RELAY_BROKEN + " 2>&1")
        record = records(relay)[-1]
        finished(record, "activate", "2", "3", RELAY_BROKEN, "failed (exit 4)")
        ok("activate FAILED (exit 4)" in out and "down until the CMS migration" not in out and record["switchExit"] == 4
           and record["unexpectedUnits"] == ["terrace-drill-broken.service"] and record["failedUnits"] == [] and record["notStartedUnits"] == [],
           f"relay: recorded failed (exit 4), naming only the broken unit: {record}")
        relay.succeed("systemctl stop terrace-drill-broken.service; systemctl reset-failed terrace-drill-broken.service")

    with subtest("relay: rollback past the failed attempt to the update, then to the first system"):
        user_manager_gone(relay)
        out = relay.succeed("terrace-production rollback")
        ok(f"Running generation 2: {RELAY_NEXT}." in out and running(relay) == RELAY_NEXT, f"relay: rollback from the failed generation 3 ran generation 2: {out.strip()[-300:]!r}")
        finished(records(relay)[-1], "rollback", "3", "2", RELAY_NEXT, "success")
        headscale_before = state(relay, "headscale.service", "InvocationID")
        out = relay.succeed("terrace-production rollback")
        ok(f"Running generation 1: {RELAY_BASE}." in out and running(relay) == RELAY_BASE and boot_default(relay) == RELAY_BASE, "relay: a second rollback ran generation 1, the boot default with it")
        finished(records(relay)[-1], "rollback", "2", "1", RELAY_BASE, "success")
        ok(state(relay, "headscale.service", "InvocationID") != headscale_before, "relay: the rollback restarted Headscale with its previous settings")
        relay_serves("relay after the rollbacks")
        # R5: re-arm. With the hold marker in place the timers stay off (start condition), as on a restored host.
        ok(all(state(relay, unit, "ActiveState") == "inactive" for unit in BACKUP_TIMERS), "relay: the held backup timers stayed off across the switches")
        relay.shutdown()

    # ---- the app at the previous release ----
    with subtest("app: the previous release adopted, migrated, populated; its timers held"):
        app.start()
        app.wait_for_unit("multi-user.target")
        operator.succeed("rm -rf /tmp/shared/stage/app")
        op("terrace-secrets host unstage app")
        ok("Installed 13 encrypted secrets." in op("terrace-secrets push app --address app --identity /root/.ssh/operator --rehearsal"), "app: secrets pushed")
        if app.execute("test -L /nix/var/nix/profiles/system")[0] != 0:
            app.succeed("nix-env -p /nix/var/nix/profiles/system --set $(readlink -f /run/current-system)")
        ok(generation(app) == "1" and running(app) == BASE, "app: generation 1 is the installed system")
        ok("Adopted" in app.succeed("terrace-production adopt --new --first-deployment-without-repository app 2>&1"), "app: adopted as new")
        # Both hold markers, placed by hand (no repository in this drill, as for the relay); the sweep's is removed in part A.
        app.succeed(f"install -m 0600 /dev/null {HOLD} && install -m 0600 /dev/null {SERVICE_HOLD} && systemctl stop terrace-cms-sweep.timer " + " ".join(BACKUP_TIMERS))
        app.wait_for_unit("keycloak.service", timeout=360)
        app.succeed("systemctl start terrace-cms-migrate.service", timeout=300)
        ok(marker() == OLD_PIN and ledger() == OLD_MIGRATIONS, f"app: the previous release migrated ({len(OLD_MIGRATIONS)} migrations)")
        app.succeed("systemctl start terrace-cms.service terrace-web.service", timeout=300)
        app.succeed("install -m 0600 /dev/null " + MEDIA + "/private.png && echo 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGPgUbL4DwACCgFmeGgpMwAAAABJRU5ErkJggg==' | base64 -d >" + MEDIA + "/private.png && cp " + MEDIA + "/private.png " + MEDIA + "/public.png && chown terrace_cms:terrace_cms " + MEDIA + "/*.png")
        sql("insert into media(alt,public,filename,mime_type,filesize,width,height) values ('Synthetic private',false,'private.png','image/png',70,1,1),('Synthetic public',true,'public.png','image/png',70,1,1)")
        app.succeed("runuser -u postgres -- psql -XAt -v ON_ERROR_STOP=1 -d terrace_cms -f /etc/gateway-content.sql >/dev/null 2>&1")
        sql("insert into users(display_name,role,email) values ('Synthetic editor','admin','editor@terrace.invalid')")
        cms_serves("app at the previous release")
        before_upgrade = data()
        tables_before = tables()
        ok(all(not value.startswith("0:") for key, value in before_upgrade.items() if key != "media files"), f"app: populated: {sorted(before_upgrade)}")

    # ---- B. Rollback past failed and unfinished generations, and --generation N (same release: no CMS state involved) ----
    with subtest("app: an unfinished attempt: the switch unit stopped in the middle of an activation"):
        deploy("app", GOOD)
        app.succeed("terrace-production activate " + GOOD)
        finished(records(app)[-1], "activate", "1", "2", GOOD, "success")
        deploy("app", PROBE)
        # The client runs in its own transient unit, whose plain shell writes the output and the exit status. (The test
        # driver runs every command with `set -euo pipefail`, so a backgrounded `(client; echo $? >file)` stops at the
        # client's exit 1 without writing the file: that, not the client, is what failed the second and third runs.)
        app.succeed("systemd-run --unit=terrace-drill-client --collect -p StandardOutput=file:/tmp/probe.log -p StandardError=file:/tmp/probe.log "
                    + "/run/current-system/sw/bin/bash -c " + shlex.quote("rc=0; /run/current-system/sw/bin/terrace-production activate " + PROBE + " || rc=$?; echo $rc >/tmp/probe.rc"))
        app.wait_until_succeeds("test -e /run/terrace-drill-probe-started && systemctl is-active --quiet terrace-switch.service", timeout=300)
        # As a killed unit or a host losing power mid-switch would leave it: the switch never finishes.
        app.succeed("systemctl stop terrace-switch.service")
        # Diagnostics if no exit status appears: the client's processes, its output, the unit, the lock.
        try:
            app.wait_until_succeeds("test -e /tmp/probe.rc", timeout=120)
        except Exception:
            print("UPDATE-DRILL-DIAGNOSTICS " + json.dumps({"ps": app.execute("ps -eo pid,ppid,etimes,args | grep -E '[t]errace-production|[s]ystemd-run|[s]witch-to-conf'")[1],
                  "client": app.execute("systemctl status --no-pager terrace-drill-client.service 2>&1; journalctl -u terrace-drill-client.service -o cat --no-pager")[1],
                  "log": app.execute("cat /tmp/probe.log")[1], "unit": app.execute("systemctl status --no-pager terrace-switch.service 2>&1")[1],
                  "lock": app.execute("flock -n /var/lib/terrace-production/deploy.lock true && echo free || echo held")[1]}))
            raise
        rc, out = int(app.succeed("cat /tmp/probe.rc").strip()), app.succeed("cat /tmp/probe.log")
        last = records(app)[-1]
        # systemd counts the stopped unit as a clean exit (systemd-run --wait returns 0); the client reads the missing
        # finished record instead (the first run of this drill found the client reporting success here).
        ok(rc == 1 and "the terrace-switch unit ended without writing its finished record (stopped or killed mid-switch)" in out and "activate FAILED (exit 1)" in out
           and "Running generation" not in out and (last["phase"], last["action"], last["to"], last["generationBefore"]) == ("started", "activate", PROBE, "2"),
           f"the client reported FAILED (exit {rc}), and the log ends with the attempt's started record, no finished one: {last}; {out.strip()[-600:]!r}")
        ok("Nothing was set back: to make the running system the boot default again, run terrace-production rollback" in out, "the client says it set nothing back (no automatic revert without the unit) and names the way back")
        ok(app.execute("flock -n /var/lib/terrace-production/deploy.lock true")[0] == 0, "the client released the lock")
        ok(app.execute("test -e /run/terrace-drill-probe-done")[0] != 0, "the activation never completed")
        ok(running(app) == GOOD and generation(app) == "3" and boot_default(app) == PROBE, "running: generation 2's system; the boot default: the attempt's generation 3")
        # The activation had replaced /etc before it was stopped (NixOS runs its etc step before this probe).
        ok(app.succeed("cat /etc/terrace-drill-variant").strip() == "probe", "the stopped activation had already replaced /etc with the attempt's")
        status = app.succeed("terrace-production status")
        ok("WARNING: the boot default, generation 3" in status and "is not the running system" in status and "WARNING: the last activation started at" in status and "never finished" in status,
           f"status warns of the pending boot default and the unfinished activation: {status}")
        # --generation refuses a generation that is not older than the running system's, also with a pending boot default.
        refused(app, "terrace-production rollback --generation 3", f"generation 3 is not older than the current generation 2 (the running system's): rollback only goes back; to go forward, activate that system: terrace-production activate {PROBE}",
                "--generation naming the pending boot default")
        out = app.succeed("terrace-production rollback")
        ok("the boot default goes back to generation 2" in out and boot_default(app) == GOOD and running(app) == GOOD and generation(app) == "2", f"rollback cancelled the pending generation: {out.strip()[-300:]!r}")
        finished(records(app)[-1], "cancel", "3", "2", GOOD, "success")
        ok(records(app)[-1]["mode"] == "boot", "the cancel ran switch-to-configuration boot")
        ok("WARNING" not in app.succeed("terrace-production status"), "status: no warning after the cancel")
        ok(app.succeed("cat /etc/terrace-drill-variant").strip() == "probe", "the cancel (a boot step, no activation) left the attempt's /etc in place")
        cms_serves("after the unfinished attempt")

    with subtest("app: a real rollback passes over a failed and an unfinished generation"):
        deploy("app", BROKEN)
        out = app.fail("terrace-production activate " + BROKEN + " 2>&1")
        record = records(app)[-1]
        finished(record, "activate", "2", "4", BROKEN, "failed (exit 4)")
        ok("activate FAILED (exit 4)" in out and record["unexpectedUnits"] == ["terrace-drill-broken.service"] and record["failedUnits"] == [], f"generation 4 failed, naming the broken unit: {record}")
        ok(app.execute("test -e /etc/terrace-drill-variant")[0] != 0, "the completed (failed) switch replaced the attempt's /etc")
        app.succeed("systemctl stop terrace-drill-broken.service; systemctl reset-failed terrace-drill-broken.service")
        # Signed with the offline spare key only (the app trusts both from its installation): accepted like the working key.
        ok("deploy path: on, 2 trusted deploy key(s)" in app.succeed("terrace-production status"), "the app trusts two deploy keys, the working key and the spare")
        deploy("app", GOOD2, key="spare")
        sigs = app.succeed("nix --extra-experimental-features nix-command path-info --sigs " + GOOD2)
        ok("terrace-drill-spare-1:" in sigs and "terrace-drill-deploy-1:" not in sigs, f"the system's toplevel carries the spare key's signature only: {sigs.strip()}")
        out = app.succeed("terrace-production activate " + GOOD2)
        ok("Signatures: " in out and "Signatures: 0 new" not in out and "verified against 2 trusted deploy key(s)" in out, f"activate verified the new paths against both deploy keys: {out.strip()[-300:]!r}")
        finished(records(app)[-1], "activate", "4", "5", GOOD2, "success")
        status = app.succeed("terrace-production status")
        ok(f"previous generation: 2 {GOOD}" in status and "skipped by rollback, its last record failed: generation 4 ('failed (exit 4)')" in status
           and "skipped by rollback, its last record failed: generation 3 ('unfinished (started " in status, f"status names generation 2 as the rollback target and the two skipped: {status}")
        # --generation N on a failed or unfinished generation: a WARNING, every check still runs (dry runs).
        for number, what in [("4", "failed (exit 4)"), ("3", "unfinished (started ")]:
            out = dry_run(app, f"terrace-production rollback --generation {number}", f"--generation {number} passes its checks")
            ok(f"WARNING: generation {number} was chosen explicitly; its last record: {what}" in out, f"--generation {number} warns: {what}")
        out = app.succeed("terrace-production rollback 2>&1")
        ok("Skipping generation 4: its last record is 'failed (exit 4)'" in out and "Skipping generation 3: its last record is 'unfinished (started " in out
           and f"Running generation 2: {GOOD}." in out, f"rollback named both skipped generations and ran generation 2: {out.strip()[-500:]!r}")
        finished(records(app)[-1], "rollback", "5", "2", GOOD, "success")
        ok(running(app) == GOOD and boot_default(app) == GOOD and app.succeed("cat /etc/terrace-drill-variant").strip() == "good", "generation 2 runs and is the boot default, its /etc in place")
        cms_serves("after the rollback past the failed generations")

    with subtest("app: rollback --generation N, and its refusal of a newer generation"):
        refused(app, "terrace-production rollback --generation 5", f"generation 5 is not older than the current generation 2 (the running system's): rollback only goes back; to go forward, activate that system: terrace-production activate {GOOD2}",
                "--generation naming a newer generation")
        out = app.succeed("terrace-production rollback --generation 1 2>&1")
        ok(f"Running generation 1: {BASE}." in out and "WARNING" not in out and running(app) == BASE and boot_default(app) == BASE, f"--generation 1 ran the installed system: {out.strip()[-300:]!r}")
        finished(records(app)[-1], "rollback", "2", "1", BASE, "success")
        cms_serves("after --generation 1")
        ok(data() == before_upgrade and ledger() == OLD_MIGRATIONS and marker() == OLD_PIN, "same-release switches changed no data, ledger or marker")

    # ---- E. A switch that fails before the new system runs is reverted automatically ----
    with subtest("E: a switch that fails at the boot loader: the boot default is the running system again, with no operator action"):
        deploy("app", GRUBFAIL)
        rc, out = app.execute("terrace-production activate " + GRUBFAIL + " 2>&1")
        record = records(app)[-1]
        ok(rc == 1 and "activate FAILED (exit " in out and record["result"].startswith("failed (exit ") and record["switchExit"] not in [0, 4, None]
           and record["generationAfter"] == "6" and record["revert"] == {"from": "6", "to": "1", "result": "success"} and record["bootDefaultAfter"] == "1",
           f"E: the failed switch is recorded with the revert of the boot default from generation 6 to 1: {record}")
        ok("will not proceed with blocklists" in app.succeed("cat /var/lib/terrace-production/last-switch.log"), "E: the switch failed installing GRUB on the VM disk, before it changed the running system")
        ok("the boot default was set back to generation 1, the running system; generation 6 stays in the list, recorded as failed" in out, f"E: the client says what it did: {out.strip()[-500:]!r}")
        ok(running(app) == BASE and boot_default(app) == BASE and generation(app) == "1" and app.execute("test -L /nix/var/nix/profiles/system-6-link")[0] == 0,
           "E: the running system is the boot default; the failed generation 6 stays in the list")
        ok(app.succeed("grep -m 1 -o 'init=[^ ]*' /boot/grub/grub.cfg").strip() == f"init={BASE}/init", "E: the boot menu's first entry boots the running system")
        status = app.succeed("terrace-production status")
        ok("WARNING" not in status, f"E: status has nothing to warn about: {status}")
        cms_serves("E: after the reverted switch")
        # A reboot is left out: a test VM boots the system the test framework names, not the boot menu's default.

    # ---- A. Failed and timed-out migrations, and fixing forward ----
    with subtest("app: quiesce and activate the next release (gated); the switch starts the timers step 6 stopped"):
        # Step 6's first two lines, with the sweep timer's hold marker removed: the backup timers stay held (no repository).
        app.succeed(f"rm {SERVICE_HOLD}")
        app.succeed("systemctl stop terrace-backup.timer terrace-backup-check.timer terrace-cms-sweep.timer")
        app.succeed("systemctl stop terrace-cms-sweep.service terrace-web.service terrace-cms.service")
        ok(state(app, "terrace-cms-sweep.timer", "ActiveState") == "inactive", "the sweep timer is stopped")
        tried = {unit: int(state(app, unit, "ConditionTimestampMonotonic")) for unit in BACKUP_TIMERS}
        deploy("app", NEXT)
        out = operator.succeed(ssh("app") + "sudo terrace-production activate " + NEXT)
        ok("THE SITE IS DOWN until the CMS migration" in out and running(app) == NEXT and generation(app) == "7", f"the next release runs as generation 7, down until its migration: {out.strip()[-300:]!r}")
        gated_record(records(app)[-1], "activation of the next release")
        # switch-to-configuration starts every active target again (nixpkgs switch-to-configuration-ng), so
        # terrace-production.target starts what it wants: the stopped sweep timer runs again, and the held backup timers
        # are tried and skipped by their start condition.
        app.wait_until_succeeds("systemctl is-active --quiet terrace-cms-sweep.timer", timeout=60)
        ok(state(app, "terrace-cms-sweep.timer", "ActiveState") == "active", "the activation started the sweep timer that step 6 stopped")
        ok(all(int(state(app, unit, "ConditionTimestampMonotonic")) > tried[unit] and state(app, unit, "ConditionResult") == "no" and state(app, unit, "ActiveState") == "inactive" for unit in BACKUP_TIMERS),
           "the activation tried to start the backup timers too; only their hold marker kept them off")
        app.succeed("systemctl stop terrace-cms-sweep.timer")
        ok(state(app, "terrace-cms-migrate.service", "TimeoutStartUSec") == "1min", "the failing build's migration time limit is 60 s (the drill's change; the module's is 180 s)")
        ok(marker() == OLD_PIN and ledger() == OLD_MIGRATIONS, "before the migration: old marker, old ledger")

    with subtest("A1: a migration that fails inside bin/migrate (a conflicting object in the database)"):
        # A leftover object with the name the new migration creates (as a hand-made experiment could leave), so the
        # release's own migration fails on its first statement.
        sql("create type public.enum_roster_runs_status as enum ('drill-leftover')")
        mark = app.succeed("date +%s").strip()
        migrate_rc, invocation = migrate_invocation("A1", block=True)
        ok(migrate_rc != 0 and state(app, "terrace-cms-migrate.service", "Result") == "exit-code", f"A1: the migration failed (exit {migrate_rc}, Result exit-code)")
        # This run's lines: the unit in its journal namespace since just before this start (the fifth run of the drill
        # found no line under _SYSTEMD_INVOCATION_ID in the namespace journal; the dump below says what the entries carry).
        lines = "journalctl --namespace=terrace-apps -u terrace-cms-migrate.service --since @" + mark + " -o cat --no-pager"
        try:
            app.wait_until_succeeds("journalctl --namespace=terrace-apps --sync >/dev/null 2>&1 || true; " + lines
                                    + " | grep -qF 'CMS release migration failed; inspect the protected runtime configuration and database state.'", timeout=60)
        except Exception:
            print("UPDATE-DRILL-DIAGNOSTICS " + json.dumps({"invocation": invocation, "sync": app.execute("journalctl --namespace=terrace-apps --sync 2>&1; echo exit $?")[1],
                  "lines": app.execute(lines + " 2>&1")[1], "entries": app.execute("journalctl --namespace=terrace-apps -u terrace-cms-migrate.service -n 3 -o json --no-pager 2>&1")[1],
                  "system": app.execute("journalctl -u terrace-cms-migrate.service --since @" + mark + " -o cat --no-pager 2>&1")[1]}))
            raise
        ok(app.succeed(lines).count("CMS release migration failed") == 1, "A1: this run's journal says 'CMS release migration failed' once (the cause is hidden by the tool)")
        ok(marker() == OLD_PIN and ledger() == OLD_MIGRATIONS, "A1: the marker holds the old revision and the ledger lists no new migration")
        ok(tables() == tables_before and sql("select to_regclass('public.roster_runs') is null") == "t" and data() == before_upgrade, "A1: no table added, every table and media file unchanged (the migration's transaction rolled back)")
        site_down("A1")
        status = app.succeed("terrace-production status")
        ok(f"running system: {NEXT} (Womb {NEXT_REV}, website {NEW_PIN})" in status and f"previous generation: 5 {GOOD2} (Womb {OLD_REV}, website {OLD_PIN})" in status
           and "WARNING" not in status and "switched; down until the CMS migration" in status, f"A1: status shows nothing of the failed migration: {status}")
        # What the tool allows: with the ledger unchanged a rollback to the previous release passes every check (the
        # decision between fixing forward and restoring is the operator's); activating the running system is refused.
        out = dry_run(app, "terrace-production rollback", "A1: a rollback to the previous release passes every check after the failed migration")
        ok(f"Checks passed; rollback {GOOD2} (Womb {OLD_REV}, website {OLD_PIN})" in out, "A1: the dry run targets generation 5, the previous release")
        refused(app, "terrace-production activate " + NEXT, "is already the running system", "A1: activating the running system again")
        sql("drop type public.enum_roster_runs_status")

    with subtest("A2: a migration killed at its time limit while waiting for a lock, before its commit"):
        lock_holder("payload_locked_documents_rels")
        migrate_invocation("A2", block=False)
        # The new migration's statements wait for the lock (pg_stat_activity shows its text).
        app.wait_until_succeeds("test \"$(runuser -u postgres -- psql -XAt -d postgres -c " + shlex.quote(
            "select count(*) from pg_stat_activity where usename = 'terrace_cms' and wait_event_type = 'Lock' and query like '%CREATE TABLE \"roster_runs\"%'") + ")\" = 1", timeout=120)
        wait_timed_out("A2")
        # The database session outlives the killed unit: PostgreSQL does not notice the gone client while it waits.
        ok(cms_backends("and wait_event_type = 'Lock'") == 1, "A2: after the unit was killed, its database session still waits for the lock")
        release_lock()
        app.wait_until_succeeds("test \"$(runuser -u postgres -- psql -XAt -d postgres -c " + shlex.quote("select count(*) from pg_stat_activity where usename = 'terrace_cms'") + ")\" = 0", timeout=120)
        ok(cms_backends() == 0, "A2: once the lock was released the orphaned session ended by itself")
        ok(marker() == OLD_PIN and ledger() == OLD_MIGRATIONS and tables() == tables_before and data() == before_upgrade, "A2: old marker, old ledger, no table added, data unchanged (nothing committed)")
        site_down("A2")

    with subtest("A3 (H9): a migration killed at its time limit after its commit, before the marker"):
        # PostgreSQL's own way to hold a committed transaction: a synchronous standby that never answers, for the CMS
        # role's sessions only (everything else commits locally). The migration commits, then waits for the standby.
        sql("alter role terrace_cms set synchronous_commit = 'on'", "postgres")
        sql("alter system set synchronous_commit = 'local'", "postgres")
        sql("alter system set synchronous_standby_names = 'terrace_drill_absent_standby'", "postgres")
        sql("select pg_reload_conf()", "postgres")
        app.wait_until_succeeds("test \"$(runuser -u postgres -- psql -XAt -d postgres -c 'show synchronous_standby_names')\" = terrace_drill_absent_standby", timeout=60)
        migrate_invocation("A3", block=False)
        app.wait_until_succeeds("test \"$(runuser -u postgres -- psql -XAt -d postgres -c " + shlex.quote(
            "select count(*) from pg_stat_activity where usename = 'terrace_cms' and wait_event = 'SyncRep'") + ")\" = 1", timeout=120)
        wait_timed_out("A3")
        ok(cms_backends("and wait_event = 'SyncRep'") == 1, "A3: after the unit was killed, its committed transaction still waits for the standby")
        ok(ledger() == OLD_MIGRATIONS, "A3: while it waits, other sessions do not see the migration yet")
        sql("alter system reset synchronous_standby_names", "postgres")
        sql("alter system reset synchronous_commit", "postgres")
        sql("alter role terrace_cms reset synchronous_commit", "postgres")
        sql("select pg_reload_conf()", "postgres")
        app.wait_until_succeeds("test \"$(runuser -u postgres -- psql -XAt -d postgres -c " + shlex.quote("select count(*) from pg_stat_activity where usename = 'terrace_cms'") + ")\" = 0", timeout=120)
        ok(ledger() == NEW_MIGRATIONS and marker() == OLD_PIN, f"A3 (H9): the ledger lists the new migration {ADDED}, the marker still the old revision")
        ok(sql("select to_regclass('public.roster_runs') is not null") == "t", "A3: the migration's tables exist")
        site_down("A3")
        status = app.succeed("terrace-production status")
        ok(f"running system: {NEXT}" in status and "WARNING" not in status, f"A3: status shows nothing of the half-finished migration: {status}")
        # What the tool refuses now: every way back to the previous release, with or without a flag.
        for command in ["terrace-production rollback --dry-run", "terrace-production rollback", "terrace-production rollback --cms-stays-down",
                        "terrace-production rollback --generation 1", "terrace-production activate " + GOOD2]:
            refused(app, command, H9, f"A3: {command}")
        refused(app, "terrace-production activate " + NEXT, "is already the running system", "A3: activating the running system again")

    with subtest("A4: fixing forward: the corrected build signed, copied, activated, with the failed migration unit reset by the command"):
        deploy("app", FIXED)
        out = dry_run(app, "terrace-production activate " + FIXED, "A4: the corrected build passes every check")
        ok(f"Checks passed; activate {FIXED} (Womb {FIXED_REV}, website {NEW_PIN})" in out, "A4: the dry run names the corrected build")
        ok(state(app, "terrace-cms-migrate.service", "ActiveState") == "failed", "A4: the migration unit is still failed from A3")
        # Activated as documented, with no reset by hand: the command records the failed migration unit and resets it,
        # as it does the CMS and website, so the earlier failure does not count against this switch.
        out = operator.succeed(ssh("app") + "sudo terrace-production activate " + FIXED)
        ok(running(app) == FIXED and generation(app) == "8" and "THE SITE IS DOWN until the CMS migration" in out, f"A4: the corrected build runs as generation 8: {out.strip()[-300:]!r}")
        started, record = records(app)[-2:]
        ok(started["phase"] == "started" and "terrace-cms-migrate.service" in started["failedBefore"], f"A4: the started record names the failed migration unit in failedBefore: {started}")
        finished(record, "activate", "7", "8", FIXED, "switched; down until the CMS migration: " + " ".join(GATED))
        gated_record(record, "A4: activation of the corrected build")
        ok(record["preexistingFailedUnits"] == [] and state(app, "terrace-cms-migrate.service", "ActiveState") == "inactive", "A4: the migration unit was reset, nothing left failed from before")

    with subtest("A4: fixing forward: the corrected build's migration outlasts the failing build's limit"):
        app.wait_until_succeeds("systemctl is-active --quiet terrace-cms-sweep.timer", timeout=60)
        ok(state(app, "terrace-cms-sweep.timer", "ActiveState") == "active", "A4: this activation also started the sweep timer again")
        app.succeed("systemctl stop terrace-cms-sweep.timer")
        ok(state(app, "terrace-cms-migrate.service", "TimeoutStartUSec") == "3min", "A4: the corrected build's migration time limit is the module's 180 s")
        # The same kind of wait that killed A2, now longer than the failing build's limit.
        lock_holder("payload_migrations")
        uptime = int(float(app.succeed("cut -d' ' -f1 /proc/uptime").strip()))
        migrate_invocation("A4", block=False)
        app.wait_until_succeeds("test \"$(runuser -u postgres -- psql -XAt -d postgres -c " + shlex.quote(
            "select count(*) from pg_stat_activity where usename = 'terrace_cms' and wait_event_type = 'Lock'") + ")\" = 1", timeout=120)
        app.wait_until_succeeds(f"test $(( $(cut -d. -f1 /proc/uptime) - {uptime} )) -ge 75", timeout=300)
        ok(state(app, "terrace-cms-migrate.service", "ActiveState") == "activating" and cms_backends("and wait_event_type = 'Lock'") == 1,
           "A4: 75 s after its start the migration still waits for the lock, unkilled")
        release_lock()
        app.wait_until_succeeds("test \"$(systemctl show terrace-cms-migrate.service -p ActiveState --value)\" = inactive", timeout=300)
        ok(state(app, "terrace-cms-migrate.service", "Result") == "success", "A4: the migration succeeded once the lock was released")
        ok(marker() == NEW_PIN and ledger() == NEW_MIGRATIONS, "A4: the marker holds the new revision; the ledger is the new pin's (the run found nothing left to apply)")
        app.succeed("systemctl reset-failed terrace-cms.service terrace-web.service && systemctl start terrace-cms.service terrace-web.service", timeout=300)
        app.succeed("systemctl start terrace-cms-sweep.timer")
        cms_serves("A4: after fixing forward")
        added = sorted(set(tables()) - set(tables_before))
        ok(data() == before_upgrade and set(tables_before) <= set(tables()) and added == ["roster_run_items", "roster_runs"], f"A4: data unchanged; the migration added exactly its two tables: {added}")
        ok(app.succeed("systemctl list-units --state=failed --plain --no-legend").strip() == "", "A4: no failed unit")

    with subtest("stop owned guests"):
        app.shutdown()
        operator.shutdown()
    print("UPDATE-DRILL-CHECKS " + json.dumps(checks))
    print("UPDATE-DRILL-PROOF " + json.dumps({"assertions": len(checks), "seconds": round(time.time() - started_at, 2)}))
  '';
}
