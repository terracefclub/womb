"""Preview and apply exact create-only Pulumi plans for an armed cloud lease."""

import argparse
from collections import Counter
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time

import rehearsal_lease as lease
import rehearsal_timer as timer


PROJECT = "terrace-cloud-rehearsal"
PHASES = ("foundation", "original", "replacement")
RESOURCE_TYPES = {
    "key": ("ssh_keys", "hcloud:index/sshKey:SshKey"),
    "firewall": ("firewalls", "hcloud:index/firewall:Firewall"),
    "app-ip": ("primary_ips", "hcloud:index/primaryIp:PrimaryIp"),
    "relay-ip": ("primary_ips", "hcloud:index/primaryIp:PrimaryIp"),
    "app": ("servers", "hcloud:index/server:Server"),
    "relay": ("servers", "hcloud:index/server:Server"),
}
PROJECT_FILES = ("Pulumi.yaml", "README.md", "index.ts", "input.ts", "rehearsal.test.ts", "resources.ts", "tsconfig.json")
PARENT_FILES = ("budget.ts", "package.json", "bun.lock", "tsconfig.json")


def phase_targets(phase, run):
    if phase not in PHASES:
        raise lease.Rejected("phase rejected")
    lease.expected_names(run)
    prefix = "terrace-r-" + run
    urn_prefix = f"urn:pulumi:{phase}::{PROJECT}::"
    names = ("key", "firewall") if phase == "foundation" else ("app-ip", "relay-ip", "app", "relay")
    return {urn_prefix + RESOURCE_TYPES[name][1] + "::" + name:
            (RESOURCE_TYPES[name][0], f"{prefix}-{name}" if phase == "foundation" else f"{prefix}-{phase}-{name}")
            for name in names}


def parse_json(data):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise lease.Rejected("duplicate JSON key")
        return result

    def invalid(_value):
        raise lease.Rejected("invalid JSON constant")

    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid)


def validate_preview(digest, phase, run, provider_version):
    targets = phase_targets(phase, run)
    prefix = f"urn:pulumi:{phase}::{PROJECT}::"
    stack = prefix + f"pulumi:pulumi:Stack::{PROJECT}-{phase}"
    provider = prefix + "pulumi:providers:hcloud::default_" + provider_version.replace(".", "_")
    if digest.get("maybeCorrupt", False) is not False or any(x.get("severity") == "error" for x in digest.get("diagnostics", [])):
        raise lease.Rejected("preview diagnostics rejected")
    steps = {}
    for step in digest["steps"]:
        urn = step["urn"]
        if urn in steps or urn not in {*targets, stack, provider} or step["op"] != "create":
            raise lease.Rejected("preview operation rejected")
        steps[urn] = step
    if not {*targets, stack}.issubset(steps) or digest["changeSummary"] != {"create": len(steps)}:
        raise lease.Rejected("preview inventory rejected")
    for urn, (_, name) in targets.items():
        inputs = steps[urn]["newState"]["inputs"]
        if inputs["name"] != name or inputs["labels"] != {lease.LABEL: run}:
            raise lease.Rejected("preview name or label rejected")
    return {"creates": sorted(targets), "providerAndStackCreates": len(steps) - len(targets)}


def validate_saved_plan(plan, phase, run, provider_version):
    targets = phase_targets(phase, run)
    prefix = f"urn:pulumi:{phase}::{PROJECT}::"
    stack = prefix + f"pulumi:pulumi:Stack::{PROJECT}-{phase}"
    provider = prefix + "pulumi:providers:hcloud::default_" + provider_version.replace(".", "_")
    plans = plan["resourcePlans"]
    if set(plans) != {*targets, stack, provider} or plan.get("config"):
        raise lease.Rejected("saved plan inventory or config rejected")
    if any(entry["steps"] != ["create"] for entry in plans.values()):
        raise lease.Rejected("saved plan contains an unexpected operation")


def ids_by_kind(owned):
    return {kind: sorted(lease.positive_id(row["id"]) for row in owned[kind]) for kind in lease.DELETION_ORDER}


def without_owned(inventory, owned):
    baseline = lease.baseline_from(inventory)
    for kind, ids in ids_by_kind(owned).items():
        for rid in ids:
            del baseline[kind][str(rid)]
    return baseline


