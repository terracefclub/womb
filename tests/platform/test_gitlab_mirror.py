import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mirror", ROOT / "tools/mirror-gitlab.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MirrorTests(unittest.TestCase):
    def test_only_published_refs_and_no_delete_or_force(self):
        sha = "a" * 40
        self.assertEqual(MODULE.refspecs("terracefclub/womb", {"trunk": sha}, []),
                         [f"{sha}:refs/heads/trunk"])
        heads = {name: sha for name in ("dev", "prod", "dev-post-vanilla")}
        specs = MODULE.refspecs("terracefclub/tfc-website", heads, ["refs/tags/release-1"])
        self.assertTrue(all(":refs/heads/github/" in x for x in specs[:3]))
        self.assertEqual(specs[-1], "refs/tags/release-1:refs/tags/github/release-1")

    def test_failure_facts_never_reflect_private_error(self):
        private = b"credential-marker URL-PRIVATE KEY-PRIVATE Permission denied (publickey)"
        text = str(MODULE.MirrorFailure("push", 128, private))
        import json
        facts = json.loads(text)
        self.assertEqual(facts["stage"], "push")
        self.assertEqual(facts["returncode"], 128)
        self.assertTrue(facts["sshPermissionDenied"])
        for marker in ("credential-marker", "URL-PRIVATE", "KEY-PRIVATE"):
            self.assertNotIn(marker, text)
        timed = json.loads(str(MODULE.MirrorFailure("fetch", stderr=b"x" * 65537, timeout=True)))
        self.assertTrue(timed["timeout"])
        self.assertTrue(timed["stderrTruncated"])

    def test_bad_inventory_and_refs_rejected(self):
        for heads, tags in (({}, []), ({"trunk": "a" * 40, "unreviewed": "a" * 40}, []),
                            ({"trunk": "not-a-sha"}, []), ({"trunk": "a" * 40}, ["refs/tags/a:refs/heads/trunk"])):
            with self.subTest(heads=heads), self.assertRaises(ValueError):
                MODULE.refspecs("terracefclub/womb", heads, tags)


if __name__ == "__main__":
    unittest.main()
