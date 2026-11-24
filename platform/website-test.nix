{
  pkgs,
  release,
  module,
}: let
  inputs = "/var/lib/terrace-website-inputs";
  media = "/var/lib/terrace-cms-media";
  outbox = "/var/lib/terrace-cms-outbox";
  operator = "/var/lib/terrace-cms-operator";
  importPlan = ./website-fixtures/wp-import-plan.synthetic.json;
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
      # A non-default per-address lock wait (CMS default 3000 ms), proven below with a held lock.
      lifecycleLockTimeoutMs = 500;
      # The smallest run-time bound, so the proof can wait for it (every real tool run here takes seconds).
      operatorToolMaxSeconds = 60;
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
      # Keep old app PIDs alive during stop, so a migration that did not wait for the stop would find them.
      systemd.services.terrace-cms.serviceConfig.ExecStop = "${pkgs.coreutils}/bin/sleep 3";
      systemd.services.terrace-web.serviceConfig.ExecStop = "${pkgs.coreutils}/bin/sleep 3";
      # Test-only: a sweep that stays running (activating) while the flag exists, for the accepted-migration check.
      systemd.services.terrace-cms-sweep.serviceConfig.ExecStartPre = lib.mkAfter [
        "${pkgs.writeShellScript "website-test-slow-sweep" ''
          if test -e /run/website-test-slow-sweep; then ${pkgs.coreutils}/bin/sleep 120; fi
        ''}"
      ];
      # Ordered after the module's preflight (mkOrder 2000), so these run where the migration statements would: after
      # every check and after the stop.
      systemd.services.terrace-cms-migrate.serviceConfig.ExecStartPre = lib.mkOrder 2500 [
        "+${pkgs.writeShellScript "website-test-stopped-before-migration" ''
          set -euo pipefail
          if test -f /var/lib/terrace-cms/old-pids; then
            while read -r pid; do
              test "$pid" -gt 1
              test ! -e "/proc/$pid" || { echo 'Old app survived into migration.' >&2; exit 1; }
            done </var/lib/terrace-cms/old-pids
            for unit in terrace-cms terrace-web terrace-cms-sweep; do
              state=$(${pkgs.systemd}/bin/systemctl show "$unit.service" -p ActiveState --value)
              test "$state" = inactive || test "$state" = failed || { echo "$unit is $state at migration time." >&2; exit 1; }
            done
            touch /var/lib/terrace-cms/stop-verified
          fi
        ''}"
        # Test-only stand-in for a long migration, used by the sweep-yield check.
        "+${pkgs.writeShellScript "website-test-slow-migration" ''
          if test -e /run/website-test-slow-migration; then ${pkgs.coreutils}/bin/sleep 20; fi
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
          intake=$(${pkgs.openssl}/bin/openssl rand -hex 32)
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
          LIFECYCLE_INTAKE_SECRET=$intake
          EOF
          cat >${inputs}/web.env <<EOF
          CMS_ORIGIN=http://127.0.0.1:13048
          CONTENT_SOURCE=cms
          ORIGIN=http://127.0.0.1:14294
          MEMBER_AUTH_ORIGIN=http://127.0.0.1:14294
          MEMBER_AUTH_ISSUER=http://127.0.0.1:8088/realms/terrace
          MEMBER_AUTH_CLIENT_ID=terrace-web
          MEMBER_AUTH_CLIENT_SECRET=$browser
          LIFECYCLE_INTAKE_SECRET=$intake
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
                for table in ["events", "_events_v", "media", "users", "members", "payload_migrations", "message_outbox", "roster_runs", "roster_run_items",
                              "member_applications", "member_invitations", "wp_import_runs", "wp_import_journal", "pages", "posts", "redirects"]}

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
        # The pinned release records its registry; the ledger must hold exactly those names in order.
        assert [row.rsplit(":", 1)[0] for row in before_ledger.splitlines()] == ${builtins.toJSON release.migrations}
        source.succeed("systemctl start terrace-cms-migrate.service")
        assert sql(source, "select name || ':' || batch from payload_migrations order by id") == before_ledger
        for change, undo in [
            ("chmod 0640 ${inputs}/cms.env", "chmod 0400 ${inputs}/cms.env"),
            ("echo CMS_BOOTSTRAP_TEST=0 >>${inputs}/cms.env", "sed -i '/^CMS_BOOTSTRAP_TEST=/d' ${inputs}/cms.env"),
            ("sed -i '/^CMS_AUTH_ISSUER=/d' ${inputs}/cms.env", "echo CMS_AUTH_ISSUER=http://127.0.0.1:8088/realms/terrace >>${inputs}/cms.env"),
            ("cp -a ${inputs}/cms.env /root/cms.env.keep && sed -i 's/^LIFECYCLE_INTAKE_SECRET=.*/LIFECYCLE_INTAKE_SECRET=too-short/' ${inputs}/cms.env", "cp -a /root/cms.env.keep ${inputs}/cms.env && rm /root/cms.env.keep"),
        ]:
            source.succeed(change)
            source.fail("systemctl start terrace-cms-migrate.service")
            assert sql(source, "select name || ':' || batch from payload_migrations order by id") == before_ledger
            # Each refused start also starts the namespace journal sync unit; four refusals within
            # ten seconds would otherwise trip that unit's default start limit, not the gate under test.
            source.succeed(undo + "; systemctl reset-failed terrace-cms-migrate.service terrace-website-inputs.service systemd-journald-sync@terrace-apps.service")
        # The reset above must not hide a broken sync unit: it starts cleanly on its own.
        source.succeed("systemctl start systemd-journald-sync@terrace-apps.service")
        assert dict(line.split("=", 1) for line in source.succeed("systemctl show systemd-journald-sync@terrace-apps.service -p Result,ActiveState").splitlines())["Result"] == "success"
        source.fail("systemctl is-failed --quiet systemd-journald-sync@terrace-apps.service")
        source.succeed("systemctl reset-failed terrace-cms.service terrace-web.service && systemctl start terrace-cms.service terrace-web.service")
        # A guard that refuses (here the identity guard, on a too-short intake secret) refuses before the preflight stops
        # anything: the CMS and website keep running, same processes.
        pids = source.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value").strip()
        source.succeed("cp -a ${inputs}/cms.env /root/cms.env.keep && sed -i 's/^LIFECYCLE_INTAKE_SECRET=.*/LIFECYCLE_INTAKE_SECRET=too-short/' ${inputs}/cms.env")
        source.fail("systemctl start terrace-cms-migrate.service")
        assert "Native Terrace runtime requires complete per-user identity" in source.succeed("journalctl --namespace=terrace-apps -u terrace-cms-migrate.service -o cat --no-pager")
        source.succeed("cp -a /root/cms.env.keep ${inputs}/cms.env && rm /root/cms.env.keep && systemctl reset-failed terrace-cms-migrate.service")
        source.succeed("systemctl is-active --quiet terrace-cms.service && systemctl is-active --quiet terrace-web.service")
        assert source.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value").strip() == pids
        # Accepted migration: the running CMS, website and a running sweep are all stopped (their processes gone) before
        # the step standing in for the first migration statement.
        source.succeed("touch /run/website-test-slow-sweep && systemctl start --no-block terrace-cms-sweep.service")
        source.wait_until_succeeds("systemctl show terrace-cms-sweep.service -p ActiveState --value | grep -qx activating", timeout=60)
        source.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value | grep -v '^$' >/var/lib/terrace-cms/old-pids && systemctl show terrace-cms-sweep.service -p ControlPID --value | grep -v '^0$' >>/var/lib/terrace-cms/old-pids")
        assert len(source.succeed("cat /var/lib/terrace-cms/old-pids").split()) == 3
        source.succeed("systemctl start terrace-cms-migrate.service && test -f /var/lib/terrace-cms/stop-verified")
        # old-pids stays: every later migration in this proof checks again that these units are down when it runs.
        source.succeed("rm /run/website-test-slow-sweep /var/lib/terrace-cms/stop-verified")
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
        # A synthetic bulk-roster journal entry (website migration 20261119_165021_roster_journal): ids, hashes and counts
        # only, as the tool writes; it must come back from the backup like any other CMS row.
        sql(source, "insert into roster_runs(run_id,operator_label,status,file_sha256,plan_sha256,run_limit,database_identity,counts,started_at,finished_at) values ('roster-synthetic-1','Synthetic operator','completed',repeat('a',64),repeat('b',64),50,'terrace_cms:synthetic','{\"created\":1}',now(),now())")
        sql(source, "insert into roster_run_items(run_id,row_number,state) select id,2,'applied' from roster_runs where run_id='roster-synthetic-1'")
        assert sql(source, "select count(*) from roster_run_items i join roster_runs r on r.id = i.run_id where r.status = 'completed'") == "1"
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

    with subtest("private lifecycle outbox and packaged daily sweep as the CMS account"):
        assert source.succeed("stat -c %U:%G:%a ${outbox}").strip() == "terrace_cms:terrace_cms:700"
        source.fail("runuser -u terrace_web -- ls ${outbox}")
        # Module-owned paths override the protected environment file.
        cms_pid = source.succeed("systemctl show terrace-cms.service -p MainPID --value").strip()
        source.succeed(f"tr '\\0' '\\n' </proc/{cms_pid}/environ | grep -qx 'LIFECYCLE_OUTBOX_DIR=${outbox}'")
        source.succeed("systemctl is-enabled terrace-cms-sweep.timer && systemctl is-active terrace-cms-sweep.timer")
        timer = dict(line.split("=", 1) for line in source.succeed("systemctl show terrace-cms-sweep.timer -p TimersCalendar,Persistent").splitlines())
        assert "OnCalendar=*-*-* 03:30:00" in timer["TimersCalendar"] and timer["Persistent"] == "yes", timer
        assert dict(line.split("=", 1) for line in source.succeed("systemctl show terrace-cms-sweep.service -p User,Type").splitlines()) == {"User": "terrace_cms", "Type": "oneshot"}
        # A pending synthetic outbox row (no file copy yet): the sweep writes its private mirror.
        pending = sql(source, "insert into message_outbox(kind,recipient_label,recipient_email,subject,body) values ('denial','Synthetic Applicant','host-applicant@terrace.invalid','Synthetic subject','Synthetic body') returning id").splitlines()[0]
        source.succeed("systemctl start terrace-cms-sweep.service")
        assert dict(line.split("=", 1) for line in source.succeed("systemctl show terrace-cms-sweep.service -p Result,ExecMainStatus").splitlines()) == {"Result": "success", "ExecMainStatus": "0"}
        mirror = "${outbox}/" + pending.zfill(6) + "-denial.json"
        assert source.succeed(f"stat -c %U:%a {mirror}").strip() == "terrace_cms:600"
        source.succeed(f"jq -e '.to == \"host-applicant@terrace.invalid\"' {mirror} >/dev/null")
        assert sql(source, "select count(*) from message_outbox where mirrored_at is null") == "0"
        # The real timer unit fires its service: a persistent timer whose last run
        # predates a scheduled elapse catches up immediately when (re)started.
        before = source.succeed("systemctl show terrace-cms-sweep.service -p InvocationID --value").strip()
        source.succeed("systemctl stop terrace-cms-sweep.timer && mkdir -p /var/lib/systemd/timers && touch -d '3 days ago' /var/lib/systemd/timers/stamp-terrace-cms-sweep.timer && systemctl start terrace-cms-sweep.timer")
        source.wait_until_succeeds(f"test \"$(systemctl show terrace-cms-sweep.service -p InvocationID --value)\" != {before} && systemctl show terrace-cms-sweep.service -p ActiveState --value | grep -qx inactive", timeout=120)
        assert dict(line.split("=", 1) for line in source.succeed("systemctl show terrace-cms-sweep.service -p Result,ExecMainStatus").splitlines()) == {"Result": "success", "ExecMainStatus": "0"}
        # A sweep requested during a migration waits for it and never stops it.
        migration = source.succeed("systemctl show terrace-cms-migrate.service -p InvocationID --value").strip()
        source.succeed("touch /run/website-test-slow-migration && systemctl start --no-block terrace-cms-migrate.service")
        source.wait_until_succeeds("systemctl show terrace-cms-migrate.service -p ActiveState --value | grep -qx activating", timeout=30)
        running = source.succeed("systemctl show terrace-cms-migrate.service -p InvocationID --value").strip()
        assert running != migration
        swept = source.succeed("systemctl show terrace-cms-sweep.service -p InvocationID --value").strip()
        source.succeed("systemctl start --no-block terrace-cms-sweep.service && sleep 3")
        # The sweep's start job waits behind the running migration instead of stopping it.
        assert source.succeed("systemctl show terrace-cms-migrate.service -p ActiveState --value").strip() == "activating"
        assert source.succeed("systemctl show terrace-cms-sweep.service -p ActiveState --value").strip() == "inactive"
        source.succeed("systemctl list-jobs --no-legend | grep -E 'terrace-cms-sweep.service +start +waiting' >/dev/null")
        source.wait_until_succeeds("systemctl show terrace-cms-migrate.service -p ActiveState --value | grep -qx inactive", timeout=120)
        migrated = dict(line.split("=", 1) for line in source.succeed("systemctl show terrace-cms-migrate.service -p InvocationID,Result,ExecMainExitTimestampMonotonic").splitlines())
        assert migrated["InvocationID"] == running and migrated["Result"] == "success", migrated
        source.wait_until_succeeds(f"test \"$(systemctl show terrace-cms-sweep.service -p InvocationID --value)\" != {swept} && systemctl show terrace-cms-sweep.service -p ActiveState --value | grep -qx inactive", timeout=180)
        sweep = dict(line.split("=", 1) for line in source.succeed("systemctl show terrace-cms-sweep.service -p Result,ExecMainStatus,ExecMainStartTimestampMonotonic").splitlines())
        assert sweep["Result"] == "success" and sweep["ExecMainStatus"] == "0" and int(sweep["ExecMainStartTimestampMonotonic"]) > int(migrated["ExecMainExitTimestampMonotonic"]), sweep
        source.succeed("rm /run/website-test-slow-migration && systemctl start terrace-cms.service terrace-web.service")

    with subtest("operator tools through terrace-cms-tool: roster and importer as the CMS account, refusals, lock wait, no secret exposure"):
        def tool(args, expect=0):
            rc, out = source.execute("terrace-cms-tool " + " ".join(shlex.quote(a) for a in args) + " 2>/tmp/tool.err")
            err = source.succeed("cat /tmp/tool.err")
            assert rc == expect, (args, rc, out[-2000:], err[-2000:])
            return out, err
        def summary(out):
            start = out.find("\n{\n") + 1 if not out.startswith("{") else 0
            return json.loads(out[start:])
        def background(name, args):
            # A tool run that outlives this command: its own transient unit (not a shell job), output and exit status in files.
            base = "/var/lib/website-test-tool/" + name
            source.succeed("install -d -m 0700 /var/lib/website-test-tool && rm -f " + base + ".*")
            script = "/run/current-system/sw/bin/terrace-cms-tool " + " ".join(shlex.quote(a) for a in args) + f" >{base}.out 2>{base}.err; echo $? >{base}.rc"
            source.succeed(f"systemd-run --quiet --collect --unit=website-test-{name} --description='website test {name}' ${pkgs.bash}/bin/bash -c " + shlex.quote(script))
            return base
        def files(directory):
            # Every file and its hash (names may contain spaces).
            return source.succeed(f"cd {directory} && find . -type f -print0 | sort -z | xargs -0 -r sha256sum")
        def directories():
            return {name: files(path) for name, path in [("outbox", "${outbox}"), ("media", "${media}"), ("operator", "${operator}")]}
        def written_only_report(before, after, report_path):
            # A dry run leaves the outbox and uploads byte-identical; in the operator directory it adds exactly its own report.
            added = sorted(set(after["operator"].splitlines()) - set(before["operator"].splitlines()))
            assert after["outbox"] == before["outbox"] and after["media"] == before["media"], "a dry run changed the outbox or uploads"
            assert set(before["operator"].splitlines()) <= set(after["operator"].splitlines()) and len(added) == 1 and added[0].endswith("./" + report_path.rsplit("/", 1)[1]), added
        def counts():
            return {name: sql(source, query) for name, query in [
                ("applications", "select count(*) from member_applications"), ("invitations", "select count(*) from member_invitations"),
                ("outbox", "select count(*) from message_outbox"), ("runs", "select count(*) from roster_runs"), ("items", "select count(*) from roster_run_items"),
                ("import_runs", "select count(*) from wp_import_runs"), ("pages", "select count(*) from pages"), ("posts", "select count(*) from posts"), ("redirects", "select count(*) from redirects")]}
        secrets = [line.split("=", 1)[1] for line in source.succeed("cat ${inputs}/cms.env").splitlines() if line.split("=", 1)[0] in ("PAYLOAD_SECRET", "LIFECYCLE_INTAKE_SECRET", "CMS_AUTH_CLIENT_SECRET", "MEMBER_AUTH_INTROSPECTION_SECRET")]
        assert len(secrets) == 4 and all(len(value) == 64 for value in secrets)
        assert source.succeed("stat -c %U:%G:%a ${operator}").strip() == "terrace_cms:terrace_cms:700"
        # A synthetic administrator to name as --operator (attribution only; no password, no sign-in).
        sql(source, "insert into users(display_name,role,email) values ('Synthetic operator','admin','operator@terrace.invalid')")
        roster = "${operator}/roster file.csv"
        source.succeed("printf 'name,affiliation,class_year,contact_email\nSynthetic Alumna,alumni,2004,roster-alumna@terrace.invalid\nSynthetic Member,current-member,,roster-member@terrace.invalid\n' >/root/roster.csv"
                       " && install -o terrace_cms -g terrace_cms -m 0600 /root/roster.csv " + shlex.quote(roster) + " && rm /root/roster.csv")
        reports = iter(range(100))
        report = lambda: f"${operator}/report-{next(reports)}.json"

        # Wrapper boundary: root only, known tools only, a migration in progress or a closed gate refuses, arguments unchanged, exit status propagated.
        assert source.fail("runuser -u nobody -- terrace-cms-tool roster-invite --surprise 2>&1").strip() == "terrace-cms-tool must run as root."
        rc, out = source.execute("terrace-cms-tool roster-inviter 2>&1")
        assert rc == 64 and out.startswith("Usage: terrace-cms-tool roster-invite|wp-import"), (rc, out)
        out, err = tool(["roster-invite", "--surprise"], expect=1)
        assert out == "" and err.startswith("Usage: roster-invite.ts"), err
        out, err = tool(["wp-import", "--surprise"], expect=1)
        assert out == "" and err.startswith("Usage: wp-import.ts"), err
        rows_before = counts()
        dirs_before = directories()
        # A path with a space and an option-like argument reach the tool unchanged; the default command is a dry run.
        dry_report = report()
        dry = summary(tool(["roster-invite", "--file", roster, "--operator", "operator@terrace.invalid", "--report", dry_report, "--reinvite-lapsed"])[0])
        assert dry["mode"] == "dry-run" and dry["reinviteLapsed"] is True and dry["counts"] == {"rows": 2, "create": 2, "reject": 0, "skip": 0, "deferred": 0}, dry
        assert counts() == rows_before
        written_only_report(dirs_before, directories(), dry_report)
        source.succeed("mv /var/lib/terrace-cms/migrated-revision /root/migrated-revision")
        out, err = tool(["roster-invite", "dry-run", "--file", roster, "--operator", "operator@terrace.invalid", "--report", report()], expect=1)
        assert "CMS migration gate is closed" in err, err
        source.succeed("mv /root/migrated-revision /var/lib/terrace-cms/migrated-revision")
        source.succeed("touch /run/website-test-slow-migration && systemctl start --no-block terrace-cms-migrate.service")
        source.wait_until_succeeds("systemctl show terrace-cms-migrate.service -p ActiveState --value | grep -qx activating", timeout=30)
        out, err = tool(["roster-invite", "dry-run", "--file", roster, "--operator", "operator@terrace.invalid", "--report", report()], expect=75)
        assert err.strip() == "A CMS migration is in progress; run the tool after it has finished.", err
        source.wait_until_succeeds("systemctl show terrace-cms-migrate.service -p ActiveState --value | grep -qx inactive", timeout=120)
        # Each migration stops the CMS and website; their start limit (3 in 10 minutes) is not under test here.
        source.succeed("rm /run/website-test-slow-migration && systemctl reset-failed terrace-cms.service terrace-web.service && systemctl start terrace-cms.service terrace-web.service")
        # A migration refuses to start under a running operator tool (a unit with the tools' name prefix stands in for one),
        # before anything is stopped: the CMS and website keep running.
        cms_pid = source.succeed("systemctl show terrace-cms.service -p MainPID --value").strip()
        source.succeed("systemd-run --unit=terrace-cms-tool-probe ${pkgs.coreutils}/bin/sleep 60")
        source.fail("systemctl start terrace-cms-migrate.service")
        assert "A CMS operator tool is running (terrace-cms-tool-probe.service); migration refused before anything was stopped." in source.succeed("journalctl --namespace=terrace-apps -u terrace-cms-migrate.service -o cat --no-pager")
        source.succeed("systemctl is-active --quiet terrace-cms.service && systemctl is-active --quiet terrace-web.service")
        assert source.succeed("systemctl show terrace-cms.service -p MainPID --value").strip() == cms_pid
        source.succeed("systemctl stop terrace-cms-tool-probe.service && systemctl reset-failed terrace-cms-migrate.service")
        assert sql(source, "select name || ':' || batch from payload_migrations order by id") == before_ledger

        # The writes run in the public profile, as on a production host: swap in a public variant of the protected file (same
        # secrets) now that the migration checks are done (the CMS keeps running as started), and put the original back byte for byte afterwards.
        original_cms_env = source.succeed("sha256sum ${inputs}/cms.env").split()[0]
        source.succeed("cp -a ${inputs}/cms.env /root/cms.env.loopback && chmod 0600 ${inputs}/cms.env"
                       " && sed -i -e 's|^CMS_ORIGIN=.*|CMS_ORIGIN=https://editor.terrace.invalid|' -e 's|_ISSUER=.*|_ISSUER=https://identity.terrace.invalid/realms/terrace|' ${inputs}/cms.env"
                       " && printf 'CMS_PUBLIC_DEPLOYMENT=1\nLIFECYCLE_PUBLIC_ORIGIN=https://web.terrace.invalid\n' >>${inputs}/cms.env && chmod 0400 ${inputs}/cms.env")
        # Roster: a write needs --confirm-production (the host's database counts as a production target); then apply, no-op, rollback.
        plan = summary(tool(["roster-invite", "dry-run", "--file", roster, "--operator", "operator@terrace.invalid", "--report", report()])[0])
        out, err = tool(["roster-invite", "apply", "--file", roster, "--operator", "operator@terrace.invalid", "--plan-sha256", plan["planSha256"]], expect=1)
        assert err.strip() == "This is a production target. Pass --confirm-production." and counts() == rows_before, err
        # The wrapper's lock file is root's alone: another user cannot open it, so cannot hold it.
        assert source.succeed("stat -c %U:%a /run/terrace-cms-tool.lock").strip() == "root:600"
        assert "Permission denied" in source.fail("runuser -u nobody -- ${pkgs.util-linux}/bin/flock -n /run/terrace-cms-tool.lock true 2>&1")
        # It is never replaced: a wider mode is narrowed on the same inode under the lock; a file root does not own, or a
        # link, is refused with exit 72 before the tool runs and is left as it was.
        inode = source.succeed("stat -c %i /run/terrace-cms-tool.lock").strip()
        source.succeed("chmod 0644 /run/terrace-cms-tool.lock")
        out, err = tool(["wp-import", "--surprise"], expect=1)
        assert out == "" and err.startswith("Usage: wp-import.ts"), err
        assert source.succeed("stat -c %i:%U:%a /run/terrace-cms-tool.lock").strip() == inode + ":root:600"
        refusal = "The operator tool lock /run/terrace-cms-tool.lock is not a root-owned regular file ({}); nothing was run. Inspect it, remove it by hand and run the tool again."
        source.succeed("chown nobody /run/terrace-cms-tool.lock")
        out, err = tool(["wp-import", "--surprise"], expect=72)
        assert out == "" and err.strip() == refusal.format("owned by uid " + source.succeed("id -u nobody").strip()), err
        assert source.succeed("stat -c %i:%U:%a /run/terrace-cms-tool.lock").strip() == inode + ":nobody:600"
        source.succeed("chown root /run/terrace-cms-tool.lock && mv /run/terrace-cms-tool.lock /run/terrace-cms-tool.lock.kept && ln -s /run/terrace-cms-tool.lock.kept /run/terrace-cms-tool.lock")
        out, err = tool(["wp-import", "--surprise"], expect=72)
        assert out == "" and err.strip() == refusal.format("a symbolic link"), err
        source.succeed("test -L /run/terrace-cms-tool.lock && rm /run/terrace-cms-tool.lock && mv /run/terrace-cms-tool.lock.kept /run/terrace-cms-tool.lock")
        assert source.succeed("stat -c %i:%U:%a /run/terrace-cms-tool.lock").strip() == inode + ":root:600"
        files_before = set(source.succeed("ls ${outbox}").split())
        applied = summary(tool(["roster-invite", "apply", "--file", roster, "--operator", "operator@terrace.invalid", "--plan-sha256", plan["planSha256"], "--report", report(), "--confirm-production"])[0])
        assert applied["counts"]["created"] == 2 and applied["counts"]["failed"] == 0, applied
        assert sql(source, f"select r.status || ':' || count(i.id) from roster_runs r join roster_run_items i on i.run_id = r.id where r.run_id = '{applied['runId']}' group by r.status") == "completed:2"
        assert sql(source, "select count(*) from member_invitations i join member_applications a on a.id = i.application_id where a.contact_email like 'roster-%@terrace.invalid' and i.status = 'pending'") == "2"
        links = sorted(set(source.succeed("ls ${outbox}").split()) - files_before)
        assert len(links) == 2, links
        assert source.succeed("cd ${outbox} && stat -c %U:%G:%a " + " ".join(links)).split() == ["terrace_cms:terrace_cms:600"] * 2
        for name in links:
            source.succeed(f"jq -e '.body | test(\"https://web.terrace.invalid/join/\")' ${outbox}/{name} >/dev/null")
        after_apply = counts()
        again = summary(tool(["roster-invite", "dry-run", "--file", roster, "--operator", "operator@terrace.invalid", "--report", report()])[0])
        assert again["counts"]["create"] == 0 and again["counts"]["skip"] == 2, again
        noop = summary(tool(["roster-invite", "apply", "--file", roster, "--operator", "operator@terrace.invalid", "--plan-sha256", again["planSha256"], "--confirm-production"])[0])
        assert noop["runId"] is None and noop["counts"]["created"] == 0 and counts() == after_apply, noop
        # A run left `running` (as by a tool stopped mid-run; simulated by moving its heartbeat back): refused while it is
        # younger than 30 minutes, then the documented rollback marks it failed and rolls it back.
        sql(source, f"update roster_runs set status = 'running', updated_at = now() - interval '5 minutes' where run_id = '{applied['runId']}'")
        out, err = tool(["roster-invite", "rollback", "--run", applied["runId"], "--operator", "operator@terrace.invalid", "--confirm-production"], expect=1)
        assert err.strip() == "A roster run is still marked running. Wait for it, or retry after it has gone stale.", err
        sql(source, f"update roster_runs set updated_at = now() - interval '31 minutes' where run_id = '{applied['runId']}'")
        back = summary(tool(["roster-invite", "rollback", "--run", applied["runId"], "--operator", "operator@terrace.invalid", "--confirm-production"])[0])
        assert back["ok"] is True and back["revoked"] == 2 and back["withdrawn"] == 2, back
        assert sql(source, f"select status from roster_runs where run_id = '{applied['runId']}'") == "rolled-back"

        # Importer: on this host it needs --allow-nonlocal-database (the module's DATABASE_URI form is not "local" to it) and,
        # for writes, --confirm-production. Neither is added by the wrapper.
        source.succeed("install -o terrace_cms -g terrace_cms -m 0600 ${importPlan} ${operator}/plan.json")
        plan_file = "${operator}/plan.json"
        rows_before = counts()
        out, err = tool(["wp-import", "dry-run", "--plan", plan_file], expect=1)
        assert err.strip() == "The database is not local. Pass --allow-nonlocal-database only for an approved target.", err
        dirs_before = directories()
        dry_report = report()
        dry = summary(tool(["wp-import", "dry-run", "--plan", plan_file, "--report", dry_report, "--allow-nonlocal-database"])[0])
        assert dry["mode"] == "dry-run" and dry["counts"]["failed"] == 0 and dry["counts"]["created"] > 0 and counts() == rows_before, dry
        written_only_report(dirs_before, directories(), dry_report)
        out, err = tool(["wp-import", "apply", "--plan", plan_file, "--run-id", "wpimport-host-1", "--allow-nonlocal-database"], expect=1)
        assert err.strip().startswith("The database is a production target") and "--confirm-production" in err and counts() == rows_before, err
        imported = summary(tool(["wp-import", "apply", "--plan", plan_file, "--run-id", "wpimport-host-1", "--report", report(), "--allow-nonlocal-database", "--confirm-production"])[0])
        assert imported["counts"]["created"] == dry["counts"]["created"] and imported["counts"]["failed"] == 0, imported
        after_import = counts()
        assert after_import["import_runs"] == str(int(rows_before["import_runs"]) + 1)
        reapplied = summary(tool(["wp-import", "apply", "--plan", plan_file, "--report", report(), "--allow-nonlocal-database", "--confirm-production"])[0])
        assert reapplied["counts"]["created"] == 0 and reapplied["counts"]["updated"] == 0 and reapplied["counts"]["skipped"] == imported["counts"]["created"], reapplied
        assert {k: v for k, v in counts().items() if k != "import_runs"} == {k: v for k, v in after_import.items() if k != "import_runs"}
        undone = summary(tool(["wp-import", "rollback", "--run", "wpimport-host-1", "--report", report(), "--allow-nonlocal-database", "--confirm-production"])[0])
        assert undone["ok"] is True and undone["deleted"] == imported["counts"]["created"] + imported["counts"]["redirects"]["created"], undone
        assert {k: v for k, v in counts().items() if k not in ("import_runs",)} == {k: v for k, v in rows_before.items() if k not in ("import_runs",)}

        # LIFECYCLE_LOCK_TIMEOUT_MS=500 (module option): with the address lock held, the public apply endpoint answers 503 within the bound
        # and a roster row waits about half a second, then stops with address-locked and stores nothing.
        held = "roster-locked@terrace.invalid"
        source.succeed("printf 'name,affiliation,class_year,contact_email\nSynthetic Locked,alumni,2004," + held + "\n' >/root/locked.csv && install -o terrace_cms -g terrace_cms -m 0600 /root/locked.csv ${operator}/locked.csv && rm /root/locked.csv")
        locked_plan = summary(tool(["roster-invite", "dry-run", "--file", "${operator}/locked.csv", "--operator", "operator@terrace.invalid", "--report", report()])[0])
        source.succeed("systemd-run --unit=website-test-lock-holder -p User=postgres -p Environment=PGAPPNAME=website-test-holder ${pkgs.postgresql_18}/bin/psql -X -d terrace_cms -c " + shlex.quote(f"begin; select pg_advisory_xact_lock(hashtextextended('terrace-application-email:{held}', 0)); select pg_sleep(60); commit"))
        waiting_sql = "select count(*) from pg_locks w join pg_locks h on h.locktype = 'advisory' and h.granted and w.classid = h.classid and w.objid = h.objid and w.objsubid = h.objsubid and h.pid <> w.pid where w.locktype = 'advisory' and not w.granted"
        source.wait_until_succeeds("test $(runuser -u postgres -- psql -XAt -d terrace_cms -c \"select count(*) from pg_locks where locktype = 'advisory' and granted\") -ge 1", timeout=30)
        intake = [line.split("=", 1)[1] for line in source.succeed("cat ${inputs}/cms.env").splitlines() if line.startswith("LIFECYCLE_INTAKE_SECRET=")][0]
        answer = source.succeed("curl --silent --max-time 10 --output /tmp/apply.json --write-out '%{http_code} %{time_total}' -H 'content-type: application/json' -H " + shlex.quote("x-terrace-intake: " + intake)
                                + " --data " + shlex.quote(json.dumps({"name": "Synthetic Locked Form", "affiliation": "alumni", "contactEmail": held})) + " http://127.0.0.1:13048/api/lifecycle/apply").split()
        assert answer[0] == "503" and float(answer[1]) < 2.5, answer
        source.succeed("jq -e '. == {\"error\":\"Unavailable.\"}' /tmp/apply.json >/dev/null")
        locked_run = background("locked", ["roster-invite", "apply", "--file", "${operator}/locked.csv", "--operator", "operator@terrace.invalid", "--plan-sha256", locked_plan["planSha256"], "--confirm-production"])
        # Sample pg_locks: from the first sample showing the tool's backend waiting on the held key to the first that does not.
        first: float | None = None
        last: float | None = None
        for _ in range(1200):
            now = time.monotonic()
            if int(sql(source, waiting_sql) or "0") > 0:
                first = first or now
            elif first is not None:
                last = now
                break
            if source.execute(f"test -e {locked_run}.rc")[0] == 0:
                break
        assert first is not None and last is not None, "the tool never waited on the held lock"
        waited = last - first
        source.wait_until_succeeds(f"test -s {locked_run}.rc", timeout=60)
        assert source.succeed(f"cat {locked_run}.rc").strip() == "1"
        locked = summary(source.succeed(f"cat {locked_run}.out"))
        assert locked["failure"] == "address-locked" and locked["counts"]["created"] == 0 and locked["counts"]["failed"] == 1, locked
        assert sql(source, f"select count(*) from member_applications where contact_email = '{held}'") == "0"
        assert 0.1 <= waited < 2.0, f"lock wait {waited:.2f}s with LIFECYCLE_LOCK_TIMEOUT_MS=500"
        print(f"roster lock wait with LIFECYCLE_LOCK_TIMEOUT_MS=500: {waited:.2f}s; public apply answered {answer[0]} in {answer[1]}s")
        source.succeed("runuser -u postgres -- psql -XAt -d terrace_cms -c \"select count(pg_terminate_backend(pid)) from pg_stat_activity where application_name = 'website-test-holder'\" >/dev/null"); source.wait_until_fails("systemctl is-active --quiet website-test-lock-holder.service", timeout=30)

        # The transient unit, frozen while it runs: the CMS account and the CMS units' own settings, and no secret value in its
        # properties (as root or as another user) or in any process's arguments.
        frozen_run = background("frozen", ["roster-invite", "dry-run", "--file", roster, "--operator", "operator@terrace.invalid", "--report", report()])
        source.wait_until_succeeds("systemctl list-units --plain --no-legend 'terrace-cms-tool-roster-invite-*' | grep -q .", timeout=30)
        unit = source.succeed("systemctl list-units --plain --no-legend 'terrace-cms-tool-roster-invite-*' | cut -d' ' -f1").strip()
        source.succeed(f"systemctl freeze {unit}")
        shown = source.succeed(f"systemctl show {unit}") + source.succeed(f"runuser -u nobody -- systemctl show {unit}") + source.succeed("ps -eo args")
        assert "terrace-cms-tool-run roster-invite dry-run" in shown and not any(value in shown for value in secrets), "secret value in unit properties or arguments"
        props = lambda name: dict(line.split("=", 1) for line in source.succeed(f"systemctl show {name} -p User,Group,UMask,ProtectSystem,ProtectHome,NoNewPrivileges,PrivateTmp,PrivateDevices,RestrictAddressFamilies,MemoryHigh,MemoryMax,CPUQuotaPerSecUSec,TasksMax,LogNamespace,WorkingDirectory,EnvironmentFiles").splitlines())
        tool_props, cms_props = props(unit), props("terrace-cms.service")
        assert tool_props == cms_props and tool_props["User"] == "terrace_cms" and tool_props["EnvironmentFiles"].startswith("${inputs}/cms.env"), (tool_props, cms_props)
        assert source.succeed(f"systemctl show {unit} -p RuntimeMaxUSec --value").strip() == "1min"
        # What another local user can see of a running tool (recorded, then documented): argv carries the operator's email and file paths.
        seen = {view: "operator@terrace.invalid" in source.execute(f"runuser -u nobody -- {command} 2>&1")[1] and source.execute(f"runuser -u nobody -- {command} >/dev/null 2>&1")[0] == 0
                for view, command in [("ps", "ps -eo args"), ("status", f"systemctl status --no-pager {unit}"), ("show", f"systemctl show {unit}"), ("cat", f"systemctl cat --no-pager {unit}")]}
        print("TOOL-VISIBILITY " + json.dumps(seen))
        assert not any(value in source.execute(f"runuser -u nobody -- systemctl cat --no-pager {unit} 2>&1")[1] for value in secrets)
        source.succeed(f"systemctl thaw {unit}")
        source.wait_until_succeeds(f"test -s {frozen_run}.rc", timeout=60)
        assert source.succeed(f"cat {frozen_run}.rc").strip() == "0" and summary(source.succeed(f"cat {frozen_run}.out"))["mode"] == "dry-run"
        source.wait_until_fails(f"systemctl is-active --quiet {unit}", timeout=30)
        # No secret value in any journal, including the tools' namespace.
        logs = source.succeed("journalctl --no-pager -o json; journalctl --namespace=terrace-apps --no-pager -o json")
        assert len(logs) > 1000 and not any(value in logs for value in secrets), "secret value in a journal"
        # Nor the tools' own output (stdout and stderr go to the caller through --pipe): the roster file's hash is printed by the
        # tool only, and the two refusals only on its stderr. Nor their arguments: the fixed unit description keeps the command
        # line (operator email, file paths, plan hash) out of systemd's start and stop messages.
        # Checked per entry: every entry's message, and every field of the tool units' own entries. Not the _CMDLINE of
        # unrelated processes: this harness's own psql and runuser command lines (which name the synthetic operator) are logged there.
        entries = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
        def text(value):
            return value if isinstance(value, str) else json.dumps(value)
        tool_entries = [entry for entry in entries if any(text(entry.get(field, "")).startswith("terrace-cms-tool-") for field in ("_SYSTEMD_UNIT", "UNIT", "USER_UNIT"))]
        for marker in [plan["fileSha256"], "Usage: roster-invite.ts", "This is a production target", "operator@terrace.invalid", plan["planSha256"], "roster file.csv"]:
            found = [entry for entry in entries if marker in text(entry.get("MESSAGE", ""))] + [entry for entry in tool_entries if marker in json.dumps(entry)]
            assert not found, "tool output or argument in a journal: " + marker + " " + json.dumps([{key: entry.get(key) for key in ("MESSAGE", "_COMM", "SYSLOG_IDENTIFIER", "_SYSTEMD_UNIT", "UNIT")} for entry in found[:3]])
        assert len(tool_entries) > 0 and any("Terrace CMS operator tool (roster-invite)" in text(entry.get("MESSAGE", "")) for entry in entries)
        # Inputs and reports stay in the operator directory, which no backup includes (checked against the snapshot below); purge it.
        names = source.succeed("ls ${operator}").strip().split("\n")
        assert {"locked.csv", "plan.json", "roster file.csv"} <= set(names) and all(name in ("locked.csv", "plan.json", "roster file.csv") or (name.startswith("report-") and name.endswith(".json")) for name in names), names
        assert source.succeed("cd ${operator} && stat -c %U:%a -- *").split() == ["terrace_cms:600"] * len(names)
        operator_files = source.succeed("find ${operator} -type f | wc -l").strip()
        source.succeed("cp -a /root/cms.env.loopback ${inputs}/cms.env && rm /root/cms.env.loopback")
        assert source.succeed("sha256sum ${inputs}/cms.env").split()[0] == original_cms_env

        # A tool that does not end (a dry run blocked on a held table lock stands in for a hung or orphaned run).
        source.succeed("systemd-run --unit=website-test-table-holder -p User=postgres -p Environment=PGAPPNAME=website-test-holder ${pkgs.postgresql_18}/bin/psql -X -d terrace_cms -c " + shlex.quote("begin; lock table member_applications in access exclusive mode; select pg_sleep(900); commit"))
        source.wait_until_succeeds("test $(runuser -u postgres -- psql -XAt -d terrace_cms -c \"select count(*) from pg_locks where relation = 'member_applications'::regclass and mode = 'AccessExclusiveLock' and granted\") = 1", timeout=30)
        def hung(name):
            run = background(name, ["roster-invite", "dry-run", "--file", roster, "--operator", "operator@terrace.invalid", "--report", report()])
            source.wait_until_succeeds("systemctl list-units --plain --no-legend 'terrace-cms-tool-roster-invite-*' | grep -q .", timeout=30)
            return run, source.succeed("systemctl list-units --plain --no-legend 'terrace-cms-tool-roster-invite-*' | cut -d' ' -f1").strip()
        rc_and_result = lambda run, name: (source.succeed(f"cat {run}.rc").strip(), source.succeed(f"journalctl --no-pager -u {name} -o cat | grep -o \"Failed with result '[a-z-]*'\" | tail -n 1").strip())
        # One at a time: while a wrapper runs, another is refused by its lock.
        first, first_unit = hung("hung-first")
        out, err = tool(["roster-invite", "dry-run", "--file", roster, "--operator", "operator@terrace.invalid", "--report", report()], expect=73)
        assert err.strip() == "Another terrace-cms-tool run is starting or running; run the tools one at a time.", err
        # A dropped session: the wrapper dies, its tool unit keeps running, and the next wrapper names that unit.
        source.succeed("systemctl kill --signal=SIGKILL website-test-hung-first.service")
        source.wait_until_fails("systemctl is-active --quiet website-test-hung-first.service", timeout=30)
        source.succeed(f"systemctl is-active --quiet {first_unit}")
        out, err = tool(["roster-invite", "dry-run", "--file", roster, "--operator", "operator@terrace.invalid", "--report", report()], expect=73)
        assert err.strip() == f"An operator tool unit is still active: {first_unit}. Wait for it, or stop it: systemctl stop 'terrace-cms-tool-*'", err
        # A migration started now is refused before anything is stopped: the reason is in the apps journal, the CMS and the
        # website keep running and serving (same processes), and the ledger is unchanged.
        pids = source.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value").strip()
        source.fail("systemctl start terrace-cms-migrate.service")
        reason = source.succeed("journalctl --namespace=terrace-apps -u terrace-cms-migrate.service -o cat --no-pager")
        assert f"A CMS operator tool is running ({first_unit}); migration refused before anything was stopped. Start the migration after it has finished." in reason, reason[-1500:]
        for service in ["terrace-cms", "terrace-web"]:
            source.succeed(f"systemctl is-active --quiet {service}.service")
        assert source.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value").strip() == pids
        assert_content(source)
        assert sql(source, "select name || ':' || batch from payload_migrations order by id") == before_ledger
        # The documented way out (docs/website-host.md, "When a tool is still running"), command for command.
        listed = source.succeed("systemctl list-units --plain --no-legend --state=active,activating,deactivating 'terrace-cms-tool-*'")
        assert listed.split()[0] == first_unit, listed
        source.succeed("systemctl stop 'terrace-cms-tool-*'")
        source.succeed("runuser -u postgres -- psql -XAt -d terrace_cms -c \"select count(pg_terminate_backend(pid)) from pg_stat_activity where application_name = 'website-test-holder'\" >/dev/null"); source.wait_until_fails("systemctl is-active --quiet website-test-table-holder.service", timeout=30)
        source.succeed("systemctl reset-failed terrace-cms-migrate.service")
        source.succeed("systemctl start terrace-cms-migrate.service")
        source.succeed("systemctl start terrace-cms.service terrace-web.service")
        assert source.succeed("systemctl show terrace-cms.service terrace-web.service -p MainPID --value").strip() != pids  # the accepted migration stopped them
        assert sql(source, "select name || ':' || batch from payload_migrations order by id") == before_ledger
        assert_content(source)
        # Exit statuses: a tool unit killed by a signal, and one stopped at the run-time bound (60 s here).
        source.succeed("systemd-run --unit=website-test-table-holder-2 -p User=postgres -p Environment=PGAPPNAME=website-test-holder ${pkgs.postgresql_18}/bin/psql -X -d terrace_cms -c " + shlex.quote("begin; lock table member_applications in access exclusive mode; select pg_sleep(900); commit"))
        source.wait_until_succeeds("test $(runuser -u postgres -- psql -XAt -d terrace_cms -c \"select count(*) from pg_locks where relation = 'member_applications'::regclass and mode = 'AccessExclusiveLock' and granted\") = 1", timeout=30)
        killed, killed_unit = hung("hung-killed")
        source.succeed(f"systemctl kill --signal=SIGKILL {killed_unit}")
        source.wait_until_succeeds(f"test -s {killed}.rc", timeout=60)
        bounded, bounded_unit = hung("hung-bounded")
        source.wait_until_succeeds(f"test -s {bounded}.rc", timeout=120)
        exits = {"signal": rc_and_result(killed, killed_unit), "bound": rc_and_result(bounded, bounded_unit)}
        print("TOOL-EXIT " + json.dumps(exits))
        assert exits["signal"][0] != "0" and exits["bound"][0] != "0" and exits["bound"][1] == "Failed with result 'timeout'", exits
        source.succeed("runuser -u postgres -- psql -XAt -d terrace_cms -c \"select count(pg_terminate_backend(pid)) from pg_stat_activity where application_name = 'website-test-holder'\" >/dev/null"); source.wait_until_fails("systemctl is-active --quiet website-test-table-holder-2.service", timeout=30)
        for service in ["terrace-cms", "terrace-web"]:
            source.succeed(f"systemctl is-active --quiet {service}.service")
        # A real importer apply stopped mid-run: its run row and the plan's 6 media documents (no redirect hook) are
        # written, then its first event waits on a held lock (the document hooks write redirects) and the tool unit is
        # stopped there. Then the documented follow-up: rollback
        # refuses a run still marked running, refuses a takeover before it is idle long enough, and takes it over after.
        imports_before = counts()
        media_before = int(sql(source, "select count(*) from media"))
        source.succeed("systemd-run --unit=website-test-redirect-holder -p User=postgres -p Environment=PGAPPNAME=website-test-holder ${pkgs.postgresql_18}/bin/psql -X -d terrace_cms -c " + shlex.quote("begin; lock table redirects in access exclusive mode; select pg_sleep(900); commit"))
        source.wait_until_succeeds("test $(runuser -u postgres -- psql -XAt -d terrace_cms -c \"select count(*) from pg_locks where relation = 'redirects'::regclass and mode = 'AccessExclusiveLock' and granted\") = 1", timeout=30)
        stopped_run = background("import-stopped", ["wp-import", "apply", "--plan", plan_file, "--run-id", "wpimport-stopped-1", "--allow-nonlocal-database", "--confirm-production"])
        source.wait_until_succeeds("test $(runuser -u postgres -- psql -XAt -d terrace_cms -c \"select count(*) from pg_locks where relation = 'redirects'::regclass and not granted\") -ge 1", timeout=120)
        source.succeed("systemctl stop 'terrace-cms-tool-*'")
        source.wait_until_succeeds(f"test -s {stopped_run}.rc", timeout=60)
        source.succeed("runuser -u postgres -- psql -XAt -d terrace_cms -c \"select count(pg_terminate_backend(pid)) from pg_stat_activity where application_name = 'website-test-holder'\" >/dev/null"); source.wait_until_fails("systemctl is-active --quiet website-test-redirect-holder.service", timeout=30)
        left = counts()
        assert sql(source, "select status from wp_import_runs where run_id = 'wpimport-stopped-1'") == "running"
        assert {k: v for k, v in left.items() if k != "import_runs"} == {k: v for k, v in imports_before.items() if k != "import_runs"}, (imports_before, left)
        assert int(sql(source, "select count(*) from media")) == media_before + 6
        # systemd counts a death by SIGTERM (its stop) as clean, so the wrapper of a stopped tool exits 0: check the run, not the status.
        assert source.succeed(f"cat {stopped_run}.rc").strip() == "0"
        print("TOOL-STOPPED-IMPORT " + json.dumps({"wrapperExit": "0", "before": imports_before, "left": left, "media": [media_before, media_before + 6]}))
        refused = summary(tool(["wp-import", "rollback", "--run", "wpimport-stopped-1", "--allow-nonlocal-database", "--confirm-production"], expect=1)[0])
        assert refused["ok"] is False and [r["reason"] for r in refused["refusals"]] == ["run-still-running"], refused
        refused = summary(tool(["wp-import", "rollback", "--run", "wpimport-stopped-1", "--take-over-stale", "--allow-nonlocal-database", "--confirm-production"], expect=1)[0])
        assert refused["ok"] is False and [r["reason"] for r in refused["refusals"]] == ["run-not-stale"], refused
        source.wait_until_succeeds("test $(runuser -u postgres -- psql -XAt -d terrace_cms -c \"select (now() - updated_at > interval '61 seconds')::int from wp_import_runs where run_id = 'wpimport-stopped-1'\") = 1", timeout=120)
        taken = summary(tool(["wp-import", "rollback", "--run", "wpimport-stopped-1", "--take-over-stale", "--stale-after-minutes", "1", "--allow-nonlocal-database", "--confirm-production"])[0])
        assert taken["ok"] is True and taken["deleted"] == 6 and taken["refusals"] == [], taken
        assert sql(source, "select status from wp_import_runs where run_id = 'wpimport-stopped-1'") == "rolled-back"
        assert {k: v for k, v in counts().items() if k != "import_runs"} == {k: v for k, v in imports_before.items() if k != "import_runs"}
        assert int(sql(source, "select count(*) from media")) == media_before
        before_outbox = source.succeed("cd ${outbox} && sha256sum *.json").strip()

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
        # The operator directory (personal-data inputs and reports) is not in the backup; the outbox with the link files is.
        listed = source.succeed(f"terrace-backup restic primary ls {snapshot} 2>/dev/null")
        assert "${operator}" not in listed and "${outbox}/" in listed, "backup coverage of operator and outbox directories"
        assert int(operator_files) > 0
        source.succeed("find ${operator} -mindepth 1 -delete && test -z \"$(ls -A ${operator})\"")
        assert data_signature(source) == before_rows

    with subtest("recover database, uploads and runtime inputs into disposable guest"):
        restore_seconds = timed(recovery, f"umask 077; terrace-backup-restore --target primary --snapshot {snapshot} --dest ${restored} --keep-running >/var/tmp/website-restore.log 2>&1")
        assert recovery.succeed("stat -c %a /var/tmp/website-restore.log").strip() == "600"
        recovery.succeed("grep -qx 'RECONCILIATION OK' ${restored}/reconciliation.txt")
        assert data_signature(recovery, True) == before_rows
        assert int(sql(recovery, "select count(*) from pg_tables where schemaname !~ '^pg_' and schemaname != 'information_schema'", True)) == table_count
        recovery.succeed("test ! -e ${inputs} && cp -a ${restored}/files${inputs} ${inputs} && test -z \"$(ls -A ${media})\" && cp -a ${restored}/files${media}/. ${media}/ && chown -R terrace-restore:terrace-restore ${media}")
        # The outbox (live invitation links) comes back with its bytes, owner and private modes.
        recovery.succeed("test -z \"$(ls -A ${outbox})\" && cp -a ${restored}/files${outbox}/. ${outbox}/")
        assert recovery.succeed("cd ${outbox} && sha256sum *.json").strip() == before_outbox
        # restic keeps numeric owners; the static CMS UID makes them the same account on any host
        # (this guest otherwise allocates its system accounts in a different order).
        for machine in [source, recovery]:
            assert machine.succeed("id -u terrace_cms && id -g terrace_cms").split() == ["44121", "44121"]
        # The sweep's file and the roster tool's link files: the directory and every file keep the CMS account and private modes.
        outbox_modes = recovery.succeed("stat -c %U:%a ${outbox} ${outbox}/*.json").split()
        assert outbox_modes[0] == "terrace_cms:700" and len(outbox_modes) == 1 + len(before_outbox.splitlines()) and set(outbox_modes[1:]) == {"terrace_cms:600"}, outbox_modes
        # Behaviour, not just owners: the CMS account reads a restored mirror file and writes beside it.
        recovery.succeed("runuser -u terrace_cms -- sh -c 'cat ${outbox}/*.json >/dev/null && umask 077 && echo probe >${outbox}/restore-write-probe && rm ${outbox}/restore-write-probe'")
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
