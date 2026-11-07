"""Validate the isolated Terrace bootstrap preview before reporting it."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


PROJECT = Path(__file__).resolve().parent
URN_PREFIX = "urn:pulumi:bootstrap::terrace-cloud::"
TARGETS = {
    URN_PREFIX + "hcloud:index/firewall:Firewall::bootstrap-firewall": "bootstrap-firewall",
    URN_PREFIX + "hcloud:index/sshKey:SshKey::operator": "operator",
    URN_PREFIX + "hcloud:index/server:Server::relay-server": "relay-server",
    URN_PREFIX + "hcloud:index/server:Server::app-server": "app-server",
}
STACK = URN_PREFIX + "pulumi:pulumi:Stack::terrace-cloud-bootstrap"
MAX_DIGEST_BYTES = 4 * 1024 * 1024
DEADLINE = 120


def validated_preview(digest):
    """Accept only complete create/unchanged plans for the fixed inventory."""
    version = json.loads((PROJECT / "package.json").read_text())["dependencies"]["@pulumi/hcloud"]
    provider = URN_PREFIX + "pulumi:providers:hcloud::default_" + version.replace(".", "_")
    allowed = {*TARGETS, STACK, provider}
    if digest.get("maybeCorrupt", False) is not False:
        raise ValueError
    for diagnostic in digest.get("diagnostics", []):
        if diagnostic.get("severity") == "error":
            raise ValueError

    steps = {}
    for step in digest["steps"]:
        urn, operation = step["urn"], step["op"]
        if urn not in allowed or urn in steps or operation not in ("create", "same"):
            raise ValueError
        steps[urn] = step
    if not {*TARGETS, STACK}.issubset(steps):
        raise ValueError

    summary = digest["changeSummary"]
    if any(op not in ("create", "same") or type(count) is not int or count < 0
           for op, count in summary.items()):
        raise ValueError
    if Counter(summary) != Counter(step["op"] for step in steps.values()):
        raise ValueError

    # Exclude configuration, diagnostics and provider state from the review file.
    resources = []
    for urn in TARGETS:
        step = steps[urn]
        inputs = step["newState"].get("inputs", {})
        if not isinstance(inputs, dict):
            raise ValueError
        inputs = inputs.copy()
        if urn == URN_PREFIX + "hcloud:index/sshKey:SshKey::operator" and "publicKey" in inputs:
            public_key = inputs.pop("publicKey")
            inputs["publicKeySha256"] = hashlib.sha256(public_key.encode()).hexdigest()
        resources.append({"urn": urn, "operation": step["op"], "inputs": inputs})
    return resources


def preview():
    env = dict(os.environ)
    env.pop("PULUMI_ENABLE_STREAMING_JSON_PREVIEW", None)
    args = ["pulumi", "preview", "--stack", "bootstrap", "--non-interactive", "--json",
            "--show-sames", "--suppress-permalink", "--suppress-outputs", "--show-secrets=false"]
    for urn in TARGETS:
        args.extend(("--target", urn))

    # Pulumi's digest can contain private provider diagnostics. Keep it off the terminal.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(args, cwd=PROJECT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            if process.wait(timeout=DEADLINE):
                raise ValueError
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        output.seek(0)
        data = output.read(MAX_DIGEST_BYTES + 1)
        if len(data) > MAX_DIGEST_BYTES:
            raise ValueError
        return validated_preview(json.loads(data))


def review_destination(value):
    path = Path(value)
    if not path.is_absolute() or not path.name:
        raise ValueError
    parent = path.parent.resolve(strict=True)
    directory = parent.stat()
    if not parent.is_dir() or directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
        raise ValueError
    if any((p / ".git").exists() or ((p / "HEAD").is_file() and (p / "objects").is_dir())
           for p in (parent, *parent.parents)):
        raise ValueError
    return parent / path.name


def write_review(destination, resources):
    # Link a complete 0600 file into place without replacing an existing path.
    with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent) as output:
        json.dump({"resources": resources}, output, indent=2)
        output.write("\n")
        output.flush()
        os.link(output.name, destination)


def interrupted(_signum, _frame):
    raise InterruptedError


def main(args):
    with_review_file = len(args) == 3 and args[:2] == ["bootstrap", "--review-file"]
    if (args != ["bootstrap"] and not with_review_file) or not os.environ.get("HCLOUD_TOKEN"):
        print("Use preview.sh bootstrap [--review-file /absolute/private/preview.json] "
              "with the dedicated HCLOUD_TOKEN.", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        destination = review_destination(args[2]) if with_review_file else None
        if destination is not None and os.path.lexists(destination):
            raise ValueError
        resources = preview()
        if destination is not None:
            write_review(destination, resources)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.TimeoutExpired):
        print("Bootstrap preview rejected. Check the dedicated project, inputs and exact create/unchanged plan.", file=sys.stderr)
        return 1
    print("Bootstrap preview accepted for four targets with create or unchanged operations.")
    for item in resources:
        print(f"{item['operation']}: {TARGETS[item['urn']]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
