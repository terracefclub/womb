"""Exercise installed, leased synthetic hosts through their pinned operator SSH."""

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time


CA = "/var/lib/terrace-rehearsal-trust/ca.pem"
GATEWAY_INPUTS = "/var/lib/terrace-gateway-test"
ROLES = ("app", "relay")
APP_WRITERS = (
    "postgresql", "keycloak", "nginx", "k3s", "terrace-web", "terrace-cms",
    "terrace-documenso", "podman-terrace-windmill-server",
    "podman-terrace-windmill-worker", "vaultwarden",
)
RELAY_WRITERS = ("headscale", "nginx")
APP_ONESHOTS = (
    "cloud-rehearsal-inputs", "terrace-website-inputs", "terrace-cms-migrate", "terrace-documenso-migrate",
    "terrace-documenso-bootstrap", "terrace-windmill-migrate", "terrace-windmill-bootstrap",
    "terrace-vaultwarden-migrate", "terrace-vaultwarden-migration-server",
    "terrace-vaultwarden-bootstrap", "terrace-vaultwarden-guard", "terrace-identity-health",
    "documenso-test-mailpit",
)
APP_SETUP = (
    "terrace-documenso-inputs", "terrace-documenso-database", "terrace-documenso-image",
    "terrace-windmill-db", "terrace-vaultwarden-inputs", "terrace-vaultwarden-database",
    "keycloakPostgreSQLInit",
)
RELAY_SETUP = ("cloud-rehearsal-relay-address",)
BACKUP_UNITS = ("terrace-backup", "terrace-backup-check", "terrace-backup-freshness")
APP_PATHS = (
    GATEWAY_INPUTS, "/var/lib/terrace-website-inputs", "/var/lib/terrace-cms-media",
    "/var/lib/rancher/k3s/server", "/var/lib/rancher/k3s/storage", "/etc/rancher/node",
    "/var/lib/terrace-documenso-inputs", "/var/lib/terrace-documenso",
    "/var/lib/terrace-windmill-inputs", "/var/lib/terrace-windmill",
    "/var/lib/terrace-vaultwarden-inputs", "/var/lib/terrace-vaultwarden",
    "/var/lib/vaultwarden",
)
PROTOCOL_RESULT = (
    "Native HTTPS protocol checks passed: 198 bounded assertions; "
    "SSO, roles, state, CSRF, cookies, publication, private cache and logout."
)
SNAPSHOT = re.compile(r"[a-f0-9]{64}")
KUBE_NAME = re.compile(r"[a-z0-9](?:[-.a-z0-9]{0,251}[a-z0-9])?")
NODE_UID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
VOLUME = re.compile(r"pvc-[a-f0-9-]{36}")