def verify_foundation(owned, config):
    firewall, = owned["firewalls"]
    rule, = firewall["rules"]
    if (rule["direction"] != "in" or rule["protocol"] != "tcp" or rule["port"] != "22"
            or sorted(rule["source_ips"]) != sorted(config["sshCidrs"]) or rule["destination_ips"] != []
            or firewall["applied_to"] != []):
        raise lease.Rejected("foundation SSH firewall rejected")
    key, = owned["ssh_keys"]
    if key["public_key"].split()[:2] != config["sshPublicKey"].split()[:2]:
        raise lease.Rejected("foundation operator key rejected")


def verify_cohort(owned, inventory, config, created):
    primary = {row["id"]: row for row in owned["primary_ips"]}
    for server in owned["servers"]:
        lease.require_disposable_server(server, owned, inventory)
        net = server["public_net"]
        address = net["ipv4"]
        ip = primary[address["id"]]
        expected_type = config["appType"] if server["name"].endswith("-app") else config["relayType"]
        location = (server.get("location") or {}).get("name") or server["datacenter"]["location"]["name"]
        if (ip["id"] not in created["primary_ips"] or ip["assignee_type"] != "server"
                or ip["auto_delete"] is not False or ip["protection"]["delete"] is not False
                or ip["ip"] != address["ip"] or not ipaddress.IPv4Address(ip["ip"]).is_global
                or net["firewalls"] != [{"id": config["firewallId"], "status": "applied"}]
                or server["server_type"]["name"] != expected_type or server["image"]["name"] != config["image"]
                or location != config["location"] or server["rescue_enabled"] is not False
                or server["backup_window"] is not None or server["protection"] != {"delete": False, "rebuild": False}):
            raise lease.Rejected("created server attachment or protection rejected")


def read_json(path, limit=65536):
    return parse_json(lease.read_private(path, limit))


def optional_sha(path):
    return hashlib.sha256(lease.read_private(path, 65536)).hexdigest() if os.path.lexists(path) else None


def expected_before(api, manifest, directory, phase, config):
    inventory = api.inventory()
    owned, collisions = lease.select_owned(manifest["run"], manifest["baseline"], inventory, "final")
    found = ids_by_kind(owned)
    if collisions:
        raise lease.Rejected("run identity collision")
    expected = {kind: [] for kind in lease.DELETION_ORDER}
    if phase != "foundation":
        expected = read_json(directory / "foundation-created.json")
        if expected != {"servers": [], "primary_ips": [], "firewalls": [config["firewallId"]],
                        "ssh_keys": [config["sshKeyId"]]}:
            raise lease.Rejected("foundation identity mismatch")
    if phase == "replacement":
        receipts = sorted(directory.glob("original-absence-*.json"))
        if not receipts:
            raise lease.Rejected("original absence missing")
        receipt = read_json(receipts[-1])
        original = read_json(directory / "original-created.json")
        if (receipt["run"] != manifest["run"] or receipt["phase"] != "original"
                or receipt["baselinePreserved"] is not True or receipt["absenceReads"] != 3
                or receipt["unresolvedDispatches"] != []
                or receipt["verifiedAbsent"] != {kind: sorted(ids) for kind, ids in original.items()}):
            raise lease.Rejected("original absence rejected")
    if found != expected:
        raise lease.Rejected("provider inventory is not ready for this phase")
    if phase != "foundation":
        verify_foundation(owned, read_json(directory / "foundation-input.json"))
    if without_owned(inventory, owned) != manifest["baseline"]:
        raise lease.Rejected("baseline inventory changed")
    return expected


def fingerprint_source(read):
    paths = [*("rehearsal-create/" + name for name in PROJECT_FILES), *PARENT_FILES]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode() + b"\0")
        digest.update(hashlib.sha256(read(path)).digest())
    return digest.hexdigest()


def git_source_sha(commit):
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise lease.Rejected("reviewed Git commit rejected")
    return fingerprint_source(lambda path: subprocess.check_output(
        ["git", "show", f"{commit}:infra/cloud/{path}"], stderr=subprocess.DEVNULL, timeout=10))


def source_sha(project):
    allowed_configs = {f"Pulumi.{phase}.yaml" for phase in PHASES}
    if ({path.name for path in project.iterdir()} - allowed_configs != set(PROJECT_FILES)
            or {path.name for path in project.parent.iterdir()} != {*PARENT_FILES, "rehearsal-create", "node_modules"}):
        raise lease.Rejected("staged source inventory rejected")
    dependencies = project.parent / "node_modules"
    info = dependencies.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise lease.Rejected("dependency staging rejected")
    for package in ("@pulumi/hcloud", "@pulumi/pulumi"):
        candidate = (dependencies / package).resolve(strict=True)
        if not candidate.is_relative_to(dependencies.resolve(strict=True)):
            raise lease.Rejected("dependency is outside the private staging boundary")
    return fingerprint_source(lambda path: lease.read_private(project.parent / path, 1024 * 1024))


