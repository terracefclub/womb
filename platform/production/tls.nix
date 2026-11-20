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
  # Production directories accepted without the owner's explicit other-CA override.
  knownDirectories = ["https://acme-v02.api.letsencrypt.org/directory"];
  # Staging and test endpoints of the known CAs and local test servers, matched on
  # whole host and path tokens (so `attestation` is not `test`).
  directoryTokens = lib.splitString "." (builtins.replaceStrings ["-" "/" ":" "_"] ["." "." "." "."] (lib.removePrefix "https://" tls.acme.directory));
  testDirectory = lib.any (token: builtins.elem token ["staging" "stg" "test" "testing" "sandbox" "pebble" "localhost" "boulder" "dev"]) directoryTokens;
  certificate = config.security.acme.certs.${certName};
  credentialSources = map (entry: lib.last (lib.splitString ":" entry)) (lib.toList (config.systemd.services.nginx.serviceConfig.LoadCredential or []));
  nginxUser = config.services.nginx.user;
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
      names=(${lib.escapeShellArgs names})
      for name in "''${names[@]}"; do
        # The chain is verified here, the name separately below: each cause has its own message.
        if ! served=$(timeout 30 openssl s_client -connect 127.0.0.1:443 -servername "$name" \
            -verify_return_error -CAfile "$trust" </dev/null 2>/dev/null); then
          echo "$name: FAIL: no verified TLS session (untrusted, self-signed or placeholder chain, or nothing listening)"
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
  # lego 5 (5.5.1, cmd/internal/storage/storage_accounts.go) writes the account
  # file with `"registration": null` and the key before it registers. If that
  # first registration cannot reach the CA, every later run tries to "recover" the
  # never-registered account, gets accountDoesNotExist and fails for good. Remove
  # exactly such an account (it exists nowhere but here) so the next run registers
  # afresh. Any other shape is left alone and reported: re-check this step (and run
  # platform/production-tls-check) on every lego or nixpkgs update.
  unregistered = pkgs.writeShellApplication {
    name = "terrace-acme-unregistered-account";
    runtimeInputs = [pkgs.coreutils pkgs.jq];
    text = ''
      shopt -s nullglob
      for account in /var/lib/acme/.lego/accounts/*/*/*/account.json; do
        dir=$(dirname "$account")
        state=$(jq -r 'if .registration == null then "unregistered" elif (.registration | type) == "object" then "registered" else "unknown" end' "$account" 2>/dev/null) || state=unknown
        case $state in
          registered) ;;
          unregistered)
            echo "Removing an ACME account that was never registered: $dir"
            rm -f -- "$account" "''${dir:?}"/*.key
            ;;
          *) echo "WARNING: unrecognised ACME account file $account left in place; review tls.nix after a lego update" >&2 ;;
        esac
      done
    '';
  };
  tool = pkgs.writeShellApplication {
    name = "terrace-tls";
    runtimeInputs = [pkgs.openssl pkgs.coreutils pkgs.findutils pkgs.gnused pkgs.systemd check];
    text = ''
      mode=${tls.mode}
      cert=${lib.escapeShellArg certName}
      dir=${lib.escapeShellArg directory}
      usage() { echo 'usage: terrace-tls status | check | renew [--force --yes] | prepare-acme' >&2; exit 2; }
      root() { [[ $(id -u) == 0 ]] || { echo 'terrace-tls: run as root (sudo terrace-tls ...)' >&2; exit 1; }; }
      case "''${1:-}" in
        status)
          root
          echo "mode: $mode; names: ${toString names}; alert below ${toString tls.minDays} days"
          if [[ $mode == acme ]]; then
            echo "certificate authority: ${lib.optionalString acme tls.acme.directory}"
            if [[ -r $dir/cert.pem ]]; then
              openssl x509 -noout -subject -issuer -enddate -ext subjectAltName -in "$dir/cert.pem"
              issued=(/var/lib/acme/.lego/"$cert"/*/"$cert".crt)
              [[ -e ''${issued[0]} ]] || echo 'NOT YET ISSUED BY ACME: nginx serves the placeholder or the carried-over manual certificate'
              [[ -e $dir/acme-success ]] || echo 'PLACEHOLDER: self-signed stand-in, not trusted by anyone'
            else
              echo 'no certificate yet (the host is not adopted, or nginx has not started)'
            fi
            systemctl show ${orderUnit}.service -p ActiveState -p SubState -p Result -p NRestarts -p ExecMainExitTimestamp --no-pager
            systemctl list-timers --all --no-pager ${lib.escapeShellArg "acme-renew-${certName}.timer"} terrace-tls-check.timer
          else
            systemctl list-timers --all --no-pager terrace-tls-check.timer
          fi
          systemctl show terrace-tls-check.service -p Result -p ExecMainExitTimestamp --no-pager
          ;;
        check) terrace-tls-check ;;
        renew)
          root
          [[ $mode == acme ]] || { echo 'terrace-tls: manual mode: replace the certificate with terrace-secrets set, then push (docs/secrets.md)' >&2; exit 1; }
          case "''${2:-}" in
            "") ;;
            --force)
              [[ ''${3:-} == --yes ]] || { echo 'terrace-tls: --force orders a new certificate now and counts against the CA rate limits (5 a week for the same names at Lets Encrypt); repeat with --force --yes' >&2; exit 2; }
              # Without lego's stored certificate resource (certificate, key, issuer and
              # metadata together; a partial set makes lego fail every later run) the order
              # unit takes its full path and orders a new certificate. The served copy in
              # /var/lib/acme/<name> stays until the new one is installed.
              find /var/lib/acme/.lego/"$cert" -mindepth 2 -maxdepth 2 -type f -name "$cert.*" -delete
              ;;
            *) usage ;;
          esac
          # A manual run starts a fresh retry budget.
          systemctl reset-failed ${orderUnit}.service 2>/dev/null || true
          if systemctl start ${orderUnit}.service; then
            echo "terrace-tls: ${orderUnit}.service succeeded"
          else
            echo "terrace-tls: ${orderUnit}.service FAILED; the previous certificate is still served and the unit retries on its own${lib.optionalString acme " (${toString tls.acme.retries} attempts a day, ${toString tls.acme.retrySeconds} s apart)"}. journalctl -u ${orderUnit}.service" >&2
            exit 1
          fi
          ;;
        prepare-acme)
          # Before switching a live manual host to ACME: stage the certificate it serves
          # now as the first ACME certificate, so the switch never serves the placeholder.
          root
          [[ $mode == manual ]] || { echo 'terrace-tls: this host already gets its certificate by ACME' >&2; exit 1; }
          [[ -r /run/secrets/tls/certificate && -r /run/secrets/tls/key ]] || { echo 'terrace-tls: no delivered certificate in /run/secrets/tls' >&2; exit 1; }
          umask 077
          install -d -m 0750 "$dir"
          install -m 0640 /run/secrets/tls/certificate "$dir/fullchain.pem"
          install -m 0640 /run/secrets/tls/certificate "$dir/chain.pem"
          install -m 0640 /run/secrets/tls/key "$dir/key.pem"
          cat "$dir/key.pem" "$dir/fullchain.pem" >"$dir/full.pem"
          chmod 0640 "$dir/full.pem"
          ln -sf fullchain.pem "$dir/cert.pem"
          # Tells the NixOS placeholder step that a usable certificate is already in place.
          touch "$dir/acme-success"
          echo "terrace-tls: the served certificate is staged in $dir; deploy the ACME configuration now (docs/tls.md)."
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
      otherAuthority = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Owner decision: use a CA other than Let's Encrypt. Production then accepts any public https directory URL that does not look like a staging or test endpoint.";
      };
      retrySeconds = lib.mkOption {
        type = lib.types.ints.positive;
        default = 3600;
        description = "Wait before the order unit retries a failed order on its own (production: at least 900).";
      };
      retries = lib.mkOption {
        type = lib.types.ints.between 1 5;
        default = 3;
        description = "Attempts a day before a failed order stays a failed unit (systemctl --failed) until the renewal timer or an operator starts it again.";
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
          assertion = directoryHost != null && (!production || builtins.elem tls.acme.directory knownDirectories || (tls.acme.otherAuthority && publicName directoryHost && directoryPort == null && !testDirectory));
          message = "Terrace production refuses a staging, test or unlisted ACME directory (${tls.acme.directory}); use Let's Encrypt's production directory, or set tls.acme.otherAuthority for another CA's production directory.";
        }
        {
          assertion = !production || lib.all (name: !(config.systemd.services.${name}.environment ? LEGO_CA_CERTIFICATES) && !(config.systemd.services.${name}.environment ? LEGO_TLS_SKIP_VERIFY)) [orderUnit "acme-${certName}"];
          message = "Terrace production refuses test-only ACME client trust (LEGO_CA_CERTIFICATES, LEGO_TLS_SKIP_VERIFY).";
        }
        {
          assertion = !production || (certificate.extraLegoFlags == [] && certificate.extraLegoRunFlags == [] && certificate.extraLegoRenewFlags == [] && certificate.environmentFile == null && certificate.credentialFiles == {} && certificate.dnsProvider == null && certificate.csr == null && certificate.s3Bucket == null && certificate.listenHTTP == null);
          message = "Terrace production runs the ACME client for HTTP-01 only: no extra lego flags, environment, credentials, CSR or other challenge.";
        }
        {
          assertion = lib.versions.major pkgs.lego.version == "5";
          message = "lego is no longer major version 5: review the unregistered-account step in platform/production/tls.nix and re-run platform/production-tls-check.";
        }
        {
          # Retries on its own, bounded, then stays visibly failed.
          assertion = let
            unit = config.systemd.services.${orderUnit};
          in
            (unit.serviceConfig.Restart or "no") == "on-failure" && unit.serviceConfig.RestartSec == toString tls.acme.retrySeconds && unit.unitConfig.StartLimitBurst == tls.acme.retries && unit.unitConfig.StartLimitIntervalSec == "1d" && (!production || tls.acme.retrySeconds >= 900);
          message = "Terrace production retries a failed certificate order at most ${toString tls.acme.retries} times a day, at least 15 minutes apart, then keeps it a failed unit.";
        }
        {
          assertion = lib.sort lib.lessThan (lib.attrNames vhosts) == lib.sort lib.lessThan (tlsVhosts ++ httpVhosts) && portEightyOk;
          message = "Terrace production port 80 serves only the ACME challenge and a redirect to HTTPS for its own names (421 for any other host).";
        }
        {
          assertion =
            credentialSources
            != []
            && lib.all (source: builtins.elem source ["${directory}/fullchain.pem" "${directory}/key.pem"]) credentialSources
            && builtins.elem "${directory}/key.pem" credentialSources
            && builtins.elem "credentials" (lib.splitString " " (toString (config.systemd.services.nginx.serviceConfig.RefreshOnReload or "")))
            && lib.all (name: lib.hasPrefix "/run/credentials/nginx.service/" vhosts.${name}.sslCertificate && lib.hasPrefix "/run/credentials/nginx.service/" vhosts.${name}.sslCertificateKey) tlsVhosts
            && certificate.group == "acme"
            && !(builtins.elem nginxUser (config.users.groups.acme.members or []))
            && !(builtins.elem "acme" (config.users.users.${nginxUser}.extraGroups or []))
            && config.users.users.${nginxUser}.group != "acme";
          message = "Terrace production ACME: nginx gets the certificate as systemd credentials from the ACME directory, refreshed on reload, and the nginx user cannot read the key itself.";
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
          # Not nginx: the key stays unreadable to the nginx user; systemd (root)
          # hands nginx a copy as a credential and refreshes it on reload.
          group = "acme";
          # Blocking, so a failed reload fails the order unit (visible) instead of
          # being lost in the background; nothing to reload while nginx is stopped.
          reloadServices = [];
          postRun = "${pkgs.systemd}/bin/systemctl try-reload-or-restart nginx.service";
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
        lib.optionalAttrs (!app) {
          # The relay module's own port-80 redirect is replaced by terrace-http below.
          ${certName} = {
            forceSSL = lib.mkForce false;
            onlySSL = true;
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
          # The role modules load tls.certificateFile/keyFile (the ACME directory) as
          # credentials; systemd re-reads them on `systemctl reload`, so a renewal
          # needs no restart and nginx never reads the key file itself.
          serviceConfig.RefreshOnReload = "credentials";
        };
        # Pulled in by nginx only, so nothing ACME runs before adoption.
        "acme-${certName}" = {
          wantedBy = lib.mkForce [];
          serviceConfig.LimitCORE = 0;
        };
        # A failed order retries on its own a few times a day, then stays a failed
        # unit (start limit) until the renewal timer or an operator starts it again.
        ${orderUnit} = {
          unitConfig = {
            StartLimitIntervalSec = "1d";
            StartLimitBurst = tls.acme.retries;
          };
          serviceConfig = {
            Restart = lib.mkForce "on-failure";
            RestartSec = lib.mkForce (toString tls.acme.retrySeconds);
            LimitCORE = 0;
            ExecStartPre = ["${unregistered}/bin/terrace-acme-unregistered-account"];
          };
        };
        acme-setup.serviceConfig.LimitCORE = 0;
      };
    })
  ]);
}
