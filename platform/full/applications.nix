# Imported only by the explicit disposable all-services rehearsal variant.
{lib, ...}: {
  imports = [../windmill.nix ../vaultwarden.nix];
  terrace.windmill = {
    enable = true;
    membershipRuntime = true;
    hostname = "windmill.terrace.invalid";
    certificateFile = "/var/lib/terrace-gateway-test/cert.pem";
    certificateKeyFile = "/var/lib/terrace-gateway-test/key.pem";
  };
  terrace.vaultwarden = {
    enable = true;
    hostname = "vault.terrace.invalid";
    identityHostname = "identity.terrace.invalid";
    certificateFile = "/var/lib/terrace-gateway-test/cert.pem";
    certificateKeyFile = "/var/lib/terrace-gateway-test/key.pem";
    # The public synthetic CA is in the system bundle. The runtime input
    # directory remains root-private rather than making it readable to the app.
    identityCaFile = "/etc/ssl/certs/ca-certificates.crt";
  };
  # Never let a blank replacement create app data before the restore step.
  systemd.services = lib.genAttrs ["podman-terrace-windmill-server" "podman-terrace-windmill-worker" "vaultwarden"] (_: {
    wantedBy = lib.mkForce [];
  });
  # Test-only firewall piercing proves actual backend loopback binding.
  networking.firewall.allowedTCPPorts = [18001 18002 18003 18222 18224];
}