class Rejected(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Rejected(reason)


# The commands below follow the accepted native order in platform/full/scenario.py.
# This two-host run checks native gateway protocol, application health, PVC and
# relay identity. Browser, signing, automation and peer topology are separate.
SQL = r"""
sql() {
  runuser -u postgres -- psql -XAt -w -v ON_ERROR_STOP=1 -d "$1" -c "$2"
}
"""
WAIT = r"""
wait_for() {
  for attempt in $(seq 1 40); do
    if "$@" >/dev/null 2>&1; then return; fi
    sleep 2
  done
  return 1
}
"""
INACTIVE = r"""
expected=$'ActiveState=inactive\nControlPID=0\nLoadState=loaded\nMainPID=0'
for name in "$@"; do
  if [[ "$name" == *.timer ]]; then
    facts=$(systemctl show "$name" -p LoadState,ActiveState | LC_ALL=C sort)
    test "$facts" = $'ActiveState=inactive\nLoadState=loaded'
  else
    facts=$(systemctl show "$name" -p LoadState,ActiveState,MainPID,ControlPID | LC_ALL=C sort)
    test "$facts" = "$expected"
  fi
done
"""
BOOTSTRAP_APP = SQL + WAIT + r"""
export SSL_CERT_FILE=/var/lib/terrace-rehearsal-trust/ca.pem
systemctl start postgresql.target keycloak.service nginx.service
count=$(sql keycloak "select count(*) from user_entity where realm_id=(select id from realm where name='terrace')")
if test "$count" = 0; then python3 /etc/gateway-protocol.py seed >/dev/null; fi
test "$(sql keycloak "select count(*) from user_entity where realm_id=(select id from realm where name='terrace')")" = 5
test "$(sql keycloak "select count(*) from user_entity where username='gateway-fixture-bootstrap'")" = 0
envfile=/var/lib/terrace-gateway-test/keycloak.env
test -f "$envfile"
test ! -L "$envfile"
chmod 0600 "$envfile"
truncate -s 0 "$envfile"
chmod 0400 "$envfile"
systemctl start terrace-cms-migrate.service
systemctl start terrace-cms.service terrace-web.service
systemctl start k3s.service
wait_for k3s kubectl wait --for=condition=Ready node/gateway --timeout=5s
local_retain() {
  test "$(k3s kubectl get storageclass local-retain -o jsonpath='{.reclaimPolicy}')" = Retain
}
wait_for local_retain
systemctl start terrace-documenso-migrate.service
systemctl start terrace-documenso-bootstrap.service
systemctl start terrace-documenso.service
wait_for curl --fail --silent --max-time 5 http://127.0.0.1:13050/api/health
curl --fail --silent --max-time 5 http://127.0.0.1:13050/api/health |
  jq -e '.checks.database.status == "ok"' >/dev/null
systemctl start terrace-vaultwarden-migrate.service
systemctl start terrace-windmill-migrate.service
"""
BOOTSTRAP_RELAY = WAIT + r"""
systemctl start headscale.service nginx.service
if headscale users list -o json | jq -e 'length == 0' >/dev/null; then
  headscale users create rehearsal-synthetic >/dev/null
fi
headscale users list -o json | jq -e 'length == 1 and .[0].name == "rehearsal-synthetic"' >/dev/null
wait_for curl --fail --silent --max-time 5 --cacert /var/lib/terrace-rehearsal-trust/ca.pem https://relay.terrace.invalid/health
sqlite3 /var/lib/headscale/db.sqlite "PRAGMA integrity_check" | grep -qx ok
"""
APP_SIGNATURE = SQL + r"""
sql keycloak "select id from realm where name='terrace'"
sql keycloak "select md5(string_agg(id||username,chr(10) order by id)) from user_entity"
sql terrace_cms 'select name||batch from payload_migrations order by id'
sql terrace_documenso 'select migration_name||checksum from "_prisma_migrations" order by migration_name'
sql terrace_documenso 'select md5(coalesce(string_agg(row_to_json(r)::text,chr(10) order by r.id),'"'"''"'"')) from "User" r'
sql terrace_vaultwarden 'select version from __diesel_schema_migrations order by version'
sql postgres "select md5(coalesce(string_agg(row_to_json(r)::text,chr(10) order by r.rolname),'')) from
  (select rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolconnlimit,rolpassword,rolvaliduntil,rolbypassrls from pg_authid) r"
sql postgres "select md5(coalesce(string_agg(row_to_json(r)::text,chr(10) order by r.member,r.role,r.grantor),'')) from
  (select m.rolname as member,g.rolname as role,b.rolname as grantor,a.admin_option,a.inherit_option,a.set_option
  from pg_auth_members a join pg_roles m on m.oid=a.member join pg_roles g on g.oid=a.roleid
  join pg_roles b on b.oid=a.grantor) r"
sql postgres "select md5(coalesce(string_agg(row_to_json(r)::text,chr(10) order by r.role,r.database),'')) from
  (select p.rolname as role,d.datname as database,a.setconfig from pg_db_role_setting a
  left join pg_roles p on p.oid=a.setrole left join pg_database d on d.oid=a.setdatabase) r"
for table in password workspace usr script variable; do
  sql terrace_windmill "select md5(coalesce(string_agg(row_to_json(r)::text,chr(10) order by row_to_json(r)::text),'')) from $table r"
done
for table in users ciphers attachments; do
  sql terrace_vaultwarden "select md5(coalesce(string_agg(row_to_json(r)::text,chr(10) order by row_to_json(r)::text),'')) from $table r"
done
sha256sum /var/lib/terrace-website-inputs/*.env \
  /var/lib/terrace-documenso-inputs/secrets.json \
  /var/lib/terrace-windmill-inputs/bootstrap.json /var/lib/terrace-windmill/migrated-image \
  /var/lib/terrace-vaultwarden-inputs/input.json /var/lib/vaultwarden/rsa_key.pem
"""
MEDIA_FIXTURE = SQL + r"""
media=/var/lib/terrace-cms-media
test ! -e "$media/private.png"
test ! -L "$media/private.png"
test ! -e "$media/public.png"
test ! -L "$media/public.png"
printf '%s' 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGPgUbL4DwACCgFmeGgpMwAAAABJRU5ErkJggg==' |
  base64 -d >"$media/private.png"
install -m 0600 -o terrace_cms -g terrace_cms "$media/private.png" "$media/public.png"
chown terrace_cms:terrace_cms "$media/private.png"
chmod 0600 "$media/private.png"
sql terrace_cms "insert into media(alt,public,filename,mime_type,filesize,width,height)
  values ('Synthetic private',false,'private.png','image/png',70,1,1),
         ('Synthetic public',true,'public.png','image/png',70,1,1)" >/dev/null
# This run does not use the retained-device browser fixture. Keep its synthetic
# vault master passwords out of the server backup.
path=/var/lib/terrace-gateway-test/vaultwarden-browser.json
test -f "$path"
test ! -L "$path"
rm -- "$path"
"""
PVC_PROBE = WAIT + r"""
k3s kubectl apply -f /etc/rehearsal-probe.yaml >/dev/null
wait_for k3s kubectl wait --for=condition=Ready pod/rehearsal --timeout=5s
if test "$1" = original; then
  k3s kubectl exec rehearsal -- /bin/busybox sh -c 'printf "retained-synthetic\n" >/data/marker'
else
  k3s kubectl exec rehearsal -- /bin/busybox sh -c \
    'test "$(cat /data/marker)" = retained-synthetic && printf "restored-write\n" >/data/restored'
  k3s kubectl exec rehearsal -- /bin/busybox grep -qx restored-write /data/restored
fi
k3s kubectl get node gateway -o jsonpath='{.metadata.uid}{"\n"}'
k3s kubectl get pvc rehearsal -o jsonpath='{.spec.volumeName}{"\n"}'
"""
STOP_APP = r"""
systemctl stop vaultwarden.service
systemctl stop podman-terrace-windmill-worker.service
systemctl stop podman-terrace-windmill-server.service
systemctl stop terrace-documenso.service terrace-web.service terrace-cms.service keycloak.service nginx.service
systemctl stop "$@"
k3s kubectl delete pod --all --all-namespaces --wait=true --timeout=120s >/dev/null
systemctl stop k3s.service
systemctl kill --kill-whom=all --signal=SIGKILL k3s.service >/dev/null 2>&1 || true
for attempt in $(seq 1 30); do
  if rehearsal-cgroups-empty; then break; fi
  sleep 1
done
rehearsal-cgroups-empty
if pgrep -x 'containerd|containerd-shim|k3s' >/dev/null; then exit 1; else test "$?" = 1; fi
containers=$(podman ps -q)
test -z "$containers"
"""
RESTORE = r"""
test ! -e /var/lib/rehearsal-recovery
test ! -L /var/lib/rehearsal-recovery
umask 077
set -o noclobber
terrace-backup-restore --target primary --snapshot "$1" --dest /var/lib/rehearsal-recovery >/var/tmp/rehearsal-restore.log 2>&1
grep -qx 'RECONCILIATION OK' /var/lib/rehearsal-recovery/reconciliation.txt
"""
COPY_RESTORED = r"""
for target in "$@"; do
  source=/var/lib/rehearsal-recovery/files$target
  test -d "$source"
  test ! -L "$source"
  test ! -L "$target"
  if test "$target" = /var/lib/rancher/k3s/server; then
    entries=$(find "$target" -mindepth 1 -printf '%y %P\n' | LC_ALL=C sort)
    test "$entries" = $'d manifests\nl manifests/local-retain.yaml'
    manifest=$target/manifests/local-retain.yaml
    link=$(readlink "$manifest")
    test -f "$manifest"
    [[ "$link" == /nix/store/* ]]
    test "$link" = "$(readlink "/var/lib/rehearsal-recovery/files$manifest")"
  elif test -e "$target"; then
    entries=$(ls -A "$target")
    test -z "$entries"
  fi
  mkdir -p "$target"
  cp -a "$source/." "$target/"
done
"""
ADOPT_POSTGRES = SQL + r"""
pg_dir=$(cat /etc/rehearsal-pg-directory)
[[ "$pg_dir" =~ ^/var/lib/postgresql/[0-9]+$ ]]
test ! -L "$pg_dir"
if test -e "$pg_dir"; then
  entries=$(ls -A "$pg_dir")
  test -z "$entries"
fi
install -d -m 0700 -o postgres -g postgres "$pg_dir"
cp -a /var/lib/rehearsal-recovery/pgdata/. "$pg_dir/"
chown -R postgres:postgres "$pg_dir"
systemctl start postgresql.target
test "$(sql postgres "select rolsuper and rolcanlogin from pg_authid where oid=10 and rolname='postgres'")" = t
test "$(sql postgres "select count(*) from pg_roles where rolname='terrace-restore'")" = 0
test "$(sql postgres "select count(*) from pg_auth_members where roleid=10 or member=10")" = 0
test "$(sql postgres "select count(*)=5 and bool_and(not pg_has_role(oid,10::oid,'MEMBER')
  and not pg_has_role(oid,10::oid,'SET')) from pg_roles where rolname in
  ('keycloak','terrace_cms','terrace_documenso','terrace_vaultwarden','terrace_windmill')")" = t
test "$(sql postgres "select count(*) from pg_stat_activity
  where usesysid=10 and backend_type='client backend' and pid<>pg_backend_pid()")" = 0
[[ "$(sql postgres "show hba_file")" == /nix/store/* ]]
test "$(sql postgres "show fsync")" = on
for user in terrace-restore postgres; do
  if runuser -u terrace-restore -- psql -X -w -h /run/postgresql -U "$user" -d postgres -c 'select 1' >/dev/null 2>&1; then
    exit 1
  fi
done
test "$(systemctl show vaultwarden.service -p User --value)" = terrace_vaultwarden
check_login() {
  "$@" -c 'select 1' >/dev/null 2>&1
  if "$@" -c 'set role postgres' >/dev/null 2>&1; then return 1; fi
}
for user in terrace_cms terrace_windmill terrace_vaultwarden; do
  check_login runuser -u "$user" -- psql -X -w -v ON_ERROR_STOP=1 -h /run/postgresql -U "$user" -d "$user"
done
PGPASSWORD="$(cat /var/lib/terrace-gateway-test/db-password)" \
  check_login psql -X -w -v ON_ERROR_STOP=1 -h 127.0.0.1 -U keycloak -d keycloak
PGPASSWORD="$(jq -r .databasePassword /var/lib/terrace-documenso-inputs/secrets.json)" \
  check_login psql -X -w -v ON_ERROR_STOP=1 -h /run/postgresql -U terrace_documenso -d terrace_documenso
"""

WIND_ROLES = SQL + r"""
test "$(sql postgres "select count(*)=2 and bool_and(not rolcanlogin and not rolsuper and not rolcreaterole
  and not rolcreatedb and not rolreplication and rolpassword is null and rolbypassrls=(rolname='windmill_admin'))
  from pg_authid where rolname in ('windmill_user','windmill_admin')")" = t
expected=$'terrace_windmill:windmill_admin:postgres:false:true:true\nterrace_windmill:windmill_user:postgres:false:true:true\nwindmill_admin:windmill_user:postgres:false:true:true'
grants=$(sql postgres "select member.rolname||':'||group_role.rolname||':'||grantor.rolname||':'||
  m.admin_option::text||':'||m.inherit_option::text||':'||m.set_option::text
  from pg_auth_members m join pg_roles member on member.oid=m.member join pg_roles group_role on group_role.oid=m.roleid
  join pg_roles grantor on grantor.oid=m.grantor where group_role.rolname in ('windmill_user','windmill_admin') order by 1")
test "$grants" = "$expected"
test "$(sql postgres "select count(*)=5 and bool_and(rolcanlogin and not rolsuper and not rolcreaterole
  and not rolcreatedb and not rolreplication and not rolbypassrls)
  from pg_roles where rolname in ('keycloak','terrace_cms','terrace_documenso','terrace_vaultwarden','terrace_windmill')")" = t
"""
START_APPLICATIONS = WAIT + r"""
systemctl start podman-terrace-windmill-server.service podman-terrace-windmill-worker.service
wait_for curl --fail --silent --max-time 5 http://127.0.0.1:18001/api/version
systemctl is-active --quiet podman-terrace-windmill-worker.service
systemctl start vaultwarden.service
systemctl start terrace-vaultwarden-bootstrap.service
wait_for curl --fail --silent --max-time 5 http://127.0.0.1:18222/alive
"""
RELAY_SIGNATURE = r"""
headscale users list -o json | jq -cS 'sort_by(.id)'
sha256sum /var/lib/headscale/noise_private.key /var/lib/headscale/derp_server_private.key
"""
INSTALL_INPUT = r"""
test ! -e "$1"
test ! -L "$1"
umask 077
set -o noclobber
cat >"$1"
chmod "$2" "$1"
digest=$(sha256sum "$1")
test "${digest%% *}" = "$3"
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def owned_ids(receipt, kind):
    values = receipt[kind]
    require(isinstance(values, list) and len(values) == 2
            and all(type(value) is int and value > 0 for value in values)
            and len(set(values)) == 2, "invalid owned cohort IDs")
    return sorted(values)


def check_original_absence(run, original, replacement, absence, backup):
    require(backup["run"] == run and backup["verified"] is True, "backup receipt rejected")
    require(absence["run"] == run and absence["phase"] == "original"
            and absence["baselinePreserved"] is True and absence["absenceReads"] == 3
            and absence["unresolvedDispatches"] == [],
            "original absence receipt rejected")
    for kind in ("servers", "primary_ips"):
        old = owned_ids(original, kind)
        new = owned_ids(replacement, kind)
        require(not set(old).intersection(new) and absence["verifiedAbsent"][kind] == old,
                "replacement overlaps the original cohort")
    require(backup["servers"] == owned_ids(original, "servers"), "backup is for different originals")
    require(absence["verifiedAbsent"]["firewalls"] == [] and absence["verifiedAbsent"]["ssh_keys"] == [],
            "original retirement included shared resources")


def check_snapshots(value, expected=None):
    require(isinstance(value, list) and len(value) == 2, "off-host snapshot inventory rejected")
    result = {}
    for snapshot in value:
        host, snapshot_id = snapshot["hostname"], snapshot["id"]
        require(host in ("gateway", "relay") and host not in result
                and isinstance(snapshot_id, str) and SNAPSHOT.fullmatch(snapshot_id),
                "off-host snapshot identity rejected")
        result[host] = snapshot_id
    require(len(set(result.values())) == 2, "duplicate off-host snapshot identity")
    require(expected is None or expected == result, "off-host snapshots changed")
    return result


def deployment_replicas(data):
    result = []
    items = json.loads(data)["items"]
    require(isinstance(items, list) and len(items) <= 32, "deployment inventory rejected")
    for item in items:
        namespace, name = item["metadata"]["namespace"], item["metadata"]["name"]
        replicas = item["spec"]["replicas"]
        require(isinstance(namespace, str) and KUBE_NAME.fullmatch(namespace)
                and isinstance(name, str) and KUBE_NAME.fullmatch(name)
                and type(replicas) is int and 0 <= replicas <= 32, "deployment identity rejected")
        result.append([namespace, name, replicas])
    require(len({(row[0], row[1]) for row in result}) == len(result), "duplicate deployment identity")
    return sorted(result)


class Host:
    def __init__(self, directory, role, binding, ssh, runner, socket, expires):
        self.directory = directory
        self.role = role
        self.id = binding["serverId"]
        self.ssh = ssh
        self.runner = runner
        self.socket = socket
        self.expires = expires

    def run(self, stage, script, *args, seconds=30, capture=False, data=None, repository=False):
        reserve = min(seconds, int(self.expires - time.time()) - 30)
        require(reserve >= 5 and not os.path.lexists(self.directory / "closed.json"),
                "service lease closed or expired")
        command = shlex.join([
            "timeout", "--kill-after=10", str(reserve), "sudo", "-n", "bash", "-ceu",
            "set -o pipefail\n" + script, "terrace", *args,
        ])
        ssh = self.ssh
        if repository:
            ssh = [*ssh[:-1], "-o", "ExitOnForwardFailure=yes",
                   "-R", "127.0.0.1:22422:" + str(self.socket), ssh[-1]]
        try:
            if data is None:
                return self.runner([*ssh, command], reserve + 20, capture=capture)
            with tempfile.TemporaryFile() as stream:
                stream.write(data)
                stream.seek(0)
                return self.runner([*ssh, command], reserve + 20, stdin=stream, capture=capture)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise Rejected(f"{self.role} {stage} failed") from None


def install_runtime(host, inputs):
    host.run("runtime-input-directories", r"""
for dir in /var/lib/terrace-rehearsal-tls /var/lib/rehearsal-escrow /var/lib/terrace-rehearsal-trust; do
  test ! -L "$dir"
  if test -e "$dir"; then
    test -d "$dir"
    test "$(stat -c %u "$dir")" = 0
    entries=$(ls -A "$dir")
    test -z "$entries"
  fi
done
install -d -m 0700 /var/lib/terrace-rehearsal-tls /var/lib/rehearsal-escrow
install -d -m 0755 /var/lib/terrace-rehearsal-trust
""")
    for name, path, mode in (
        ("ca.pem", CA, "0644"),
        (host.role + "-cert.pem", "/var/lib/terrace-rehearsal-tls/cert.pem", "0444"),
        (host.role + "-key.pem", "/var/lib/terrace-rehearsal-tls/key.pem", "0400"),
        ("repository-password", "/var/lib/rehearsal-escrow/repository-password", "0400"),
    ):
        value = inputs[name]
        host.run("runtime-input-copy", INSTALL_INPUT, path, mode, digest(value), data=value)


def inactive(host, names):
    services = (*names, *BACKUP_UNITS, "cloud-rehearsal-tls")
    timers = BACKUP_UNITS
    if host.role == "app":
        services += (*APP_ONESHOTS, *APP_SETUP)
        timers += ("terrace-identity-health",)
        if "postgresql" in names:
            services += ("postgresql-setup",)
    else:
        services += RELAY_SETUP
    host.run("writer-absence", INACTIVE, *(name + ".service" for name in services),
             *(name + ".timer" for name in timers))


def bootstrap_app(host):
    host.run("gateway-bootstrap", BOOTSTRAP_APP, seconds=720)
    host.run("windmill-role-boundary", WIND_ROLES)
    host.run("application-readiness", START_APPLICATIONS, seconds=420)


def bootstrap_relay(host):
    host.run("relay-bootstrap", BOOTSTRAP_RELAY, seconds=180)
    return digest(host.run("relay-identity", RELAY_SIGNATURE, capture=True))


def app_signature(host):
    return digest(host.run("application-signature", APP_SIGNATURE, seconds=90, capture=True))


def protocol(host, phase):
    require(phase in ("", "-restored"), "protocol phase rejected")
    result = host.run(
        "gateway-https-protocol",
        'SSL_CERT_FILE="$1" TERRACE_SYNTHETIC_PHASE="$2" python3 /etc/gateway-protocol.py test',
        CA, phase, seconds=180, capture=True,
    )
    require(result.decode().strip() == PROTOCOL_RESULT, "gateway protocol witness rejected")
    return 198


def pvc(host, phase):
    raw = host.run("retained-pvc", PVC_PROBE, phase, seconds=330, capture=True)
    node, volume = raw.decode().splitlines()
    require(NODE_UID.fullmatch(node) and VOLUME.fullmatch(volume), "PVC identity rejected")
    return {"nodeUid": node, "volume": volume}


def scale(host, replicas, *, stop):
    for namespace, name, count in replicas:
        require(isinstance(namespace, str) and KUBE_NAME.fullmatch(namespace)
                and isinstance(name, str) and KUBE_NAME.fullmatch(name)
                and type(count) is int and 0 <= count <= 32, "invalid saved deployment")
        host.run("deployment-scale",
                 'k3s kubectl scale deployment "$2" -n "$1" --replicas="$3" >/dev/null',
                 namespace, name, str(0 if stop else count))


def quiesce(app, relay):
    replicas = deployment_replicas(app.run(
        "deployment-inventory", "k3s kubectl get deployments --all-namespaces -o json", capture=True,
    ))
    scale(app, replicas, stop=True)
    app.run("gateway-quiescence", STOP_APP, *(name + ".service" for name in APP_SETUP), seconds=210)
    inactive(app, tuple(name for name in APP_WRITERS if name != "postgresql"))
    relay.run("relay-quiescence", "systemctl stop nginx.service headscale.service\nsystemctl stop \"$@\"",
              *(name + ".service" for name in RELAY_SETUP), seconds=90)
    inactive(relay, RELAY_WRITERS)
    return replicas


def stop_database(app):
    app.run("database-stop", "systemctl stop postgresql.target postgresql-setup.service postgresql.service")
    inactive(app, APP_WRITERS)


def original(app, relay, inputs, verify_repository):
    for host, names in ((app, APP_WRITERS), (relay, RELAY_WRITERS)):
        inactive(host, names)
        install_runtime(host, inputs)
    app.run("fresh-identity-inputs",
            'test ! -e "$1/terrace-realm.json"\ntest ! -L "$1/terrace-realm.json"\n'
            "systemctl start cloud-rehearsal-inputs.service", GATEWAY_INPUTS, seconds=140)
    relay.run("fresh-relay-state", "test ! -e /var/lib/headscale/db.sqlite\ntest ! -L /var/lib/headscale/db.sqlite")
    bootstrap_app(app)
    first = app_signature(app)
    bootstrap_app(app)
    require(app_signature(app) == first, "second gateway bootstrap changed retained inputs or database identity")
    relay_signature = bootstrap_relay(relay)
    require(bootstrap_relay(relay) == relay_signature, "second relay bootstrap changed identity")
    app.run("synthetic-media", MEDIA_FIXTURE)
    node = pvc(app, "original")
    assertions = protocol(app, "")
    replicas = quiesce(app, relay)
    signature = app_signature(app)
    app.run("repository-init", "terrace-backup init primary", seconds=90, repository=True)
    for host in (app, relay):
        host.run("native-backup", "systemctl start terrace-backup.service", seconds=300, repository=True)
    snapshots, repo_hash = verify_repository()
    require(app_signature(app) == signature, "database changed during quiescent backup")
    stop_database(app)
    inactive(relay, RELAY_WRITERS)
    return {
        "verified": True, "servers": sorted([app.id, relay.id]), "snapshots": snapshots,
        "repositoryConfigSha256": repo_hash, "inputSha256": {name: digest(value) for name, value in inputs.items()},
        "gatewaySignature": signature, "relaySignature": relay_signature, "pvc": node,
        "replicas": replicas, "protocolAssertions": assertions, "idempotentBootstrap": True,
        "writersStopped": True,
    }


def replacement(app, relay, inputs, backup, verify_repository):
    require(backup["inputSha256"] == {name: digest(value) for name, value in inputs.items()},
            "retained TLS or repository escrow changed")
    snapshots, repo_hash = verify_repository()
    require(snapshots == backup["snapshots"] and repo_hash == backup["repositoryConfigSha256"],
            "retained encrypted repository changed")
    for host, name, writers in ((app, "gateway", APP_WRITERS), (relay, "relay", RELAY_WRITERS)):
        inactive(host, writers)
        install_runtime(host, inputs)
        snapshot = snapshots[name]
        require(SNAPSHOT.fullmatch(snapshot), "restore snapshot rejected")
        host.run("native-restore", RESTORE, snapshot, seconds=360, repository=True)
    app.run("application-adoption", COPY_RESTORED, *APP_PATHS, seconds=180)
    app.run("postgresql-adoption", ADOPT_POSTGRES, seconds=180)
    app.run("restored-windmill-role-boundary", WIND_ROLES)
    require(app_signature(app) == backup["gatewaySignature"], "restored native PostgreSQL identity changed")
    app.run("vault-fixture-exclusion",
            'test ! -e "$1/vaultwarden-browser.json"\ntest ! -L "$1/vaultwarden-browser.json"', GATEWAY_INPUTS)
    relay.run("relay-state-adoption", COPY_RESTORED, "/var/lib/headscale", seconds=90)
    relay.run("sqlite-adoption", """
install -m 0600 -o headscale -g headscale \
  /var/lib/rehearsal-recovery/files/var/lib/terrace-backup/dumps/sqlite/headscale.sqlite \
  /var/lib/headscale/db.sqlite
""")
    bootstrap_app(app)
    scale(app, backup["replicas"], stop=False)
    require(pvc(app, "replacement") == backup["pvc"], "restored PVC or node identity changed")
    require(bootstrap_relay(relay) == backup["relaySignature"], "restored relay identity changed")
    assertions = protocol(app, "-restored")
    require(assertions == backup["protocolAssertions"], "restored protocol assertion count changed")
    quiesce(app, relay)
    stop_database(app)
    return {
        "servers": sorted([app.id, relay.id]), "protocolAssertions": assertions,
        "retainedPVCReadWrite": True, "nativeDatabaseAdoption": True,
        "relayIdentityAndPrivateHTTPS": True, "writersStopped": True,
    }


def listener(directory, manifest, rescue):
    receipt, _ = rescue.read_json(directory / "repository-listener.json", 4096)
    run = manifest["run"]
    require(receipt["run"] == run and receipt["unit"] == f"terrace-cloud-{run}-repository.service"
            and receipt["socket"] == str(directory / "rest.sock")
            and receipt["repositoryDirectory"] == str(directory / "repository-data")
            and type(receipt["expires"]) is int and time.time() < receipt["expires"],
            "private repository listener receipt rejected")
    socket = directory / "rest.sock"
    require(re.fullmatch(r"/[A-Za-z0-9/._-]+", str(socket)), "repository socket path rejected")
    info = socket.lstat()
    require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            "private repository socket rejected")
    rescue.private_directory(directory / "repository-data")
    return socket, receipt["expires"]


# The first check on an installed host; the native install fixture runs the same text.
# findmnt's raw output (-r) is unpadded, so the comparison is exact.
INSTALLED_IDENTITY = r"""
test "$(readlink -f /run/current-system)" = "$1"
test "$(hostname)" = "$2"
test "$(findmnt -rn -o FSTYPE --mountpoint /)" = ext4
"""


def authenticate_host(directory, phase, role, rescue, socket, expires):
    binding = rescue.lease_binding(directory, phase, role, time.time())
    prefix = phase + "-" + role
    installed, _ = rescue.read_json(directory / (prefix + "-installed.json"))
    boot, _ = rescue.read_json(directory / (prefix + "-installed-boot.json"), 4096)
    require(installed["binding"] == binding and installed["mountedExt4Verified"] is True
            and boot == binding
            and rescue.STORE.fullmatch(installed["system"])
            and SNAPSHOT.fullmatch(installed["planSha256"]), "installed host receipt rejected")
    key = installed["installedHostKey"]
    blob = base64.b64decode(key, validate=True)
    fingerprint = b"SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=")
    known_hosts = directory / (prefix + "-installed-known-hosts")
    pinned = rescue.known_key(rescue.read_private(known_hosts, 4096), binding["ipv4"], fingerprint)
    require(pinned == key, "installed SSH key mismatch")
    options = [
        f"UserKnownHostsFile={known_hosts}" if option.startswith("UserKnownHostsFile=") else option
        for option in rescue.ssh_options(directory, prefix)
    ]
    ssh = ["ssh", *options, "operator@" + binding["ipv4"]]
    host = Host(directory, role, binding, ssh, rescue.checked_process, socket,
                min(binding["expires"], expires))
    hostname = "gateway" if role == "app" else "relay"
    host.run("installed-system-identity", INSTALLED_IDENTITY, installed["system"], hostname)
    return host


def offhost_verifier(directory, rescue, expires):
    repository = directory / "repository-data" / "repository"
    password = directory / "repository-password"

    def checked(stage, command, seconds, *, capture=False, env):
        reserve = min(seconds, int(expires - time.time()) - 30)
        require(reserve >= 5 and not os.path.lexists(directory / "closed.json"),
                "service lease closed or expired")
        try:
            return rescue.checked_process(command, reserve, capture=capture, env=env)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise Rejected(f"off-host {stage} failed") from None

    def verify():
        rescue.private_directory(repository)
        rescue.read_private(password, 4096)
        config = rescue.read_private(repository / "config", 65536)
        try:
            json.loads(config)
        except ValueError:
            pass
        else:
            raise Rejected("off-host repository configuration is not encrypted")
        env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME") if key in os.environ}
        command = ["restic", "--repo", str(repository), "--password-file", str(password), "--no-cache"]
        checked("encrypted data check", [*command, "check", "--read-data"], 300, env=env)
        data = checked("snapshot inventory", [*command, "snapshots", "--json"], 30, capture=True, env=env)
        return check_snapshots(json.loads(data)), digest(config)

    return verify


def run_phase(directory, phase, rescue):
    manifest, _ = rescue.read_json(directory / "lease.json", 4 * 1024 * 1024)
    run = manifest["run"]
    rescue.expected_names(run)
    require(manifest["schema"] == 1 and type(manifest["expires"]) is int
            and manifest["expires"] > time.time() + 300
            and not os.path.lexists(directory / "closed.json"), "service lease is closed or expired")
    created, _ = rescue.read_json(directory / f"{phase}-created.json")
    ids = owned_ids(created, "servers")
    require(all(str(server_id) not in manifest["baseline"]["servers"] for server_id in ids),
            "service cohort includes a baseline server")
    output = directory / ("backup-verified.json" if phase == "original" else "services-verified.json")
    require(not os.path.lexists(output), "service phase already verified")
    if phase == "replacement":
        backup, _ = rescue.read_json(directory / "backup-verified.json", 16384)
        old, _ = rescue.read_json(directory / "original-created.json")
        receipts = sorted(directory.glob("original-absence-*.json"))
        require(bool(receipts), "original absence receipt missing")
        absence, _ = rescue.read_json(receipts[-1])
        check_original_absence(run, old, created, absence, backup)
    require(not os.path.lexists(directory / f"{phase}-services-started.json"),
            "service phase was already started")
    socket, expires = listener(directory, manifest, rescue)
    tls = rescue.private_directory(directory / "tls")
    inputs = {name: rescue.read_private(tls / name, 16384)
              for name in ("ca.pem", "app-cert.pem", "app-key.pem", "relay-cert.pem", "relay-key.pem")}
    inputs["repository-password"] = rescue.read_private(directory / "repository-password", 4096)
    app, relay = (authenticate_host(directory, phase, role, rescue, socket, expires) for role in ROLES)
    require(sorted([app.id, relay.id]) == ids, "installed servers do not match the created cohort")
    expires = min(expires, manifest["expires"], app.expires, relay.expires)
    verify_repository = offhost_verifier(directory, rescue, expires)
    rescue.write_private(directory / f"{phase}-services-started.json", {"run": run, "phase": phase})
    try:
        if phase == "original":
            result = original(app, relay, inputs, verify_repository)
        else:
            result = replacement(app, relay, inputs, backup, verify_repository)
        require(time.time() < expires and not os.path.lexists(directory / "closed.json"),
                "service lease closed during verification")
        rescue.write_private(output, {"run": run, "phase": phase, **result})
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, subprocess.TimeoutExpired) as error:
        # Persist only our fixed error labels, never child stderr or input values.
        reason = str(error) if type(error) is Rejected else "service stage rejected"
        try:
            rescue.write_private(directory / f"{phase}-services-failure.json",
                                 {"run": run, "phase": phase, "reason": reason})
        except (OSError, ValueError):
            pass
        raise


def main():
    import rescue_install as rescue

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("original", "replacement"))
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        directory = rescue.private_directory(args.directory)
        # Independent provider cleanup must remain able to acquire its own lock.
        fd = os.open(directory / "services.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "rb") as lock:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                    "service lock is not private")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run_phase(directory, args.phase, rescue)
    except Rejected as error:
        print("Cloud rehearsal service proof rejected:", error, file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, subprocess.TimeoutExpired):
        print("Cloud rehearsal service proof failed. Inspect the protected run state.", file=sys.stderr)
        return 1
    print("Cloud rehearsal service proof verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
