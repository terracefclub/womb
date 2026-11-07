import copy
import base64
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


def digest(phase, *, probe_only=False):
    targets = apply.phase_targets(phase, RUN, probe_only=probe_only)
    prefix = f"urn:pulumi:{phase}::{apply.PROJECT}::"
    return {
        "steps": [{"urn": urn, "op": "create", "newState": {"inputs": {"name": name, "labels": {lease.LABEL: RUN}}}}
                  for urn, (_, name) in targets.items()] + [
            {"urn": prefix + f"pulumi:pulumi:Stack::{apply.PROJECT}-{phase}", "op": "create"},
            {"urn": prefix + "pulumi:providers:hcloud::default_1_42_0", "op": "create"},
        ],
        "changeSummary": {"create": len(targets) + 2},
    }


def saved_plan(phase, *, probe_only=False):
    return {"resourcePlans": {step["urn"]: {"steps": ["create"]}
                              for step in digest(phase, probe_only=probe_only)["steps"]}}


class RehearsalApplyTest(unittest.TestCase):
    def test_bootstrap_key_is_bound_to_reviewed_bytes_and_public_pin(self):
        body = b"syntheticPrivateKeyBodyOnly" * 3
        private = b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + body + b"\n-----END OPENSSH PRIVATE KEY-----\n"
        public = "ssh-ed25519 " + "x" * 68
        cfg = {"bootstrapHostKeys": {"relay": {"publicKey": public,
                                                  "privateKeySha256": hashlib.sha256(private).hexdigest()}}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "original-relay-bootstrap-ed25519"
            path.write_bytes(private)
            path.chmod(0o600)
            with patch.object(apply.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, public.encode())):
                needles = apply.bootstrap_material(root, "original", cfg, probe_only=True)
                with self.assertRaises(lease.Rejected):
                    apply.bootstrap_material(root, "original", cfg, probe_only=False)
            for leak in (private, body, base64.b64encode(b"x" + body)):
                with self.assertRaisesRegex(lease.Rejected, "unencrypted"):
                    apply.reject_plaintext(leak, needles)
            with patch.object(apply.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"wrong pin")):
                with self.assertRaisesRegex(lease.Rejected, "does not match approved private key"):
                    apply.bootstrap_material(root, "original", cfg, probe_only=True)
            path.write_bytes(private + b"changed")
            with self.assertRaisesRegex(lease.Rejected, "bytes differ"):
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


if __name__ == "__main__":
    unittest.main()
