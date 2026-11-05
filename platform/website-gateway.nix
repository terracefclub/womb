{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.websiteGateway;
  website = config.terrace.website;
  dnsName = value:
    builtins.stringLength value
    <= 253
    && builtins.match "([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z0-9]([a-z0-9-]*[a-z0-9])?" value != null
    && lib.all (part: builtins.stringLength part <= 63) (lib.splitString "." value)
    && builtins.match ".*[a-z].*" (lib.last (lib.splitString "." value)) != null
    && !(lib.hasSuffix ".localhost" value);
  runtimeFile = value: lib.hasPrefix "/" value && !(lib.hasPrefix "/nix/store" value) && !(lib.hasInfix "\n" value);
  proxy = host: port: {
    proxyPass = "http://127.0.0.1:${toString port}";
    recommendedProxySettings = false;
    extraConfig = ''
      proxy_set_header Host ${host};
      proxy_set_header X-Forwarded-Host ${host};
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-Port 443;
      proxy_set_header X-Forwarded-For $remote_addr;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header Forwarded "";
      proxy_set_header X-Forwarded-Prefix "";
      proxy_cache off;
      proxy_hide_header Cache-Control;
      proxy_hide_header Expires;
      proxy_hide_header CDN-Cache-Control;
      proxy_hide_header Cloudflare-CDN-Cache-Control;
      proxy_hide_header Surrogate-Control;
      add_header Cache-Control "private, no-store" always;
      add_header X-Content-Type-Options "nosniff" always;
      proxy_connect_timeout 5s;
      proxy_read_timeout 30s;
      proxy_send_timeout 30s;
    '';
  };
  virtualHost = host: port: {
    onlySSL = true;
    sslCertificate = "/run/credentials/nginx.service/terrace-website-cert";
    sslCertificateKey = "/run/credentials/nginx.service/terrace-website-key";
    extraConfig = ''
      access_log off;
      client_max_body_size 16m;
      add_header Cache-Control "private, no-store" always;
      add_header X-Content-Type-Options "nosniff" always;
      if ($ssl_server_name != ${host}) { return 421; }
    '';
    locations."/" = proxy host port;
  };
  runtimeGuard = component:
    pkgs.writeShellScript "terrace-${component}-https-guard" ''
      exec ${website.nodePackage}/bin/node -e '
        const e = process.env;
        if (e.CMS_ORIGIN !== "https://${cfg.cmsHostname}" ||
          ${
        if component == "cms"
        then ''e.CMS_PUBLIC_DEPLOYMENT !== "1"''
        else ''e.ORIGIN !== "https://${cfg.webHostname}" || e.MEMBER_AUTH_ORIGIN !== e.ORIGIN''
      }
          ${lib.optionalString (config.terrace.identity.enable or false) ''|| e.MEMBER_AUTH_ISSUER !== "https://${config.terrace.identity.hostname}/realms/terrace"''}) {
          console.error("HTTPS gateway runtime origins or public activation policy rejected.");
          process.exit(1);
        }
      '
    '';
in {
  imports = [./website.nix];
  options.terrace.websiteGateway = {
    enable = lib.mkEnableOption "optional HTTPS origin gateway for the native website/CMS";
    webHostname = lib.mkOption {
      type = lib.types.str;
      description = "Exact lowercase website DNS hostname; no DNS is created.";
    };
    cmsHostname = lib.mkOption {
      type = lib.types.str;
      description = "Exact lowercase CMS DNS hostname.";
    };
    certificateFile = lib.mkOption {
      type = lib.types.str;
      description = "Runtime certificate chain covering both hostnames.";
    };
    certificateKeyFile = lib.mkOption {
      type = lib.types.str;
      description = "Root-readable runtime private key; never evaluated by Nix.";
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = website.enable;
        message = "The website gateway requires the native website module.";
      }
      {
        assertion = lib.all dnsName [cfg.webHostname cfg.cmsHostname] && cfg.webHostname != cfg.cmsHostname;
        message = "The website gateway requires distinct canonical DNS hostnames.";
      }
      {
        assertion = !(config.terrace.identity.enable or false) || !(builtins.elem config.terrace.identity.hostname [cfg.webHostname cfg.cmsHostname]);
        message = "Application and identity hostnames must be distinct.";
      }
      {
        assertion = lib.all runtimeFile [cfg.certificateFile cfg.certificateKeyFile];
        message = "Website TLS inputs must be external absolute runtime file paths.";
      }
    ];
    services.nginx = {
      enable = true;
      recommendedTlsSettings = true;
      virtualHosts = {
        ${cfg.webHostname} = virtualHost cfg.webHostname website.webPort;
        ${cfg.cmsHostname} = virtualHost cfg.cmsHostname website.cmsPort;
        "terrace-reject-unknown" = {
          default = true;
          onlySSL = true;
          sslCertificate = "/run/credentials/nginx.service/terrace-website-cert";
          sslCertificateKey = "/run/credentials/nginx.service/terrace-website-key";
          extraConfig = ''
            access_log off;
            add_header Cache-Control "private, no-store" always;
            add_header X-Content-Type-Options "nosniff" always;
          '';
          locations."/".return = "421";
        };
      };
    };
    systemd.services.nginx.serviceConfig = {
      LoadCredential = ["terrace-website-cert:${cfg.certificateFile}" "terrace-website-key:${cfg.certificateKeyFile}"];
      LogNamespace = "terrace-apps";
      MemoryMax = "256M";
      TasksMax = 128;
    };
    systemd.services.terrace-cms.serviceConfig.ExecStartPre = lib.mkAfter [(runtimeGuard "cms")];
    systemd.services.terrace-cms-migrate.serviceConfig.ExecStartPre = lib.mkAfter [(runtimeGuard "cms")];
    systemd.services.terrace-web.serviceConfig.ExecStartPre = lib.mkAfter [(runtimeGuard "web")];
    networking.firewall.allowedTCPPorts = [443];
  };
}
