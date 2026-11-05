import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

source = Path(__file__).parents[2] / "platform/documenso/signing-proof.py"
spec = importlib.util.spec_from_file_location("documenso_signing", source)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def signed_fixture():
    before = b"%PDF-1.7\n" + b"x" * 100 + b"/Contents "
    gap = b"<3003616263000000>"
    after = b"\n/ByteRange [0 %08d %08d %08d]\n%%%%EOF\n"
    after = after % (len(before), len(before) + len(gap), len(after % (0, 0, 0)))
    return before + gap + after


class SigningProofTests(unittest.TestCase):
    def test_private_log_copy_is_bounded_no_follow_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            source = directory / "app.log"
            source.write_bytes(b"synthetic-private-marker")
            source.chmod(0o600)
            output = directory / "copy.log"
            proof.private_log_copy(source, output, os.getuid())
            self.assertEqual(output.read_bytes(), source.read_bytes())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                proof.private_log_copy(source, output, os.getuid())
            source.chmod(0o644)
            with self.assertRaises(ValueError):
                proof.private_log_copy(source, directory / "other.log", os.getuid())
            source.unlink()
            source.symlink_to(output)
            with self.assertRaises(OSError):
                proof.private_log_copy(source, directory / "other.log", os.getuid())
            source.unlink()
            os.mkfifo(source, 0o600)
            with self.assertRaises(ValueError):
                proof.private_log_copy(source, directory / "other.log", os.getuid())
            source.unlink()
            with source.open("wb") as stream:
                stream.truncate(4 * 1024 * 1024 + 1)
            source.chmod(0o600)
            with self.assertRaises(ValueError):
                proof.private_log_copy(source, directory / "other.log", os.getuid())

    def test_only_exact_same_origin_native_signing_links_are_retained(self):
        token = "synthetic-recipient-token"
        path = "/sign/" + token
        self.assertEqual(proof.signing_path(proof.ORIGIN + path), path)
        for value in ["http://documents.terrace.invalid" + path,
                      "https://other.terrace.invalid" + path,
                      "https://user:private@documents.terrace.invalid" + path,
                      proof.ORIGIN + ":443" + path, proof.ORIGIN + path + "?private=value",
                      proof.ORIGIN + path + "#fragment", proof.ORIGIN + "/sign/short",
                      proof.ORIGIN + "/sign/../private", proof.ORIGIN + "/sign/%2fprivate"]:
            with self.subTest(kind="rejected link"), self.assertRaises(ValueError):
                proof.signing_path(value)

    def test_mail_html_is_parsed_as_data_without_fetching_links(self):
        parser = proof.SigningLinks()
        link = proof.ORIGIN + "/sign/synthetic-recipient-token"
        parser.feed('<script>private</script><img src="https://external.invalid/tracker">'
                    '<a href="https://external.invalid/private">other</a><a href="' + link + '">sign</a>')
        self.assertEqual(parser.paths, {"/sign/synthetic-recipient-token"})

    def test_byte_ranges_extract_exact_signed_bytes_and_trim_only_zero_padding(self):
        data = signed_fixture()
        signed, cms = proof.signature_parts(data)
        self.assertEqual(cms, b"\x30\x03abc")
        self.assertNotIn(b"<3003616263000000>", signed)
        self.assertEqual(len(signed), len(data) - len(b"<3003616263000000>"))

    def test_incomplete_overlapping_multiple_or_nonzero_origin_ranges_rejected(self):
        data = signed_fixture()
        candidates = [data[:-1], data.replace(b"/ByteRange [0", b"/ByteRange [1"),
                      data + b"/ByteRange [0 1 2 3]", data.replace(b"3003616263000000", b"3003616263000001"),
                      data.replace(b"3003616263000000", b"3080616263000000"), b"%PDF-1.7\n", b"x" * 200]
        for value in candidates:
            with self.subTest(kind="rejected PDF"), self.assertRaises(ValueError):
                proof.signature_parts(value)


if __name__ == "__main__":
    unittest.main()
