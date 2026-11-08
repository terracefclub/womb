"""Preview and apply exact create-only Pulumi plans for an armed cloud lease."""

import argparse
from collections import Counter
import fcntl
import hashlib
import io
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
PROJECT_FILES = ("Pulumi.yaml", "README.md", "bootstrap.ts", "index.ts", "input.ts", "rehearsal.test.ts",
                 "resources.ts", "tsconfig.json")
PARENT_FILES = ("budget.ts", "package.json", "bun.lock", "tsconfig.json")
PULUMI_OUTPUT_LIMIT = 4 * 1024 * 1024
PULUMI_SECRET_SIGNATURE = {"4dabf18193072939515e22adb298388d": "1b47061264138c4ac30d75fd1eb44270"}
PROVIDER_ERROR_CODES = re.compile(
    r"\((resource_unavailable|resource_limit_exceeded|invalid_server_type|invalid_input|"
    r"forbidden|maintenance|rate_limit_exceeded|service_error|conflict|timeout)(?:,|\))"
)


def phase_targets(phase, run, *, probe_only=False):
    if phase not in PHASES or (probe_only and phase != "original"):
        raise lease.Rejected("phase rejected")
    lease.expected_names(run)
    prefix = "terrace-r-" + run
    urn_prefix = f"urn:pulumi:{phase}::{PROJECT}::"
    names = ("key", "firewall") if phase == "foundation" else ("app-ip", "relay-ip", "app", "relay")
    if probe_only:
        names = ("relay-ip", "relay")
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


def validate_preview(digest, phase, run, provider_version, *, probe_only=False):
    targets = phase_targets(phase, run, probe_only=probe_only)
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


def validate_saved_plan(plan, phase, run, provider_version, *, probe_only=False):
    targets = phase_targets(phase, run, probe_only=probe_only)
    prefix = f"urn:pulumi:{phase}::{PROJECT}::"
    stack = prefix + f"pulumi:pulumi:Stack::{PROJECT}-{phase}"
    provider = prefix + "pulumi:providers:hcloud::default_" + provider_version.replace(".", "_")
    plans = plan["resourcePlans"]
    if set(plans) != {*targets, stack, provider} or plan.get("config"):
        raise lease.Rejected("saved plan inventory or config rejected")
    if any(entry["steps"] != ["create"] for entry in plans.values()):
        raise lease.Rejected("saved plan contains an unexpected operation")


def bootstrap_material(directory, phase, config, *, probe_only):
    """Validate the reviewed host-key proof scope and return private-key needles.

    Bootstrap hosts generate their own SSH host key. User data carries only a one-use
    proof nonce, which may appear in the plan and backend: the hcloud bridge copies
    raw user data into unencrypted plan metadata. No private key may appear anywhere.
    """
    original_proofs = None
    if phase == "replacement":
        original, _ = lease.reviewed_phase_input(directory, "original")
        original_proofs = original.get("bootstrapProofs")
        if ("bootstrapProofs" in original) != ("bootstrapProofs" in config):
            raise lease.Rejected("replacement bootstrap mode differs from the original")
    if "bootstrapHostKeys" in config:
        raise lease.Rejected("host private keys must not pass through user data")
    if "bootstrapProofs" not in config:
        if "bootstrapOperatorPublicKey" in config:
            raise lease.Rejected("bootstrap operator key is outside a bootstrap phase")
        return ()
    proofs = config["bootstrapProofs"]
    roles = {"relay"} if probe_only else set(lease.ROLES)
    if phase not in lease.PHASES or not isinstance(proofs, dict) or set(proofs) != roles:
        raise lease.Rejected("bootstrap proof scope rejected")
    operator = config.get("bootstrapOperatorPublicKey", "")
    foundation = read_json(directory / "foundation-input.json")["sshPublicKey"]
    if (not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]{68}", operator)
            or operator != " ".join(foundation.split()[:2])):
        raise lease.Rejected("bootstrap operator key differs from the foundation")
    seen = set()
    for role, proof in proofs.items():
        if (not isinstance(proof, dict) or set(proof) != {"nonceSha256"}
                or not re.fullmatch(r"[a-f0-9]{64}", proof["nonceSha256"])
                or proof["nonceSha256"] in seen):
            raise lease.Rejected("bootstrap proof format rejected")
        seen.add(proof["nonceSha256"])
        if original_proofs and any(proof["nonceSha256"] == old["nonceSha256"]
                                   for old in original_proofs.values()):
            raise lease.Rejected("replacement proof nonces must differ from the original")
        nonce = lease.read_private(directory / f"{phase}-{role}-bootstrap-nonce", 65)
        if (hashlib.sha256(nonce).hexdigest() != proof["nonceSha256"] or not re.fullmatch(rb"[a-f0-9]{64}\n", nonce)
                or len(set(nonce.strip())) < 8):
            raise lease.Rejected("bootstrap proof nonce differs from approved input")
    # No private key is an input any more. Still reject any PEM or OpenSSH
    # private-key armor marker in output, plan and artifacts.
    return (b"PRIVATE KEY-----",)


