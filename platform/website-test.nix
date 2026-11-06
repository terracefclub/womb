{
  pkgs,
  release,
  module,
}: let
  inputs = "/var/lib/terrace-website-inputs";
  media = "/var/lib/terrace-cms-media";
  secrets = "/var/lib/website-backup-secrets";
  restored = "/var/lib/terrace-website-recovery";
  repository = "/home/restic/website-repository";
  acceptEscrow = pkgs.writeShellScript "accept-website-test-escrow" ''
    set -euo pipefail
    umask 077
    ${pkgs.coreutils}/bin/install -d -m 0700 ${secrets}
    ${pkgs.coreutils}/bin/cat >${secrets}/repository-password
    ${pkgs.coreutils}/bin/chmod 0400 ${secrets}/repository-password
  '';
  common = {lib, ...}: {
    imports = [module];
    virtualisation = {
      memorySize = 5120;
      cores = 2;
    };
    terrace.website = {
      enable = true;
      webPackage = release.web;
      cmsPackage = release.cms;
      revision = release.revision;
    };
    terrace.backup = {
      enable = true;
      check.readDataSubset = "100%";
      targets.primary = {
        enable = true;
        passwordFile = "${secrets}/repository-password";
      };
    };
    systemd.timers = lib.genAttrs ["terrace-backup" "terrace-backup-check" "terrace-backup-freshness"] (_: {wantedBy = lib.mkForce [];});
    environment.systemPackages = [pkgs.curl pkgs.jq pkgs.openssh pkgs.netcat-openbsd pkgs.postgresql_18];
  };
