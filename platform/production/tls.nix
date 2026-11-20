# TLS for both production roles (docs/tls.md). Each host terminates TLS for its
# own public names: the app host for the website, CMS and identity names, the
# relay for its own name. The certificate either comes from the operator through
# terrace-secrets (`manual`, the default) or from an ACME CA by http-01 on this
# host (`acme`). An expiry check runs in both modes.
{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.production;
  tls = cfg.tls;
  acme = tls.mode == "acme";
  app = cfg.role == "app";
  production = cfg.instance == "production";
  names = cfg.publicHostnames;
  certName = builtins.head names;
  directory = "/var/lib/acme/${certName}";
  webroot = "/var/lib/acme/acme-challenge";
  challenge = "^~ /.well-known/acme-challenge/";
  redirect = "301 https://$host$request_uri";
  orderUnit = "acme-order-renew-${certName}";
  # TLS server blocks of this role (port 443 only).
  tlsVhosts = names ++ lib.optional app "terrace-reject-unknown";
  httpVhosts = ["terrace-http" "terrace-http-reject"];
  vhosts = config.services.nginx.virtualHosts;
  trust =
    if tls.trustAnchorFile == null
    then "/etc/ssl/certs/ca-certificates.crt"
    else tls.trustAnchorFile;
  reservedSuffixes = ["invalid" "test" "example" "localhost" "local" "localdomain" "internal" "intranet" "private" "lan" "home" "corp" "home.arpa" "example.com" "example.net" "example.org"];
  reserved = name: lib.any (suffix: name == suffix || lib.hasSuffix ".${suffix}" name) reservedSuffixes;
  publicName = name: builtins.match "([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]([a-z0-9-]*[a-z0-9])?" name != null && !(reserved name);
  # https://<DNS name>[:port]/<path>, no user info; production also refuses a port.
  directoryParts = builtins.match "https://([a-z0-9.-]+)(:[0-9]{1,5})?(/[^ ]*)?" tls.acme.directory;
  directoryHost =
    if directoryParts == null
    then null
    else builtins.head directoryParts;
  directoryPort =
    if directoryParts == null
    then null
    else builtins.elemAt directoryParts 1;
  # Staging and test endpoints of the known CAs, and anything that looks like a local test server.
  testDirectory = lib.any (word: lib.hasInfix word (lib.toLower tls.acme.directory)) ["staging" "stg" "test" "sandbox" "pebble" "localhost" "boulder"];
  contactOk = builtins.match "[A-Za-z0-9._%+-]+@([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z][a-z0-9-]*[a-z0-9]" tls.acme.contact != null;
  contactDomain = lib.last (lib.splitString "@" tls.acme.contact);
  http = vhosts.terrace-http or {};
  httpReject = vhosts.terrace-http-reject or {};
  # A plain-HTTP server block that serves nothing but what is listed here.
  bare = vhost:
    !vhost.onlySSL
    && !vhost.forceSSL
    && !vhost.addSSL
    && !vhost.rejectSSL
    && !vhost.enableACME
    && vhost.useACMEHost == null
    && vhost.listen == []
    && vhost.listenAddresses == []
    && vhost.root == null
    && vhost.globalRedirect == null
    && vhost.basicAuth == {}
    && vhost.basicAuthFile == null
    && vhost.extraConfig == "access_log off;\n"
    && lib.all (location: location.proxyPass == null && location.alias == null && location.tryFiles == null && location.fastcgiParams == {} && location.uwsgiPass == null) (lib.attrValues vhost.locations);
  portEightyOk =
    lib.attrNames http.locations
    == lib.sort lib.lessThan ["/" challenge]
    && http.locations."/".return == redirect
    && http.locations."/".root == null
    && http.locations."/".extraConfig == ""
    && http.locations.${challenge}.root == webroot
    && http.locations.${challenge}.return == null
    && http.locations.${challenge}.extraConfig == "default_type text/plain;\n"
    && http.serverName == certName
    && http.serverAliases == lib.tail names
    && !http.default
    && bare http
    && lib.attrNames httpReject.locations == ["/"]
    && httpReject.locations."/".return == "421"
    && httpReject.locations."/".root == null
    && httpReject.locations."/".extraConfig == ""
    && httpReject.default
    && bare httpReject;
  check = pkgs.writeShellApplication {
    name = "terrace-tls-check";
    runtimeInputs = [pkgs.openssl pkgs.coreutils pkgs.gnused];
    text = ''
      # Connects to this host's own HTTPS listener for every public name and
      # verifies what is actually served: chain to the trust store, the name,
      # and at least ${toString tls.minDays} days left. Prints no key material.
      min_days=${toString tls.minDays}
      trust=${lib.escapeShellArg trust}
      failed=0
      for name in ${lib.escapeShellArgs names}; do
        if ! served=$(timeout 30 openssl s_client -connect 127.0.0.1:443 -servername "$name" \
            -verify_return_error -verify_hostname "$name" -CAfile "$trust" </dev/null 2>/dev/null); then
          echo "$name: FAIL: no verified TLS session (untrusted, self-signed or placeholder chain, wrong name, or nothing listening)"
          failed=1
          continue
        fi
        leaf=$(sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' <<<"$served")
        match=$(openssl x509 -noout -checkhost "$name" <<<"$leaf" 2>/dev/null || true)
        if [[ -z $leaf || $match != *"does match"* ]]; then
          echo "$name: FAIL: the served certificate does not name $name"
          failed=1
          continue
        fi
        end=$(openssl x509 -noout -enddate <<<"$leaf" | cut -d= -f2)
        if ! openssl x509 -noout -checkend $((min_days * 86400)) <<<"$leaf" >/dev/null; then
          echo "$name: FAIL: expires $end, fewer than $min_days days left"
          failed=1
          continue
        fi
        issuer=$(openssl x509 -noout -issuer -nameopt RFC2253 <<<"$leaf" | sed 's/^issuer=//')
        echo "$name: OK: expires $end; issuer $issuer"
      done
      exit "$failed"
    '';
  };
  tool = pkgs.writeShellApplication {
    name = "terrace-tls";
    runtimeInputs = [pkgs.openssl pkgs.coreutils pkgs.gnused pkgs.systemd check];
    text = ''
      mode=${tls.mode}
      cert=${lib.escapeShellArg certName}
      usage() { echo 'usage: terrace-tls status | check | renew [--force]' >&2; exit 2; }
      case "''${1:-}" in
        status)
          echo "mode: $mode; names: ${toString names}; alert below ${toString tls.minDays} days"
          if [[ $mode == acme ]]; then
            echo "certificate authority: ${lib.optionalString acme tls.acme.directory}"
            if [[ -r ${directory}/cert.pem ]]; then
              openssl x509 -noout -subject -issuer -enddate -ext subjectAltName -in ${directory}/cert.pem
              [[ -e ${directory}/acme-success ]] || echo 'PLACEHOLDER: no certificate has been issued yet (self-signed stand-in)'
            else
              echo 'no certificate yet (the host is not adopted, or nginx has not started)'
            fi
            systemctl show ${orderUnit}.service -p ActiveState -p Result -p ExecMainExitTimestamp --no-pager
            systemctl list-timers --all --no-pager ${lib.escapeShellArg "acme-renew-${certName}.timer"} terrace-tls-check.timer
          else
            systemctl list-timers --all --no-pager terrace-tls-check.timer
          fi
          systemctl show terrace-tls-check.service -p Result -p ExecMainExitTimestamp --no-pager
          ;;
        check) terrace-tls-check ;;
        renew)
          [[ $(id -u) == 0 ]] || { echo 'terrace-tls: run as root' >&2; exit 1; }
          [[ $mode == acme ]] || { echo 'terrace-tls: manual mode: replace the certificate with terrace-secrets set, then push (docs/secrets.md)' >&2; exit 1; }
          if [[ ''${2:-} == --force ]]; then
            # Without lego's stored certificate the order unit takes its full path and
            # orders a new certificate; the served copy stays until the new one is installed.
            rm -f /var/lib/acme/.lego/"$cert"/*/"$cert".crt
          elif [[ -n ''${2:-} ]]; then
            usage
          fi
          if systemctl start ${orderUnit}.service; then
            echo "terrace-tls: ${orderUnit}.service succeeded"
          else
            echo "terrace-tls: ${orderUnit}.service FAILED; the previous certificate is still served. journalctl -u ${orderUnit}.service" >&2
            exit 1
          fi
          ;;
        *) usage ;;
      esac
    '';
  };
in {
  options.terrace.production.tls = {
    mode = lib.mkOption {
      type = lib.types.enum ["manual" "acme"];
      default = "manual";
      description = "`manual`: the operator delivers tls/certificate and tls/key with terrace-secrets. `acme`: this host obtains and renews its certificate by ACME http-01 (docs/tls.md).";
    };
    acme = {
      directory = lib.mkOption {
        type = lib.types.str;
        description = "Required with `acme`: the CA's ACME directory URL (an owner decision: the CA whose terms are accepted). Production refuses staging and test endpoints.";
      };
      acceptTerms = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Required true with `acme`: the owner has read and accepted the CA's subscriber agreement.";
      };
      contact = lib.mkOption {
        type = lib.types.str;
        description = "Required with `acme`: the address registered with the CA account (expiry and policy notices).";
      };
    };
    minDays = lib.mkOption {
      type = lib.types.ints.between 1 60;
      default = 14;
      description = "terrace-tls-check fails when a served certificate has fewer days left than this.";
    };
    trustAnchorFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Rehearsal only: a runtime CA file for the expiry check instead of the system trust store. Production refuses it.";
    };
    certificateName = lib.mkOption {
      type = lib.types.str;
      readOnly = true;
      internal = true;
      default = certName;
      description = "The ACME certificate (first public hostname; the others are extra names on it).";
    };
    certificateFile = lib.mkOption {
      type = lib.types.str;
      readOnly = true;
      internal = true;
      default =
        if acme
        then "${directory}/fullchain.pem"
        else cfg.secrets.path."tls/certificate";
      description = "The certificate chain nginx serves.";
    };
    keyFile = lib.mkOption {
      type = lib.types.str;
      readOnly = true;
      internal = true;
      default =
        if acme
        then "${directory}/key.pem"
        else cfg.secrets.path."tls/key";
      description = "The private key nginx serves with.";
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      assertions = [
        {
          assertion = !production || tls.trustAnchorFile == null;
          message = "Terrace production refuses a TLS trust anchor override (terrace.production.tls.trustAnchorFile is for the rehearsal CA).";
        }
        {
          # Every public name is served over TLS only; nothing of the application on port 80.
          assertion = lib.all (name: vhosts ? ${name} && vhosts.${name}.onlySSL && !vhosts.${name}.forceSSL && !vhosts.${name}.addSSL && !vhosts.${name}.rejectSSL && vhosts.${name}.listen == []) (lib.optionals (app || acme) tlsVhosts);
          message = "Terrace production serves every public hostname over HTTPS only (onlySSL, no plain-HTTP listener).";
        }
      ];
      environment.systemPackages = [tool check];
      # Both modes: fail visibly (systemctl --failed) when the served certificate is
      # untrusted, names the wrong host or runs out within minDays. Alerting is G-23.
      systemd.services.terrace-tls-check = {
        description = "Check the served TLS certificates (chain, names, days left)";
        after = ["nginx.service"];
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${check}/bin/terrace-tls-check";
          DynamicUser = true;
          PrivateTmp = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          NoNewPrivileges = true;
          RestrictAddressFamilies = ["AF_INET" "AF_INET6" "AF_UNIX"];
          LimitCORE = 0;
        };
      };
      systemd.timers.terrace-tls-check.timerConfig = {
        OnActiveSec = "1h";
        OnCalendar = "daily";
        Persistent = true;
        RandomizedDelaySec = "1h";
      };
      terrace.production.writerTimers = ["terrace-tls-check"] ++ lib.optional acme "acme-renew-${certName}";
    }
    (lib.mkIf acme {
      assertions = [
        {
          assertion = tls.acme.acceptTerms;
          message = "Terrace production ACME needs terrace.production.tls.acme.acceptTerms = true: the owner accepts the CA's subscriber agreement.";
        }
        {
          assertion = contactOk && (!production || !(reserved contactDomain));
          message = "Terrace production ACME needs a real contact address (terrace.production.tls.acme.contact).";
        }
        {
          assertion = directoryHost != null && (!production || (publicName directoryHost && directoryPort == null && !testDirectory));
          message = "Terrace production refuses a staging or test ACME directory (${tls.acme.directory}); use the CA's production https:// directory URL.";
        }
        {
          assertion = !production || lib.all (name: !(config.systemd.services.${name}.environment ? LEGO_CA_CERTIFICATES) && !(config.systemd.services.${name}.environment ? LEGO_TLS_SKIP_VERIFY)) [orderUnit "acme-${certName}"];
          message = "Terrace production refuses test-only ACME client trust (LEGO_CA_CERTIFICATES, LEGO_TLS_SKIP_VERIFY).";
        }
        {
          assertion = (config.systemd.services.${orderUnit}.serviceConfig.Restart or "no") == "no";
          message = "Terrace production keeps a failed certificate order a failed unit (no automatic restart hiding it).";
        }
        {
          assertion = lib.sort lib.lessThan (lib.attrNames vhosts) == lib.sort lib.lessThan (tlsVhosts ++ httpVhosts) && portEightyOk;
          message = "Terrace production port 80 serves only the ACME challenge and a redirect to HTTPS for its own names (421 for any other host).";
        }
        {
          assertion = lib.all (name: vhosts.${name}.sslCertificate == "${directory}/fullchain.pem" && vhosts.${name}.sslCertificateKey == "${directory}/key.pem") tlsVhosts && config.systemd.services.nginx.serviceConfig.LoadCredential == [];
          message = "Terrace production ACME: nginx reads the ACME certificate in place (reloaded on renewal), no stale credential copy.";
        }
      ];
      security.acme = {
        inherit (tls.acme) acceptTerms;
        certs.${certName} = {
          domain = certName;
          extraDomainNames = lib.tail names;
          server = tls.acme.directory;
          email = tls.acme.contact;
          inherit webroot;
          group = config.services.nginx.group;
          reloadServices = ["nginx.service"];
        };
      };
      terrace.production = {
        # The secrets store holds no tls/* for this host (inventory `omittedBy`).
        secrets.features = ["acme-tls"];
        # Port 80: the challenge and the redirect (the relay already opens it).
        publicTCPPorts = lib.optional app 80;
      };
      networking.firewall.allowedTCPPorts = lib.optional app 80;
      services.nginx.virtualHosts =
        lib.genAttrs tlsVhosts (_: {
          sslCertificate = lib.mkForce "${directory}/fullchain.pem";
          sslCertificateKey = lib.mkForce "${directory}/key.pem";
        })
        // lib.optionalAttrs (!app) {
          # The relay module's own port-80 redirect is replaced by terrace-http below.
          ${certName} = {
            forceSSL = lib.mkForce false;
            onlySSL = true;
            sslCertificate = lib.mkForce "${directory}/fullchain.pem";
            sslCertificateKey = lib.mkForce "${directory}/key.pem";
          };
        }
        // {
          terrace-http = {
            serverName = certName;
            serverAliases = lib.tail names;
            extraConfig = ''
              access_log off;
            '';
            locations.${challenge} = {
              root = webroot;
              extraConfig = ''
                default_type text/plain;
              '';
            };
            locations."/".return = redirect;
          };
          terrace-http-reject = {
            default = true;
            extraConfig = ''
              access_log off;
            '';
            locations."/".return = "421";
          };
        };
      systemd.services = {
        nginx = {
          # The placeholder unit runs first, so nginx always has a certificate to
          # start with; the order runs after nginx can answer the challenge.
          wants = ["acme-${certName}.service"];
          after = ["acme-${certName}.service"];
          before = ["${orderUnit}.service"];
          # Read in place: a reload (SIGHUP) picks up a renewal without a restart.
          serviceConfig.LoadCredential = lib.mkForce [];
        };
        # Pulled in by nginx only, so nothing ACME runs before adoption.
        "acme-${certName}" = {
          wantedBy = lib.mkForce [];
          serviceConfig.LimitCORE = 0;
        };
        # A failed order stays failed (visible); the renewal timer tries again.
        ${orderUnit}.serviceConfig = {
          Restart = lib.mkForce "no";
          LimitCORE = 0;
        };
        acme-setup.serviceConfig.LimitCORE = 0;
      };
    })
  ]);
}
