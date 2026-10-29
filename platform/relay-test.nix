{
  pkgs,
  module,
}: let
  certificate = pkgs.runCommand "terrace-relay-test-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir -p $out
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout $out/key.pem -out $out/cert.pem \
      -subj '/CN=relay' -addext 'subjectAltName=DNS:relay' 2>/dev/null
  '';
in {
  name = "terrace-access-relay";
  nodes = {
    relay = {config, ...}: {
      imports = [module];
      terrace.relay = {
        enable = true;
        hostname = "relay";
        publicIPv4 = "192.168.1.1";
      };
      services.nginx.virtualHosts.relay = {
        enableACME = false;
        sslCertificate = "${certificate}/cert.pem";
        sslCertificateKey = "${certificate}/key.pem";
      };
      environment.systemPackages = [pkgs.curl pkgs.jq];
      environment.etc."headscale/server-test.yaml".source = config.services.headscale.configFile;
    };
    client = {...}: {
      environment.systemPackages = [pkgs.curl pkgs.netcat-openbsd];
      security.pki.certificateFiles = ["${certificate}/cert.pem"];
    };
  };
  testScript = ''
    start_all()
    relay.wait_for_unit("headscale.service")
    relay.wait_for_unit("nginx.service")
    client.wait_until_succeeds("curl --fail --max-time 5 https://relay/health")
    assert client.succeed("curl -s -o /dev/null -w '%{http_code}' http://relay/health").strip() == "301"
    client.succeed("nc -z -w 3 relay 443")
    for port in [8080, 9090, 50443, 6443]:
        client.fail(f"nc -z -w 3 relay {port}")
    relay.succeed("headscale --config /etc/headscale/server-test.yaml configtest >/dev/null 2>&1")
    relay.succeed("headscale users create persistence-test >/dev/null 2>&1")
    relay.succeed("test -s /var/lib/headscale/noise_private.key && test -s /var/lib/headscale/derp_server_private.key")
    relay.succeed("headscale policy get | jq -e '.acls == []' >/dev/null")
    relay.succeed("systemctl restart headscale.service")
    client.wait_until_succeeds("curl --fail --max-time 5 https://relay/health")
    relay.succeed("headscale users list -o json | jq -e 'any(.[]; .name == \"persistence-test\")' >/dev/null")
    relay.succeed("sqlite3 /var/lib/headscale/db.sqlite 'PRAGMA integrity_check' | grep -qx ok")
    relay.fail("systemctl is-active k3s.service")
  '';
}
