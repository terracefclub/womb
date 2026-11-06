import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

source = Path(__file__).parents[2] / "platform/backup/bootstrap-identity.py"
spec = importlib.util.spec_from_file_location("bootstrap_identity", source)
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


def catalog(name="postgres", create="CREATE ROLE postgres;"):
    return {"oid": 10, "name": name, "create": create, "serverVersionNum": 180001, "login": True, "superuser": True}


SQL = b"-- Roles\nCREATE ROLE postgres;\nALTER ROLE postgres WITH SUPERUSER LOGIN;\nCREATE ROLE app;\nGRANT app TO postgres WITH ADMIN FALSE GRANTED BY postgres;\n"


class BootstrapIdentityTests(unittest.TestCase):
    def test_exactly_one_bootstrap_create_removed_every_other_byte_unchanged(self):
        metadata = identity.capture(SQL, catalog())
        self.assertTrue(identity.prepare_bytes(SQL, metadata) == SQL.replace(b"CREATE ROLE postgres;\n", b"", 1))

    def test_quoted_space_and_unicode_identifiers(self):
        for name, token in [('Source Root', '"Source Root"'), ("équipe", '"équipe"')]:
            create = "CREATE ROLE " + token + ";"
            data = (create + "\nALTER ROLE " + token + " WITH LOGIN SUPERUSER;\n").encode()
            metadata = identity.capture(data, catalog(name, create))
            self.assertTrue(identity.prepare_bytes(data, metadata) == data[len(create.encode()) + 1:])

    def test_embedded_quote_bootstrap_rejects_before_initdb(self):
        create = 'CREATE ROLE "Source ""Root";'
        with self.assertRaises(ValueError):
            identity.capture((create + '\n').encode(), catalog('Source "Root', create))

    def test_name_bounds_and_all_control_categories_rejected(self):
        for name in ["", "a" * 64, "é" * 32, "a\nb", "a\rb", "a\x00b", "a\x7fb", "a\x85b", "a\u202eb"]:
            with self.subTest(length=len(name)), self.assertRaises(ValueError):
                identity.identity(name, 'CREATE ROLE "' + name + '";')

    def test_injected_or_mismatched_identifier_not_a_canonical_create(self):
        for create in ['CREATE ROLE postgres; DROP ROLE app;', 'CREATE ROLE postgres; -- extra',
                       'CREATE ROLE "postgres" trailing;', 'CREATE ROLE other;', 'CREATE ROLE "post"gres";',
                       'CREATE ROLE "postgres; SELECT 1";']:
            with self.subTest(create=create), self.assertRaises(ValueError):
                identity.identity("postgres", create)

    def test_missing_duplicate_noncanonical_and_nonnewline_create_rejected(self):
        for value in [SQL.replace(b"CREATE ROLE postgres;\n", b""), SQL + b"CREATE ROLE postgres;\n",
                      SQL.replace(b"CREATE ROLE postgres;\n", b" CREATE ROLE postgres;\n"), b"CREATE ROLE postgres;"]:
            with self.assertRaises(ValueError):
                identity.capture(value, catalog())

    def test_hash_mismatch_rejects_create_looking_text_or_sql_change(self):
        metadata = identity.capture(SQL, catalog())
        with self.assertRaises(ValueError):
            identity.prepare_bytes(SQL + b"-- CREATE ROLE postgres;\n", metadata)

    def test_exact_schema_types_and_source_oid_required(self):
        good = identity.capture(SQL, catalog())
        for key, value in [("schema", True), ("schema", 2), ("sourceOid", 11), ("sourceOid", True),
                           ("globalsSha256", "invalid"), ("serverVersionNum", True), ("login", 1), ("superuser", 1)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                identity.validate({**good, key: value})
        with self.assertRaises(ValueError):
            identity.validate({**good, "extra": True})

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            identity.parse_json(b'{"schema":1,"schema":2}')

    def test_nologin_or_non_superuser_source_is_explicitly_unsupported(self):
        for flag in ["login", "superuser"]:
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                identity.capture(SQL, {**catalog(), flag: False})

    def test_private_stable_reads_reject_fifo_symlink_public_oversize(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(SQL)
            path.chmod(0o600)
            self.assertEqual(identity.read_private(path, 4096), SQL)
            with self.assertRaises(ValueError):
                identity.read_private(path, 1)
            link = Path(temporary) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                identity.read_private(link, 4096)
            fifo = Path(temporary) / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ValueError):
                identity.read_private(fifo, 4096)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                identity.read_private(path, 4096)

    def test_atomic_private_output_never_overwrites_or_follows_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "output"
            identity.write_private(path, b"complete")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                identity.write_private(path, b"replacement")
            link = Path(temporary) / "link"
            link.symlink_to(path)
            with self.assertRaises(FileExistsError):
                identity.write_private(link, b"replacement")
            self.assertEqual(path.read_bytes(), b"complete")
            self.assertEqual(sorted(p.name for p in Path(temporary).iterdir()), ["link", "output"])

    def test_cli_errors_do_not_reflect_private_paths_or_sql(self):
        marker = "synthetic-private-input"
        result = subprocess.run([sys.executable, str(source), "prepare", marker], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(result.stderr.strip(), "PostgreSQL bootstrap identity input rejected; private SQL suppressed.")


if __name__ == "__main__":
    unittest.main()
