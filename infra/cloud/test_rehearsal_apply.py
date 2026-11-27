import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import rehearsal_apply as apply
import rehearsal_lease as lease
from test_rehearsal_lease import RUN, PREFIX, FakeApi, inventory, populate, resource, write_reviewed_input


FOUNDATION = {"phase": "foundation", "run": RUN, "sshPublicKey": "ssh-ed25519 synthetic",
              "sshCidrs": ["192.0.2.1/32"]}


def create_foundation(data):
    data["firewalls"] = [resource(8, PREFIX + "-firewall", applied_to=[], rules=[
        {"direction": "in", "protocol": "tcp", "port": "22", "source_ips": FOUNDATION["sshCidrs"],
         "destination_ips": []},
    ])]
    data["ssh_keys"].append(resource(9, PREFIX + "-key", public_key=FOUNDATION["sshPublicKey"]))


SINGLE = {"run": RUN, "phase": "original", "roles": ["app"], "sshKeyId": 9, "firewallId": 8,
          "image": "debian-13", "location": "nbg1", "appType": "cpx42", "relayType": "cpx22"}


HOSTS = {"app": (4, 5, "cpx42"), "relay": (6, 7, "cpx22")}


def create_host(data, role):
    server_id, ip_id, server_type = HOSTS[role]
    name = PREFIX + "-original-" + role
    data["servers"].append(resource(
        server_id, name, volumes=[], private_net=[], load_balancers=[], server_type={"name": server_type},
        image={"name": "debian-13"}, location={"name": "nbg1"}, rescue_enabled=False, backup_window=None,
        protection={"delete": False, "rebuild": False}, status="running",
        public_net={"ipv4": {"id": ip_id, "ip": "93.184.215.14"}, "ipv6": None,
                    "firewalls": [{"id": 8, "status": "applied"}], "floating_ips": []}))
    data["primary_ips"].append(resource(ip_id, name + "-ip", assignee_id=server_id, assignee_type="server",
                                        auto_delete=False, protection={"delete": False}, ip="93.184.215.14"))


def create_single_host(data):
    create_host(data, "app")


def digest(phase, *, probe_only=False, single_app=False):
    targets = apply.phase_targets(phase, RUN, probe_only=probe_only, single_app=single_app)
    prefix = f"urn:pulumi:{phase}::{apply.PROJECT}::"
    return {
        "steps": [{"urn": urn, "op": "create", "newState": {"inputs": {"name": name, "labels": {lease.LABEL: RUN}}}}
                  for urn, (_, name) in targets.items()] + [
            {"urn": prefix + f"pulumi:pulumi:Stack::{apply.PROJECT}-{phase}", "op": "create"},
            {"urn": prefix + "pulumi:providers:hcloud::default_1_42_0", "op": "create"},
        ],
        "changeSummary": {"create": len(targets) + 2},
    }


def saved_plan(phase, *, probe_only=False, single_app=False):
    return {"resourcePlans": {step["urn"]: {"steps": ["create"]}
                              for step in digest(phase, probe_only=probe_only, single_app=single_app)["steps"]}}


