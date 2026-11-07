{tlsNames}: {
  lib,
  pkgs,
  ...
}: let
  tls = "/var/lib/terrace-rehearsal-tls";
  ca = "/var/lib/terrace-rehearsal-trust/ca.pem";
in {
  imports = [../backup.nix];
  networking.firewall = {
    enable = true;
    allowedTCPPorts = lib.mkForce [22];
    allowedUDPPorts = lib.mkForce [];
  };
  services.openssh.settings = {
    GatewayPorts = "no";
    PermitOpen = "127.0.0.1:443";
    PermitListen = "127.0.0.1:22422";
  };
  terrace.backup = {
    enable = true;
    check.readDataSubset = "100%";
    targets.primary = {
      enable = true;
      repository = "rest:http://127.0.0.1:22422/repository";
      passwordFile = "/var/lib/rehearsal-escrow/repository-password";
    };
  };
  systemd.timers = lib.genAttrs ["terrace-backup" "terrace-backup-check" "terrace-backup-freshness"] (_: {wantedBy = lib.mkForce [];});
  systemd.services.cloud-rehearsal-tls = {
    requiredBy = ["nginx.service"];
    before = ["nginx.service"];
    path = [pkgs.coreutils pkgs.diffutils pkgs.gnugrep pkgs.openssl];
    serviceConfig = {
      Type = "oneshot";
      UMask = "0077";
      TimeoutStartSec = 15;
      StandardOutput = "null";
      StandardError = "null";
    };
    script = ''
      set -euo pipefail
      test -d ${tls} && test ! -L ${tls} && test "$(stat -c %u:%a ${tls})" = 0:700
      test -d ${builtins.dirOf ca} && test ! -L ${builtins.dirOf ca} && test "$(stat -c %u:%a ${builtins.dirOf ca})" = 0:755
      for path in ${tls}/cert.pem ${tls}/key.pem ${ca}; do
        test -f "$path" && test ! -L "$path" && test "$(stat -c %u "$path")" = 0
      done
      test "$(stat -c %a ${tls}/key.pem)" = 400
      test "$(stat -c %a ${ca})" = 644
      openssl x509 -in ${tls}/cert.pem -noout -ext basicConstraints | grep 'CA:FALSE' >/dev/null
      ${lib.concatMapStringsSep "\n" (host: "openssl verify -purpose sslserver -verify_hostname " + lib.escapeShellArg host + " -CAfile ${ca} ${tls}/cert.pem") tlsNames}
      openssl pkey -in ${tls}/key.pem -passin pass: -pubout |
        cmp - <(openssl x509 -in ${tls}/cert.pem -noout -pubkey)
    '';
  };
}
