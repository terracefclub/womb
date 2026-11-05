{
  config,
  lib,
  pkgs,
  nodes,
  ...
}: {
  # Disposable rehearsal clients only; no production profile imports this file.
  services.tailscale = {
    enable = true;
    openFirewall = true;
    port = 41641;
    disableTaildrop = true;
    disableUpstreamLogging = true;
  };
  security.pki.certificateFiles = lib.mkAfter [nodes.relay.services.nginx.virtualHosts.relay.sslCertificate];
  systemd.services.tailscaled = {
    wantedBy = lib.mkForce [];
    serviceConfig = {
      StateDirectory = "tailscale";
      StateDirectoryMode = "0700";
      UMask = "0077";
      MemoryMax = "192M";
      TasksMax = 96;
      TimeoutStopSec = 30;
      StandardOutput = "null";
      StandardError = "null";
    };
  };
  systemd.tmpfiles.rules = ["d /var/lib/rehearsal-tailnet 0700 root root -"];

  # Frigate owns the automation guest's existing nginx. Keep this bounded
  # private sensor proxy in its own process, with no additional shared vhost.
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = lib.mkIf (config.networking.hostName == "automation") [18444];
  systemd.services.rehearsal-private-sensor = lib.mkIf (config.networking.hostName == "automation") {
    description = "Disposable tailnet-only synthetic sensor read endpoint";
    after = ["tailscaled.service" "home-assistant.service"];
    serviceConfig = {
      ExecStart = "${pkgs.nginx}/bin/nginx -p /run/rehearsal-private-sensor -c ${pkgs.writeText "rehearsal-private-sensor.conf" ''
        daemon off;
        master_process off;
        pid /run/rehearsal-private-sensor/nginx.pid;
        error_log stderr crit;
        events { worker_connections 32; }
        http {
          access_log off;
          client_max_body_size 1k;
          client_body_temp_path /run/rehearsal-private-sensor/body;
          proxy_temp_path /run/rehearsal-private-sensor/proxy;
          fastcgi_temp_path /run/rehearsal-private-sensor/fastcgi;
          uwsgi_temp_path /run/rehearsal-private-sensor/uwsgi;
          scgi_temp_path /run/rehearsal-private-sensor/scgi;
          server {
            listen 0.0.0.0:18444;
            server_name _;
            add_header Cache-Control "private, no-store" always;
            location = /api/states/sensor.synthetic_kitchen_temperature {
              limit_except GET { deny all; }
              proxy_set_header Host "127.0.0.1:8123";
              proxy_set_header Authorization $http_authorization;
              proxy_set_header X-Forwarded-For "";
              proxy_hide_header Cache-Control;
              proxy_hide_header Expires;
              proxy_hide_header CDN-Cache-Control;
              proxy_hide_header Cloudflare-CDN-Cache-Control;
              proxy_pass http://127.0.0.1:8123/api/states/sensor.synthetic_kitchen_temperature;
            }
            location / { return 403; }
          }
        }
      ''}";
      DynamicUser = true;
      RuntimeDirectory = "rehearsal-private-sensor";
      RuntimeDirectoryMode = "0700";
      UMask = "0077";
      MemoryMax = "64M";
      TasksMax = 16;
      TimeoutStopSec = 15;
      NoNewPrivileges = true;
      PrivateTmp = true;
      PrivateDevices = true;
      ProtectHome = true;
      ProtectSystem = "strict";
      StandardOutput = "null";
      StandardError = "null";
    };
  };
}
