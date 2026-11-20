# Rehearsal-only harness for the ACME VM proof (tls-test.nix): the installer
# stand-in, trust for the local test CA (pebble) in the ACME client and in the
# expiry check, and client tools. The production profile refuses all of it.
{
  config,
  pkgs,
  ...
}: let
  certificate = config.terrace.production.tls.certificateName;
  state = "/var/lib/terrace-tls-rehearsal";
in {
  imports = [./rehearsal-stage.nix];
  terrace.production.rehearsalHarness = true;
  # Written by the test from the local CA's management API; pebble makes new roots per start.
  terrace.production.tls.trustAnchorFile = "${state}/roots.pem";
  systemd.services."acme-order-renew-${certificate}".environment.LEGO_CA_CERTIFICATES = "${state}/acme-server-ca.pem";
  environment.systemPackages = [pkgs.curl pkgs.openssl pkgs.jq pkgs.iproute2 pkgs.netcat-openbsd];
}
