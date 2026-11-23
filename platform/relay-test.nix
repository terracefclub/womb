{
  pkgs,
  module,
}: let
  # Synthetic, with a random build-time key: a rebuild makes a different certificate
  # under the same path, so clients reference this path (curl --cacert, the full
  # rehearsal's /etc/terrace-relay-ca.pem) and nothing copies it (no security.pki).
  certificate = pkgs.runCommand "terrace-relay-proof-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
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
      # The client's system bundle also trusts the relay certificate (assembled at boot from
      # its store path, never a cached copy), so the guard's failing path below shows that
      # the guard checks only the anchor it is given and cannot pass through the system store.
      imports = [(import ./test-system-trust.nix {ca = "${certificate}/cert.pem";})];
      environment.systemPackages = [pkgs.curl pkgs.netcat-openbsd];
    };
  };
  testScript = ''
    ${import ./test-trust.nix {inherit pkgs;}}
    CURL = "curl --cacert ${certificate}/cert.pem "
    start_all()
    relay.wait_for_unit("headscale.service")
    relay.wait_for_unit("nginx.service")
    client.wait_for_unit("multi-user.target")
    trust_consistent(client, "${certificate}/cert.pem", "relay:443", ["relay"], "relay")
    # The guard's failing path inside a VM: a certificate for the same name with a different key, made now.
    client.wait_for_unit("terrace-test-system-trust.service")
    client.succeed("curl --fail --max-time 5 https://relay/health")  # the system bundle alone trusts the served chain
    client.succeed("${pkgs.openssl.bin}/bin/openssl req -x509 -newkey rsa:2048 -sha256 -days 1 -nodes -keyout /tmp/other-key.pem -out /tmp/other.pem -subj /CN=relay -addext subjectAltName=DNS:relay 2>/dev/null")
    try:
        trust_consistent(client, "/tmp/other.pem", "relay:443", ["relay"], "deliberately wrong anchor")
    except AssertionError as error:
        assert "test CA and trust anchor disagree" in str(error) and "self-signed certificate" in str(error), str(error)
        print("trust guard failing path: " + str(error))
    else:
        raise AssertionError("trust guard accepted an anchor that did not sign the served chain")
    client.wait_until_succeeds(CURL + "--fail --max-time 5 https://relay/health")
    assert client.succeed("curl -s -o /dev/null -w '%{http_code}' http://relay/health").strip() == "301"
    client.succeed("nc -z -w 3 relay 443")
    for port in [8080, 9090, 50443, 6443]:
        client.fail(f"nc -z -w 3 relay {port}")
    relay.succeed("headscale --config /etc/headscale/server-test.yaml configtest >/dev/null 2>&1")
    relay.succeed("headscale users create persistence-test >/dev/null 2>&1")
    relay.succeed("test -s /var/lib/headscale/noise_private.key && test -s /var/lib/headscale/derp_server_private.key")
    relay.succeed("headscale policy get | jq -e '.acls == []' >/dev/null")
    relay.succeed("systemctl restart headscale.service")
    client.wait_until_succeeds(CURL + "--fail --max-time 5 https://relay/health")
    relay.succeed("headscale users list -o json | jq -e 'any(.[]; .name == \"persistence-test\")' >/dev/null")
    relay.succeed("sqlite3 /var/lib/headscale/db.sqlite 'PRAGMA integrity_check' | grep -qx ok")
    relay.fail("systemctl is-active k3s.service")
  '';
}
