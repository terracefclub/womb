"""Tiny real PostgreSQL18 proof; private disposable socket-only clusters."""
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location("bootstrap_identity", sys.argv[1])
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)
work = Path(os.environ["TMPDIR"]) / "pg-proof"
clusters = []
observed = []

# The exact semantic fingerprints used by the combined recovery rehearsal.
FINGERPRINTS = [
    "SELECT md5(coalesce(string_agg(row_to_json(r)::text,chr(10) ORDER BY r.rolname),'')) FROM (SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolconnlimit,rolpassword,rolvaliduntil,rolbypassrls FROM pg_authid) r",
    "SELECT md5(coalesce(string_agg(row_to_json(r)::text,chr(10) ORDER BY r.member,r.role,r.grantor),'')) FROM (SELECT m.rolname AS member,g.rolname AS role,b.rolname AS grantor,a.admin_option,a.inherit_option,a.set_option FROM pg_auth_members a JOIN pg_roles m ON m.oid=a.member JOIN pg_roles g ON g.oid=a.roleid JOIN pg_roles b ON b.oid=a.grantor) r",
    "SELECT md5(coalesce(string_agg(row_to_json(r)::text,chr(10) ORDER BY r.role,r.database),'')) FROM (SELECT p.rolname AS role,d.datname AS database,a.setconfig FROM pg_db_role_setting a LEFT JOIN pg_roles p ON p.oid=a.setrole LEFT JOIN pg_database d ON d.oid=a.setdatabase) r",
]


def command(arguments, value=None, expect=0):
    result = subprocess.run(arguments, input=value, stdout=subprocess.PIPE, stderr=errors,
                            timeout=30, env={"PATH": os.environ["PATH"], "LC_ALL": "C", "PGCONNECT_TIMEOUT": "5"})
    if result.returncode != expect or len(result.stdout) > identity.MAX_SQL:
        raise ValueError()
    return result.stdout


def cluster(label, name):
    directory = work / label
    directory.mkdir(mode=0o700)
    socket = directory / "sock"
    socket.mkdir(mode=0o700)
    data = directory / "data"
    command(["initdb", "-D", str(data), "-U", name, "--auth-local=trust", "--no-instructions", "--no-locale", "-E", "UTF8"])
    item = {"directory": directory, "socket": socket, "data": data, "name": name}
    clusters.append(item)
    command(["pg_ctl", "-D", str(data), "-w", "-t", "15", "-l", str(directory / "postgres.log"),
             "-o", "-c listen_addresses= -c unix_socket_directories=" + str(socket) + " -c fsync=off -c shared_buffers=16MB", "start"])
    pid = int((data / "postmaster.pid").read_text().splitlines()[0])
    start = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(') ', 1)[1].split()[19]
    observed.append({"pid": pid, "start": start})
    return item


def psql(item, query, database="postgres", expect=0):
    return command(["psql", "-XqAt", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=sqlstate", "-h", str(item["socket"]),
                    "-U", item["name"], "-d", database, "-f", "-"], query.encode(), expect).decode().strip()


def helper(arguments, value=None, expect=0):
    return command([sys.executable, sys.argv[1], *map(str, arguments)], value, expect)


def signature(item):
    return [psql(item, query) for query in FINGERPRINTS]


def backup(item, suffix):
    sql = work / (suffix + ".sql")
    identity.write_private(sql, command(["pg_dumpall", "--globals-only", "-h", str(item["socket"]), "-U", item["name"]]))
    metadata = work / (suffix + ".json")
    helper(["capture", sql, metadata], (psql(item, identity.QUERY) + "\n").encode())
    dump = work / (suffix + ".dump")
    identity.write_private(dump, command(["pg_dump", "--format=custom", "--create", "-h", str(item["socket"]), "-U", item["name"], "saved"]))
    return sql, metadata, dump


def restored(sql, metadata, dump, label):
    prepared = work / (label + "-prepared.sql")
    retained = work / (label + "-bootstrap.json")
    helper(["prepare", sql, metadata, prepared, retained])
    name = helper(["name", retained]).decode().rstrip("\n")
    item = cluster(label, name)
    assert psql(item, "SELECT oid=10 FROM pg_roles WHERE rolname=current_user") == "t"
    psql(item, prepared.read_text())
    command(["pg_restore", "--create", "--exit-on-error", "--dbname=postgres", "-h", str(item["socket"]), "-U", name], dump.read_bytes())
    assert psql(item, "SELECT count(*) FROM retained", "saved") == "1"
    assert psql(item, "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='saved'") == "terrace_windmill"
    return item


def source_fixture(label, name, identifier):
    source = cluster(label, name)
    password = secrets.token_hex(24)
    psql(source, "CREATE ROLE windmill_user NOLOGIN; CREATE ROLE windmill_admin NOLOGIN BYPASSRLS; "
         "CREATE ROLE terrace_windmill LOGIN CREATEROLE CONNECTION LIMIT 10 PASSWORD '" + password + "'; "
         "CREATE ROLE zz_auditor LOGIN NOINHERIT; CREATE ROLE zz_audited NOLOGIN; "
         "GRANT windmill_user TO windmill_admin WITH ADMIN FALSE, INHERIT TRUE, SET TRUE GRANTED BY " + identifier + "; "
         "GRANT windmill_admin,windmill_user TO terrace_windmill WITH ADMIN FALSE, INHERIT TRUE, SET TRUE GRANTED BY " + identifier + "; "
         "GRANT zz_audited TO zz_auditor WITH ADMIN TRUE, INHERIT FALSE, SET FALSE GRANTED BY " + identifier + "; "
         "ALTER ROLE " + identifier + " SET work_mem='2MB'; ALTER ROLE " + identifier + " PASSWORD '" + secrets.token_hex(24) + "'; "
         "CREATE DATABASE saved OWNER terrace_windmill;")
    psql(source, "CREATE TABLE retained (value text); INSERT INTO retained VALUES ('synthetic-retained'); ALTER TABLE retained OWNER TO terrace_windmill;", "saved")
    psql(source, "ALTER DATABASE saved SET work_mem='4MB';")
    return source


