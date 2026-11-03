{
  pkgs,
  identityModule,
  backupModule,
}: let
  inputs = "/var/lib/identity-test-inputs";
  secrets = "/var/lib/identity-backup-secrets";
  restored = "/var/tmp/identity-recovery";
  repository = "/home/restic/identity-repository";
  certificate = pkgs.runCommand "identity-recovery-test-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir -p $out
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout $out/key.pem -out $out/cert.pem \
      -subj '/CN=identity' -addext 'subjectAltName=DNS:identity' 2>/dev/null
  '';
  realm = pkgs.writeText "terrace-realm.json" (builtins.toJSON {
    realm = "terrace";
    enabled = true;
    displayName = "Recovery hosting fixture";
    registrationAllowed = false;
    sslRequired = "external";
    users = [];
    clients = [];
  });
  # Test-only escrow delivery over SSH stdin. Secret bytes never enter driver logs or argv.
  acceptEscrow = pkgs.writeShellScript "accept-recovery-test-escrow" ''
    set -euo pipefail
    umask 077
    ${pkgs.coreutils}/bin/install -d -m 0700 ${secrets}
    ${pkgs.coreutils}/bin/cat >${secrets}/repository-password
    ${pkgs.coreutils}/bin/chmod 0400 ${secrets}/repository-password
  '';
  common = {lib, ...}: {
    imports = [identityModule backupModule];
    virtualisation = {
      memorySize = 3584;
      cores = 2;
    };
    terrace.identity = {
      enable = true;
      hostname = "identity";
      realmFile = "${inputs}/terrace-realm.json";
      databasePasswordFile = "${inputs}/db-password";
      certificateFile = "${inputs}/cert.pem";
      certificateKeyFile = "${inputs}/key.pem";
    };
    terrace.backup = {
      enable = true;
      extraPaths.identity-inputs = inputs;
      postgresql.package = pkgs.postgresql_18;
      check.readDataSubset = "100%";
      targets.primary = {
        enable = true;
        passwordFile = "${secrets}/repository-password";
      };
    };
    # The test invokes every operation explicitly; no scheduled/background backup races.
    systemd.timers = lib.genAttrs ["terrace-backup" "terrace-backup-check" "terrace-backup-freshness" "terrace-identity-health"] (_: {
      wantedBy = lib.mkForce [];
    });
    security.pki.certificateFiles = ["${certificate}/cert.pem"];
    environment.systemPackages = [pkgs.curl pkgs.jq pkgs.openssh pkgs.netcat-openbsd pkgs.postgresql_18];
  };
