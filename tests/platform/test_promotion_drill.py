"""The restore promotion drill runs the document's own commands.

platform/production/promotion-test.nix (the VM drill) takes every command it runs
from platform/production/promotion-commands.json, and its evaluation refuses a
command that is not verbatim in docs/restore-promotion.md. These tests repeat
that check without Nix and add the reverse: every shell block of the
procedure (section 2) is run by the drill or excluded with a reason, so a
command added to or changed in the document fails CI until the drill follows.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs/restore-promotion.md").read_text()
COMMANDS = json.loads((ROOT / "platform/production/promotion-commands.json").read_text())
DRILL = (ROOT / "platform/production/promotion-test.nix").read_text()


def flat(text):
    # Inline commands may wrap across lines (and list indentation), as the drill's evaluation allows.
    return text.replace("\n  ", " ").replace("\n", " ")


def fenced(kind, lines):
    return f"```{kind}\n" + "\n".join(lines) + "\n```"


def procedure_blocks():
    section = DOC[DOC.index("## 2. The procedure"):DOC.index("## 3. Traceability")]
    return [block.rstrip("\n").split("\n") for block in re.findall(r"```sh\n(.*?)```", section, re.S)]


class PromotionDrillCommands(unittest.TestCase):
    def test_blocks_are_verbatim_in_the_document(self):
        for name, lines in COMMANDS["blocks"].items():
            with self.subTest(name=name):
                self.assertIn(fenced("sh", lines), DOC)

    def test_outputs_are_verbatim_in_the_document(self):
        for name, lines in COMMANDS["outputs"].items():
            with self.subTest(name=name):
                self.assertIn(fenced("text", lines), DOC)

    def test_inline_commands_are_in_the_document(self):
        for name, text in COMMANDS["inline"].items():
            with self.subTest(name=name):
                self.assertIn(text, flat(DOC))

    def test_every_procedure_block_is_run_or_excluded(self):
        run = [lines for lines in COMMANDS["blocks"].values()]
        excluded = [entry["firstLine"] for entry in COMMANDS["excluded"].values()]
        for lines in procedure_blocks():
            with self.subTest(block=lines[0]):
                self.assertTrue(lines in run or lines[0] in excluded, "a shell block of section 2 that the drill neither runs nor excludes")
        for entry in COMMANDS["excluded"].values():
            self.assertTrue(entry["reason"].strip())

    def test_the_drill_uses_every_entry(self):
        for group in ["blocks", "outputs", "inline"]:
            for name in COMMANDS[group]:
                with self.subTest(name=name):
                    self.assertIn(f'"{name}"', DRILL)

    def test_a_changed_command_is_caught(self):
        # Control: the same check rejects a block that differs by one character.
        lines = list(COMMANDS["blocks"]["step15-adopt"])
        lines[0] = lines[0].replace("--restored", "--restore")
        self.assertNotIn(fenced("sh", lines), DOC)


if __name__ == "__main__":
    unittest.main()