def pulumi(project, env, args, *, seconds, output=False):
    with tempfile.TemporaryFile() as log:
        result = subprocess.run(["pulumi", *args], cwd=project, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.DEVNULL, timeout=seconds, check=False)
        if result.returncode:
            raise lease.Rejected("Pulumi command failed")
        if output:
            log.seek(0)
            data = log.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise lease.Rejected("Pulumi output exceeds limit")
            return parse_json(data)


def require_run_unit(run, phase, action):
    suffix = "apply" if action == "create" else "preview"
    unit = f"terrace-cloud-{run}-{phase}-{suffix}.service"
    cgroup = Path("/proc/self/cgroup").read_text()
    expected = {"MainPID": str(os.getpid()), "KillMode": "control-group", "Type": "exec",
                "RuntimeMaxUSec": "3min 50s", "TimeoutStopUSec": "10s"}
    if (not any(line.endswith("/system.slice/" + unit) for line in cgroup.splitlines())
            or timer.read_properties(unit, expected) != expected):
        raise lease.Rejected("command is not in the bounded run unit")


def observe_server(api, directory, manifest, phase, role):
    if phase not in lease.PHASES or role not in lease.ROLES or os.path.lexists(directory / "closed.json"):
        raise lease.Rejected("server binding scope rejected")
    if manifest["expires"] <= time.time() + timer.CREATE_RESERVE:
        raise lease.Rejected("server binding is outside the lease window")
    inventory = api.inventory()
    owned, collisions = lease.select_owned(manifest["run"], manifest["baseline"], inventory, "final")
    created = read_json(directory / f"{phase}-created.json")
    name = f"terrace-r-{manifest['run']}-{phase}-{role}"
    rows = [row for row in owned["servers"] if row["name"] == name and row["id"] in created["servers"]]
    if collisions or len(rows) != 1:
        raise lease.Rejected("server binding identity rejected")
    server = rows[0]
    lease.require_disposable_server(server, owned, inventory)
    public = server["public_net"]["ipv4"]
    ipv4 = ipaddress.IPv4Address(public["ip"])
    if public["id"] not in created["primary_ips"] or not ipv4.is_global:
        raise lease.Rejected("server binding IP rejected")
    return {"run": manifest["run"], "phase": phase, "role": role, "serverId": server["id"],
            "ipv4": str(ipv4), "expires": manifest["expires"], "observedAt": int(time.time())}


