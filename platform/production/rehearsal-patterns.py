"""Rehearsal only: turn secret values into grep -F patterns in a private tmpfs file.

Reads a decrypted sops JSON document on stdin, or with --directory the files of a
sops-nix generation. Prints only a count. Multi-line keys contribute their inner
base64 lines (their first lines are a format prefix shared by every key).
"""
import argparse
import json
import os
import sys
from pathlib import Path

DATABASE = ("identity/client/",)


def values_from_tree(tree, prefix=""):
    for key, value in tree.items():
        if not prefix and key == "sops":
            continue
        if isinstance(value, dict):
            yield from values_from_tree(value, prefix + key + "/")
        else:
            yield prefix + key, value


def values_from_directory(root):
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not relative.startswith("rendered/"):
            yield relative, path.read_text()


def patterns(name, value):
    if name == "rehearsal/users-json":
        return [user["password"] for user in json.loads(value)]
    lines = [line.strip() for line in value.strip().splitlines()]
    if len(lines) == 1:
        return [lines[0]] if len(lines[0]) >= 32 else []
    body = [line for line in lines if line and not line.startswith("-----")]
    return [line for line in body[2:-1] if len(line) >= 40]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--directory")
    parser.add_argument("--exclude-database", action="store_true",
                        help="omit the OIDC client secrets, which Keycloak keeps in its database by design")
    args = parser.parse_args()
    items = values_from_directory(args.directory) if args.directory else values_from_tree(json.load(sys.stdin))
    found = []
    for name, value in items:
        if args.exclude_database and name.startswith(DATABASE):
            continue
        found.extend(patterns(name, value))
    if not found:
        sys.exit("no patterns")
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(found) + "\n")
    print(len(found))


main()