in {
  name = "terrace-website-recovery";
  nodes = {
    source = {lib, ...}: {
      imports = [common];
      # Keep old app PIDs alive during stop so Conflicts without After would race.
      systemd.services.terrace-cms.serviceConfig.ExecStop = "${pkgs.coreutils}/bin/sleep 3";
      systemd.services.terrace-web.serviceConfig.ExecStop = "${pkgs.coreutils}/bin/sleep 3";
      systemd.services.terrace-cms-migrate.serviceConfig.ExecStartPre = lib.mkAfter [
        "+${pkgs.writeShellScript "website-test-stopped-before-migration" ''
          set -euo pipefail
          if test -f /var/lib/terrace-cms/old-pids; then
            while read -r pid; do
              test "$pid" -gt 1
              test ! -e "/proc/$pid" || { echo 'Old app survived into migration.' >&2; exit 1; }
            done </var/lib/terrace-cms/old-pids
            touch /var/lib/terrace-cms/stop-verified
          fi
        ''}"
      ];
      terrace.backup.targets.primary = {
        repository = "sftp:restic@recovery:website-repository";
        sftp = {
          identityFile = "${secrets}/id_ed25519";
          knownHostsFile = "${secrets}/known_hosts";
        };
      };
      systemd.services.website-test-inputs = {
        requiredBy = ["terrace-website-inputs.service"];
        before = ["terrace-website-inputs.service"];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          UMask = "0077";
        };
        script = ''
          mkdir -m 0700 -p ${inputs}
          payload=$(${pkgs.openssl}/bin/openssl rand -hex 32)
          editor=$(${pkgs.openssl}/bin/openssl rand -hex 32)
          resource=$(${pkgs.openssl}/bin/openssl rand -hex 32)
          member=$(${pkgs.openssl}/bin/openssl rand -hex 32)
          browser=$(${pkgs.openssl}/bin/openssl rand -hex 32)
          cat >${inputs}/cms.env <<EOF
          PAYLOAD_SECRET=$payload
          CMS_ORIGIN=http://127.0.0.1:13048
          CMS_AUTH_ISSUER=http://127.0.0.1:8088/realms/terrace
          CMS_AUTH_CLIENT_ID=terrace-cms
          CMS_AUTH_AUDIENCE=terrace-editorial
          CMS_AUTH_CLIENT_SECRET=$editor
          CMS_AUTH_INTROSPECTION_CLIENT_ID=terrace-editorial
          CMS_AUTH_INTROSPECTION_SECRET=$resource
          MEMBER_AUTH_ISSUER=http://127.0.0.1:8088/realms/terrace
          MEMBER_AUTH_CLIENT_ID=terrace-web
          MEMBER_AUTH_AUDIENCE=terrace-community
          MEMBER_AUTH_INTROSPECTION_CLIENT_ID=terrace-community
          MEMBER_AUTH_INTROSPECTION_SECRET=$member
          EOF
          cat >${inputs}/web.env <<EOF
          CMS_ORIGIN=http://127.0.0.1:13048
          CONTENT_SOURCE=cms
          ORIGIN=http://127.0.0.1:14294
          MEMBER_AUTH_ORIGIN=http://127.0.0.1:14294
          MEMBER_AUTH_ISSUER=http://127.0.0.1:8088/realms/terrace
          MEMBER_AUTH_CLIENT_ID=terrace-web
          MEMBER_AUTH_CLIENT_SECRET=$browser
          EOF
          chmod 0400 ${inputs}/*.env
        '';
      };
    };
    recovery = {lib, ...}: {
      imports = [common];
      terrace.website = {
        manageDatabase = false;
        databaseSocketDirectory = "${restored}/pgsock";
      };
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
      systemd.services.terrace-cms = {
        wantedBy = lib.mkForce [];
        serviceConfig = {
          User = lib.mkForce "terrace-restore";
          Group = lib.mkForce "terrace-restore";
        };
      };
      systemd.services.terrace-cms-migrate.serviceConfig = {
        User = lib.mkForce "terrace-restore";
        Group = lib.mkForce "terrace-restore";
      };
      systemd.services.terrace-web.wantedBy = lib.mkForce [];
    };
  };
  testScript = ''
    import json, shlex, time
    start_all()
    source.wait_for_unit("postgresql.target")
    recovery.wait_for_unit("sshd.service")

    def timed(machine, command):
        before = time.monotonic()
        machine.succeed(command)
        return time.monotonic() - before

    def sql(machine, query, isolated=False):
        prefix = "runuser -u terrace-restore -- psql -h ${restored}/pgsock -U postgres" if isolated else "runuser -u postgres -- psql"
        return machine.succeed(prefix + " -X -A -t -d terrace_cms -c " + shlex.quote(query) + " 2>/dev/null").strip()

    def api(machine, route):
        return json.loads(machine.succeed("curl --fail --silent --max-time 10 " + shlex.quote("http://127.0.0.1:13048" + route)))

    def data_signature(machine, isolated=False):
        return {table: sql(machine, f"select md5(string_agg(to_jsonb(t)::text, chr(10) order by to_jsonb(t)::text)) from {table} t", isolated)
                for table in ["events", "_events_v", "media", "users", "members", "payload_migrations"]}

    def assert_content(machine):
        docs = api(machine, "/api/events?limit=10&draft=true")["docs"]
        assert [doc["slug"] for doc in docs] == ["synthetic-host-event"]
        assert api(machine, "/api/media?limit=10")["totalDocs"] == 1
        machine.succeed("curl --fail --silent http://127.0.0.1:14294/events/synthetic-host-event/ | grep -F 'Synthetic hosted event' >/dev/null")
        machine.succeed("test $(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:13048/api/media/file/private.png) = 403")
        machine.succeed("curl --fail --silent http://127.0.0.1:13048/api/media/file/public.png | cmp - ${media}/public.png")
        assert api(machine, "/api/members/me").get("user") is None
        machine.succeed("test $(curl --silent --output /dev/null --write-out '%{http_code}' 'http://127.0.0.1:13048/api/members?limit=1') = 403")

    with subtest("explicit migration gate on actual immutable bundles"):
        source.wait_until_fails("systemctl is-active --quiet terrace-cms.service")
        assert sql(source, "select count(*) from pg_tables where schemaname='public'") == "0"
        migration_seconds = timed(source, "systemctl start terrace-cms-migrate.service")
        before_ledger = sql(source, "select name || ':' || batch from payload_migrations order by id")
        assert len(before_ledger.splitlines()) == 6
        source.succeed("systemctl start terrace-cms-migrate.service")
        assert sql(source, "select name || ':' || batch from payload_migrations order by id") == before_ledger
        for change, undo in [
            ("chmod 0640 ${inputs}/cms.env", "chmod 0400 ${inputs}/cms.env"),
            ("echo CMS_BOOTSTRAP_TEST=0 >>${inputs}/cms.env", "sed -i '/^CMS_BOOTSTRAP_TEST=/d' ${inputs}/cms.env"),
            ("sed -i '/^CMS_AUTH_ISSUER=/d' ${inputs}/cms.env", "echo CMS_AUTH_ISSUER=http://127.0.0.1:8088/realms/terrace >>${inputs}/cms.env"),
        ]:
            source.succeed(change)
            source.fail("systemctl start terrace-cms-migrate.service")
            assert sql(source, "select name || ':' || batch from payload_migrations order by id") == before_ledger
            source.succeed(undo + "; systemctl reset-failed terrace-cms-migrate.service terrace-website-inputs.service")
        source.succeed("systemctl reset-failed terrace-cms.service terrace-web.service && systemctl start terrace-cms.service terrace-web.service")
        source.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value | grep -v '^$' >/var/lib/terrace-cms/old-pids && systemctl start terrace-cms-migrate.service && test -f /var/lib/terrace-cms/stop-verified")
        source.fail("systemctl is-active --quiet terrace-cms.service")
        source.fail("systemctl is-failed --quiet terrace-cms.service")
        source.fail("systemctl is-active --quiet terrace-web.service")
        assert sql(source, "select name || ':' || batch from payload_migrations order by id") == before_ledger
        source.succeed("systemctl start terrace-cms.service terrace-web.service")

    with subtest("synthetic actual schema, published/private data and native image cache"):
        # Test-only fixture SQL: no account bootstrap, password or provider request.
        source.succeed("install -m 0600 /dev/null ${media}/public.png && echo 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGPgUbL4DwACCgFmeGgpMwAAAABJRU5ErkJggg==' | base64 -d >${media}/public.png && cp ${media}/public.png ${media}/private.png && chown terrace_cms:terrace_cms ${media}/*.png")
        sql(source, "insert into media(alt,public,filename,mime_type,filesize,width,height) values ('Synthetic public',true,'public.png','image/png',70,1,1),('Synthetic private',false,'private.png','image/png',70,1,1)")
        sql(source, "insert into events(slug,title,summary,starts_at,category,location_name,cover_image_id,_status) values ('synthetic-host-event','Synthetic hosted event','User-free host recovery fixture',now(),'community','Synthetic room',1,'published'),('synthetic-secret-draft','Synthetic private draft','Unpublished fixture',now(),'community','Synthetic room',null,'draft')")
        sql(source, "insert into _events_v(parent_id,version_slug,version_title,version_summary,version_starts_at,version_category,version_location_name,version_cover_image_id,version_updated_at,version_created_at,version__status,latest) select id,slug,title,summary,starts_at,category::text::enum__events_v_version_category,location_name,cover_image_id,updated_at,created_at,_status::text::enum__events_v_version_status,true from events")
        assert sql(source, "select count(*) from users") == "0"
        assert sql(source, "select count(*) from members") == "0"
        assert_content(source)
        source.succeed("curl --fail --silent 'http://127.0.0.1:13048/_next/image?url=%2Fapi%2Fmedia%2Ffile%2Fpublic.png&w=64&q=75' >/dev/null && test -n \"$(find /var/cache/terrace-cms/images -type f -print -quit)\"")
        source.succeed("test $(curl --silent --output /dev/null --write-out '%{http_code}' 'http://127.0.0.1:13048/_next/image?url=%2Fapi%2Fmedia%2Ffile%2Fprivate.png&w=64&q=75') != 200")
        source.succeed("ss -ltn | grep -F '127.0.0.1:13048' >/dev/null && ss -ltn | grep -F '127.0.0.1:14294' >/dev/null")
        recovery.fail("curl --silent --fail --connect-timeout 2 http://source:13048/api/events")
        source.fail("runuser -u terrace_web -- test -r ${inputs}/cms.env")
        source.fail("runuser -u terrace_web -- test -r ${media}/private.png")
        source.fail("runuser -u terrace_cms -- touch ${release.cms}/forbidden-write")
        source.succeed("test $(systemctl show terrace-cms.service -p MemoryMax --value) = 2147483648 && test $(systemctl show terrace-web.service -p MemoryMax --value) = 805306368")
        source.succeed("systemd-run --wait --unit=terrace-log-proof --property=LogNamespace=terrace-apps ${pkgs.coreutils}/bin/echo SYNTHETIC_PRIVATE_LOG_MARKER >/dev/null 2>&1")
        source.wait_until_succeeds("journalctl --namespace=terrace-apps -u terrace-log-proof --no-pager -o cat | grep -F SYNTHETIC_PRIVATE_LOG_MARKER >/dev/null")
        source.fail("runuser -u nobody -- journalctl --namespace=terrace-apps --no-pager -o cat 2>/dev/null | grep -F SYNTHETIC_PRIVATE_LOG_MARKER >/dev/null")
        source.fail("journalctl --no-pager -o cat | grep -Fx SYNTHETIC_PRIVATE_LOG_MARKER >/dev/null")
        source.fail("runuser -u terrace_web -- psql -h /run/postgresql -U terrace_cms -d terrace_cms -c 'select 1' >/dev/null 2>&1")
        source_pids = source.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value").strip()
        before_rows = data_signature(source)
        before_inputs = source.succeed("cd ${inputs} && sha256sum *.env 2>/dev/null").strip()
        table_count = int(sql(source, "select count(*) from pg_tables where schemaname !~ '^pg_' and schemaname != 'information_schema'"))

    with subtest("encrypted backup automatically includes real CMS and protected inputs"):
        source.succeed("install -d -m 0700 ${secrets} && ssh-keygen -q -t ed25519 -N \"\" -f ${secrets}/id_ed25519 && head -c 32 /dev/urandom | base64 >${secrets}/repository-password && chmod 0400 ${secrets}/id_ed25519 ${secrets}/repository-password")
        pub = source.succeed("cat ${secrets}/id_ed25519.pub").strip()
        recovery.succeed("install -d -o restic -g users -m 0700 /home/restic/.ssh && echo " + shlex.quote(pub) + " >/home/restic/.ssh/authorized_keys && chown restic:users /home/restic/.ssh/authorized_keys && chmod 0600 /home/restic/.ssh/authorized_keys")
        recovery.succeed("install -d -m 0700 /root/.ssh && echo " + shlex.quote('restrict,command="${acceptEscrow}" ' + pub) + " >>/root/.ssh/authorized_keys && chmod 0600 /root/.ssh/authorized_keys")
        source.succeed("ssh-keyscan -t ed25519 recovery >${secrets}/known_hosts 2>/dev/null && ssh -i ${secrets}/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${secrets}/known_hosts root@recovery <${secrets}/repository-password >/dev/null 2>&1")
        source.succeed("terrace-backup init primary >/dev/null 2>&1")
        backup_seconds = timed(source, "systemctl start terrace-backup.service")
        check_seconds = timed(source, "systemctl start terrace-backup-check.service")
        snapshots = json.loads(source.succeed("terrace-backup restic primary snapshots --json 2>/dev/null"))
        assert len(snapshots) == 1
        snapshot = snapshots[0]["id"]
        recovery.fail("jq . ${repository}/config >/dev/null 2>&1")
        assert data_signature(source) == before_rows

    with subtest("recover database, uploads and runtime inputs into disposable guest"):
        restore_seconds = timed(recovery, f"umask 077; terrace-backup-restore --target primary --snapshot {snapshot} --dest ${restored} --keep-running >/var/tmp/website-restore.log 2>&1")
        assert recovery.succeed("stat -c %a /var/tmp/website-restore.log").strip() == "600"
        recovery.succeed("grep -qx 'RECONCILIATION OK' ${restored}/reconciliation.txt")
        assert data_signature(recovery, True) == before_rows
        assert int(sql(recovery, "select count(*) from pg_tables where schemaname !~ '^pg_' and schemaname != 'information_schema'", True)) == table_count
        recovery.succeed("test ! -e ${inputs} && cp -a ${restored}/files${inputs} ${inputs} && test -z \"$(ls -A ${media})\" && cp -a ${restored}/files${media}/. ${media}/ && chown -R terrace-restore:terrace-restore ${media}")
        assert recovery.succeed("cd ${inputs} && sha256sum *.env 2>/dev/null").strip() == before_inputs
        recovery.fail("runuser -u nobody -- test -r ${inputs}/cms.env")
        recovery.fail("nc -z -w 2 127.0.0.1 5432")
        verify_start = time.monotonic()
        recovery.succeed("systemctl start terrace-cms-migrate.service && systemctl start terrace-cms.service terrace-web.service")
        assert sql(recovery, "select name || ':' || batch from payload_migrations order by id", True) == before_ledger
        assert_content(recovery)
        verify_seconds = time.monotonic() - verify_start
        assert source.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value").strip() == source_pids
        assert data_signature(source) == before_rows
        assert_content(source)
        peaks = {unit: source.succeed(f"systemctl show {unit}.service -p MemoryPeak --value").strip() for unit in ["terrace-cms", "terrace-web", "postgresql"]}
        recovery.succeed("systemctl stop terrace-web.service terrace-cms.service && terrace-backup-restore --stop ${restored} >/dev/null 2>&1")
        recovery.fail("systemctl is-failed --quiet terrace-cms.service")
        recovery.fail("test -e ${restored}/pgdata/postmaster.pid")
        print(f"Actual CMS schema: {table_count} tables; migrations {migration_seconds:.2f}s; backup {backup_seconds:.2f}s; complete check {check_seconds:.2f}s; restore {restore_seconds:.2f}s; recovered HTTP verification {verify_seconds:.2f}s; source peak bytes {peaks}")
  '';
}
