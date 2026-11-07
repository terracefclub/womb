"""Private Hetzner rehearsal lease. Only this tool's cleanup path writes to the API."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


LABEL = "terrace-rehearsal"
DELETION_ORDER = ("servers", "primary_ips", "firewalls", "ssh_keys")
INVENTORY = (*DELETION_ORDER, "volumes", "networks", "floating_ips", "load_balancers",
             "placement_groups", "certificates", "snapshots", "backups")
PHASES = ("original", "replacement")
ROLES = ("app", "relay")


class Rejected(ValueError):
    pass


class Retryable(OSError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_private(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        first = os.fstat(fd)
        if (not stat.S_ISREG(first.st_mode) or first.st_uid != os.geteuid()
                or stat.S_IMODE(first.st_mode) != 0o600 or first.st_nlink != 1
                or not 1 <= first.st_size <= limit):
            raise Rejected("private input rejected")
        data = stream.read(limit + 1)
        last = os.fstat(fd)
        if (len(data) != first.st_size or first.st_mtime_ns != last.st_mtime_ns
                or first.st_ctime_ns != last.st_ctime_ns):
            raise Rejected("private input changed")
        return data


def private_directory(path):
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            or any((p / ".git").exists() for p in (path, *path.resolve().parents))):
        raise Rejected("private directory rejected")
    return path


def write_private(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent) as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        os.link(stream.name, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def expected_names(run):
    if not re.fullmatch(r"[a-f0-9]{32}", run):
        raise Rejected("run ID rejected")
    prefix = "terrace-r-" + run
    servers = {f"{prefix}-{phase}-{role}" for phase in PHASES for role in ROLES}
    return {"servers": servers, "primary_ips": {f"{name}-ip" for name in servers},
            "firewalls": {prefix + "-firewall"}, "ssh_keys": {prefix + "-key"}}


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise Rejected("resource ID rejected")
    return value


def snapshot_rows(inventory):
    if set(inventory) != set(INVENTORY):
        raise Rejected("inventory classes rejected")
    rows = {}
    for kind, items in inventory.items():
        rows[kind] = {positive_id(row["id"]): row for row in items}
        if len(rows[kind]) != len(items):
            raise Rejected("duplicate inventory identity")
    return rows


def baseline_identity(row):
    # Preserve all pre-existing resource identities and associations. Runtime status
    # may legitimately change outside this run and is not a deletion predicate.
    keys = ("name", "labels", "created", "assignee_id", "assignee_type", "server",
            "servers", "volumes", "private_net", "load_balancers", "applied_to",
            "auto_delete", "protection", "fingerprint", "ip")
    result = {key: row[key] for key in keys if key in row}
    if "public_net" in row:
        net = row["public_net"]
        result["publicAttachments"] = {
            "ipv4": (net.get("ipv4") or {}).get("id"), "ipv6": (net.get("ipv6") or {}).get("id"),
            "firewalls": [entry["id"] for entry in net["firewalls"]], "floating_ips": net["floating_ips"],
        }
    return result


def baseline_from(inventory):
    return {kind: {str(rid): baseline_identity(row) for rid, row in items.items()}
            for kind, items in snapshot_rows(inventory).items()}


def select_owned(run, baseline, inventory, phase):
    if phase not in ("original", "final") or set(baseline) != set(INVENTORY):
        raise Rejected("cleanup scope rejected")
    names = expected_names(run)
    owned = {kind: [] for kind in DELETION_ORDER}
    collisions = False
    for kind, rows in snapshot_rows(inventory).items():
        matched_names = set()
        for rid, row in rows.items():
            name = row.get("name")
            name_matches = name in names.get(kind, set())
            label_matches = row.get("labels", {}).get(LABEL) == run
            if not (name_matches or label_matches):
                continue
            if not (name_matches and label_matches) or str(rid) in baseline[kind]:
                collisions = True
                continue
            if name in matched_names:
                raise Rejected("ambiguous owned resource name")
            matched_names.add(name)
            if phase == "final" or (kind in ("servers", "primary_ips") and "-original-" in name):
                owned[kind].append(row)
    return owned, collisions


def require_disposable_server(server, owned, inventory):
    net = server["public_net"]
    ipv4 = (net.get("ipv4") or {}).get("id")
    assigned = {row["id"] for row in inventory["primary_ips"] if row["assignee_id"] == server["id"]}
    allowed_ips = {row["id"] for row in owned["primary_ips"] if row["name"] == server["name"] + "-ip"}
    firewall_ids = {row["id"] for row in owned["firewalls"]}
    expected_ip = {ipv4} if ipv4 is not None else set()
    if (assigned != expected_ip or not expected_ip <= allowed_ips
            or (net.get("ipv6") or {}).get("id") is not None or net["floating_ips"]
            or any(entry["id"] not in firewall_ids for entry in net["firewalls"])
            or server["volumes"] or server["private_net"] or server["load_balancers"]
            or any(row.get("created_from", {}).get("id") == server["id"] for row in inventory["backups"]
                   if row.get("created_from"))):
        raise Rejected("server has an unexpected attachment")


class Api:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, route, *, delete=False, missing=False):
        req = Request("https://api.hetzner.cloud/v1/" + route,
                      headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
                      method="DELETE" if delete else "GET")
        try:
            with self.opener.open(req, timeout=8) as response:
                data = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as error:
            if (delete or missing) and error.code == 404:
                return None
            if error.code in (409, 429) or 500 <= error.code <= 599:
                raise Retryable("provider request retryable") from None
            raise Rejected("provider request failed") from None
        except OSError:
            raise Retryable("provider connection retryable") from None
        if len(data) > 2 * 1024 * 1024:
            raise Rejected("provider response exceeds limit")
        return json.loads(data) if data else None

    def inventory(self):
        inventory = {}
        for kind in INVENTORY:
            endpoint = "images" if kind in ("snapshots", "backups") else kind
            items = []
            for page in range(1, 21):
                query = {"per_page": 50, "page": page}
                if endpoint == "images":
                    query["type"] = {"snapshots": "snapshot", "backups": "backup"}[kind]
                body = self.request(endpoint + "?" + urlencode(query))
                items.extend(body[endpoint])
                next_page = body["meta"]["pagination"]["next_page"]
                if next_page is None:
                    inventory[kind] = items
                    break
                if next_page != page + 1:
                    raise Rejected("pagination rejected")
            else:
                raise Rejected("inventory exceeds limit")
        return inventory

    def delete(self, kind, rid):
        if kind not in DELETION_ORDER:
            raise Rejected("delete kind rejected")
        body = self.request(f"{kind}/{positive_id(rid)}", delete=True)
        return positive_id(body["action"]["id"]) if body and "action" in body else None

    def get(self, kind, rid):
        key = {"servers": "server", "primary_ips": "primary_ip", "firewalls": "firewall", "ssh_keys": "ssh_key"}[kind]
        body = self.request(f"{kind}/{positive_id(rid)}", missing=True)
        return body[key] if body else None

    def action_status(self, aid):
        action = self.request(f"actions/{positive_id(aid)}")["action"]
        if action["id"] != aid or action["status"] not in ("running", "success", "error"):
            raise Rejected("provider action rejected")
        return action["status"]


def delete_owned(api, kind, row, all_owned, inventory):
    fresh = api.get(kind, row["id"])
    if fresh is None:
        return None
    if fresh["id"] != row["id"] or baseline_identity(fresh) != baseline_identity(row):
        raise Retryable("resource changed before deletion")
    if kind == "servers":
        require_disposable_server(fresh, all_owned, inventory)
    if kind == "primary_ips" and fresh["assignee_id"] is not None:
        raise Rejected("IP is still assigned")
    if kind == "firewalls" and fresh["applied_to"] != []:
        raise Rejected("firewall is still applied")
    return api.delete(kind, fresh["id"])


def clean(api, manifest, phase, known, *, deadline, delay, pending=(),
          record_action=lambda _: None, record_owned=lambda _kind, _rid: None):
    observed = {kind: set(known.get(kind, ())) for kind in DELETION_ORDER}
    actions = dict.fromkeys(pending, "running")
    stable = 0
    while time.monotonic() < deadline:
        try:
            for aid, status in actions.items():
                if status == "running":
                    actions[aid] = api.action_status(aid)
            if "running" in actions.values():
                time.sleep(delay)
                continue
            inventory = api.inventory()
            all_owned, collisions = select_owned(manifest["run"], manifest["baseline"], inventory, "final")
            owned, _ = select_owned(manifest["run"], manifest["baseline"], inventory, phase)
            if phase == "original" and any("-replacement-" in r["name"] for rows in all_owned.values() for r in rows):
                raise Rejected("replacement already exists")
            # Re-list each dependency after deletion rather than assuming DELETE is synchronous.
            active = next((kind for kind in DELETION_ORDER if owned[kind]), None)
            for kind, rows in owned.items():
                for row in rows:
                    rid = row["id"]
                    if rid not in observed[kind]:
                        record_owned(kind, rid)
                        observed[kind].add(rid)
            if active:
                stable = 0
                for row in owned[active]:
                    aid = delete_owned(api, active, row, all_owned, inventory)
                    if aid is not None:
                        actions[aid] = "running"
                        record_action(aid)
            else:
                current = baseline_from(inventory)
                if collisions or any(str(rid) in current[kind] for kind, ids in observed.items() for rid in ids):
                    raise Rejected("owned identity did not disappear")
                expected = {kind: dict(rows) for kind, rows in manifest["baseline"].items()}
                if phase == "original":
                    for kind in ("firewalls", "ssh_keys"):
                        expected[kind].update({str(r["id"]): baseline_identity(r) for r in all_owned[kind]})
                if current != expected:
                    raise Rejected("inventory does not match expected baseline")
                stable += 1
                if stable == 3:
                    return {"phase": phase, "verifiedAbsent": {k: sorted(v) for k, v in observed.items()},
                            "baselinePreserved": True, "absenceReads": stable, "actions": actions}
        except Retryable:
            stable = 0
        time.sleep(delay)
    raise Rejected("cleanup deadline elapsed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "retire-original", "cleanup"))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--run")
    parser.add_argument("--expires", type=int)
    args = parser.parse_args()
    try:
        directory = private_directory(args.directory)
        token = read_private(args.credential_file, 4096).strip()
        if not re.fullmatch(rb"[A-Za-z0-9_-]{16,256}", token):
            raise Rejected("credential format rejected")
        digest = hashlib.sha256(token).hexdigest()
        api = Api(token.decode("ascii"))
        manifest_path = directory / "lease.json"
        with os.fdopen(os.open(directory / "lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action == "prepare":
                if args.run is None or args.expires is None or not time.time() + 300 < args.expires <= time.time() + 21600:
                    raise Rejected("lease expiration rejected")
                inventory = api.inventory()
                baseline = baseline_from(inventory)
                owned, collisions = select_owned(args.run, baseline, inventory, "final")
                if collisions or any(owned.values()):
                    raise Rejected("lease identities already exist")
                write_private(manifest_path, {"schema": 1, "run": args.run, "expires": args.expires,
                                              "credentialSha256": digest, "baseline": baseline})
                print("Cloud rehearsal baseline prepared.")
                return
            manifest = json.loads(read_private(manifest_path, 4 * 1024 * 1024))
            if manifest["schema"] != 1 or manifest["credentialSha256"] != digest:
                raise Rejected("credential does not match the lease")
            phase = "original" if args.action == "retire-original" else "final"
            if phase == "original":
                backup = json.loads(read_private(directory / "backup-verified.json", 16384))
                original = json.loads(read_private(directory / "original-created.json", 16384))
                if (backup["run"] != manifest["run"] or backup["verified"] is not True
                        or sorted(backup["servers"]) != sorted(original["servers"])
                        or len(set(original["servers"])) != len(ROLES)):
                    raise Rejected("off-host backup receipt rejected")
            if phase == "final" and not (directory / "closed.json").exists():
                write_private(directory / "closed.json", {"closedAt": int(time.time())})
            # Recorded numeric IDs survive a lost label or changed provider name.
            known = {kind: set() for kind in DELETION_ORDER}
            phases = ("foundation", "original", "replacement") if phase == "final" else ("original",)
            for cohort in phases:
                path = directory / f"{cohort}-created.json"
                if path.exists():
                    for kind, ids in json.loads(read_private(path, 16384)).items():
                        known[kind].update(positive_id(rid) for rid in ids)
            journal_phases = ("original", "final") if phase == "final" else ("original",)
            for journal_phase in journal_phases:
                for path in directory.glob(f"{journal_phase}-owned-*.json"):
                    entry = json.loads(read_private(path, 1024))
                    known[entry["kind"]].add(positive_id(entry["id"]))
            def record_action(aid):
                path = directory / f"{phase}-action-{aid}.json"
                if not path.exists():
                    write_private(path, {"id": aid})

            def record_owned(kind, rid):
                write_private(directory / f"{phase}-owned-{kind}-{rid}.json", {"kind": kind, "id": rid})

            pending = {positive_id(json.loads(read_private(path, 1024))["id"])
                       for journal_phase in journal_phases for path in directory.glob(f"{journal_phase}-action-*.json")}
            receipt = clean(api, manifest, phase, known, deadline=time.monotonic() + 300, delay=10,
                            pending=pending, record_action=record_action, record_owned=record_owned)
            write_private(directory / f"{phase}-absence-{time.time_ns()}.json", receipt)
            print("Cloud rehearsal cleanup verified.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("Cloud rehearsal lease or cleanup failed. Inspect the protected run state.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
