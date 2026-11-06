#!/usr/bin/env python3
"""Run one existing CI job unchanged in its pinned container (no YAML dependency)."""
import argparse
import pathlib
import re
import subprocess
import tempfile

JOBS = frozenset(("membership-contract", "check-supervision", "oci-metadata",
                  "documenso-contract", "vaultwarden-contract"))
DISABLED = "workflow:\n  rules:\n    - when: never\n\n"


def parse_jobs(text):
    if text.startswith(DISABLED):
        text = text[len(DISABLED):]
    jobs = {}
    current = None
    state = None
    for line in text.splitlines():
        if not line:
            continue
        if re.fullmatch(r"[a-z][a-z-]+:", line):
            name = line[:-1]
            if name not in JOBS or name in jobs:
                raise ValueError("Unknown or duplicate CI job")
            current = jobs[name] = {"image": None, "commands": []}
            state = "image"
        elif current is not None and state == "image" and line.startswith("  image: "):
            image = line[len("  image: "):]
            if not re.fullmatch(r"[a-z0-9/_.:-]+@sha256:[0-9a-f]{64}", image):
                raise ValueError("CI image must have an immutable digest")
            current["image"] = image
            state = "script"
        elif current is not None and state == "script" and line == "  script:":
            state = "commands"
        elif current is not None and state == "commands" and line.startswith("    - "):
            command = line[len("    - "):]
            if not command or any(ord(c) < 32 for c in command):
                raise ValueError("Invalid CI command")
            current["commands"].append(command)
        else:
            raise ValueError("Unsupported CI syntax; extend the adapter explicitly")
    if jobs.keys() != JOBS or any(not j["image"] or not j["commands"] for j in jobs.values()):
        raise ValueError("Incomplete CI job inventory")
    return jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=sorted(JOBS))
    parser.add_argument("--validate", action="store_true", help="Validate configuration without starting a container")
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parent.parent
    job = parse_jobs((root / ".gitlab-ci.yml").read_text())[args.job]
    if args.validate:
        print("CI configuration valid")
        return
    with tempfile.TemporaryDirectory(prefix="terrace-ci-") as temporary:
        cidfile = pathlib.Path(temporary) / "container-id"
        try:
            subprocess.run(["docker", "run", "--init", "--cidfile", str(cidfile),
                            "--volume", f"{root}:/workspace", "--workdir", "/workspace",
                            job["image"], "/bin/sh", "-eu", "-c", "\n".join(job["commands"])],
                           check=True, timeout=540)
        finally:
            if cidfile.exists():
                cid = cidfile.read_text().strip()
                if not re.fullmatch(r"[0-9a-f]{64}", cid):
                    raise ValueError("Invalid task-owned container identity")
                subprocess.run(["docker", "rm", "--force", cid], check=True, timeout=20,
                               stdout=subprocess.DEVNULL)



if __name__ == "__main__":
    main()
