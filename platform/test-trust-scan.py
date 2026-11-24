#!/usr/bin/env python3
"""Show that every output built from a VM proof's build-time random CA still references it.

A synthetic CA or certificate made at build time with a random key can be rebuilt with a
new key under the same store path (platform/test-trust.nix). Every output built from it
must therefore reference it at run time; an output built from it that does not reference
it may hold a copy that a partial garbage collection can split from the key.

    platform/test-trust-scan.py [--build] TOP_DRV CA_NAME

TOP_DRV is the proof's derivation (for a flake check, `nix eval --raw
path:./platform#checks.x86_64-linux.<check>.drvPath`); CA_NAME is the name of the CA
derivation in its closure (for example terrace-identity-proof-certificate). The scan
finds every derivation in TOP_DRV's closure that is built, directly or not, from the CA
derivation and checks each of its outputs: the CA output is in its closure
(`nix-store -qR`), or it dropped its reference and may be a copy.
--build first realises the CA and every such derivation except TOP_DRV itself (the VM
run), so nothing is left unchecked; run it under the shared build lock.

Exit 0 only when the CA output is valid, every dependent output except TOP_DRV's own is
built and references it, and there is at least one. TOP_DRV's own output (the test
result) is reported, not required: it is a log, built after the proof ran.

It checks store references only. It cannot show that an output which references the CA
holds no copy of its bytes as well, nor catch bytes that entered through a source path or
builtins.readFile; those rest on reading the sources.
"""
import argparse
import re
import subprocess
import sys

EDGE = re.compile(r'^"([^"]+)" -> "([^"]+)"')


def nix_store(*args):
    return subprocess.run(['nix-store', *args], capture_output=True, text=True, check=True).stdout.split()


def dependents(graph_lines, ca, top):
    """Derivation names built from `ca` on the way to `top`, from `nix-store -q --graph` output. The graph's
    edge direction is not relied on: the direction in which `top` is reachable from `ca` is the dependents'."""
    edges = ({}, {})
    for line in graph_lines:
        match = EDGE.match(line.strip())
        if match:
            edges[0].setdefault(match.group(1), []).append(match.group(2))
            edges[1].setdefault(match.group(2), []).append(match.group(1))
    for following in edges:
        seen, todo = set(), [ca]
        while todo:
            for node in following.get(todo.pop(), []):
                if node not in seen:
                    seen.add(node)
                    todo.append(node)
        if top in seen:
            return sorted(node for node in seen if node.endswith('.drv'))
    raise SystemExit(f'{ca} is not built into {top}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--build', action='store_true', help='realise the CA and its dependents (not the VM run) first')
    parser.add_argument('top')
    parser.add_argument('ca_name')
    args = parser.parse_args()
    top = args.top
    if not top.startswith('/nix/store/') or not top.endswith('.drv'):
        raise SystemExit('TOP_DRV must be a /nix/store/...drv path')
    closure = nix_store('-qR', top)
    matches = [path for path in closure if path.endswith('-' + args.ca_name + '.drv')]
    if len(matches) != 1:
        raise SystemExit(f'expected one {args.ca_name}.drv in the closure of {top}, found {len(matches)}')
    ca_drv = matches[0]
    graph = subprocess.run(['nix-store', '-q', '--graph', top], capture_output=True, text=True, check=True).stdout.splitlines()
    name = lambda path: path.rsplit('/', 1)[-1]
    drvs = ['/nix/store/' + drv for drv in dependents(graph, name(ca_drv), name(top))]
    if args.build:
        subprocess.run(['nix-store', '--realise', ca_drv, *[drv for drv in drvs if drv != top]],
                       stdout=subprocess.DEVNULL, check=True)
    (ca,) = nix_store('-q', '--outputs', ca_drv)
    if subprocess.run(['nix-store', '--check-validity', ca], capture_output=True).returncode != 0:
        raise SystemExit(f'the CA output {ca} is not valid: build it (--build) or run the proof first')
    print(f'CA {name(ca)}; {len(drvs)} derivations built from it')
    counts = {'references': 0, 'copy': 0, 'unbuilt': 0}
    for drv in drvs:
        for out in nix_store('-q', '--outputs', drv):
            try:
                refs = nix_store('-qR', out)
            except subprocess.CalledProcessError:
                if drv == top:
                    print('test result not built  ', name(out))
                else:
                    counts['unbuilt'] += 1
                    print('NOT BUILT (unchecked)  ', name(out))
                continue
            if ca in refs:
                counts['references'] += 1
                print('references the CA      ', name(out))
            elif drv == top:
                print('test result            ', name(out))
            else:
                counts['copy'] += 1
                print('NO REFERENCE (a copy?) ', name(out))
    print(f"outputs referencing the CA: {counts['references']}; without a reference: {counts['copy']}; unchecked: {counts['unbuilt']}")
    sys.exit(0 if counts['references'] and not counts['copy'] and not counts['unbuilt'] else 1)


if __name__ == '__main__':
    main()
