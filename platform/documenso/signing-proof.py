"""Disposable signing acceptance only. Never print mail, tokens, PDFs or errors."""
import html.parser
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

ORIGIN = "https://documents.terrace.invalid"
ROOT = Path("/root/documenso-functional")


def signing_path(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "documents.terrace.invalid" or parsed.query or parsed.fragment:
        raise ValueError()
    if not re.fullmatch(r"/sign/[A-Za-z0-9_-]{16,256}", parsed.path):
        raise ValueError()
    return parsed.path


class SigningLinks(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paths = set()

    def handle_starttag(self, tag, attributes):
        if tag != "a":
            return
        for key, value in attributes:
            if key == "href" and value:
                try:
                    self.paths.add(signing_path(value))
                except ValueError:
                    pass


def signature_parts(data):
    if not 100 < len(data) <= 2 * 1024 * 1024 or not data.startswith(b"%PDF-"):
        raise ValueError()
    ranges = re.findall(rb"/ByteRange\s*\[\s*([0-9]+)\s+([0-9]+)\s+([0-9]+)\s+([0-9]+)\s*\]", data)
    if len(ranges) != 1:
        raise ValueError()
    zero, first, second, remaining = map(int, ranges[0])
    if zero != 0 or not 0 < first < second < len(data) or second + remaining != len(data):
        raise ValueError()
    gap = data[first:second].strip()
    if not re.fullmatch(rb"<[0-9A-Fa-f\s]+>", gap) or len(gap) > 131074:
        raise ValueError()
    cms = bytes.fromhex(gap[1:-1].decode("ascii"))
    if len(cms) < 2 or cms[0] != 0x30:
        raise ValueError()
    count = cms[1] & 0x7f if cms[1] & 0x80 else 0
    if count > 4 or cms[1] == 0x80 or len(cms) < 2 + count:
        raise ValueError()
    length = int.from_bytes(cms[2:2 + count], "big") if count else cms[1]
    total = 2 + count + length
    if total > len(cms) or any(cms[total:]):
        raise ValueError()
    return data[:first] + data[second:], cms[:total]


def verify_pdf(path, certificate):
    def private_bytes(source, limit):
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > limit:
                raise ValueError()
            value = os.read(descriptor, limit + 1)
            if len(value) > limit:
                raise ValueError()
            return value
        finally:
            os.close(descriptor)

    signed, cms = signature_parts(private_bytes(path, 2 * 1024 * 1024))
    private_bytes(certificate, 65536)
    with tempfile.TemporaryDirectory(prefix="documenso-signature-", dir=ROOT) as temporary:
        directory = Path(temporary)
        for name, content in [("content", signed), ("signature", cms)]:
            (directory / name).write_bytes(content)
            (directory / name).chmod(0o600)
        command = ["openssl", "cms", "-verify", "-binary", "-inform", "DER", "-in", str(directory / "signature"),
                   "-content", str(directory / "content"), "-nointern", "-certfile", str(certificate), "-noverify", "-out", os.devnull]
        # Only the exact generated fixture certificate may verify this signer.
        assert subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
        tampered = bytearray(signed)
        tampered[-1] ^= 1
        (directory / "content").write_bytes(tampered)
        assert subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode != 0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_arguments):
        raise ValueError()


def private_log_copy(source, destination, owner=0):
    parent = source.parent.stat()
    if source.parent.is_symlink() or parent.st_uid != owner or parent.st_mode & 0o777 != 0o700 or destination.parent != source.parent:
        raise ValueError()
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o777 != 0o600 or info.st_size > 4 * 1024 * 1024:
            raise ValueError()
        value = os.read(descriptor, 4 * 1024 * 1024 + 1)
        if len(value) > 4 * 1024 * 1024:
            raise ValueError()
        output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(output, "wb") as stream:
            stream.write(value)
    finally:
        os.close(descriptor)


def capture_mail():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + 60

    def request(path):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError()
        data = bytearray()
        with opener.open("http://127.0.0.1:8025/api/v1/" + path, timeout=min(5, remaining)) as response:
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError()
                chunk = response.read(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError()
        return json.loads(data)

    for _ in range(30):
        paths = set()
        messages = request("messages?limit=50")["messages"]
        if not isinstance(messages, list) or len(messages) > 50:
            raise ValueError()
        for message in messages:
            if not any(person.get("Address") == "local-member@terrace.invalid" for person in message["To"]):
                continue
            identifier = message["ID"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
                raise ValueError()
            body = request("message/" + identifier)
            parser = SigningLinks()
            parser.feed(body["HTML"])
            paths.update(parser.paths)
        if paths:
            if len(paths) != 1:
                raise ValueError()
            destination = Path("/var/lib/terrace-documenso-mail-test")
            destination.mkdir(mode=0o700, exist_ok=True)
            if destination.is_symlink() or destination.stat().st_uid != 0 or destination.stat().st_mode & 0o777 != 0o700:
                raise ValueError()
            descriptor = os.open(destination / "recipient.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            with os.fdopen(descriptor, "w") as output:
                json.dump({"path": paths.pop()}, output)
            return
        time.sleep(1)
    raise ValueError()


if __name__ == "__main__":
    try:
        os.umask(0o077)
        if sys.argv[1:] == ["mail"]:
            capture_mail()
            print("Synthetic signing request captured in the local mail sink.")
        elif sys.argv[1:] == ["evidence"]:
            directory = Path("/var/lib/documenso-test-evidence")
            private_log_copy(directory / "app.log", directory / "runtime-copy.log")
            print("Private bounded signing runtime evidence retained.")
        elif sys.argv[1:] == ["verify"]:
            verify_pdf(ROOT / "signed.pdf", ROOT / "signing.crt")
            print("Native signed PDF verified against the fixture certificate; altered bytes rejected.")
        else:
            raise ValueError()
    except Exception:
        print("Synthetic signing proof failed; private diagnostics suppressed.", file=sys.stderr)
        sys.exit(1)