def reject_plaintext(data, needles):
    if any(needle in data for needle in needles):
        raise lease.Rejected("unencrypted bootstrap data rejected")


def encrypted_secret(value):
    return (isinstance(value, dict) and set(value) == {*PULUMI_SECRET_SIGNATURE, "ciphertext"}
            and all(value[key] == marker for key, marker in PULUMI_SECRET_SIGNATURE.items())
            and isinstance(value["ciphertext"], str) and bool(value["ciphertext"]))


def check_secret_plan(plan, server_urns):
    for urn in server_urns:
        record = plan["resourcePlans"][urn]
        if not encrypted_secret(record["goal"]["inputDiff"]["adds"]["userData"]):
            raise lease.Rejected("saved bootstrap user data is not encrypted")
        state = record.get("state", {})
        if "userData" in state and not encrypted_secret(state["userData"]):
            raise lease.Rejected("saved bootstrap state user data is not encrypted")


def check_pulumi_artifacts(stage, needles):
    total = 0
    count = 0
    for root in (stage / "backend", stage / "pulumi-home/logs", stage / "pulumi-home/workspaces"):
        if not os.path.lexists(root):
            continue
        paths = [root]
        while paths:
            path = paths.pop()
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                paths.extend(path.iterdir())
                continue
            if not stat.S_ISREG(info.st_mode):
                raise lease.Rejected("unexpected Pulumi artifact")
            count += 1
            total += info.st_size
            if count > 128 or total > 16 * 1024 * 1024:
                raise lease.Rejected("Pulumi artifact inventory exceeds limit")
            reject_plaintext(lease.read_private(path, PULUMI_OUTPUT_LIMIT), needles)


def check_secret_checkpoint(stage, phase, server_urns):
    path = stage / "backend/.pulumi/stacks" / PROJECT / (phase + ".json")
    checkpoint = read_json(path, PULUMI_OUTPUT_LIMIT)
    resources = {row["urn"]: row for row in checkpoint["checkpoint"]["latest"]["resources"]}
    for urn in server_urns:
        record = resources[urn]
        if not encrypted_secret(record["inputs"]["userData"]):
            raise lease.Rejected("applied bootstrap input is not encrypted")
        outputs = record.get("outputs", {})
        if "userData" in outputs and not encrypted_secret(outputs["userData"]):
            raise lease.Rejected("applied bootstrap output is not encrypted")


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
        if lease.reviewed_probe_mode(directory, "original"):
            raise lease.Rejected("console probe does not authorize replacement")
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


def failed_update(log, targets, status):
    if len(log) > PULUMI_OUTPUT_LIMIT:
        return {"exitStatus": status, "outputExceeded": True, "errors": []}
    errors = set()
    for line in io.BytesIO(log):
        try:
            event = parse_json(line)
        except (ValueError, RecursionError):
            continue
        diagnostic = event.get("diagnosticEvent") if isinstance(event, dict) else None
        if not isinstance(diagnostic, dict) or diagnostic.get("severity") != "error":
            continue
        urn = diagnostic.get("urn")
        message = diagnostic.get("message")
        if not isinstance(urn, str) or not isinstance(message, str) or urn not in targets:
            continue
        name = urn.rsplit("::", 1)[1]
        for code in PROVIDER_ERROR_CODES.findall(message):
            errors.add((name, code))
    return {"exitStatus": status, "outputExceeded": False,
            "errors": [{"resource": name, "code": code} for name, code in sorted(errors)]}


