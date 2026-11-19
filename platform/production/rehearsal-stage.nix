# Rehearsal-only stand-in for the Rescue installer, which stages the
# operator-generated host key (and only that: the ciphertext arrives later with
# `terrace-secrets push`). The production profile refuses this unit.
{
  config,
  pkgs,
  ...
}: let
  stage = "/tmp/shared/stage/${config.networking.hostName}";
in {
  systemd.services.terrace-rehearsal-stage = {
    description = "Rehearsal installer stand-in: stage the operator-generated host key";
    wantedBy = ["sysinit.target"];
    before = ["sops-install-secrets.service" "sshd.service" "sysinit.target"];
    after = ["local-fs.target"];
    unitConfig = {
      DefaultDependencies = false;
      RequiresMountsFor = ["/tmp/shared" "/var/lib"];
    };
    path = [pkgs.coreutils];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      UMask = "0077";
    };
    script = ''
      set -euo pipefail
      if test -f ${stage}/ssh_host_ed25519_key && test ! -e /etc/ssh/ssh_host_ed25519_key; then
        install -d -m 0755 /etc/ssh
        install -m 0600 ${stage}/ssh_host_ed25519_key /etc/ssh/ssh_host_ed25519_key
        install -m 0644 ${stage}/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ed25519_key.pub
      fi
    '';
  };
}
