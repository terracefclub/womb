"""Authenticate an exact leased Rescue SSH key through the provider VNC console."""

import argparse
import base64
import hashlib
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

import rescue_install as rescue


ORIGIN = "https://console-fixture.invalid"
CONSOLE_HOSTS = {"console.hetzner.cloud", "web-console.hetzner.cloud"}
FIELDS = {"run", "phase", "role", "serverId", "ipv4", "expires", "observedAt",
          "wssUrl", "vncPassword", "rescuePassword"}
LOGIN = re.compile(r"(?im)^(?:[a-z0-9.-]+[ ]+)?login:[ \t]*$")
PASSWORD = re.compile(r"(?im)^password:[ \t]*$")
ROOT = re.compile(r"(?m)^root@[a-zA-Z0-9.-]+[^\r\n]*#[ \t]*$")


def validate_receipt(receipt, binding, now):
    rescue.require(set(receipt) == FIELDS, "unexpected console receipt")
    rescue.require(type(receipt["serverId"]) is int, "unexpected console server ID")
    for field in ("run", "phase", "role", "serverId", "ipv4"):
        rescue.require(receipt[field] == binding[field], "console is for a different host")
    observed, expires = receipt["observedAt"], receipt["expires"]
    rescue.require(type(observed) is int and type(expires) is int
                   and observed <= now < expires <= binding["expires"]
                   and 0 < expires - observed <= 60,
                   "console credential expired")
    parsed = urlsplit(receipt["wssUrl"])
    rescue.require(parsed.scheme == "wss" and parsed.netloc in CONSOLE_HOSTS
                   and parsed.path == "/" and not parsed.fragment
                   and parsed.geturl() == receipt["wssUrl"],
                   "unexpected provider console URL")
    query = parse_qs(parsed.query, strict_parsing=True, max_num_fields=2)
    rescue.require(set(query) == {"server_id", "token"} and query["server_id"] == [str(binding["serverId"])]
                   and len(query["token"]) == 1 and re.fullmatch(r"[A-Za-z0-9_.~-]{16,1024}", query["token"][0]),
                   "unexpected provider console query")
    for field in ("vncPassword", "rescuePassword"):
        rescue.require(re.fullmatch(r"[!-~]{6,128}", receipt[field]), "unexpected console credential form")


def candidate_key(raw, ipv4):
    host, kind, encoded = raw.decode("ascii").strip().split()
    rescue.require(host == ipv4 and kind == "ssh-ed25519", "unexpected SSH key observation")
    blob = base64.b64decode(encoded, validate=True)
    rescue.require(blob.startswith(rescue.KEY_PREFIX) and len(blob) == len(rescue.KEY_PREFIX) + 32,
                   "invalid Ed25519 host key")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return fingerprint


def console_command(fingerprint, marker):
    # The full marker never appears in an echoed shell command.
    rescue.require(re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint), "invalid key fingerprint")
    rescue.require(re.fullmatch(r"TERRACE[A-Z]{12}", marker), "invalid console marker")
    return ("clear; test \"$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub "
            "| cut -d ' ' -f2)\" = " + shlex.quote(fingerprint)
            + " && printf '%s%s\\n' " + shlex.quote(marker[:7]) + " " + shlex.quote(marker[7:]))


def marker_seen(text, marker):
    return sum(line.strip() == marker for line in text.splitlines()) == 1


