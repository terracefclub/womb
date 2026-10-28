#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
if [ "${1:-}" != "bootstrap" ] || [ -z "${HCLOUD_TOKEN:-}" ]; then
  echo "select the bootstrap stack and supply the dedicated Terrace HCLOUD_TOKEN at runtime" >&2
  exit 2
fi

pulumi preview --stack bootstrap --suppress-permalink --suppress-outputs \
  --target 'urn:pulumi:bootstrap::terrace-cloud::hcloud:index/firewall:Firewall::bootstrap-firewall' \
  --target 'urn:pulumi:bootstrap::terrace-cloud::hcloud:index/sshKey:SshKey::operator' \
  --target 'urn:pulumi:bootstrap::terrace-cloud::hcloud:index/server:Server::bootstrap-server' \
  2>/dev/null