def pulumi(project, env, args, *, seconds, output=False, failure_receipt=None, targets=(), needles=()):
    with tempfile.TemporaryFile() as log:
        result = subprocess.run(["pulumi", *args], cwd=project, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.DEVNULL, timeout=seconds, check=False)
        if output or needles or (result.returncode and failure_receipt is not None):
            log.seek(0)
            data = log.read(PULUMI_OUTPUT_LIMIT + 1)
        if needles:
            if len(data) > PULUMI_OUTPUT_LIMIT:
                raise lease.Rejected("Pulumi output exceeds limit")
            reject_plaintext(data, needles)
        if result.returncode:
            if failure_receipt is not None:
                evidence = failed_update(data, targets, result.returncode)
                if needles:
                    evidence["unscopedProviderCodes"] = sorted(set(PROVIDER_ERROR_CODES.findall(
                        data.decode("utf-8", errors="replace"))))
                lease.write_private(failure_receipt, evidence)
            raise lease.Rejected("Pulumi command failed")
        if output:
            if len(data) > PULUMI_OUTPUT_LIMIT:
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
    probe_only = "probeOnly" in config
    if probe_only and config["probeOnly"] is not True:
        raise lease.Rejected("console probe flag rejected")
    scope = phase_targets(phase, manifest["run"], probe_only=probe_only)
    needles = bootstrap_material(directory, phase, config, probe_only=probe_only)
    server_urns = [urn for urn, (kind, _) in scope.items() if kind == "servers"]
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
    targets = [flag for urn in scope for flag in ("--target", urn)]
    fixed = ["--stack", phase, "--non-interactive", "--suppress-outputs", "--suppress-permalink", *targets]
    if action == "preview":
        pulumi(project, env, ["stack", "init", phase, "--non-interactive"], seconds=25, needles=needles)
        digest = pulumi(project, env, ["preview", *fixed, "--json", "--show-secrets=false", "--save-plan", str(plan)],
                        seconds=120, output=True, needles=needles)
        report = validate_preview(digest, phase, manifest["run"], version, probe_only=probe_only)
        plan_data = lease.read_private(plan, 4 * 1024 * 1024)
        planned = parse_json(plan_data)
        validate_saved_plan(planned, phase, manifest["run"], version, probe_only=probe_only)
        if needles:
            reject_plaintext(plan_data, needles)
            check_secret_plan(planned, server_urns)
            check_pulumi_artifacts(stage, needles)
        report.update(probeOnly=probe_only, sourceSha256=source, inputSha256=hashlib.sha256(config_data).hexdigest(),
                      planSha256=hashlib.sha256(plan_data).hexdigest(),
                      stackConfigSha256=optional_sha(stack_config))
        lease.write_private(review_path, report)
        print("Accepted create-only preview", hashlib.sha256(lease.read_private(review_path, 65536)).hexdigest())
        return
    timer.check_ready(directory, manifest, credential)
    review_data = lease.read_private(review_path, 65536)
    report = parse_json(review_data)
    plan_data = lease.read_private(plan, PULUMI_OUTPUT_LIMIT)
    if (approval != hashlib.sha256(review_data).hexdigest() or report["sourceSha256"] != source
            or report["probeOnly"] is not probe_only
            or report["inputSha256"] != hashlib.sha256(config_data).hexdigest()
            or report["stackConfigSha256"] != optional_sha(stack_config)
            or report["planSha256"] != hashlib.sha256(plan_data).hexdigest()):
        raise lease.Rejected("approved preview binding rejected")
    if needles:
        reject_plaintext(plan_data, needles)
        check_secret_plan(parse_json(plan_data), server_urns)
        check_pulumi_artifacts(stage, needles)
    lease.write_private(directory / f"{phase}-dispatched.json", {"run": manifest["run"], "reviewSha256": approval,
                                                                "dispatchedAt": int(time.time())})
    # Pulumi 3.268's update JSON includes raw values in engine events even
    # with show-secrets disabled. Its text renderer redacts them.
    display = [] if needles else ["--json"]
    try:
        pulumi(project, env, ["up", *fixed, "--yes", "--skip-preview", *display, "--show-secrets=false",
                              "--plan", str(plan)], seconds=170,
               failure_receipt=directory / f"{phase}-apply-failure.json", targets=scope, needles=needles)
    finally:
        if needles:
            check_pulumi_artifacts(stage, needles)
    if needles:
        check_secret_checkpoint(stage, phase, server_urns)
    result = pulumi(project, env, ["stack", "output", "owned", "--stack", phase, "--json", "--non-interactive"],
                    seconds=15, output=True, needles=needles)
    counts = Counter(kind for kind, _ in scope.values())
    inventory = api.inventory()
    owned, collisions = lease.select_owned(manifest["run"], manifest["baseline"], inventory, "final")
    observed = ids_by_kind(owned)
    if collisions or set(result) != set(lease.DELETION_ORDER):
        raise lease.Rejected("created identity collision")
    for kind in lease.DELETION_ORDER:
        ids = sorted(lease.positive_id(rid) for rid in result[kind])
        if len(ids) != counts[kind] or len(set(ids)) != len(ids) or sorted(before[kind] + ids) != observed[kind]:
            raise lease.Rejected("created IDs disagree with inventory")
    new_names = {(kind, row["name"]) for kind, rows in owned.items()
                 for row in rows if row["id"] not in before[kind]}
    if new_names != set(scope.values()):
        raise lease.Rejected("created provider names disagree with plan")
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