def operate(action, directory, manifest, token, phase, credential, approval, approved_source):
    require_run_unit(manifest["run"], phase, action)
    config_path = directory / f"{phase}-input.json"
    config_data = lease.read_private(config_path, 65536)
    config = parse_json(config_data)
    if config["run"] != manifest["run"] or config["phase"] != phase:
        raise lease.Rejected("phase input rejected")
    if os.path.lexists(directory / "closed.json"):
        raise lease.Rejected("lease closed")
    project = directory / "project" / "rehearsal-create"
    version = read_json(project.parent / "package.json")["dependencies"]["@pulumi/hcloud"]
    source = source_sha(project)
    if not re.fullmatch(r"[a-f0-9]{64}", approved_source or "") or source != approved_source:
        raise lease.Rejected("staged source does not match reviewed Git bytes")
    api = lease.Api(token.decode("ascii"))
    before = expected_before(api, manifest, directory, phase, config)
    stage = directory / phase
    backend = stage / "backend"
    password = stage / "passphrase.json"
    plan = stage / "plan.json"
    review_path = stage / "review.json"
    stack_config = project / f"Pulumi.{phase}.yaml"
    if action == "preview":
        if os.path.lexists(stack_config):
            raise lease.Rejected("pre-existing stack configuration")
        stage.mkdir(mode=0o700)
        backend.mkdir(mode=0o700)
        (stage / "pulumi-home").mkdir(mode=0o700)
        lease.write_private(password, {"value": secrets.token_urlsafe(32)})
    if lease.private_directory(stage) != stage:
        raise lease.Rejected("phase directory rejected")
    # Do not inherit provider endpoints, HTTP proxies, debug logs or Node overrides.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME") if key in os.environ}
    env.update(HCLOUD_TOKEN=token.decode("ascii"), PULUMI_EXPERIMENTAL="true",
               PULUMI_HOME=str(stage / "pulumi-home"), PULUMI_BACKEND_URL=backend.as_uri(),
               PULUMI_CONFIG_PASSPHRASE=read_json(password)["value"], PULUMI_SKIP_UPDATE_CHECK="true",
               TERRACE_REHEARSAL_INPUT=str(config_path))
    targets = [flag for urn in phase_targets(phase, manifest["run"]) for flag in ("--target", urn)]
    fixed = ["--stack", phase, "--non-interactive", "--suppress-outputs", "--suppress-permalink", *targets]
    if action == "preview":
        pulumi(project, env, ["stack", "init", phase, "--non-interactive"], seconds=25)
        digest = pulumi(project, env, ["preview", *fixed, "--json", "--show-secrets=false", "--save-plan", str(plan)],
                        seconds=120, output=True)
        report = validate_preview(digest, phase, manifest["run"], version)
        plan_data = lease.read_private(plan, 4 * 1024 * 1024)
        validate_saved_plan(parse_json(plan_data), phase, manifest["run"], version)
        report.update(sourceSha256=source, inputSha256=hashlib.sha256(config_data).hexdigest(),
                      planSha256=hashlib.sha256(plan_data).hexdigest(),
                      stackConfigSha256=optional_sha(stack_config))
        lease.write_private(review_path, report)
        print("Accepted create-only preview", hashlib.sha256(lease.read_private(review_path, 65536)).hexdigest())
        return
    timer.check_ready(directory, manifest, credential)
    review_data = lease.read_private(review_path, 65536)
    report = parse_json(review_data)
    if (approval != hashlib.sha256(review_data).hexdigest() or report["sourceSha256"] != source
            or report["inputSha256"] != hashlib.sha256(config_data).hexdigest()
            or report["stackConfigSha256"] != optional_sha(stack_config)
            or report["planSha256"] != hashlib.sha256(lease.read_private(plan, 4 * 1024 * 1024)).hexdigest()):
        raise lease.Rejected("approved preview binding rejected")
    lease.write_private(directory / f"{phase}-dispatched.json", {"run": manifest["run"], "reviewSha256": approval,
                                                                "dispatchedAt": int(time.time())})
    pulumi(project, env, ["up", *fixed, "--yes", "--skip-preview", "--plan", str(plan)], seconds=170)
    result = pulumi(project, env, ["stack", "output", "owned", "--stack", phase, "--json", "--non-interactive"],
                    seconds=15, output=True)
    counts = Counter(kind for kind, _ in phase_targets(phase, manifest["run"]).values())
    inventory = api.inventory()
    owned, collisions = lease.select_owned(manifest["run"], manifest["baseline"], inventory, "final")
    observed = ids_by_kind(owned)
    if collisions or set(result) != set(lease.DELETION_ORDER):
        raise lease.Rejected("created identity collision")
    for kind in lease.DELETION_ORDER:
        ids = sorted(lease.positive_id(rid) for rid in result[kind])
        if len(ids) != counts[kind] or len(set(ids)) != len(ids) or sorted(before[kind] + ids) != observed[kind]:
            raise lease.Rejected("created IDs disagree with inventory")
    if without_owned(inventory, owned) != manifest["baseline"]:
        raise lease.Rejected("post-create inventory disagrees with baseline")
    if phase == "foundation":
        verify_foundation(owned, config)
    else:
        verify_cohort(owned, inventory, config, result)
    lease.write_private(directory / f"{phase}-created.json", result)
    print("Cloud rehearsal phase creation verified.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "create", "binding"))
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--review-sha")
    parser.add_argument("--source-sha")
    parser.add_argument("--role", choices=lease.ROLES)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        directory = lease.private_directory(args.directory)
        token = lease.read_private(args.credential_file, 4096).strip()
        manifest = read_json(directory / "lease.json", 4 * 1024 * 1024)
        if manifest["schema"] != 1 or manifest["credentialSha256"] != hashlib.sha256(token).hexdigest():
            raise lease.Rejected("credential does not match the lease")
        with os.fdopen(os.open(directory / "lock", os.O_WRONLY | os.O_NOFOLLOW), "wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action == "binding":
                api = lease.Api(token.decode("ascii"))
                binding = observe_server(api, directory, manifest, args.phase, args.role)
                path = directory / f"{args.phase}-{args.role}-binding-{time.time_ns()}.json"
                lease.write_private(path, binding)
                print("Protected server binding written to", path)
            else:
                operate(args.action, directory, manifest, token, args.phase, args.credential_file,
                        args.review_sha, args.source_sha)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, subprocess.TimeoutExpired):
        print("Cloud rehearsal preview or apply rejected. Check the protected run state.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
