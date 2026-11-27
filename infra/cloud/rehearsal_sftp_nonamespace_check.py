"""Negative check for rehearsal_sftp.start(): with the mount namespace deliberately removed, start must refuse.

Run on the build machine against a prepared (not started) run directory:
  python3 -B infra/cloud/rehearsal_sftp_nonamespace_check.py --directory RUN --rclone RCLONE
It removes the namespace properties from the server unit, calls start(), and passes only if start refuses and no
unit or listener remains. It never prints private material.
"""

import argparse
from pathlib import Path
import subprocess
import sys

import rehearsal_lease as lease
import rehearsal_sftp as sftp


NAMESPACE = ("--property=TemporaryFileSystem=", "--property=BindReadOnlyPaths=", "--property=BindPaths=")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--rclone", required=True, type=Path)
    args = parser.parse_args()
    directory = lease.private_directory(args.directory)
    original = sftp.server_properties
    sftp.server_properties = lambda *a: [p for p in original(*a) if not p.startswith(NAMESPACE)]
    try:
        sftp.start(directory, args.rclone, 300)
    except lease.Rejected as error:
        print("start refused without the namespace:", error)
    else:
        print("FAIL: start accepted a server without the namespace")
        return 1
    finally:
        sftp.server_properties = original
    listing = subprocess.run([str(sftp.SYSTEMD / "systemctl"), "--user", "list-units", "terrace-lease-sftp-*", "--no-legend", "--all"],
                             stdout=subprocess.PIPE, text=True, check=False).stdout.strip()
    leftovers = [name for name in listing.split() if name.startswith("terrace-lease-sftp-") and name.endswith(".service")]
    receipt = (directory / sftp.RECEIPT).exists()
    print("units left:", leftovers or "none", "| receipt written:", receipt)
    return 1 if leftovers or receipt else 0


if __name__ == "__main__":
    sys.exit(main())
