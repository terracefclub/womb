#!/usr/bin/env bash
# terrace-deploy: on the build machine, sign a production system closure with the deploy signing key, copy it to
# the host over the existing `operator` login, and print the command that activates it. It activates nothing.
# docs/host-updates.md ("Deploy path"); the key's custody is in docs/secrets.md ("Deploy signing key").
# Unit tests: tests/platform/test_terrace_deploy.py (stub nix).
#
#   platform/production/terrace-deploy.sh --key SECRET_KEY_FILE --address HOST --identity SSH_KEY [--known-hosts FILE] TOPLEVEL
#
# Needs `nix` and `ssh` on PATH, and a user the build machine's Nix daemon lets add signatures (root or a trusted
# user): `nix store sign` writes the signatures into the local store. The host accepts the copy only if it trusts
# the key's public half (terrace.production.deploy.trustedKeys); `operator` is not a trusted Nix user there.
set -euo pipefail
usage() { echo 'usage: terrace-deploy.sh --key SECRET_KEY_FILE --address HOST --identity SSH_KEY [--known-hosts FILE] TOPLEVEL' >&2; exit 2; }
fail() { echo "terrace-deploy: $*" >&2; exit 1; }
key="" address="" identity="" known_hosts="" toplevel=""
while (( $# )); do
  case $1 in
    --key) key=${2:-}; shift ;;
    --address) address=${2:-}; shift ;;
    --identity) identity=${2:-}; shift ;;
    --known-hosts) known_hosts=${2:-}; shift ;;
    -*) usage ;;
    *) [[ -z $toplevel ]] || usage; toplevel=$1 ;;
  esac
  shift
done
[[ -n $key && -n $address && -n $identity && -n $toplevel ]] || usage
[[ $address =~ ^[A-Za-z0-9.:-]+$ ]] || fail "address '$address' is not a host name or IP address"
[[ -f $key ]] || fail "signing key $key not found"
# A secret key is "name:" plus 64 bytes in base64 (88 characters); a public key (44 characters) cannot sign.
grep -Eq '^[A-Za-z0-9._-]+:[A-Za-z0-9+/]{86}==$' "$key" || fail "$key is not a Nix secret signing key (nix key generate-secret)"
[[ -f $identity && $identity != *" "* ]] || fail "SSH identity $identity not found"
[[ -z $known_hosts || -f $known_hosts ]] || fail "known hosts file $known_hosts not found"
[[ -x $toplevel/bin/switch-to-configuration ]] || fail "$toplevel is not a built NixOS system (no bin/switch-to-configuration)"
[[ -f $toplevel/etc/terrace/system.json ]] || fail "$toplevel has no Terrace system manifest; build it with infra/nixos/production-config.nix at this revision"
echo "Manifest: $(cat "$toplevel/etc/terrace/system.json")"
nix --extra-experimental-features nix-command store sign --key-file "$key" --recursive "$toplevel"
echo "Signed the closure of $toplevel."
ssh_options="-i $identity -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes"
[[ -z $known_hosts ]] || ssh_options+=" -o UserKnownHostsFile=$known_hosts"
NIX_SSHOPTS=$ssh_options nix --extra-experimental-features nix-command copy --to "ssh-ng://operator@$address" "$toplevel"
echo "Copied to operator@$address. Activate it on the host (after the checks of docs/host-updates.md):"
echo "  ssh -t operator@$address sudo terrace-production activate $toplevel"