class RehearsalApplyTest(unittest.TestCase):
    def test_replacement_keeps_bootstrap_mode_and_rejects_previous_nonces(self):
        operator = "ssh-ed25519 " + "z" * 68
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_nonce = b"0123456789abcdef" * 4 + b"\n"
            original = {"app": {"nonceSha256": hashlib.sha256(old_nonce).hexdigest()}}
            write_reviewed_input(root, {"run": RUN, "phase": "original", "bootstrapProofs": original})
            lease.write_private(root / "foundation-input.json", {"sshPublicKey": operator})
            with self.assertRaisesRegex(lease.Rejected, "mode differs"):
                apply.bootstrap_material(root, "replacement", {}, probe_only=False)
            for role in ("app", "relay"):
                (root / f"replacement-{role}-bootstrap-nonce").write_bytes(old_nonce if role == "relay" else b"fedcba9876543210" * 4 + b"\n")
                (root / f"replacement-{role}-bootstrap-nonce").chmod(0o600)
            cfg = {"bootstrapOperatorPublicKey": operator, "bootstrapProofs": {
                "relay": original["app"], "app": {"nonceSha256": hashlib.sha256(b"fedcba9876543210" * 4 + b"\n").hexdigest()}}}
            with self.assertRaisesRegex(lease.Rejected, "nonces must differ"):
                apply.bootstrap_material(root, "replacement", cfg, probe_only=False)

    def test_bootstrap_proof_is_bound_to_reviewed_nonce_and_rejects_private_keys(self):
        nonce = b"0123456789abcdef" * 4 + b"\n"
        operator = "ssh-ed25519 " + "y" * 68
        cfg = {"bootstrapOperatorPublicKey": operator,
               "bootstrapProofs": {"relay": {"nonceSha256": hashlib.sha256(nonce).hexdigest()}}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lease.write_private(root / "foundation-input.json", {"sshPublicKey": operator + " comment"})
            path = root / "original-relay-bootstrap-nonce"
            path.write_bytes(nonce)
            path.chmod(0o600)
            needles = apply.bootstrap_material(root, "original", cfg, probe_only=True)
            self.assertEqual(needles, (b"PRIVATE KEY-----",))
            with self.assertRaises(lease.Rejected):
                apply.bootstrap_material(root, "original", cfg, probe_only=False)
            with self.assertRaisesRegex(lease.Rejected, "differs from the foundation"):
                apply.bootstrap_material(root, "original", {**cfg, "bootstrapOperatorPublicKey": "ssh-ed25519 " + "x" * 68},
                                         probe_only=True)
            with self.assertRaisesRegex(lease.Rejected, "must not pass through user data"):
                apply.bootstrap_material(root, "original", {**cfg, "bootstrapHostKeys": {}}, probe_only=True)
            for leak in (b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA", b"-----BEGIN PRIVATE KEY-----"):
                with self.assertRaisesRegex(lease.Rejected, "unencrypted"):
                    apply.reject_plaintext(leak, needles)
            # The low-value proof nonce may appear in bridge metadata.
            apply.reject_plaintext(nonce, needles)
            path.write_bytes(b"ab" * 32 + b"\n")
            with self.assertRaisesRegex(lease.Rejected, "nonce differs"):
                apply.bootstrap_material(root, "original", {**cfg, "bootstrapProofs": {"relay": {
                    "nonceSha256": hashlib.sha256(b"ab" * 32 + b"\n").hexdigest()}}}, probe_only=True)
            path.write_bytes(b"0" * 64 + b"\n")
            with self.assertRaisesRegex(lease.Rejected, "nonce differs"):
                apply.bootstrap_material(root, "original", cfg, probe_only=True)

    def test_bootstrap_plan_and_checkpoint_require_ciphertext(self):
        secret = {**apply.PULUMI_SECRET_SIGNATURE, "ciphertext": "synthetic-encrypted-value"}
        urn = next(urn for urn, (kind, _) in apply.phase_targets("original", RUN, probe_only=True).items()
                   if kind == "servers")
        plan = {"resourcePlans": {urn: {"goal": {"inputDiff": {"adds": {"userData": secret}}},
                                          "state": {"userData": secret}}}}
        apply.check_secret_plan(plan, [urn])
        plan["resourcePlans"][urn]["state"]["userData"] = "synthetic-plaintext"
        with self.assertRaises(lease.Rejected):
            apply.check_secret_plan(plan, [urn])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "backend/.pulumi/stacks" / apply.PROJECT / "original.json"
            checkpoint.parent.mkdir(parents=True)
            row = {"urn": urn, "inputs": {"userData": secret}, "outputs": {"userData": secret}}
            value = {"checkpoint": {"latest": {"resources": [row]}}}
            lease.write_private(checkpoint, value)
            apply.check_secret_checkpoint(root, "original", [urn])
            apply.check_pulumi_artifacts(root, (b"synthetic-plaintext",))
            row["outputs"]["userData"] = "synthetic-plaintext"
            checkpoint.unlink()
            lease.write_private(checkpoint, value)
            with self.assertRaises(lease.Rejected):
                apply.check_secret_checkpoint(root, "original", [urn])
            with self.assertRaises(lease.Rejected):
                apply.check_pulumi_artifacts(root, (b"synthetic-plaintext",))

    def test_failed_update_retains_only_scoped_codes(self):
        targets = apply.phase_targets("original", RUN)
        app = next(urn for urn in targets if urn.endswith("::app"))
        secret = "synthetic-private-marker"
        rows = [
            {"diagnosticEvent": {"severity": "error", "urn": app,
                                 "message": f"Error during placement (resource_unavailable, {secret})"}},
            {"diagnosticEvent": {"severity": "warning", "urn": app, "message": "(maintenance)"}},
            {"diagnosticEvent": {"severity": "error", "urn": secret, "message": "(conflict)"}},
            {"diagnosticEvent": {"severity": "error", "urn": app, "message": f"({secret})"}},
        ]

        def run(*_args, stdout, **_kwargs):
            stdout.write(b"not an event\n")
            for row in rows:
                stdout.write(json.dumps(row).encode() + b"\n")
            return subprocess.CompletedProcess([], 1)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "failure.json"
            with patch.object(apply.subprocess, "run", side_effect=run), self.assertRaises(lease.Rejected):
                apply.pulumi(Path(temporary), {}, ["up"], seconds=10, targets=targets, failure_receipt=path)
            self.assertEqual(apply.read_json(path), {
                "exitStatus": 1, "outputExceeded": False,
                "errors": [{"resource": "app", "code": "resource_unavailable"}],
            })
            self.assertNotIn(secret.encode(), path.read_bytes())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(apply.failed_update(b"x" * (apply.PULUMI_OUTPUT_LIMIT + 1), targets, 1),
                         {"exitStatus": 1, "outputExceeded": True, "errors": []})

    def test_staged_source_rejects_node_resolution_shadows(self):
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name)
            project = parent / "rehearsal-create"
            project.mkdir(mode=0o700)
            deps = parent / "node_modules" / "@pulumi"
            for package in ("pulumi", "hcloud"):
                (deps / package).mkdir(parents=True)
            for root, names in ((parent, apply.PARENT_FILES), (project, apply.PROJECT_FILES)):
                for path in names:
                    lease.write_private(root / path, {"synthetic": path})
            fingerprint = apply.source_sha(project)
            self.assertRegex(fingerprint, r"^[a-f0-9]{64}$")
            lease.write_private(project / "resources.js", {"shadow": True})
            with self.assertRaises(lease.Rejected):
                apply.source_sha(project)

    def test_exact_create_only_phases(self):
        for phase in apply.PHASES:
            data = digest(phase)
            report = apply.validate_preview(data, phase, RUN, "1.42.0")
            self.assertEqual(len(report["creates"]), 2 if phase == "foundation" else 4)
            self.assertEqual(report["providerAndStackCreates"], 2)
            # Targeted provider previews may omit the default-provider bookkeeping step.
            without_provider = copy.deepcopy(data)
            without_provider["steps"].pop()
            without_provider["changeSummary"]["create"] -= 1
            self.assertEqual(apply.validate_preview(without_provider, phase, RUN, "1.42.0")["providerAndStackCreates"], 1)
            plan = saved_plan(phase)
            apply.validate_saved_plan(plan, phase, RUN, "1.42.0")
            plan["resourcePlans"][next(iter(plan["resourcePlans"]))]["steps"] = ["update"]
            with self.assertRaises(lease.Rejected):
                apply.validate_saved_plan(plan, phase, RUN, "1.42.0")
            for key, value in (("urn", "unplanned"), ("op", "delete"), ("op", "same"),
                               ("newState", {"inputs": {"name": "wrong", "labels": {}}})):
                changed = copy.deepcopy(data)
                changed["steps"][0][key] = value
                with self.assertRaises(lease.Rejected):
                    apply.validate_preview(changed, phase, RUN, "1.42.0")

    def test_console_probe_requires_exact_single_relay_plan(self):
        data = digest("original", probe_only=True)
        plan = saved_plan("original", probe_only=True)
        report = apply.validate_preview(data, "original", RUN, "1.42.0", probe_only=True)
        self.assertEqual({urn.rsplit("::", 1)[1] for urn in report["creates"]}, {"relay", "relay-ip"})
        apply.validate_saved_plan(plan, "original", RUN, "1.42.0", probe_only=True)
        with self.assertRaises(lease.Rejected):
            apply.validate_preview(data, "original", RUN, "1.42.0")
        with self.assertRaises(lease.Rejected):
            apply.validate_saved_plan(saved_plan("original"), "original", RUN, "1.42.0", probe_only=True)
        for phase in ("foundation", "replacement"):
            with self.assertRaises(lease.Rejected):
                apply.phase_targets(phase, RUN, probe_only=True)

    def test_phase_inventory_requires_baseline_foundation_and_original_absence(self):
        data = inventory()
        manifest = {"run": RUN, "baseline": lease.baseline_from(data)}
        api = FakeApi(data)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            apply.expected_before(api, manifest, root, "foundation", {})
            create_foundation(data)
            foundation = {"servers": [], "primary_ips": [], "firewalls": [8], "ssh_keys": [9]}
            lease.write_private(root / "foundation-created.json", foundation)
            lease.write_private(root / "foundation-input.json", FOUNDATION)
            original_input = {"run": RUN, "phase": "original"}
            config_path = write_reviewed_input(root, {**original_input, "probeOnly": True})
            config = {"sshKeyId": 9, "firewallId": 8}
            self.assertEqual(apply.expected_before(api, manifest, root, "original", config), foundation)
            with self.assertRaises(lease.Rejected):
                apply.expected_before(api, manifest, root, "replacement", config)
            original = {"servers": [7, 6], "primary_ips": [5, 4], "firewalls": [], "ssh_keys": []}
            lease.write_private(root / "original-created.json", original)
            lease.write_private(root / "original-absence-1.json", {
                "run": RUN, "phase": "original", "baselinePreserved": True, "absenceReads": 3, "unresolvedDispatches": [],
                "verifiedAbsent": {kind: sorted(ids) for kind, ids in original.items()},
            })
            with self.assertRaisesRegex(lease.Rejected, "does not authorize replacement"):
                apply.expected_before(api, manifest, root, "replacement", config)
            config_path.unlink()
            lease.write_private(config_path, original_input)
            with self.assertRaisesRegex(lease.Rejected, "no longer matches"):
                apply.expected_before(api, manifest, root, "replacement", config)
            write_reviewed_input(root, original_input)
            self.assertEqual(apply.expected_before(api, manifest, root, "replacement", config), foundation)
            populate(data, "original", 6, 4)
            with self.assertRaises(lease.Rejected):
                apply.expected_before(api, manifest, root, "replacement", config)

    def test_apply_uses_same_preview_plan_and_cannot_dispatch_twice(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / "foundation-input.json"
            lease.write_private(config, FOUNDATION)
            parent = root / "project"
            (parent / "rehearsal-create").mkdir(parents=True)
            lease.write_private(parent / "package.json", {"dependencies": {"@pulumi/hcloud": "1.42.0"}})
            api = FakeApi(inventory())
            manifest = {"run": RUN, "baseline": lease.baseline_from(api.data)}
            created = {"servers": [], "primary_ips": [], "firewalls": [8], "ssh_keys": [9]}
            calls = []

            def pulumi(_project, _env, args, **_options):
                calls.append(args)
                if args[0] == "preview":
                    lease.write_private(Path(args[-1]), saved_plan("foundation"))
                    return digest("foundation")
                if args[0] == "up":
                    self.assertEqual(args[-2:], ["--plan", calls[1][-1]])
                    self.assertEqual([args[i + 1] for i, value in enumerate(args) if value == "--target"],
                                     list(apply.phase_targets("foundation", RUN)))
                    create_foundation(api.data)
                if args[:2] == ["stack", "output"]:
                    return created

            source = "1" * 64
            with (patch.object(apply.lease, "Api", return_value=api), patch.object(apply, "source_sha", return_value=source),
                  patch.object(apply, "pulumi", pulumi), patch.object(apply, "require_run_unit"),
                  patch.object(apply.timer, "check_ready") as ready, patch("builtins.print")):
                args = (root, manifest, b"synthetic", "foundation", root / "credential")
                with self.assertRaises(lease.Rejected):
                    apply.operate("preview", *args, None, "2" * 64)
                apply.operate("preview", *args, None, source)
                review_sha = hashlib.sha256((root / "foundation/review.json").read_bytes()).hexdigest()
                with self.assertRaises(lease.Rejected):
                    apply.operate("create", *args, "unreviewed", source)
                self.assertFalse((root / "foundation-dispatched.json").exists())
                apply.operate("create", *args, review_sha, source)
                self.assertTrue((root / "foundation-dispatched.json").exists())
                self.assertEqual(apply.read_json(root / "foundation-created.json"), created)
                self.assertEqual(ready.call_count, 2)
                with self.assertRaises(lease.Rejected):
                    apply.operate("create", *args, review_sha, source)
                self.assertEqual(sum(args[0] == "up" for args in calls), 1)

    def test_install_binding_is_role_and_provider_id_specific(self):
        data = inventory()
        manifest = {"run": RUN, "baseline": lease.baseline_from(data), "expires": int(time.time()) + 900}
        populate(data, "original", 4, 5)
        data["servers"][1]["public_net"]["ipv4"]["ip"] = "93.184.215.14"
        data["firewalls"] = [resource(8, PREFIX + "-firewall", applied_to=[])]
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            lease.write_private(root / "original-created.json", {
                "servers": [4], "primary_ips": [5], "firewalls": [], "ssh_keys": [],
            })
            api = FakeApi(data)
            receipt = apply.observe_server(api, root, manifest, "original", "app")
            self.assertEqual(receipt["serverId"], 4)
            self.assertEqual(receipt["phase"], "original")
            self.assertEqual(receipt["ipv4"], "93.184.215.14")
            with self.assertRaises(lease.Rejected):
                apply.observe_server(api, root, manifest, "original", "relay")
            data["servers"][1]["public_net"]["ipv4"]["id"] = 2
            with self.assertRaises(lease.Rejected):
                apply.observe_server(api, root, manifest, "original", "app")


class SingleHostApplyTest(unittest.TestCase):
    """Create gates for the original-only `"roles": ["app"]` cohort."""

    def test_targets_are_exactly_one_named_app_and_its_ipv4(self):
        targets = apply.phase_targets("original", RUN, single_app=True)
        prefix = f"urn:pulumi:original::{apply.PROJECT}::"
        self.assertEqual(targets, {
            prefix + "hcloud:index/primaryIp:PrimaryIp::app-ip": ("primary_ips", PREFIX + "-original-app-ip"),
            prefix + "hcloud:index/server:Server::app": ("servers", PREFIX + "-original-app"),
        })
        for phase in ("foundation", "replacement"):
            with self.assertRaises(lease.Rejected):
                apply.phase_targets(phase, RUN, single_app=True)
        with self.assertRaises(lease.Rejected):
            apply.phase_targets("original", RUN, single_app=True, probe_only=True)

    def test_preview_and_saved_plan_are_create_only_and_exactly_scoped(self):
        data = digest("original", single_app=True)
        report = apply.validate_preview(data, "original", RUN, "1.42.0", single_app=True)
        self.assertEqual({urn.rsplit("::", 1)[1] for urn in report["creates"]}, {"app", "app-ip"})
        apply.validate_saved_plan(saved_plan("original", single_app=True), "original", RUN, "1.42.0", single_app=True)
        # The other shapes' plans do not pass this gate, nor this plan theirs.
        for other in ({}, {"probe_only": True}):
            with self.assertRaises(lease.Rejected):
                apply.validate_preview(digest("original", **other), "original", RUN, "1.42.0", single_app=True)
            with self.assertRaises(lease.Rejected):
                apply.validate_saved_plan(saved_plan("original", **other), "original", RUN, "1.42.0",
                                          single_app=True)
            with self.assertRaises(lease.Rejected):
                apply.validate_preview(data, "original", RUN, "1.42.0", **other)
        for index in range(len(data["steps"])):
            for key, value in (("op", "update"), ("op", "delete"), ("op", "same"), ("op", "replace"),
                               ("op", "create-replacement"), ("urn", "unplanned")):
                changed = copy.deepcopy(data)
                changed["steps"][index][key] = value
                with self.assertRaises(lease.Rejected):
                    apply.validate_preview(changed, "original", RUN, "1.42.0", single_app=True)
        for index in range(2):
            for inputs in ({"name": PREFIX + "-original-relay", "labels": {lease.LABEL: RUN}},
                           {"name": data["steps"][index]["newState"]["inputs"]["name"], "labels": {}},
                           {"name": data["steps"][index]["newState"]["inputs"]["name"],
                            "labels": {lease.LABEL: "b" * 32}}):
                changed = copy.deepcopy(data)
                changed["steps"][index]["newState"]["inputs"] = inputs
                with self.assertRaisesRegex(lease.Rejected, "name or label"):
                    apply.validate_preview(changed, "original", RUN, "1.42.0", single_app=True)
        plan = saved_plan("original", single_app=True)
        for urn in plan["resourcePlans"]:
            for steps in (["update"], ["delete"], ["create", "delete"], ["same"]):
                changed = copy.deepcopy(plan)
                changed["resourcePlans"][urn]["steps"] = steps
                with self.assertRaises(lease.Rejected):
                    apply.validate_saved_plan(changed, "original", RUN, "1.42.0", single_app=True)

    def test_bootstrap_proofs_cover_only_the_app(self):
        nonce = b"0123456789abcdef" * 4 + b"\n"
        operator = "ssh-ed25519 " + "y" * 68
        proof = {"nonceSha256": hashlib.sha256(nonce).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lease.write_private(root / "foundation-input.json", {"sshPublicKey": operator})
            path = root / "original-app-bootstrap-nonce"
            path.write_bytes(nonce)
            path.chmod(0o600)
            cfg = {"bootstrapOperatorPublicKey": operator, "bootstrapProofs": {"app": proof}}
            self.assertEqual(apply.bootstrap_material(root, "original", cfg, probe_only=False, single_app=True),
                             (b"PRIVATE KEY-----",))
            for proofs in ({"relay": proof}, {"app": proof, "relay": proof}, {}):
                with self.assertRaisesRegex(lease.Rejected, "proof scope"):
                    apply.bootstrap_material(root, "original", {**cfg, "bootstrapProofs": proofs},
                                             probe_only=False, single_app=True)

    def test_single_host_original_does_not_authorize_replacement(self):
        data = inventory()
        manifest = {"run": RUN, "baseline": lease.baseline_from(data)}
        create_foundation(data)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            lease.write_private(root / "foundation-created.json",
                                {"servers": [], "primary_ips": [], "firewalls": [8], "ssh_keys": [9]})
            lease.write_private(root / "foundation-input.json", FOUNDATION)
            write_reviewed_input(root, SINGLE, roles=["app"])
            original = {"servers": [4], "primary_ips": [5], "firewalls": [], "ssh_keys": []}
            lease.write_private(root / "original-created.json", original)
            lease.write_private(root / "original-absence-1.json", {
                "run": RUN, "phase": "original", "baselinePreserved": True, "absenceReads": 3,
                "unresolvedDispatches": [], "verifiedAbsent": original,
            })
            with self.assertRaisesRegex(lease.Rejected, "single-host cohort does not authorize replacement"):
                apply.expected_before(FakeApi(data), manifest, root, "replacement", SINGLE)

    def test_created_host_must_have_only_its_ipv4_and_the_run_firewall(self):
        data = inventory()
        create_foundation(data)
        create_single_host(data)
        created = {"servers": [4], "primary_ips": [5], "firewalls": [], "ssh_keys": []}
        manifest = {"run": RUN, "baseline": lease.baseline_from(inventory())}
        owned, collisions = lease.select_owned(RUN, manifest["baseline"], data, "final")
        self.assertFalse(collisions)
        apply.verify_cohort(owned, data, SINGLE, created)
        for change in (lambda s: s["public_net"].update(ipv6={"id": 77}),
                       lambda s: s["public_net"]["firewalls"].append({"id": 3, "status": "applied"}),
                       lambda s: s["public_net"].update(floating_ips=[66]),
                       lambda s: s.update(volumes=[55]),
                       lambda s: s.update(server_type={"name": "cpx22"}),
                       lambda s: s.update(location={"name": "ash"})):
            changed = copy.deepcopy(data)
            change(changed["servers"][-1])
            owned, _ = lease.select_owned(RUN, manifest["baseline"], changed, "final")
            with self.assertRaises(lease.Rejected):
                apply.verify_cohort(owned, changed, SINGLE, created)

    def test_preview_then_create_one_app_host(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            lease.write_private(root / "original-input.json", SINGLE)
            lease.write_private(root / "foundation-input.json", FOUNDATION)
            foundation = {"servers": [], "primary_ips": [], "firewalls": [8], "ssh_keys": [9]}
            lease.write_private(root / "foundation-created.json", foundation)
            parent = root / "project"
            (parent / "rehearsal-create").mkdir(parents=True)
            lease.write_private(parent / "package.json", {"dependencies": {"@pulumi/hcloud": "1.42.0"}})
            data = inventory()
            manifest = {"run": RUN, "baseline": lease.baseline_from(data)}
            create_foundation(data)
            api = FakeApi(data)
            created = {"servers": [4], "primary_ips": [5], "firewalls": [], "ssh_keys": []}
            calls = []
            preview_digest = [digest("original", single_app=True)]

            def pulumi(_project, _env, args, **_options):
                calls.append(args)
                if args[0] == "preview":
                    lease.write_private(Path(args[-1]), saved_plan("original", single_app=True))
                    return preview_digest[0]
                if args[0] == "up":
                    self.assertEqual([args[i + 1] for i, value in enumerate(args) if value == "--target"],
                                     list(apply.phase_targets("original", RUN, single_app=True)))
                    create_single_host(api.data)
                if args[:2] == ["stack", "output"]:
                    return created

            source = "1" * 64
            with (patch.object(apply.lease, "Api", return_value=api), patch.object(apply, "source_sha", return_value=source),
                  patch.object(apply, "pulumi", pulumi), patch.object(apply, "require_run_unit"),
                  patch.object(apply.timer, "check_ready"), patch("builtins.print")):
                args = (root, manifest, b"synthetic", "original", root / "credential")
                # A preview that would also create the relay is refused for this input.
                preview_digest[0] = digest("original")
                with self.assertRaises(lease.Rejected):
                    apply.operate("preview", *args, None, source)
                for path in sorted((root / "original").rglob("*"), reverse=True):
                    path.rmdir() if path.is_dir() else path.unlink()
                (root / "original").rmdir()
                preview_digest[0] = digest("original", single_app=True)
                apply.operate("preview", *args, None, source)
                review_path = root / "original/review.json"
                review = apply.read_json(review_path)
                self.assertEqual((review["probeOnly"], review["roles"]), (False, ["app"]))
                # A review whose roles differ from the input never dispatches.
                review_path.unlink()
                lease.write_private(review_path, {**review, "roles": ["app", "relay"]})
                tampered = hashlib.sha256(review_path.read_bytes()).hexdigest()
                with self.assertRaisesRegex(lease.Rejected, "approved preview binding"):
                    apply.operate("create", *args, tampered, source)
                self.assertFalse((root / "original-dispatched.json").exists())
                review_path.unlink()
                lease.write_private(review_path, review)
                review_sha = hashlib.sha256(review_path.read_bytes()).hexdigest()
                apply.operate("create", *args, review_sha, source)
                self.assertEqual(apply.read_json(root / "original-created.json"), created)
                self.assertEqual(lease.reviewed_roles(root, "original"), ("app",))
                self.assertEqual(sum(args[0] == "up" for args in calls), 1)
            self.assertEqual([row["id"] for row in api.data["servers"]], [1, 4])
            self.assertEqual(api.deleted, [])

    def test_every_review_records_roles_and_old_reviews_never_authorize_single_host(self):
        """Pair, probe and single host: the preview records the shape's roles. A review
        in the layout from before this shape (no roles key) still authorizes the pair
        and the probe as before, and is refused for the single host."""
        shapes = (({}, {}, ["app", "relay"]), ({"probeOnly": True}, {"probe_only": True}, ["relay"]),
                  ({"roles": ["app"]}, {"single_app": True}, ["app"]))
        for extra, shape, roles in shapes:
            for legacy in (False, True):
                with self.subTest(roles=roles, legacy=legacy), tempfile.TemporaryDirectory() as name:
                    root = Path(name)
                    config = {key: value for key, value in SINGLE.items() if key != "roles"}
                    lease.write_private(root / "original-input.json", {**config, **extra})
                    lease.write_private(root / "foundation-input.json", FOUNDATION)
                    lease.write_private(root / "foundation-created.json",
                                        {"servers": [], "primary_ips": [], "firewalls": [8], "ssh_keys": [9]})
                    (root / "project/rehearsal-create").mkdir(parents=True)
                    lease.write_private(root / "project/package.json", {"dependencies": {"@pulumi/hcloud": "1.42.0"}})
                    data = inventory()
                    manifest = {"run": RUN, "baseline": lease.baseline_from(data)}
                    create_foundation(data)
                    api = FakeApi(data)
                    created = {"servers": [HOSTS[r][0] for r in roles], "primary_ips": [HOSTS[r][1] for r in roles],
                               "firewalls": [], "ssh_keys": []}

                    def pulumi(_project, _env, args, **_options):
                        if args[0] == "preview":
                            lease.write_private(Path(args[-1]), saved_plan("original", **shape))
                            return digest("original", **shape)
                        if args[0] == "up":
                            for role in roles:
                                create_host(api.data, role)
                        if args[:2] == ["stack", "output"]:
                            return created

                    source = "1" * 64
                    with (patch.object(apply.lease, "Api", return_value=api),
                          patch.object(apply, "source_sha", return_value=source), patch.object(apply, "pulumi", pulumi),
                          patch.object(apply, "require_run_unit"), patch.object(apply.timer, "check_ready"),
                          patch("builtins.print")):
                        args = (root, manifest, b"synthetic", "original", root / "credential")
                        apply.operate("preview", *args, None, source)
                        review_path = root / "original/review.json"
                        review = apply.read_json(review_path)
                        self.assertEqual(review["roles"], roles)
                        self.assertIs(review["probeOnly"], "probeOnly" in extra)
                        if legacy:
                            del review["roles"]
                            review_path.unlink()
                            lease.write_private(review_path, review)
                        approval = hashlib.sha256(review_path.read_bytes()).hexdigest()
                        if legacy and "roles" in extra:
                            with self.assertRaisesRegex(lease.Rejected, "without recorded roles"):
                                apply.operate("create", *args, approval, source)
                            self.assertFalse((root / "original-dispatched.json").exists())
                            self.assertEqual(len(api.data["servers"]), 1)
                            continue
                        apply.operate("create", *args, approval, source)
                        self.assertEqual(apply.read_json(root / "original-created.json"), created)
                        self.assertEqual(lease.reviewed_roles(root, "original"), tuple(roles))


if __name__ == "__main__":
    unittest.main()
