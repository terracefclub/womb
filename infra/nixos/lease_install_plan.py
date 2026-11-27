"""Write the reviewed install plan and the installed host key for one leased production host.

Run on the build machine after `rescue_install.py --inspect-disk` recorded `<phase>-<role>-disk.json`. It joins
three inputs that already exist into exactly the files `rescue_install.py --install-approved-sha` checks:

- the build outputs of `infra/nixos/lease-host.nix` (`install`: system, Disko script, installer, disk device);
- the host key `terrace-secrets host create <host> --role <role>` staged (private) and stored (public);
- the disk receipt (its digest, its device, the provider binding's IPv4).

It writes `<prefix>-install.json`, `<prefix>-installed-ed25519` (the staged private key, byte for byte) and
`<prefix>-installed-known-hosts` (`<ipv4> ssh-ed25519 <key>` from the store's public half, which must be the
staged key's own), then validates the plan with rescue_install's own check and prints the digest to approve.
Nothing is trusted on first use: the installed system's key is the one the secrets store already pins.
"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import rescue_install as rescue

OUTPUT_FIELDS = {"schema", "role", "device", "system", "disko", "installer"}
PLAN_FIELDS = ("disko", "installer", "system")
STAGED_KEY = "ssh_host_ed25519_key"
PUBLIC_LINE = re.compile(r"ssh-ed25519 ([A-Za-z0-9+/]{68})(?: [^\n]*)?\n?")
STORE_DIR = "/nix/store"


def read_outputs(path, role, store_dir=STORE_DIR):
    """The lease host builder's `install` output: a small JSON file in the Nix store."""
    resolved = Path(os.path.realpath(path))
    rescue.require(resolved.parent == Path(os.path.realpath(store_dir)), "build outputs are not a Nix store file")
    with open(resolved, "rb") as stream:
        info = os.fstat(stream.fileno())
        rescue.require(stat.S_ISREG(info.st_mode) and 1 <= info.st_size <= 4096, "unexpected build outputs file")
        outputs = json.loads(stream.read(4097), object_pairs_hook=rescue.unique_pairs)
    rescue.require(isinstance(outputs, dict) and set(outputs) == OUTPUT_FIELDS and outputs["schema"] == 1
                   and outputs["role"] == role and rescue.valid_disk(outputs["device"]),
                   "build outputs are not this role's install outputs")
    paths = [outputs[name] for name in PLAN_FIELDS]
    rescue.require(all(isinstance(item, str) and rescue.STORE.fullmatch(item) for item in paths)
                   and len(set(paths)) == len(paths), "build outputs name unexpected store paths")
    rescue.require(outputs["system"].split("-", 1)[1].startswith(f"nixos-system-{role}-")
                   and outputs["disko"].endswith("-disko") and outputs["installer"].endswith("-nixos-install"),
                   "build outputs are not a system, a Disko script and an installer")
    return outputs


def staged_host_key(stage, stored_public):
    """The staged private key's bytes and public key, which must be the store's public half."""
    key_bytes = rescue.read_private(Path(stage) / STAGED_KEY, 4096)
    derived = rescue.checked_process(["ssh-keygen", "-y", "-f", str(Path(stage) / STAGED_KEY)], 5, capture=True)
    derived_match = PUBLIC_LINE.fullmatch(derived.decode("ascii"))
    descriptor = os.open(stored_public, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        rescue.require(stat.S_ISREG(info.st_mode) and 1 <= info.st_size <= 4096, "stored host public key rejected")
        stored_match = PUBLIC_LINE.fullmatch(stream.read(4097).decode("ascii"))
    rescue.require(derived_match is not None and stored_match is not None
                   and derived_match[1] == stored_match[1], "staged host key is not the stored host key")
    rescue.ed25519_key(derived_match[1])
    return key_bytes, derived_match[1]


def write_new_bytes(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def record_plan(directory, phase, role, outputs_path, stage, stored_public, store_dir=STORE_DIR):
    """Write the plan, the installed key and its pin; return the plan digest to approve."""
    rescue.require(phase in rescue.PHASES and role in rescue.ROLES, "unknown install role or phase")
    directory = rescue.private_directory(directory)
    prefix = f"{phase}-{role}"
    written = [directory / f"{prefix}-{name}" for name in ("install.json", "installed-ed25519", "installed-known-hosts")]
    rescue.require(not any(os.path.lexists(path) for path in written), "an install plan or installed key already exists")
    receipt, disk_digest = rescue.read_json(directory / f"{prefix}-disk.json")
    ipv4 = receipt["binding"]["ipv4"]
    address = ipaddress.IPv4Address(ipv4)
    rescue.require(str(address) == ipv4 and receipt["binding"]["role"] == role
                   and receipt["binding"]["phase"] == phase, "disk receipt is for another host")
    outputs = read_outputs(outputs_path, role, store_dir)
    # The Disko script formats the device it was built for; it must be the disk the probe verified.
    rescue.require(outputs["device"] == receipt["disk"]["device"], "build outputs format another disk than the receipt")
    key_bytes, public = staged_host_key(stage, stored_public)
    rescue.require(public != receipt["rescueKey"], "installed host key is the installer's key")
    write_new_bytes(written[1], key_bytes)
    write_new_bytes(written[2], f"{ipv4} ssh-ed25519 {public}\n".encode("ascii"))
    rescue.write_private(written[0], {"diskSha256": disk_digest, **{name: outputs[name] for name in PLAN_FIELDS}})
    _, approved = rescue.read_json(written[0])
    rescue.checked_plan(directory, prefix, approved, disk_digest)
    return approved


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("phase", choices=rescue.PHASES)
    parser.add_argument("role", choices=rescue.ROLES)
    parser.add_argument("--outputs", type=Path, required=True,
                        help="the `install` output of infra/nixos/lease-host.nix (a store path or an out-link to it)")
    parser.add_argument("--host-stage", type=Path, required=True,
                        help="the staging directory `terrace-secrets host create` printed")
    parser.add_argument("--host-public", type=Path, required=True,
                        help="$TERRACE_SECRETS_STORE/hosts/<host>/ssh_host_ed25519_key.pub")
    args = parser.parse_args()
    try:
        approved = record_plan(args.directory, args.phase, args.role, args.outputs, args.host_stage, args.host_public)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Install plan rejected. Private inputs suppressed.", file=sys.stderr)
        return 1
    print(f"Reviewed install plan and installed host key recorded; approve with --install-approved-sha {approved}")
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
