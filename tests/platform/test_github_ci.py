import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("github_ci", ROOT / "tools/github-ci.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AdapterTests(unittest.TestCase):
    def test_existing_and_disabled_config_preserve_commands(self):
        text = (ROOT / ".gitlab-ci.yml").read_text()
        text = text.removeprefix(MODULE.DISABLED)
        original = MODULE.parse_jobs(text)
        self.assertEqual(original, MODULE.parse_jobs(MODULE.DISABLED + text))
        self.assertEqual(set(original), MODULE.JOBS)
        self.assertTrue(all(job["commands"] for job in original.values()))

    def test_unsupported_or_unpinned_config_rejected(self):
        text = (ROOT / ".gitlab-ci.yml").read_text()
        for changed in (text + "include: remote\n", text.replace("@sha256:", "@untrusted:"),
                        text.replace("  script:", "  before_script:", 1), text + text):
            with self.subTest(changed=changed[-40:]), self.assertRaises(ValueError):
                MODULE.parse_jobs(changed)


if __name__ == "__main__":
    unittest.main()
