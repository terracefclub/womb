"""Proof sources must not depend on files .gitignore ignores (Python caches, node_modules, result links).

1. The Windmill evidence source (platform/windmill-evidence-source.nix) evaluates to the same store path with and
   without a planted __pycache__, in a scratch copy, and to the store path of a plain copy of a clean tree.
2. No Nix file under platform/ or infra/ copies a whole directory by path literal (`./dir`, `../dir`) other than
   through a filter: a plain directory copy takes ignored files with it.
Run: python3 -B tests/platform/test_clean_sources.py -v  (part 1 needs nix-instantiate and is skipped without it)
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
NIX = shutil.which("nix-instantiate")
# Directory path literals that are filtered (by the file named) before anything is copied.
FILTERED = {("platform/windmill-evidence-source.nix", "./windmill")}


def store_path(expression, directory):
    result = subprocess.run([NIX, "--eval", "--read-write-mode", "--json", "--expr", expression], cwd=directory, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip()


class CleanSources(unittest.TestCase):
    @unittest.skipUnless(NIX, "nix-instantiate not on PATH")
    def test_windmill_evidence_ignores_planted_caches(self):
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            shutil.copy(ROOT / "platform/windmill-evidence-source.nix", work)
            shutil.copytree(ROOT / "platform/windmill", work / "windmill", ignore=shutil.ignore_patterns("__pycache__", "node_modules", "result", "result-*"))
            filtered = "toString (import ./windmill-evidence-source.nix)"
            clean = store_path(filtered, work)
            self.assertEqual(clean, store_path('toString (builtins.path { path = ./windmill; name = "windmill"; })', work))
            (work / "windmill/__pycache__").mkdir()
            (work / "windmill/__pycache__/capture.cpython-313.pyc").write_bytes(b"planted")
            (work / "windmill/node_modules").mkdir()
            (work / "windmill/node_modules/planted.js").write_text("planted")
            (work / "windmill/result-planted").write_text("planted")
            self.assertEqual(store_path(filtered, work), clean)
            # The check has teeth: an unfiltered copy does change.
            self.assertNotEqual(store_path('toString (builtins.path { path = ./windmill; name = "windmill"; })', work), clean)

    def test_no_unfiltered_directory_copies(self):
        literal = re.compile(r"(?<![\w/.-])(\.\.?(?:/[\w.-]+)+)(?![\w/.-])")
        found = []
        for path in sorted([*ROOT.glob("platform/**/*.nix"), *ROOT.glob("infra/**/*.nix")]):
            relative = path.relative_to(ROOT).as_posix()
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for match in literal.finditer(line):
                    target = (path.parent / match.group(1)).resolve()
                    if target.is_dir() and (relative, match.group(1)) not in FILTERED and "getFlake" not in line:
                        found.append(f"{relative}:{number}: {line.strip()}")
        self.assertEqual(found, [], "a directory copied by path literal takes ignored files with it; filter it (see platform/windmill-evidence-source.nix)")


if __name__ == "__main__":
    unittest.main()
