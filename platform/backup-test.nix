{
  pkgs,
  module,
  relayModule,
}: let
  certificate = pkgs.runCommand "terrace-backup-test-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir -p $out
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout $out/key.pem -out $out/cert.pem \
      -subj '/CN=relay' -addext 'subjectAltName=DNS:relay' 2>/dev/null
  '';
  # Throwaway secrets are generated inside the VM at test time; only their paths are configured.
  secrets = "/var/lib/backup-test-secrets";
  sftpTarget = name: {
    enable = true;
    repository = "sftp:restic@target:restic-${name}";
    passwordFile = "${secrets}/${name}-password";
    sftp = {
      identityFile = "${secrets}/id_ed25519";
      knownHostsFile = "${secrets}/known_hosts";
    };
  };
in {
  name = "terrace-backups";
  nodes = {
    source = {...}: {
      imports = [module relayModule];
      virtualisation.memorySize = 2048;
      terrace.relay = {
        enable = true;
        hostname = "relay";
        publicIPv4 = "192.168.1.1";
      };
      services.nginx.virtualHosts.relay = {
        enableACME = false;
        sslCertificate = "${certificate}/cert.pem";
        sslCertificateKey = "${certificate}/key.pem";
      };
      services.postgresql = {
        enable = true;
        ensureDatabases = ["cms" "community" "keycloak"];
        ensureUsers = [
          {
            name = "community";
            ensureDBOwnership = true;
          }
        ];
      };
      terrace.backup = {
        enable = true;
        postgresql.databases = ["cms" "community" "keycloak"];
        media = {
          cms = "/var/lib/terrace-media/cms";
          community = "/var/lib/terrace-media/community";
        };
        targets.primary = sftpTarget "primary";
        targets.secondary = sftpTarget "secondary";
      };
      environment.systemPackages = [pkgs.jq pkgs.sqlite];
    };
    target = {...}: {
      # Stands in for a Storage Box: SFTP only, no shell.
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
      environment.systemPackages = [pkgs.restic pkgs.jq];
    };
  };
  testScript = ''
    import json, re, time

    S = "${secrets}"
    PG = "runuser -u postgres -- psql -X -A -t -d"
    RESTORED_PG = "runuser -u terrace-restore -- psql -X -A -t -U postgres -h {sock} -d {db}"
    DB_TABLES = {
        "cms": ["pages", "media_refs"],
        "community": ["members", "stories"],
        "keycloak": ["user_entity"],
    }
    MEDIA = {"cms": "/var/lib/terrace-media/cms", "community": "/var/lib/terrace-media/community"}
    MARKER_DB = "TERRACE-SYNTHETIC-DB-MARKER"
    MARKER_FILE = "TERRACE-SYNTHETIC-MEDIA-MARKER"

    def sql_sig(machine, db, restore_sock=None):
        parts = []
        for table in DB_TABLES[db]:
            q = f"select '{table}:' || count(*) || ':' || coalesce(md5(string_agg(x::text, ',' order by x::text)), 'none') from {table} x"
            if restore_sock:
                cmd = RESTORED_PG.format(sock=restore_sock, db=db)
            else:
                cmd = f"{PG} {db}"
            parts.append(machine.succeed(f"{cmd} -c \"{q}\"").strip())
        return parts

    def media_sig(machine, directory):
        return machine.succeed(
            f"cd {directory} && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum"
        ).strip()

    def key_sig(machine, base):
        return machine.succeed(
            f"cd {base} && sha256sum noise_private.key derp_server_private.key"
        ).strip()

    def hs_users(machine, db):
        return machine.succeed(f"sqlite3 {db} 'select name from users order by name'").split()

    def signature():
        return {
            "db": {db: sql_sig(source, db) for db in DB_TABLES},
            "media": {n: media_sig(source, p) for n, p in MEDIA.items()},
            "keys": key_sig(source, "/var/lib/headscale"),
            "hs_users": hs_users(source, "/var/lib/headscale/db.sqlite"),
        }

    def restored_signature(dest):
        sock = f"{dest}/pgsock"
        return {
            "db": {db: sql_sig(source, db, sock) for db in DB_TABLES},
            "media": {n: media_sig(source, f"{dest}/files{p}") for n, p in MEDIA.items()},
            "keys": key_sig(source, f"{dest}/files/var/lib/headscale"),
            "hs_users": hs_users(source, f"{dest}/files/var/lib/terrace-backup/dumps/sqlite/headscale.sqlite"),
        }

    def prom(target, metric):
        out = source.succeed("cat /var/lib/terrace-backup/metrics/terrace_backup.prom")
        m = re.search(r"^" + metric + r"\{target=\"" + target + r"\"\} (\S+)$", out, re.M)
        assert m, f"metric {metric} for {target} missing:\n{out}"
        return float(m.group(1))

    def snapshots(target):
        out = source.succeed(f"terrace-backup restic {target} snapshots --json")
        return sorted(json.loads(out), key=lambda s: s["time"])

    def timed_run(unit):
        start = time.time()
        source.succeed(f"systemctl start {unit}")
        return time.time() - start

    start_all()
    t0 = time.time()
    source.wait_for_unit("postgresql.service")
    source.wait_for_unit("headscale.service")
    target.wait_for_unit("sshd.service")
    target.wait_for_open_port(22)

    with subtest("seed synthetic database rows, media files and Headscale state"):
        source.succeed("""runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d cms <<'EOF'
    CREATE TABLE pages (id serial PRIMARY KEY, slug text NOT NULL, body text NOT NULL);
    INSERT INTO pages (slug, body) SELECT 'page-' || g, 'TERRACE-SYNTHETIC-DB-MARKER ' || md5(g::text) FROM generate_series(1, 200) g;
    CREATE TABLE media_refs (id serial PRIMARY KEY, page_id int REFERENCES pages (id), filename text);
    INSERT INTO media_refs (page_id, filename) SELECT g, 'upload-' || g || '.bin' FROM generate_series(1, 40) g;
    EOF""")
        source.succeed("""runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d community <<'EOF'
    SET ROLE community;
    CREATE TABLE members (id serial PRIMARY KEY, handle text NOT NULL);
    INSERT INTO members (handle) SELECT 'synthetic-member-' || g FROM generate_series(1, 50) g;
    CREATE TABLE stories (id serial PRIMARY KEY, member_id int NOT NULL REFERENCES members (id), body text);
    INSERT INTO stories (member_id, body) SELECT 1 + (g % 50), 'synthetic story ' || g FROM generate_series(1, 120) g;
    EOF""")
        source.succeed("""runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d keycloak <<'EOF'
    CREATE TABLE user_entity (id serial PRIMARY KEY, username text NOT NULL);
    INSERT INTO user_entity (username) SELECT 'synthetic-user-' || g FROM generate_series(1, 30) g;
    EOF""")
        for name, path in MEDIA.items():
            source.succeed(f"mkdir -p {path}/nested/deeper")
            for i in range(1, 41 if name == "cms" else 11):
                source.succeed(f"head -c {1000 + i * 977} /dev/urandom > {path}/nested/file-{i}.bin")
            source.succeed(f"echo '{MARKER_FILE} {name}' > {path}/nested/deeper/marker.txt")
        source.succeed("headscale users create seed-user >/dev/null 2>&1")
        source.succeed("test -s /var/lib/headscale/noise_private.key")
        sig_a = signature()

    with subtest("secrets are throwaway, outside the store, and permissions are enforced"):
        source.succeed(f"install -d -m 0700 {S}")
        source.succeed(f"ssh-keygen -q -t ed25519 -N \"\" -f {S}/id_ed25519")
        pub = source.succeed(f"cat {S}/id_ed25519.pub").strip()
        target.succeed(
            "install -d -o restic -g users -m 0700 /home/restic/.ssh"
            f" && echo '{pub}' > /home/restic/.ssh/authorized_keys"
            " && chown restic:users /home/restic/.ssh/authorized_keys && chmod 0600 /home/restic/.ssh/authorized_keys"
        )
        source.succeed(f"ssh-keyscan -t ed25519 target > {S}/known_hosts 2>/dev/null")
        for n in ["primary", "secondary"]:
            source.succeed(f"head -c 32 /dev/urandom | base64 > {S}/{n}-password")
        source.succeed(f"chmod 0400 {S}/id_ed25519 {S}/primary-password {S}/secondary-password")
        # A group/other readable secret must be refused.
        source.succeed(f"chmod 0644 {S}/primary-password")
        out = source.fail("terrace-backup init primary 2>&1")
        assert "must not be accessible by group or others" in out, out
        source.succeed(f"chmod 0400 {S}/primary-password")
        # Without a repository nothing may be reported as backed up.
        source.fail("systemctl start terrace-backup.service")
        source.fail("terrace-backup age")
        assert prom("primary", "terrace_backup_last_run_ok") == 0
        assert prom("primary", "terrace_backup_last_success_timestamp_seconds") == 0
        source.fail("test -e /var/lib/terrace-backup/dumps")
        source.succeed("terrace-backup init primary >/dev/null 2>&1")
        source.succeed("terrace-backup init secondary >/dev/null 2>&1")

    with subtest("timers, resource bounds and monitoring units are declared"):
        timers = source.succeed("systemctl list-timers --all --no-legend")
        for t in ["terrace-backup.timer", "terrace-backup-check.timer", "terrace-backup-freshness.timer"]:
            assert t in timers, timers
        for unit in ["terrace-backup.service", "terrace-backup-check.service"]:
            show = lambda p: source.succeed(f"systemctl show {unit} -P {p}").strip()
            assert show("MemoryMax") == str(768 * 1024 * 1024), show("MemoryMax")
            assert show("MemoryHigh") == str(640 * 1024 * 1024), show("MemoryHigh")
            assert show("CPUQuotaPerSecUSec") == "500ms", show("CPUQuotaPerSecUSec")
            assert show("IOWeight") == "20", show("IOWeight")
            assert show("TasksMax") == "256", show("TasksMax")
        assert "OnCalendar=*-*-* 03:15:00" in source.succeed("systemctl cat terrace-backup.timer")

    with subtest("first backup run: both targets succeed, age signal updates"):
        d1 = timed_run("terrace-backup.service")
        ts1 = prom("primary", "terrace_backup_last_success_timestamp_seconds")
        assert ts1 > 0 and prom("secondary", "terrace_backup_last_success_timestamp_seconds") > 0
        assert prom("primary", "terrace_backup_last_run_ok") == 1
        assert prom("primary", "terrace_backup_repository_bytes") > 0
        source.succeed("terrace-backup age")
        source.succeed("systemctl start terrace-backup-freshness.service")
        source.fail("test -e /var/lib/terrace-backup/dumps")  # plaintext staging is removed
        peak = source.succeed("systemctl show terrace-backup.service -P MemoryPeak").strip()
        print(f"first backup run took {d1:.1f}s, cgroup MemoryPeak {peak} bytes")
        snap_a = snapshots("primary")[-1]["id"]
        assert len(snapshots("primary")) == 1 and len(snapshots("secondary")) == 1

    with subtest("age signal moves only on success and flags staleness"):
        time.sleep(2)
        source.fail("TERRACE_BACKUP_MAX_AGE=1 terrace-backup age")
        # Daily/weekly/monthly policies retain the oldest snapshot when there
        # are too few periods. Seed an intermediate snapshot to exercise pruning
        # without changing the production policy or losing the recovery point B.
        source.succeed("terrace-backup run primary")
        snap_intermediate = snapshots("primary")[-1]["id"]
        # Mutate to state B, then back up again.
        source.succeed("""runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d cms <<'EOF'
    INSERT INTO pages (slug, body) SELECT 'page-b-' || g, 'state-b ' || g FROM generate_series(1, 20) g;
    DELETE FROM media_refs WHERE id > 35;
    EOF""")
        m = MEDIA["cms"]
        source.succeed(f"head -c 4321 /dev/urandom > {m}/nested/new-b.bin && rm {m}/nested/file-3.bin && echo more >> {m}/nested/file-4.bin")
        source.succeed("headscale users create state-b-user >/dev/null 2>&1")
        sig_b = signature()
        timed_run("terrace-backup.service")
        ts2 = prom("primary", "terrace_backup_last_success_timestamp_seconds")
        assert ts2 > ts1, (ts1, ts2)
        snap_b = [s["id"] for s in snapshots("primary")][-1]
        source.succeed("terrace-backup age")

    with subtest("a failing target does not advance its signal and does not block the other"):
        sec_before = prom("secondary", "terrace_backup_last_success_timestamp_seconds")
        pri_before = prom("primary", "terrace_backup_last_success_timestamp_seconds")
        time.sleep(1)
        source.succeed(f"chmod 0644 {S}/secondary-password")
        source.fail("systemctl start terrace-backup.service")
        assert prom("secondary", "terrace_backup_last_success_timestamp_seconds") == sec_before
        assert prom("secondary", "terrace_backup_last_run_ok") == 0
        assert prom("primary", "terrace_backup_last_success_timestamp_seconds") > pri_before
        assert prom("primary", "terrace_backup_last_run_ok") == 1
        source.succeed(f"chmod 0400 {S}/secondary-password")

    with subtest("retention prunes old snapshots and the repository check passes"):
        d = MEDIA["community"]
        source.succeed("""runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d cms <<'EOF'
    UPDATE pages SET body = 'changed ' || body WHERE id <= 10;
    EOF""")
        source.succeed("""runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d community <<'EOF'
    DELETE FROM stories WHERE id <= 5;
    EOF""")
        source.succeed(f"head -c 999 /dev/urandom > {d}/nested/new-d.bin")
        source.succeed("headscale users create state-d-user >/dev/null 2>&1")
        sig_d = signature()
        assert sig_d != sig_b and sig_b != sig_a
        timed_run("terrace-backup.service")
        primary = [s["id"] for s in snapshots("primary")]
        assert len(primary) == 4 and snap_a in primary and snap_b in primary, primary
        assert snap_intermediate not in primary, primary
        assert len(snapshots("secondary")) == 3
        source.succeed("systemctl start terrace-backup-check.service")
        assert prom("primary", "terrace_backup_check_last_ok") == 1
        assert prom("secondary", "terrace_backup_check_last_success_timestamp_seconds") > 0

    with subtest("repository is encrypted"):
        # Positive control: the markers exist in the plaintext source.
        source.succeed(f"grep -rqF {MARKER_FILE} {MEDIA['cms']}")
        source.succeed(f"runuser -u postgres -- psql -X -A -t -d cms -c \"select 1 from pages where body like '%{MARKER_DB}%' limit 1\" | grep -qx 1")
        for repo in ["restic-primary", "restic-secondary"]:
            assert int(target.succeed(f"find /home/restic/{repo}/data -type f | wc -l")) > 0
            target.fail(f"grep -rqF TERRACE-SYNTHETIC /home/restic/{repo}")
            target.fail(f"grep -rqaF 'synthetic-member' /home/restic/{repo}")
            target.fail(f"jq . /home/restic/{repo}/config")
            target.fail(f"RESTIC_PASSWORD=wrong restic -r /home/restic/{repo} snapshots")
            target.fail(f"restic -r /home/restic/{repo} snapshots </dev/null")

    with subtest("a damaged dump fails closed, stops the isolated database and protects diagnostics"):
        # Deliberately corrupt a separate tagged snapshot, never a normal backup.
        dumps = "/var/lib/terrace-backup/dumps"
        source.succeed("terrace-backup _stage")
        source.succeed(f"echo invalid-dump > {dumps}/postgresql/broken.dump")
        source.succeed(f"terrace-backup restic primary backup --tag corrupt-restore --quiet {dumps}")
        broken = json.loads(source.succeed("terrace-backup restic primary snapshots --tag corrupt-restore --json"))[0]["id"]
        source.succeed(f"rm -rf {dumps}")
        for flag, dest in [("", "/var/tmp/restore-failed"), ("--keep-running", "/var/tmp/restore-failed-keep")]:
            out = source.fail(f"terrace-backup-restore --target primary --snapshot {broken} --dest {dest} {flag} 2>&1")
            assert "pg_restore failed" in out, out
            source.fail(f"test -e {dest}/pgdata/postmaster.pid")
            for private in ["globals.err", "globals-prepared.sql", "bootstrap-role.json", "restore-broken.err", "reconciliation.txt", "initdb.log"]:
                assert source.succeed(f"stat -c %a {dest}/{private}").strip() == "600"
            assert source.succeed(f"stat -c %a {dest}/files").strip() == "700"
            source.fail(f"runuser -u nobody -- cat {dest}/files{dumps}/postgresql/broken.dump")
        source.fail("pgrep -u terrace-restore postgres")
        assert signature() == sig_d  # failed restore never touches the source

    with subtest("destroy the source data"):
        source.succeed("systemctl stop headscale.service")
        for db in DB_TABLES:
            source.succeed(f"runuser -u postgres -- dropdb --force {db}")
        source.succeed("rm -rf /var/lib/terrace-media /var/lib/headscale/*")
        source.fail("test -e /var/lib/terrace-media")
        assert "cms" not in source.succeed("runuser -u postgres -- psql -X -A -t -l")
        # Source is gone: another backup run must fail loudly rather than record success.
        ts_before = prom("primary", "terrace_backup_last_success_timestamp_seconds")
        source.fail("systemctl start terrace-backup.service")
        assert prom("primary", "terrace_backup_last_success_timestamp_seconds") == ts_before

    with subtest("restore a chosen older snapshot into an isolated directory and database"):
        source.fail("terrace-backup-restore --target primary --snapshot x --dest /var/lib/terrace-media/restore")
        source.fail("terrace-backup-restore --target primary --snapshot x --dest /var/lib/headscale")
        dest = "/var/tmp/restore-b"
        t1 = time.time()
        out = source.succeed(f"terrace-backup-restore --target primary --snapshot {snap_b} --dest {dest} --keep-running 2>&1")
        restore_seconds = time.time() - t1
        assert "RECONCILIATION OK" in out, out
        assert source.succeed(f"stat -c %a {dest}/files").strip() == "700"
        source.fail(f"runuser -u nobody -- cat {dest}/files{MEDIA['cms']}/nested/file-4.bin")
        # Row counts, relationships and ownership survive; this is state B, not the later state D.
        sig_restored = restored_signature(dest)
        assert sig_restored == sig_b, (sig_restored, sig_b)
        assert sig_restored != sig_d
        sock = f"{dest}/pgsock"
        q = lambda db, sql: source.succeed(RESTORED_PG.format(sock=sock, db=db) + f" -c \"{sql}\"").strip()
        assert q("community", "select pg_get_userbyid(datdba) from pg_database where datname = 'community'") == "community"
        assert q("community", "select count(*) from stories s left join members m on m.id = s.member_id where m.id is null") == "0"
        assert int(q("community", "select count(*) from pg_constraint where contype = 'f'")) >= 1
        assert "state-b-user" in hs_users(source, f"{dest}/files/var/lib/terrace-backup/dumps/sqlite/headscale.sqlite")
        # Isolation: no TCP listener, source cluster untouched.
        assert source.succeed(f"sed -n 6p {dest}/pgdata/postmaster.pid").strip() == ""
        assert "cms" not in source.succeed("runuser -u postgres -- psql -X -A -t -l")
        print(f"isolated restore of snapshot B took {restore_seconds:.1f}s")

    with subtest("reconciliation detects tampering"):
        victim = f"{dest}/files{MEDIA['cms']}/nested/file-4.bin"
        source.succeed(f"echo x >> {victim}")
        out = source.fail(f"terrace-backup-restore --verify-only {dest} 2>&1")
        assert "sha256 mismatch" in out, out
        source.succeed(f"truncate -s -2 {victim}")
        source.succeed(f"terrace-backup-restore --verify-only {dest}")
        source.succeed(RESTORED_PG.format(sock=sock, db="keycloak") + " -c \"delete from user_entity where id = 1\"")
        out = source.fail(f"terrace-backup-restore --verify-only {dest} 2>&1")
        assert "row counts differ" in out, out
        source.succeed(f"terrace-backup-restore --stop {dest}")

    with subtest("restore the latest snapshot from the secondary target"):
        dest2 = "/var/tmp/restore-d"
        out = source.succeed(f"terrace-backup-restore --target secondary --dest {dest2} --keep-running 2>&1")
        assert "RECONCILIATION OK" in out, out
        assert restored_signature(dest2) == sig_d
        source.succeed(f"terrace-backup-restore --stop {dest2}")

    with subtest("a successful restore stops its database by default"):
        dest3 = "/var/tmp/restore-auto-stop"
        out = source.succeed(f"terrace-backup-restore --target secondary --dest {dest3} 2>&1")
        assert "RECONCILIATION OK" in out, out
        source.fail(f"test -e {dest3}/pgdata/postmaster.pid")
        source.fail("pgrep -u terrace-restore postgres")
        print(f"total test time {time.time() - t0:.1f}s")
  '';
}
