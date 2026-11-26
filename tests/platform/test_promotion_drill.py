"""The restore promotion drill runs the documents' own commands, in their order.

platform/production/promotion-test.nix (the VM drill) takes every documented
command it runs from platform/production/promotion-commands.json. Each entry
names its document and step. These tests check, without Nix:

- each entry is verbatim in its step: a block or output exactly once, as a fenced
  block of that step; an inline command within that step's text (line wrapping
  and list indentation allowed);
- the entries are listed in the order their documents give them;
- every fenced block of the procedure (section 2 of docs/restore-promotion.md),
  whatever its info string, is an entry or excluded with a reason;
- the drill's code uses every entry through its accessors (comments do not count).

The drill's evaluation repeats the verbatim check (against section 2 or the named
section), and the drill itself checks at run time that each role first uses the
procedure's entries in this order.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMOTION = "docs/restore-promotion.md"
COMMANDS = json.loads((ROOT / "platform/production/promotion-commands.json").read_text())
ENTRIES = COMMANDS["entries"]
DRILL = (ROOT / "platform/production/promotion-test.nix").read_text()


def read(document):
    return (ROOT / document).read_text()


def flat(text):
    return text.replace("\n  ", " ").replace("\n", " ")


def section2():
    text = read(PROMOTION)
    return text[text.index("## 2. The procedure"):text.index("## 3. Traceability")]


def steps():
    regions = {}
    for part in re.split(r"(?=\*\*Step \d+\. )", section2()):
        found = re.match(r"\*\*Step (\d+)\. ", part)
        if found:
            assert found.group(1) not in regions, "a step heading appears twice"
            regions[found.group(1)] = part
    return regions


def region(entry):
    if entry["document"] == PROMOTION:
        return steps()[entry["step"]]
    text = read(entry["document"])
    start = text.index("### " + entry["step"] + ". ")
    return text[start:text.index("\n### ", start + 1)]


def needle(entry):
    if entry["kind"] == "inline":
        return entry["text"]
    return "```" + ("sh" if entry["kind"] == "block" else "text") + "\n" + "\n".join(entry["lines"]) + "\n```"


def position(entry):
    if entry["document"] == PROMOTION:
        section = flat(section2())
        part = flat(region(entry))
        return (0, section.index(part) + part.index(flat(needle(entry))))
    text = read(entry["document"])
    part = region(entry)
    return (1, len(flat(text[:text.index(part)])) + flat(part).index(flat(needle(entry))))


class PromotionDrillCommands(unittest.TestCase):
    def test_entries_are_verbatim_in_their_step(self):
        for entry in ENTRIES:
            with self.subTest(name=entry["name"]):
                part = region(entry)
                if entry["kind"] == "inline":
                    self.assertIn(entry["text"], flat(part))
                else:
                    self.assertEqual(part.count(needle(entry)), 1)
                    self.assertEqual(section2().count(needle(entry)) if entry["document"] == PROMOTION else 1, 1)

    def test_entries_follow_the_documents_order(self):
        names = [entry["name"] for entry in ENTRIES]
        self.assertEqual(names, [entry["name"] for entry in sorted(ENTRIES, key=position)])
        self.assertEqual(len(names), len(set(names)))

    def test_every_fenced_block_of_the_procedure_is_covered(self):
        covered = [entry["lines"] for entry in ENTRIES if entry["document"] == PROMOTION and entry["kind"] != "inline"]
        excluded = [entry["firstLine"] for entry in COMMANDS["excluded"]]
        for entry in COMMANDS["excluded"]:
            self.assertTrue(entry["reason"].strip())
        blocks = re.findall(r"```([^\n]*)\n(.*?)```", section2(), re.S)
        self.assertGreater(len(blocks), 20)
        for info, body in blocks:
            lines = body.rstrip("\n").split("\n")
            with self.subTest(block=lines[0], info=info):
                self.assertTrue(lines in covered or lines[0] in excluded, "a fenced block of section 2 that the drill neither runs nor excludes")

    def test_the_drill_uses_every_entry_in_code(self):
        script = DRILL[DRILL.index("testScript = ''"):]
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        used = {name for pair in re.findall(r'\b(?:lines|text|block)\(\s*"([a-z0-9-]+)"|\bstatus\(\w+, "([a-z0-9-]+)"', code) for name in pair if name}
        for entry in ENTRIES:
            with self.subTest(name=entry["name"]):
                self.assertIn(entry["name"], used)

    def test_a_changed_or_moved_command_is_caught(self):
        # Controls: the same checks reject a block that differs by one character, and a wrong order.
        entry = dict(next(e for e in ENTRIES if e["name"] == "step15-adopt"))
        entry["lines"] = [entry["lines"][0].replace("--restored", "--restore")]
        self.assertNotIn(needle(entry), section2())
        swapped = list(ENTRIES)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        self.assertNotEqual([e["name"] for e in swapped], [e["name"] for e in sorted(swapped, key=position)])


if __name__ == "__main__":
    unittest.main()
