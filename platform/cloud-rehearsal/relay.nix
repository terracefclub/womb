{privateIPv4}: {
  lib,
  pkgs,
  ...
}: let
  hostname = "relay.terrace.invalid";
  octets =
    if builtins.isString privateIPv4 && builtins.match "(0|[1-9][0-9]{0,2})([.](0|[1-9][0-9]{0,2})){3}" privateIPv4 != null
    then map lib.toInt (lib.splitString "." privateIPv4)
    else [];
  isPrivate =
    builtins.length octets
    == 4
    && lib.all (n: n <= 255) octets
    && (
      builtins.elemAt octets 0
      == 10
      || (builtins.elemAt octets 0 == 172 && builtins.elemAt octets 1 >= 16 && builtins.elemAt octets 1 <= 31)
      || lib.take 2 octets == [192 168]
    );
in {
  imports = [
    (import ./common.nix {tlsNames = [hostname];})
    ../hetzner.nix
    ../relay.nix
  ];
  networking.hostName = "relay";
  networking.hosts."127.0.0.1" = [hostname];
  assertions = [
    {
      assertion = isPrivate;
      message = "The synthetic relay requires a private RFC1918 IPv4 address.";
    }
  ];
  terrace.hetzner = {
    enable = true;
    enableK3s = false;
  };
  terrace.relay = {
    enable = true;
    inherit hostname;
    # A synthetic local advertisement. No external UDP path is implied.
    publicIPv4 = privateIPv4;
  };
  services.headscale.settings.derp.server.stun_listen_addr = lib.mkForce "${privateIPv4}:3478";
  services.nginx.defaultListenAddresses = ["127.0.0.1"];
  services.nginx.virtualHosts.${hostname} = {
    enableACME = lib.mkForce false;
    onlySSL = true;
    forceSSL = lib.mkForce false;
    sslCertificate = "/var/lib/terrace-rehearsal-tls/cert.pem";
    sslCertificateKey = "/var/lib/terrace-rehearsal-tls/key.pem";
  };
  systemd.services =
    lib.genAttrs ["headscale" "nginx"] (_: {wantedBy = lib.mkForce [];})
    // {
      cloud-rehearsal-relay-address = {
        requiredBy = ["headscale.service"];
        before = ["headscale.service"];
        unitConfig.StopWhenUnneeded = true;
        path = [pkgs.coreutils pkgs.diffutils pkgs.iproute2];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          RuntimeDirectory = "cloud-rehearsal-relay-address";
          RuntimeDirectoryMode = "0700";
          TimeoutStartSec = 10;
          TimeoutStopSec = 10;
          CapabilityBoundingSet = ["CAP_NET_ADMIN"];
        };
        script = ''
          set -euo pipefail
          ip link add dev terrace0 type dummy
          trap 'ip link del dev terrace0' ERR
          cat /sys/class/net/terrace0/ifindex >"$RUNTIME_DIRECTORY/ifindex"
          ip address add ${lib.escapeShellArg (privateIPv4 + "/32")} dev terrace0
          ip link set dev terrace0 up
        '';
        preStop = ''
          set -euo pipefail
          if test -e /sys/class/net/terrace0; then
            cmp /sys/class/net/terrace0/ifindex "$RUNTIME_DIRECTORY/ifindex"
            ip link del dev terrace0
          fi
        '';
      };
    };
}
