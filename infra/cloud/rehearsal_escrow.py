"""Stage private rehearsal trust and a bounded Unix-only restic endpoint."""

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time

import rehearsal_lease as lease
import rehearsal_timer as timer


APP_NAMES = ("identity", "web", "cms", "documents", "windmill", "vault")
REPO_LIMIT = 20 * 1024**3


def closed(directory):
    return os.path.lexists(directory / "closed.json")


def put_bytes(destination, content):
    with tempfile.NamedTemporaryFile(dir=destination.parent) as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
        os.link(output.name, destination)


def openssl(binary, *args):
    subprocess.run([binary, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True, timeout=20)


def generate_material(directory, binary):
    destination = directory / "tls"
    if (closed(directory) or os.path.lexists(destination) or os.path.lexists(directory / "repository-password")
            or not re.fullmatch(r"/nix/store/[0-9a-z]{32}-openssl-[0-9.]+-bin/bin/openssl", str(binary))
            or not binary.is_file()):
        raise lease.Rejected("private material already initialized or lease closed")
    destination.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix="ca-", dir=directory) as name:
        work = Path(name)
        ca_key, ca = work / "ca.key", work / "ca.pem"
        openssl(binary, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "2",
                "-subj", "/CN=Terrace disposable rehearsal", "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", ca_key, "-out", ca)
        put_bytes(destination / "ca.pem", ca.read_bytes())
        for role, names in (("app", APP_NAMES), ("relay", ("relay",))):
            key, csr, cert, config = (work / (role + suffix) for suffix in (".key", ".csr", ".pem", ".cnf"))
            hosts = [name + ".terrace.invalid" for name in names]
            config.write_text("[leaf]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                              "extendedKeyUsage=serverAuth\nsubjectAltName=" + ",".join("DNS:" + host for host in hosts) + "\n")
            openssl(binary, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", "/CN=" + hosts[0],
                    "-keyout", key, "-out", csr)
            openssl(binary, "x509", "-req", "-in", csr, "-CA", ca, "-CAkey", ca_key, "-set_serial", str(secrets.randbits(120) + 1),
                    "-days", "2", "-sha256", "-extfile", config, "-extensions", "leaf", "-out", cert)
            for host in hosts:
                openssl(binary, "verify", "-purpose", "sslserver", "-verify_hostname", host, "-CAfile", ca, cert)
            put_bytes(destination / (role + "-cert.pem"), cert.read_bytes())
            put_bytes(destination / (role + "-key.pem"), key.read_bytes())
    put_bytes(directory / "repository-password", (secrets.token_urlsafe(40) + "\n").encode())


def scope(directory, manifest):
    lease.expected_names(manifest["run"])
    name = "terrace-cloud-" + manifest["run"] + "-repository.service"
    socket = directory / "rest.sock"
    if len(os.fsencode(socket)) > 100:
        raise lease.Rejected("repository socket path too long")
    return name, socket, directory / "repository-data"


def repository_ready(name, socket, user):
    expected = {"ActiveState": "active", "SubState": "running", "User": user,
                "KillMode": "control-group", "RestrictAddressFamilies": "AF_UNIX"}
    facts = timer.read_properties(name, expected)
    if facts != expected:
        return False
    try:
        info = socket.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid() and not (info.st_mode & 0o077)


def start(directory, manifest, binary):
    name, socket, repository = scope(directory, manifest)
    seconds = int(manifest["expires"] - time.time())
    if (closed(directory) or not 300 <= seconds <= 21600
            or not re.fullmatch(r"/nix/store/[0-9a-z]{32}-restic-rest-server-[0-9.]+/bin/rest-server", str(binary))
            or not binary.is_file() or timer.read_property(name, "LoadState") != "not-found"
            or os.path.lexists(socket) or os.path.lexists(directory / "repository-listener.json")):
        raise lease.Rejected("repository listener preflight rejected")
    lease.read_private(directory / "repository-password", 1024)
    repository.mkdir(mode=0o700)
    user = pwd.getpwuid(os.getuid()).pw_name
    try:
        timer.system("sudo", "-n", timer.SYSTEMD / "systemd-run", "--quiet", "--collect", "--unit=" + name,
                     "--property=Type=exec", "--property=User=" + user, "--property=RuntimeMaxSec=" + str(seconds),
                     "--property=TimeoutStopSec=10s", "--property=KillMode=control-group", "--property=UMask=0077",
                     "--property=NoNewPrivileges=yes", "--property=PrivateTmp=yes", "--property=ProtectSystem=strict",
                     "--property=RestrictAddressFamilies=AF_UNIX", "--property=ReadWritePaths=" + timer.safe_path(directory),
                     binary, "--listen", "unix:" + timer.safe_path(socket), "--path", timer.safe_path(repository),
                     "--no-auth", "--append-only", "--max-size", str(REPO_LIMIT))
        for _ in range(12):
            if repository_ready(name, socket, user):
                break
            time.sleep(0.25)
        else:
            raise lease.Rejected("repository Unix listener unavailable")
        lease.write_private(directory / "repository-listener.json", {
            "run": manifest["run"], "socket": str(socket), "repositoryDirectory": str(repository),
            "expires": manifest["expires"], "unit": name,
        })
    except (OSError, ValueError, subprocess.SubprocessError):
        timer.system("sudo", "-n", timer.SYSTEMD / "systemctl", "stop", name, allow_inactive=True)
        raise


def stop(directory, manifest):
    name, socket, _ = scope(directory, manifest)
    timer.system("sudo", "-n", timer.SYSTEMD / "systemctl", "stop", name, allow_inactive=True)
    expected = {"ActiveState": "inactive", "MainPID": "0", "ControlPID": "0", "ControlGroup": ""}
    status = timer.read_properties(name, expected)
    # Collected units can omit service-specific PID properties.
    if (status["ActiveState"] != "inactive" or status.get("MainPID", "0") != "0"
            or status.get("ControlPID", "0") != "0" or status.get("ControlGroup", "")):
        raise lease.Rejected("repository unit is not inactive")
    if os.path.lexists(socket):
        raise lease.Rejected("repository socket still exists")
    try:
        (timer.CGROUP / name).stat()
    except FileNotFoundError:
        return
    raise lease.Rejected("repository cgroup still exists")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("material", "start", "stop"))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--rest-server", type=Path)
    parser.add_argument("--openssl", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        directory = lease.private_directory(args.directory)
        manifest = json.loads(lease.read_private(directory / "lease.json", 4 * 1024 * 1024))
        if manifest["schema"] != 1:
            raise lease.Rejected("unknown lease format")
        if args.action == "material" and args.openssl is not None:
            generate_material(directory, args.openssl)
        elif args.action == "start" and args.rest_server is not None:
            start(directory, manifest, args.rest_server)
        elif args.action == "stop":
            stop(directory, manifest)
        else:
            raise lease.Rejected("pinned binary required")
        print("Cloud rehearsal escrow operation verified.")
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print("Cloud rehearsal escrow operation rejected. Private output suppressed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
