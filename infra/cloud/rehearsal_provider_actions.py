"""Boot only lease-owned hosts into Rescue and back into their reviewed installation."""

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

import rehearsal_apply as apply
import rehearsal_lease as lease
import rehearsal_timer as timer


CONSOLE = "console.hetzner.cloud"
ACTIONS = {"enable_rescue", "disable_rescue", "request_console", "reboot"}


def password(value):
    if not isinstance(value, str) or not re.fullmatch(r"[!-~]{8,128}", value):
        raise lease.Rejected("provider password format rejected")
    return value


def console_url(url, sid):
    if not isinstance(url, str) or len(url) > 1024 or not re.fullmatch(r"[!-~]+", url):
        raise lease.Rejected("console URL format rejected")
    try:
        endpoint = urlsplit(url)
        values = parse_qs(endpoint.query, strict_parsing=True, max_num_fields=2)
    except ValueError:
        raise lease.Rejected("console URL format rejected") from None
    if (endpoint.scheme != "wss" or endpoint.netloc != CONSOLE or endpoint.path != "/" or endpoint.fragment
            or set(values) != {"server_id", "token"} or values["server_id"] != [str(sid)]
            or len(values["token"]) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", values["token"][0])):
        raise lease.Rejected("console endpoint does not match the leased host")
    return url


def wait_action(api, directory, phase, response):
    action = lease.positive_id(response["action"]["id"])
    path = directory / f"{phase}-action-{action}.json"
    if not os.path.lexists(path):
        lease.write_private(path, {"id": action})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = api.action_status(action)
        if status == "success":
            return
        if status == "error":
            break
        time.sleep(1)
    raise lease.Rejected("provider action did not succeed")


def dispatch(api, directory, phase, server_id, action, payload):
    if action not in ACTIONS:
        raise lease.Rejected("provider action rejected")
    route = f"servers/{lease.positive_id(server_id)}/actions/{action}"
    response = api.request(route, post=payload)
    wait_action(api, directory, phase, response)
    return response


def server_binding(value):
    return {key: item for key, item in value.items() if key != "observedAt"}


def operate(mode, directory, manifest, credential, token, phase, role):
    apply.require_run_unit(manifest["run"], f"{phase}-{role}", "create")
    timer.check_ready(directory, manifest, credential)
    api = lease.Api(token.decode("ascii"))
    binding = apply.observe_server(api, directory, manifest, phase, role)
    sid = binding["serverId"]
    prefix = f"{phase}-{role}"
    login_path = directory / f"{prefix}-rescue-login.json"
    host = api.get("servers", sid)
    if (host is None or host["id"] != sid or host["status"] != "running"
            or host["name"] != f"terrace-r-{manifest['run']}-{prefix}" or host["labels"].get(lease.LABEL) != manifest["run"]):
        raise lease.Rejected("live server action target rejected")
    if mode == "rescue":
        if host["rescue_enabled"] is not False:
            raise lease.Rejected("unexpected Rescue state")
        foundation = apply.read_json(directory / "foundation-created.json")
        key, = foundation["ssh_keys"]
        key = lease.positive_id(key)
        key_row = api.get("ssh_keys", key)
        if (key_row is None or str(key) in manifest["baseline"]["ssh_keys"]
                or key_row["id"] != key or key_row["name"] != f"terrace-r-{manifest['run']}-key"
                or key_row["labels"].get(lease.LABEL) != manifest["run"]):
            raise lease.Rejected("Rescue key is not owned by the lease")
        lease.write_private(directory / f"{prefix}-rescue-dispatched.json", server_binding(binding))
        response = dispatch(api, directory, phase, sid, "enable_rescue", {"type": "linux64", "ssh_keys": [key]})
        lease.write_private(login_path, {"binding": server_binding(binding),
                                        "rescuePassword": password(response["root_password"])})
        dispatch(api, directory, phase, sid, "reboot", {})
        lease.write_private(directory / f"{prefix}-rescue-boot.json", server_binding(binding))
    elif mode == "console":
        login = apply.read_json(login_path)
        if (login["binding"] != server_binding(binding)
                or apply.read_json(directory / f"{prefix}-rescue-boot.json") != server_binding(binding)
                or os.path.lexists(directory / f"{prefix}-installed-boot-dispatched.json")):
            raise lease.Rejected("Rescue console scope rejected")
        observed_at = int(time.time())
        response = dispatch(api, directory, phase, sid, "request_console", {})
        expires = min(binding["expires"], observed_at + 55)
        if expires - time.time() < 15:
            raise lease.Rejected("console request is no longer fresh")
        lease.write_private(directory / f"{prefix}-console-{time.time_ns()}.json", {
            **binding, "observedAt": observed_at, "expires": expires,
            "wssUrl": console_url(response["wss_url"], sid),
            "vncPassword": password(response["password"]),
            "rescuePassword": password(login["rescuePassword"]),
        })
    else:
        installed = apply.read_json(directory / f"{prefix}-installed.json")
        if installed["binding"] != server_binding(binding) or installed["mountedExt4Verified"] is not True:
            raise lease.Rejected("reviewed installed boot receipt rejected")
        lease.write_private(directory / f"{prefix}-installed-boot-dispatched.json", server_binding(binding))
        if host["rescue_enabled"] is True:
            dispatch(api, directory, phase, sid, "disable_rescue", {})
        dispatch(api, directory, phase, sid, "reboot", {})
        lease.write_private(directory / f"{prefix}-installed-boot.json", server_binding(binding))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("rescue", "console", "boot-installed"))
    parser.add_argument("phase", choices=lease.PHASES)
    parser.add_argument("role", choices=lease.ROLES)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--credential-file", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        directory = lease.private_directory(args.directory)
        token = lease.read_private(args.credential_file, 4096).strip()
        manifest = apply.read_json(directory / "lease.json", 4 * 1024 * 1024)
        if manifest["schema"] != 1 or manifest["credentialSha256"] != hashlib.sha256(token).hexdigest():
            raise lease.Rejected("credential does not match the lease")
        with os.fdopen(os.open(directory / "lock", os.O_WRONLY | os.O_NOFOLLOW), "wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            operate(args.mode, directory, manifest, args.credential_file, token, args.phase, args.role)
        print("Cloud rehearsal server action verified.")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        print("Cloud rehearsal server action rejected. Private output suppressed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
