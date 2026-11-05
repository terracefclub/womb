{pkgs}: let
  ownershipTag = "name=opt/terrace-vaultwarden-test,string=${builtins.hashFile "sha256" ./vaultwarden/scenario.py}";
  certificate = pkgs.runCommand "vaultwarden-test-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir $out
    openssl req -x509 -nodes -newkey rsa:2048 -keyout $out/key.pem -out $out/cert.pem -days 3650 \
      -subj /CN=vault.terrace.invalid -addext subjectAltName=DNS:vault.terrace.invalid >/dev/null 2>&1
  '';
  capture = pkgs.runCommand "vaultwarden-test-capture" {} ''
    mkdir $out
    cp ${./windmill/capture.py} $out/capture.py
    cp ${./windmill/diagnostics.py} $out/diagnostics.py
    cp ${./vaultwarden/capture.py} $out/vaultwarden-capture.py
  '';
in {
  name = "terrace-vaultwarden-runtime";
  nodes = {
    server = {lib, ...}: {
      imports = [./vaultwarden.nix];
      virtualisation.memorySize = 2048;
      virtualisation.qemu.options = ["-fw_cfg" ownershipTag];
      virtualisation.diskSize = 4096;
      services.postgresql.package = pkgs.postgresql_18;
      services.postgresql.settings = {
        max_connections = 32;
        shared_buffers = "64MB";
      };
      terrace.vaultwarden = {
        enable = true;
        hostname = "vault.terrace.invalid";
        identityHostname = "identity.terrace.invalid";
        certificateFile = "/var/lib/vaultwarden-test/cert.pem";
        certificateKeyFile = "/var/lib/vaultwarden-test/key.pem";
      };
      # Test certificate comes from a synthetic derivation, copied to the same
      # external runtime paths required of operators by the production module.
      systemd.services.vaultwarden.wantedBy = lib.mkForce [];
      systemd.services.nginx.wantedBy = lib.mkForce [];
      environment.systemPackages = [pkgs.bun pkgs.curl pkgs.jq pkgs.openssl pkgs.util-linux pkgs.python3];
      environment.etc."vaultwarden-test-fixture.ts".source = ./vaultwarden/fixture.ts;
      environment.etc."vaultwarden-test-certificate".source = certificate;
      environment.etc."vaultwarden-test-capture".source = capture;
    };
    client = {nodes, ...}: {
      virtualisation.memorySize = 384;
      virtualisation.qemu.options = ["-fw_cfg" ownershipTag];
      environment.systemPackages = [pkgs.curl pkgs.netcat-openbsd];
      security.pki.certificateFiles = ["${certificate}/cert.pem"];
      networking.hosts.${nodes.server.networking.primaryIPAddress} = ["vault.terrace.invalid"];
    };
  };
  testScript = builtins.readFile ./vaultwarden/scenario.py;
}
