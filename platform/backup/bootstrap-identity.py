"""Preserve the trusted source cluster's bootstrap role without relaxing SQL errors."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata

QUERY = "SELECT json_build_object('oid',oid,'name',rolname,'create','CREATE ROLE '||quote_ident(rolname)||';','serverVersionNum',current_setting('server_version_num')::int,'login',rolcanlogin,'superuser',rolsuper) FROM pg_authid WHERE oid=10"
MAX_SQL = 64 * 1024 * 1024
FIELDS = {"schema", "sourceOid", "name", "create", "globalsSha256", "serverVersionNum", "login", "superuser"}


def parse_json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError()
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=unique)


def identity(name, create):
    if not isinstance(name, str) or not 1 <= len(name.encode("utf-8")) <= 63 or any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError()
    if not isinstance(create, str) or not create.startswith("CREATE ROLE ") or not create.endswith(";"):
        raise ValueError()
    token = create[len("CREATE ROLE "):-1]
    if re.fullmatch(r"[a-z_][a-z0-9_$]*", token):
        decoded = token
    elif re.fullmatch(r'"(?:[^"]|"")*"', token):
        decoded = token[1:-1].replace('""', '"')
    else:
        raise ValueError()
    if decoded != name:
        raise ValueError()


def validate(metadata):
    if not isinstance(metadata, dict) or set(metadata) != FIELDS or type(metadata["schema"]) is not int or metadata["schema"] != 1:
        raise ValueError()
    if type(metadata["sourceOid"]) is not int or metadata["sourceOid"] != 10 or type(metadata["serverVersionNum"]) is not int or not 100000 <= metadata["serverVersionNum"] < 1000000:
        raise ValueError()
    if not isinstance(metadata["globalsSha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", metadata["globalsSha256"]):
        raise ValueError()
    if metadata["login"] is not True or metadata["superuser"] is not True:
        raise ValueError()
    identity(metadata["name"], metadata["create"])
    return metadata


def read_private(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_mode & 0o077 or not 0 < before.st_size <= maximum:
            raise ValueError()
        value = bytearray()
        while len(value) <= maximum:
            block = os.read(fd, min(1024 * 1024, maximum + 1 - len(value)))
            if not block:
                break
            value.extend(block)
        after = os.fstat(fd)
        if len(value) != before.st_size or len(value) > maximum or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError()
        return bytes(value)
    finally:
        os.close(fd)


def write_private(path, value):
    path = Path(path)
    # Publish a complete file without overwriting an existing file or symlink.
    fd, temporary = tempfile.mkstemp(prefix=".bootstrap-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def prepare_bytes(sql, metadata):
    validate(metadata)
    if not 0 < len(sql) <= MAX_SQL or hashlib.sha256(sql).hexdigest() != metadata["globalsSha256"]:
        raise ValueError()
    expected = metadata["create"].encode("utf-8") + b"\n"
    matches = re.finditer(rb"(?m)^" + re.escape(expected), sql)
    matched = next(matches, None)
    if matched is None or next(matches, None) is not None:
        raise ValueError()
    return sql[:matched.start()] + sql[matched.end():]


def capture(sql, source):
    if not isinstance(source, dict) or set(source) != {"oid", "name", "create", "serverVersionNum", "login", "superuser"}:
        raise ValueError()
    metadata = {"schema": 1, "sourceOid": source["oid"], "name": source["name"],
                "create": source["create"], "globalsSha256": hashlib.sha256(sql).hexdigest(),
                "serverVersionNum": source["serverVersionNum"], "login": source["login"], "superuser": source["superuser"]}
    prepare_bytes(sql, metadata)
    return metadata


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        raise ValueError()


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument("command", choices=["query", "capture", "prepare", "name"])
    parser.add_argument("paths", nargs="*")
    options = parser.parse_args()
    if options.command == "query" and not options.paths:
        print(QUERY)
    elif options.command == "capture" and len(options.paths) == 2:
        sql = read_private(options.paths[0], MAX_SQL)
        value = sys.stdin.buffer.read(4097)
        if len(value) > 4096:
            raise ValueError()
        metadata = capture(sql, parse_json(value))
        write_private(options.paths[1], json.dumps(metadata, ensure_ascii=False).encode("utf-8") + b"\n")
    elif options.command == "prepare" and len(options.paths) == 4:
        sql = read_private(options.paths[0], MAX_SQL)
        metadata = validate(parse_json(read_private(options.paths[1], 8192)))
        prepared = prepare_bytes(sql, metadata)
        write_private(options.paths[2], prepared)
        write_private(options.paths[3], json.dumps(metadata, ensure_ascii=False).encode("utf-8") + b"\n")
    elif options.command == "name" and len(options.paths) == 1:
        metadata = validate(parse_json(read_private(options.paths[0], 8192)))
        print(metadata["name"])
    else:
        raise ValueError()


if __name__ == "__main__":
    try:
        os.umask(0o077)
        main()
    except Exception:
        print("PostgreSQL bootstrap identity input rejected; private SQL suppressed.", file=sys.stderr)
        sys.exit(1)