def ocr(png):
    rescue.require(len(png) <= 8 * 1024 * 1024, "console frame too large")
    result = subprocess.run(["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "6"],
                            input=png, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            check=True, timeout=5)
    rescue.require(len(result.stdout) <= 32768, "console recognition output too large")
    return result.stdout.decode("utf-8")


PAGE = """<!doctype html><meta charset=utf-8><div id=console></div>
<script type=module>
import RFB from '/core/rfb.js';
window.startConsole = ({url, password}) => {
  window.rfb = new RFB(document.querySelector('#console'), url,
    {credentials:{password}, wsProtocols:['binary']});
  window.rfb.addEventListener('connect', () => window.consoleReady = true);
  window.rfb.addEventListener('disconnect', () => window.consoleReady = false);
};
window.consoleInput = text => {
  if (!window.consoleReady) throw new Error('console disconnected');
  for (const char of text) window.rfb.sendKey(char.codePointAt(0), '');
  window.rfb.sendKey(0xff0d, 'Enter');
};
</script>"""


def attest_console(receipt, fingerprint, progress):
    # Import only for the actual console stage. Offline tests use no browser.
    from playwright.sync_api import sync_playwright

    marker = "TERRACE" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(12))
    assets = Path(os.environ["TERRACE_NOVNC_ROOT"]).resolve(strict=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ["TERRACE_CONSOLE_BROWSER"],
                                             headless=True, timeout=15000)
        try:
            context = browser.new_context(service_workers="block", viewport={"width": 1280, "height": 960})

            def serve(route):
                url = urlsplit(route.request.url)
                if url.scheme != "https" or url.netloc != "console-fixture.invalid" or url.query or url.fragment:
                    route.abort()
                    return
                if url.path == "/":
                    route.fulfill(content_type="text/html", body=PAGE)
                    return
                path = (assets / url.path.lstrip("/")).resolve()
                if path.is_relative_to(assets) and path.suffix == ".js" and path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
                    route.fulfill(content_type="text/javascript", path=path)
                else:
                    route.abort()

            def websocket(route):
                if route.url == receipt["wssUrl"]:
                    route.connect_to_server()
                else:
                    route.close()

            context.route("**/*", serve)
            context.route_web_socket("**/*", websocket)
            page = context.new_page()
            page.set_default_timeout(8000)
            page.goto(ORIGIN + "/", timeout=8000)
            page.wait_for_function("typeof window.startConsole === 'function'")
            rescue.require(time.time() < receipt["expires"], "console credential expired before connect")
            progress["stage"] = "console-connect"
            page.evaluate("value => window.startConsole(value)",
                          {"url": receipt["wssUrl"], "password": receipt["vncPassword"]})
            page.wait_for_function("window.consoleReady === true", timeout=12000)
            canvas = page.locator("#console canvas")
            deadline = time.monotonic() + 60

            def wait_for(predicate):
                while time.monotonic() < deadline:
                    frame = canvas.bounding_box(timeout=3000)
                    rescue.require(frame is not None and 0 < frame["width"] <= 4096
                                   and 0 < frame["height"] <= 2160, "unexpected console dimensions")
                    if predicate(ocr(canvas.screenshot(timeout=3000))):
                        return
                    time.sleep(0.35)
                raise rescue.Rejected("expected console prompt or marker not observed")

            progress["stage"] = "login-prompt"
            wait_for(lambda text: bool(LOGIN.search(text)))
            page.evaluate("text => window.consoleInput(text)", "root")
            progress["stage"] = "password-prompt"
            wait_for(lambda text: bool(PASSWORD.search(text)))
            page.evaluate("text => window.consoleInput(text)", receipt["rescuePassword"])
            progress["stage"] = "root-prompt"
            wait_for(lambda text: bool(ROOT.search(text)))
            page.evaluate("text => window.consoleInput(text)", console_command(fingerprint, marker))
            progress["stage"] = "fingerprint-marker"
            wait_for(lambda text: marker_seen(text, marker))
            page.evaluate("window.rfb.disconnect()")
        finally:
            browser.close()


def record_text(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent) as stream:
        stream.write(value.rstrip() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
        os.link(stream.name, path)


def enroll(directory, phase, role, receipt_path, progress):
    rescue.require(receipt_path.parent == directory, "console receipt must stay in the lease directory")
    prefix = f"{phase}-{role}"
    rescue.require(re.fullmatch(re.escape(prefix) + r"-console-[1-9][0-9]{0,19}[.]json", receipt_path.name),
                   "unexpected console receipt name")
    binding = rescue.lease_binding(directory, phase, role, time.time())
    receipt, _ = rescue.read_json(receipt_path, 16384)
    validate_receipt(receipt, binding, time.time())
    progress["stage"] = "ssh-candidate"
    raw = rescue.checked_process(["ssh-keyscan", "-T", "5", "-t", "ed25519", binding["ipv4"]],
                                 8, capture=True)
    fingerprint = candidate_key(raw, binding["ipv4"])
    progress["stage"] = "browser-start"
    attest_console(receipt, fingerprint, progress)
    progress["stage"] = "binding-recheck-and-publish"
    rescue.require(rescue.lease_binding(directory, phase, role, time.time()) == binding,
                   "provider binding changed during console authentication")
    record_text(directory / f"{prefix}-rescue-known-hosts", raw)
    record_text(directory / f"{prefix}-rescue-fingerprint", fingerprint.encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("directory", type=Path)
    parser.add_argument("phase", choices=rescue.PHASES)
    parser.add_argument("role", choices=rescue.ROLES)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    directory = None
    progress = {"stage": "binding"}
    try:
        directory = rescue.private_directory(args.directory)
        enroll(directory, args.phase, args.role, args.receipt, progress)
    except Exception:
        # Playwright exceptions can include the complete WebSocket URL.
        if directory is not None:
            try:
                rescue.write_private(directory / f"{args.phase}-{args.role}-console-failure-{time.time_ns()}.json",
                                     progress)
            except OSError:
                pass
        print("Rescue console authentication failed. Private diagnostics suppressed.", file=sys.stderr)
        return 1
    print("Provider console authenticated the exact Rescue SSH host key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