def proof():
    assert re.search(rb"PostgreSQL\) 18[.]", command(["postgres", "--version"]))
    source = source_fixture("source", "postgres", "postgres")
    sql, metadata, dump = backup(source, "source")
    old = cluster("old", "terrace-restore")
    before = errors.tell()
    psql(old, sql.read_text(), expect=3)
    errors.flush()
    with errors_path.open("rb") as stream:
        stream.seek(before)
        failure = stream.read()
        rejected = re.search(rb"psql:<stdin>:([0-9]+): ERROR:\s+42501", failure)
        assert rejected is not None
        line = sql.read_bytes().splitlines()[int(rejected.group(1)) - 1]
        assert line.startswith(b"GRANT windmill_admin TO terrace_windmill ") and b"GRANTED BY postgres" in line
    print("Native PostgreSQL18 old grantor failure reproduced.", flush=True)
    target = restored(sql, metadata, dump, "restored")
    assert signature(target) == signature(source)
    assert psql(target, "SELECT count(*) FROM pg_roles WHERE rolname='terrace-restore'") == "0"
    assert psql(target, "SELECT count(*)=3 AND bool_and(g.rolname='postgres' AND NOT a.admin_option AND a.inherit_option AND a.set_option) FROM pg_auth_members a JOIN pg_roles g ON g.oid=a.grantor JOIN pg_roles r ON r.oid=a.roleid JOIN pg_roles m ON m.oid=a.member WHERE (r.rolname='windmill_user' AND m.rolname='windmill_admin') OR (r.rolname IN ('windmill_admin','windmill_user') AND m.rolname='terrace_windmill')") == "t"
    print("Native PostgreSQL18 bootstrap identity and all semantic fingerprints preserved.", flush=True)

    # Real SQL errors still abort before any following statement can run.
    bad_sql = work / "bad.sql"
    identity.write_private(bad_sql, sql.read_bytes() + b"SELECT missing_restore_probe();\nCREATE ROLE later_sentinel;\n")
    bad_metadata = work / "bad.json"
    helper(["capture", bad_sql, bad_metadata], (psql(source, identity.QUERY) + "\n").encode())
    prepared = work / "bad-prepared.sql"
    helper(["prepare", bad_sql, bad_metadata, prepared, work / "bad-retained.json"])
    bad = cluster("bad", "postgres")
    psql(bad, prepared.read_text(), expect=3)
    assert psql(bad, "SELECT count(*) FROM pg_roles WHERE rolname='later_sentinel'") == "0"
    # Exact metadata/byte ambiguity rejects before a cluster is created.
    helper(["prepare", sql, work / "missing.json", work / "never.sql", work / "never.json"], expect=1)
    changed = work / "changed.sql"
    identity.write_private(changed, sql.read_bytes() + b"-- changed\n")
    helper(["prepare", changed, metadata, work / "changed-out.sql", work / "changed-out.json"], expect=1)
    duplicate = work / "duplicate.sql"
    identity.write_private(duplicate, sql.read_bytes() + b"CREATE ROLE postgres;\n")
    helper(["capture", duplicate, work / "duplicate.json"], (psql(source, identity.QUERY) + "\n").encode(), expect=1)
    unsupported = psql(source, "ALTER ROLE postgres NOLOGIN; " + identity.QUERY + "; ALTER ROLE postgres LOGIN;")
    helper(["capture", sql, work / "nologin.json"], (unsupported + "\n").encode(), expect=1)
    print("Native PostgreSQL18 genuine errors and unsupported metadata rejected.", flush=True)

    # A separate source exercises actual quoted-space/quote bootstrap syntax.
    quoted_source = source_fixture("quoted-source", 'Source "Root', '"Source ""Root"')
    sql2, metadata2, dump2 = backup(quoted_source, "quoted")
    quoted = restored(sql2, metadata2, dump2, "quoted-restored")
    assert signature(quoted) == signature(quoted_source)
    print("Native PostgreSQL18 quoted bootstrap identity restored.", flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    work.mkdir(mode=0o700)
    errors_path = work / "commands-private.log"
    passed = False
    with errors_path.open("w+b") as errors:
        try:
            proof()
            passed = True
        except Exception:
            print("Native PostgreSQL18 bootstrap proof failed; private SQL suppressed.", file=sys.stderr)
        finally:
            for item in reversed(clusters):
                if (item["data"] / "postmaster.pid").exists():
                    try:
                        command(["pg_ctl", "-D", str(item["data"]), "-m", "fast", "-w", "-t", "15", "stop"])
                    except Exception:
                        passed = False
            for row in observed:
                deadline = time.monotonic() + 5
                while Path('/proc/' + str(row['pid'])).exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if Path('/proc/' + str(row['pid'])).exists():
                    passed = False
    if not passed:
        sys.exit(1)
    Path(sys.argv[2]).write_text(json.dumps({"postgresMajor": 18, "oldFailure": "42501", "semanticFingerprints": 3,
                                          "quotedBootstrap": True, "genuineErrorsRejected": True,
                                          "ownedClustersStopped": len(observed)}) + "\n")
    print("Native PostgreSQL18 bootstrap proof passed; all owned clusters stopped.", flush=True)
