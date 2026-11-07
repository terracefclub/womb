import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import preview


def fixture():
    resources = [preview.STACK, *preview.TARGETS]
    return {
        "steps": [{"urn": urn, "op": "create", "newState": {"inputs": {}}} for urn in resources],
        "changeSummary": {"create": len(resources)},
    }


class BootstrapPreviewTest(unittest.TestCase):
    def test_complete_create_and_unchanged_plans(self):
        digest = fixture()
        self.assertEqual([r["operation"] for r in preview.validated_preview(digest)], ["create"] * 4)
        for step in digest["steps"]:
            step["op"] = "same"
        digest["changeSummary"] = {"same": 5}
        self.assertEqual([r["operation"] for r in preview.validated_preview(digest)], ["same"] * 4)

    def test_rejects_outside_targets_and_incomplete_plans(self):
        for defect in ("extra", "duplicate", "missing", "bad-summary", "hidden-delete", "corrupt", "bad-inputs", "error"):
            with self.subTest(defect=defect):
                digest = fixture()
                if defect == "extra":
                    digest["steps"][1]["urn"] += "-other"
                elif defect == "duplicate":
                    digest["steps"][2] = digest["steps"][1]
                elif defect == "missing":
                    digest["steps"].pop()
                    digest["changeSummary"]["create"] -= 1
                elif defect == "bad-summary":
                    digest["changeSummary"]["create"] += 1
                elif defect == "hidden-delete":
                    digest["changeSummary"]["delete"] = 1
                elif defect == "corrupt":
                    digest["maybeCorrupt"] = True
                elif defect == "bad-inputs":
                    digest["steps"][1]["newState"]["inputs"] = [["name", "invalid-input-object"]]
                else:
                    digest["diagnostics"] = [{"severity": "error", "message": "synthetic diagnostic"}]
                with self.assertRaises(ValueError):
                    preview.validated_preview(digest)

    def test_rejects_mutation_of_an_allowed_urn(self):
        for operation in ("update", "delete", "replace", "create-replacement", "delete-replaced", "import", "read"):
            with self.subTest(operation=operation):
                digest = fixture()
                digest["steps"][1]["op"] = operation
                digest["changeSummary"] = {"create": 4, operation: 1}
                with self.assertRaises(ValueError):
                    preview.validated_preview(digest)

    def test_wrapper_limits_commands_and_keeps_review_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "pulumi"
            expected = ["preview", "--stack", "bootstrap", "--non-interactive", "--json",
                        "--show-sames", "--suppress-permalink", "--suppress-outputs", "--show-secrets=false"]
            for target in preview.TARGETS:
                expected.extend(("--target", target))
            fake.write_text(f"#!{sys.executable}\n"
                            "import os, sys\n"
                            f"assert sys.argv[1:] == {expected!r}\n"
                            "assert 'PULUMI_ENABLE_STREAMING_JSON_PREVIEW' not in os.environ\n"
                            "print('synthetic-private-stderr', file=sys.stderr)\n"
                            "print(os.environ['FIXTURE_DIGEST'])\n"
                            "sys.exit(int(os.environ.get('FIXTURE_EXIT', '0')))\n")
            fake.chmod(0o700)
            digest = fixture()
            digest["config"] = {"private": "synthetic-private-config"}
            digest["diagnostics"] = [{"severity": "warning", "message": "synthetic-private-warning"}]
            digest["steps"][2]["newState"]["inputs"] = {"name": "terrace-operator", "publicKey": "synthetic-public-key"}
            provider_urn = preview.URN_PREFIX + "pulumi:providers:hcloud::default_1_42_0"
            digest["steps"].append({"urn": provider_urn, "op": "create",
                                    "newState": {"inputs": {"token": "synthetic-private-provider"}}})
            digest["changeSummary"]["create"] += 1
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       HCLOUD_TOKEN="synthetic-token", PULUMI_ENABLE_STREAMING_JSON_PREVIEW="true",
                       FIXTURE_DIGEST=json.dumps(digest))
            destination = root / "review.json"
            args = [preview.PROJECT / "preview.sh", "bootstrap", "--review-file", destination]
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            report = destination.read_text()
            self.assertEqual(len(json.loads(report)["resources"]), 4)
            self.assertIn("publicKeySha256", report)
            self.assertNotIn("synthetic-private", report + result.stdout + result.stderr)
            self.assertNotIn("synthetic-public-key", report)
            repeat = subprocess.run(args, env=env, capture_output=True, timeout=10)
            self.assertEqual(repeat.returncode, 1)
            self.assertEqual(destination.read_text(), report)
            failed = subprocess.run([preview.PROJECT / "preview.sh", "bootstrap"],
                                    env=dict(env, FIXTURE_EXIT="1"), capture_output=True, text=True, timeout=10)
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(failed.stdout, "")
            self.assertNotIn("synthetic-private", failed.stderr)


if __name__ == "__main__":
    unittest.main()
