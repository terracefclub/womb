{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.relay;
in {
  options.terrace.relay = {
    enable = lib.mkEnableOption "independent Terrace Headscale relay";
    hostname = lib.mkOption {
      type = lib.types.str;
      default = "headscale.terracefclub.org";
      description = "Public DNS name pointing directly to this host, without a CDN proxy.";
    };
    publicIPv4 = lib.mkOption {
      type = lib.types.str;
      description = "Verified public relay IPv4; explicit to avoid tailnet DNS bootstrap loops.";
    };
    policy = lib.mkOption {
      type = (pkgs.formats.json {}).type;
      default = {acls = [];};
      description = "Explicit Headscale access policy. Default denies all peer traffic.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = builtins.match "[a-zA-Z0-9.-]+" cfg.hostname != null;
        message = "Terrace relay hostname must be a DNS name, not a URL.";
      }
      {
        assertion = builtins.match "[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+" cfg.publicIPv4 != null;
        message = "Terrace relay requires its verified public IPv4 address.";
      }
      {
        assertion = !config.services.k3s.enable;
        message = "The Terrace recovery relay must remain independent of the application cluster.";
      }
    ];

    services.headscale = {
      enable = true;
      address = "127.0.0.1";
      port = 8080;
      settings = {
        server_url = "https://${cfg.hostname}";
        metrics_listen_addr = "127.0.0.1:9090";
        grpc_listen_addr = "127.0.0.1:50443";
        database = {
          type = "sqlite";
          sqlite.write_ahead_log = true;
        };
        derp = {
          urls = [];
          auto_update_enabled = false;
          server = {
            enabled = true;
            region_id = 901;
            region_code = "terrace";
            region_name = "Terrace relay";
            stun_listen_addr = "0.0.0.0:3478";
            ipv4 = cfg.publicIPv4;
          };
        };
        dns = {
          magic_dns = true;
          base_domain = "nodes.terracefclub.org";
          override_local_dns = false;
        };
        policy = {
          mode = "file";
          path = pkgs.writeText "terrace-tailnet-policy.json" (builtins.toJSON cfg.policy);
        };
        log.level = "warn";
      };
    };
    services.nginx = {
      enable = true;
      recommendedProxySettings = true;
      recommendedTlsSettings = true;
      virtualHosts.${cfg.hostname} = {
        enableACME = lib.mkDefault true;
        forceSSL = true;
        locations."/" = {
          proxyPass = "http://127.0.0.1:8080";
          proxyWebsockets = true;
          extraConfig = ''
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
            proxy_buffering off;
          '';
        };
      };
    };
    networking.firewall = {
      allowedTCPPorts = [80 443];
      allowedUDPPorts = [3478];
    };
    environment.systemPackages = [config.services.headscale.package pkgs.sqlite];
  };
}