in {
  name = "terrace-identity-recovery";
  nodes = {
    source = {...}: {
      imports = [common];
      services.postgresql.package = pkgs.postgresql_18;
      terrace.backup = {
        postgresql.databases = ["keycloak"];
        targets.primary = {
          repository = "sftp:restic@recovery:identity-repository";
          sftp = {
            identityFile = "${secrets}/id_ed25519";
            knownHostsFile = "${secrets}/known_hosts";
          };
        };
      };
      systemd.services.identity-test-inputs = {
        requiredBy = ["keycloak.service" "keycloakPostgreSQLInit.service" "nginx.service"];
        before = ["keycloak.service" "keycloakPostgreSQLInit.service" "nginx.service"];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          UMask = "0077";
        };
        script = ''
          mkdir -m 0700 -p ${inputs}
          ${pkgs.openssl}/bin/openssl rand -base64 32 >${inputs}/db-password
          cp ${realm} ${inputs}/terrace-realm.json
          cp ${certificate}/{cert,key}.pem ${inputs}/
          chmod 0600 ${inputs}/*
        '';
      };
    };
    recovery = {lib, ...}: {
      imports = [common];
      terrace.backup.targets.primary.repository = repository;
      services.openssh = {
        enable = true;
        extraConfig = ''
          Match User restic
            ForceCommand internal-sftp
            AllowTcpForwarding no
            PermitTTY no
        '';
      };
      users.users.restic.isNormalUser = true;
      # Supported native NixOS/JDBC socket path, only for the disposable restored cluster.
      services.keycloak = {
        database = {
          host = lib.mkForce "${restored}/pgsock";
          createLocally = lib.mkForce false;
        };
        plugins = with pkgs.keycloak.plugins; [junixsocket-common junixsocket-native-common];
      };
      systemd.services.keycloak = {
        wantedBy = lib.mkForce [];
        serviceConfig = {
          User = lib.mkForce "terrace-restore";
          Group = lib.mkForce "terrace-restore";
          DynamicUser = lib.mkForce false;
        };
      };
      systemd.services.nginx.wantedBy = lib.mkForce [];
      systemd.services.postgresql.enable = lib.mkForce false;
      systemd.services.keycloakPostgreSQLInit.enable = lib.mkForce false;
    };
  };
  testScript = ''
    import json, shlex, time

    start_all()
    source.wait_for_unit("keycloak.service", timeout=300)
    source.wait_for_unit("nginx.service")
    recovery.wait_for_unit("sshd.service")
    recovery.wait_for_open_port(22)
    source_pid = source.succeed("systemctl show keycloak.service -p MainPID --value").strip()

    def timed(machine, command):
        start = time.monotonic()
        machine.succeed(command)
        return time.monotonic() - start

    def public(machine, path):
        return json.loads(machine.succeed(f"curl --fail --silent --max-time 10 --resolve identity:443:127.0.0.1 https://identity/realms/terrace/{path}"))

    def sql(machine, query, isolated=False):
        prefix = "runuser -u terrace-restore -- psql -h ${restored}/pgsock -U terrace-restore" if isolated else "runuser -u postgres -- psql"
        return machine.succeed(prefix + " -X -A -t -d keycloak -c " + shlex.quote(query) + " 2>/dev/null").strip()

    def critical_rows(machine, isolated=False):
        # Hash actual identity configuration/key rows without exposing their contents.
        return {table: sql(machine, f"select md5(string_agg(to_jsonb(t)::text, chr(10) order by to_jsonb(t)::text)) from {table} t", isolated)
                for table in ["realm", "client", "component", "component_config", "user_entity", "credential", "keycloak_role"]}

    def input_hashes(machine, path):
        return machine.succeed(f"cd {path} && sha256sum * 2>/dev/null").strip()

    with subtest("capture real Keycloak schema and establish private test escrow"):
        issuer = public(source, ".well-known/openid-configuration")["issuer"]
        keys = public(source, "protocol/openid-connect/certs")["keys"]
        assert issuer == "https://identity/realms/terrace" and keys
        realm_id = sql(source, "select id from realm where name='terrace'")
        before_rows = critical_rows(source)
        before_inputs = input_hashes(source, "${inputs}")
        table_count = int(sql(source, "select count(*) from pg_tables where schemaname='public'"))
        assert table_count > 50
        assert sql(source, "select count(*) from user_entity") == "0"
        source.succeed("install -d -m 0700 ${secrets} && ssh-keygen -q -t ed25519 -N \"\" -f ${secrets}/id_ed25519 && head -c 32 /dev/urandom | base64 >${secrets}/repository-password && chmod 0400 ${secrets}/id_ed25519 ${secrets}/repository-password")
        pub = source.succeed("cat ${secrets}/id_ed25519.pub").strip()
        recovery.succeed("install -d -o restic -g users -m 0700 /home/restic/.ssh && echo " + shlex.quote(pub) + " > /home/restic/.ssh/authorized_keys && chown restic:users /home/restic/.ssh/authorized_keys && chmod 0600 /home/restic/.ssh/authorized_keys")
        forced = 'restrict,command="${acceptEscrow}" ' + pub
        recovery.succeed("install -d -m 0700 /root/.ssh && echo " + shlex.quote(forced) + " >>/root/.ssh/authorized_keys && chmod 0600 /root/.ssh/authorized_keys")
        source.succeed("ssh-keyscan -t ed25519 recovery >${secrets}/known_hosts 2>/dev/null")
        source.succeed("ssh -i ${secrets}/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${secrets}/known_hosts root@recovery <${secrets}/repository-password >/dev/null 2>&1")
        recovery.fail("runuser -u restic -- test -r ${secrets}/repository-password")

    with subtest("encrypted off-host backup and complete repository check"):
        source.succeed("terrace-backup init primary >/dev/null 2>&1")
        backup_seconds = timed(source, "systemctl start terrace-backup.service")
        check_seconds = timed(source, "systemctl start terrace-backup-check.service")
        snapshots = json.loads(source.succeed("terrace-backup restic primary snapshots --json 2>/dev/null"))
        assert len(snapshots) == 1
        snapshot = snapshots[0]["id"]
        recovery.fail("jq . ${repository}/config >/dev/null 2>&1")
        recovery.fail("grep -rqaF 'Recovery hosting fixture' ${repository}")
        source.fail("test -e /var/lib/terrace-backup/dumps")
        assert critical_rows(source) == before_rows

    with subtest("recover actual schema and protected inputs on fresh guest"):
        restore_seconds = timed(recovery, f"umask 077; terrace-backup-restore --target primary --snapshot {snapshot} --dest ${restored} --keep-running > /var/tmp/identity-restore-report.log 2>&1")
        assert recovery.succeed("stat -c %a /var/tmp/identity-restore-report.log").strip() == "600"
        recovery.succeed("grep -qx 'RECONCILIATION OK' ${restored}/reconciliation.txt")
        assert critical_rows(recovery, True) == before_rows
        assert sql(recovery, "select id from realm where name='terrace'", True) == realm_id
        assert int(sql(recovery, "select count(*) from pg_tables where schemaname='public'", True)) == table_count
        assert input_hashes(recovery, "${restored}/files${inputs}") == before_inputs
        recovery.fail("runuser -u nobody -- test -r ${restored}/files${inputs}/db-password")
        recovery.succeed("test ! -e ${inputs} && cp -a ${restored}/files${inputs} ${inputs}")
        assert input_hashes(recovery, "${inputs}") == before_inputs
        recovery.fail("nc -z -w 2 127.0.0.1 5432")

    with subtest("serve recovered issuer and signing keys without changing source"):
        verify_start = time.monotonic()
        recovery.succeed("systemctl start keycloak.service nginx.service && systemctl start terrace-identity-health.service")
        assert public(recovery, ".well-known/openid-configuration")["issuer"] == issuer
        recovered_keys = public(recovery, "protocol/openid-connect/certs")["keys"]
        assert sorted(keys, key=lambda key: key["kid"]) == sorted(recovered_keys, key=lambda key: key["kid"])
        assert sql(recovery, "select id from realm where name='terrace'", True) == realm_id
        assert sql(recovery, "select count(*) from user_entity", True) == "0"
        assert source.succeed("systemctl show keycloak.service -p MainPID --value").strip() == source_pid
        assert public(source, ".well-known/openid-configuration")["issuer"] == issuer
        assert public(source, "protocol/openid-connect/certs")["keys"] == keys
        assert critical_rows(source) == before_rows
        assert input_hashes(source, "${inputs}") == before_inputs
        verify_seconds = time.monotonic() - verify_start
        recovery.succeed("umask 077; systemctl stop keycloak.service && terrace-backup-restore --verify-only ${restored} > /var/tmp/identity-reverify.log 2>&1 && terrace-backup-restore --stop ${restored} >/dev/null 2>&1")
        assert recovery.succeed("stat -c %a /var/tmp/identity-reverify.log").strip() == "600"
        recovery.fail("test -e ${restored}/pgdata/postmaster.pid")
        print(f"Keycloak real schema: {table_count} tables; backup {backup_seconds:.2f}s; full repository check {check_seconds:.2f}s; isolated restore {restore_seconds:.2f}s; application verification {verify_seconds:.2f}s")
  '';
}
