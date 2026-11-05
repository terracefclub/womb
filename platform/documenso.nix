{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.documenso;
  pin = builtins.fromJSON (builtins.readFile ./documenso/image.json);
  imageFile = import ./documenso/image.nix {inherit pkgs;};
  image = "${pin.imageName}:${pin.version}";
  runtime = "/run/terrace-documenso-inputs";
  role = "terrace_documenso";
  hostnameOK = name: builtins.stringLength name <= 253 && lib.all (label: builtins.match "[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?" label != null) (lib.splitString "." name);
  pathOK = value: builtins.match "/[a-zA-Z0-9][a-zA-Z0-9/_.-]*" value != null && !(lib.hasPrefix "/nix/store" value) && !(lib.hasInfix "//" value) && lib.all (part: part != "." && part != "..") (lib.splitString "/" value);
  commonEnvironment = {
    NODE_ENV = "production";
    NODE_OPTIONS = "--max-old-space-size=768";
    NEXT_PUBLIC_WEBAPP_URL = "https://${cfg.hostname}";
    NEXT_PRIVATE_INTERNAL_WEBAPP_URL = "http://127.0.0.1:3000";
    NEXT_PRIVATE_OIDC_WELL_KNOWN = "https://${cfg.identityHostname}/realms/terrace/.well-known/openid-configuration";
    NEXT_PRIVATE_OIDC_CLIENT_ID = "terrace-documenso";
    NEXT_PRIVATE_OIDC_PROVIDER_LABEL = "Terrace";
    NEXT_PRIVATE_OIDC_SKIP_VERIFY = "false";
    NEXT_PUBLIC_DISABLE_SIGNUP = "true";
    NEXT_PUBLIC_UPLOAD_TRANSPORT = "database";
    NEXT_PRIVATE_JOBS_PROVIDER = "local";
    NEXT_PRIVATE_SMTP_TRANSPORT = "smtp-auth";
    NEXT_PRIVATE_SMTP_HOST = "host.containers.internal";
    NEXT_PRIVATE_SMTP_PORT = toString cfg.smtpPort;
    NEXT_PRIVATE_SMTP_FROM_NAME = "Terrace documents";
    NEXT_PRIVATE_SMTP_FROM_ADDRESS = "documents@terrace.invalid";
    NEXT_PRIVATE_SMTP_UNSAFE_IGNORE_TLS = "true";
    NEXT_PRIVATE_SMTP_SECURE = "false";
    DOCUMENSO_DISABLE_TELEMETRY = "true";
    NEXT_PUBLIC_POSTHOG_KEY = "";
    NEXT_PUBLIC_FEATURE_BILLING_ENABLED = "";
    NEXT_PRIVATE_SIGNING_TRANSPORT = "local";
    NEXT_PRIVATE_SIGNING_TIMESTAMP_AUTHORITY = "";
  };
  environmentArguments = lib.concatStringsSep " " (lib.mapAttrsToList (key: value: "--env ${lib.escapeShellArg "${key}=${value}"}") commonEnvironment);
  offlineRun = name: environmentFile: command: ''
    ${pkgs.podman}/bin/podman run --rm --name ${name} --pull=never \
      --network=none --user 1001:1001 --cap-drop=ALL --security-opt=no-new-privileges \
      --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m --memory=1g --cpus=1 --pids-limit=128 \
      --log-driver=none --env-file ${environmentFile} ${environmentArguments} \
      --volume /run/postgresql:/run/postgresql:ro \
      --volume ${runtime}/credentials:/run/terrace-inputs:ro \
      --volume ${./documenso/bootstrap.cjs}:/run/terrace-bootstrap.cjs:ro \
      --workdir /app/apps/remix --entrypoint node ${image} ${command}
  '';
  privateUnit = {
    UMask = "0077";
    StandardOutput = "null";
    StandardError = "null";
    TimeoutStartSec = 180;
    TimeoutStopSec = 30;
  };
in {
  imports = [./backup.nix];
  options.terrace.documenso = {
    enable = lib.mkEnableOption "optional pinned Documenso runtime and approved-account bootstrap";
    hostname = lib.mkOption {
      type = lib.types.str;
      description = "Exact non-default HTTPS document hostname.";
    };
    identityHostname = lib.mkOption {
      type = lib.types.str;
      description = "HTTPS hostname of the dedicated local Terrace Keycloak.";
    };
    inputsDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/terrace-documenso-inputs";
      description = "Root-owned0700 runtime directory with root-owned0400 secrets.json; never put values in Nix.";
    };
    certificateFile = lib.mkOption {
      type = lib.types.str;
      description = "Runtime PEM certificate chain for the document hostname.";
    };
    certificateKeyFile = lib.mkOption {
      type = lib.types.str;
      description = "Runtime PEM certificate private key.";
    };
    identityCaFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Optional runtime PEM CA for a private identity issuer; TLS verification stays enabled.";
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 13050;
    };
    smtpPort = lib.mkOption {
      type = lib.types.port;
      default = 1025;
      description = "Local-only SMTP sink/relay port; no hosted email provider is contacted by this module.";
    };
    enableSigning = lib.mkEnableOption "local document sealing using protected signing.p12 and signing-passphrase runtime files";
    manageDatabase = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Create the dedicated native database/role; disable only for an already restored local database.";
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = pkgs.stdenv.hostPlatform.system == "x86_64-linux";
        message = "Documenso image is pinned to Linux amd64.";
      }
      {
        assertion = lib.all hostnameOK [cfg.hostname cfg.identityHostname] && cfg.hostname != cfg.identityHostname;
        message = "Documenso requires distinct exact document and identity hostnames.";
      }
      {
        assertion = lib.all pathOK ([cfg.inputsDirectory cfg.certificateFile cfg.certificateKeyFile] ++ lib.optional (cfg.identityCaFile != null) cfg.identityCaFile) && cfg.port >= 1024;
        message = "Documenso inputs must be normalized absolute runtime paths and its port unprivileged.";
      }
    ];
    virtualisation.podman.enable = true;
    virtualisation.oci-containers.backend = "podman";
    services.postgresql =
      {
        authentication = lib.mkBefore ''
          local terrace_documenso terrace_documenso scram-sha-256
        '';
      }
      // lib.optionalAttrs cfg.manageDatabase {
        enable = true;
        ensureDatabases = [role];
        ensureUsers = [
          {
            name = role;
            ensureDBOwnership = true;
          }
        ];
      };
    systemd.services.terrace-documenso-inputs = {
      description = "Validate and prepare private Documenso runtime inputs";
      requires = lib.optional cfg.enableSigning "terrace-documenso-image.service";
      after = lib.optional cfg.enableSigning "terrace-documenso-image.service";
      before = ["terrace-documenso.service" "terrace-documenso-database.service" "terrace-documenso-migrate.service" "terrace-documenso-bootstrap.service"];
      serviceConfig =
        privateUnit
        // {
          Type = "oneshot";
          RemainAfterExit = true;
          RuntimeDirectory = "terrace-documenso-inputs";
          RuntimeDirectoryMode = "0700";
        }
        // lib.optionalAttrs cfg.enableSigning {
          ExecStopPost = "-${pkgs.podman}/bin/podman rm --force --ignore terrace-documenso-signing-validation";
        };
      script = ''
        set -euo pipefail
        test ! -L ${lib.escapeShellArg cfg.inputsDirectory}
        test "$(stat -c %u:%a ${lib.escapeShellArg cfg.inputsDirectory})" = 0:700
        ${pkgs.bun}/bin/bun ${./documenso/inputs.ts} ${lib.escapeShellArg "${cfg.inputsDirectory}/secrets.json"} ${runtime}
        ${lib.optionalString (cfg.identityCaFile != null) "install -m 0444 ${lib.escapeShellArg cfg.identityCaFile} ${runtime}/identity-ca.pem"}
        ${lib.optionalString cfg.enableSigning ''
          ${pkgs.bun}/bin/bun ${./documenso/signing-inputs.ts} prepare ${lib.escapeShellArg cfg.inputsDirectory} ${runtime}
          ${pkgs.podman}/bin/podman run --rm --name terrace-documenso-signing-validation --pull=never \
            --network=none --user 1001:1001 --cap-drop=ALL --security-opt=no-new-privileges \
            --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m --memory=1g --cpus=1 --pids-limit=128 \
            --log-driver=none --env-file ${runtime}/signing.env --env NODE_OPTIONS=--max-old-space-size=512 \
            --volume ${runtime}/signing.p12:/run/terrace-signing.p12:ro \
            --volume ${./documenso/signing-validation.cjs}:/run/signing-validation.cjs:ro \
            --workdir /app/apps/remix --entrypoint node ${image} /run/signing-validation.cjs
        ''}
      '';
    };
    systemd.services.terrace-documenso-database = {
      description = "Configure only the dedicated Documenso PostgreSQL role";
      requires = ["terrace-documenso-inputs.service" "postgresql.target"];
      after = ["terrace-documenso-inputs.service" "postgresql.target"];
      path = [config.services.postgresql.package pkgs.util-linux];
      serviceConfig =
        privateUnit
        // {
          Type = "oneshot";
          RemainAfterExit = true;
        };
      script = ''
        set -euo pipefail
        runuser -u postgres -- psql -X --set ON_ERROR_STOP=1 -d postgres <${runtime}/database.sql
        runuser -u postgres -- psql -X --set ON_ERROR_STOP=1 -d postgres -c 'REVOKE CONNECT ON DATABASE terrace_documenso FROM PUBLIC; GRANT CONNECT ON DATABASE terrace_documenso TO terrace_documenso;'
        runuser -u postgres -- psql -X --set ON_ERROR_STOP=1 -d terrace_documenso -c 'REVOKE ALL ON SCHEMA public FROM PUBLIC;'
      '';
    };
    systemd.services.terrace-documenso-image = {
      description = "Load the verified immutable Documenso image archive";
      serviceConfig =
        privateUnit
        // {
          Type = "oneshot";
          RemainAfterExit = true;
        };
      script = "exec ${pkgs.podman}/bin/podman load -i ${imageFile}";
    };
    systemd.services.terrace-documenso-migrate = {
      description = "Explicit Documenso migrations with writers stopped";
      conflicts = ["terrace-documenso.service"];
      requires = ["terrace-documenso-inputs.service" "terrace-documenso-database.service" "terrace-documenso-image.service"];
      after = ["terrace-documenso.service" "terrace-documenso-inputs.service" "terrace-documenso-database.service" "terrace-documenso-image.service"];
      serviceConfig =
        privateUnit
        // {
          Type = "oneshot";
          StateDirectory = "terrace-documenso";
          StateDirectoryMode = "0700";
          ExecStopPost = "-${pkgs.podman}/bin/podman rm --force --ignore terrace-documenso-migration";
        };
      script = ''
        set -euo pipefail
        ${offlineRun "terrace-documenso-migration" "${runtime}/migration.env" "/app/node_modules/prisma/build/index.js migrate deploy --schema /app/packages/prisma/schema.prisma"}
        printf '%s\n' '${pin.imageDigest}' >/var/lib/terrace-documenso/migrated-image
      '';
    };
    systemd.services.terrace-documenso-bootstrap = {
      description = "Idempotent approved Documenso account bootstrap";
      conflicts = ["terrace-documenso.service"];
      requires = ["terrace-documenso-inputs.service" "terrace-documenso-database.service" "terrace-documenso-image.service"];
      after = ["terrace-documenso.service" "terrace-documenso-inputs.service" "terrace-documenso-database.service" "terrace-documenso-image.service"];
      serviceConfig =
        privateUnit
        // {
          Type = "oneshot";
          StateDirectory = "terrace-documenso";
          StateDirectoryMode = "0700";
          ExecStopPost = "-${pkgs.podman}/bin/podman rm --force --ignore terrace-documenso-bootstrap";
        };
      script = ''
        set -euo pipefail
        test "$(cat /var/lib/terrace-documenso/migrated-image)" = '${pin.imageDigest}'
        input_hash=$(cat ${runtime}/inputs.sha256)
        ${offlineRun "terrace-documenso-bootstrap" "${runtime}/migration.env" "/run/terrace-bootstrap.cjs"}
        test "$(cat ${runtime}/inputs.sha256)" = "$input_hash"
        printf '%s\n%s\n' '${pin.imageDigest}' "$input_hash" >/var/lib/terrace-documenso/bootstrapped-image
      '';
    };
    virtualisation.oci-containers.containers.terrace-documenso = {
      serviceName = "terrace-documenso";
      inherit image imageFile;
      pull = "never";
      user = "1001:1001";
      workdir = "/app/apps/remix";
      entrypoint = "node";
      cmd = ["build/server/main.js"];
      environment =
        commonEnvironment
        // lib.optionalAttrs (cfg.identityCaFile != null) {NODE_EXTRA_CA_CERTS = "/run/terrace-oidc-ca.pem";}
        // lib.optionalAttrs cfg.enableSigning {NEXT_PRIVATE_SIGNING_LOCAL_FILE_PATH = "/run/terrace-signing.p12";};
      environmentFiles = ["${runtime}/runtime.env"] ++ lib.optional cfg.enableSigning "${runtime}/signing.env";
      volumes =
        ["/run/postgresql:/run/postgresql:ro"]
        ++ lib.optional (cfg.identityCaFile != null) "${runtime}/identity-ca.pem:/run/terrace-oidc-ca.pem:ro"
        ++ lib.optional cfg.enableSigning "${runtime}/signing.p12:/run/terrace-signing.p12:ro";
      ports = ["127.0.0.1:${toString cfg.port}:3000"];
      log-driver = "none";
      extraOptions = ["--init" "--cap-drop=ALL" "--security-opt=no-new-privileges" "--read-only" "--tmpfs=/tmp:rw,nosuid,nodev,size=64m" "--memory=2g" "--cpus=2" "--pids-limit=256" "--add-host=${cfg.identityHostname}:host-gateway"];
    };
    systemd.services.terrace-documenso = {
      requires = ["terrace-documenso-inputs.service" "terrace-documenso-database.service"];
      after = ["terrace-documenso-inputs.service" "terrace-documenso-database.service"];
      serviceConfig =
        privateUnit
        // {
          MemoryMax = "2304M";
          CPUQuota = "200%";
          TasksMax = 320;
          TimeoutStartSec = lib.mkForce 180;
          TimeoutStopSec = lib.mkForce 30;
          SuccessExitStatus = [143];
        };
      preStart = lib.mkBefore ''
        test "$(cat /var/lib/terrace-documenso/migrated-image)" = '${pin.imageDigest}'
        ${pkgs.bun}/bin/bun ${./documenso/inputs.ts} --check ${lib.escapeShellArg "${cfg.inputsDirectory}/secrets.json"} ${runtime} /var/lib/terrace-documenso/bootstrapped-image '${pin.imageDigest}'
        ${lib.optionalString cfg.enableSigning "${pkgs.bun}/bin/bun ${./documenso/signing-inputs.ts} check ${lib.escapeShellArg cfg.inputsDirectory} ${runtime}"}
      '';
      postStart = ''
        ${pkgs.curl}/bin/curl --fail --silent --output /dev/null --connect-timeout 2 --max-time 5 --retry 30 --retry-connrefused --retry-delay 2 http://127.0.0.1:${toString cfg.port}/api/health 2>/dev/null
        ${lib.optionalString cfg.enableSigning ''
          ${pkgs.curl}/bin/curl --fail --silent --connect-timeout 2 --max-time 5 http://127.0.0.1:${toString cfg.port}/api/health 2>/dev/null |
            ${pkgs.jq}/bin/jq -e '.checks.certificate.status == "ok"' >/dev/null 2>&1
        ''}
      '';
    };
    services.nginx = {
      enable = true;
      recommendedTlsSettings = true;
      virtualHosts.${cfg.hostname} = {
        onlySSL = true;
        sslCertificate = "/run/credentials/nginx.service/terrace-documenso-cert";
        sslCertificateKey = "/run/credentials/nginx.service/terrace-documenso-key";
        extraConfig = ''
          access_log off;
          client_max_body_size 10m;
          if ($host != ${cfg.hostname}) { return 404; }
        '';
        locations."/" = {
          proxyPass = "http://127.0.0.1:${toString cfg.port}";
          recommendedProxySettings = false;
          extraConfig = ''
            proxy_set_header Host ${cfg.hostname};
            proxy_set_header X-Forwarded-Host ${cfg.hostname};
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_set_header Forwarded "";
            proxy_no_cache 1;
            proxy_cache_bypass 1;
          '';
        };
        # Per-user OIDC is public; break-glass password routes stay localhost-only.
        locations."~* \"^/api/auth/email-password([/.;]|$)\"".return = "403";
        locations."= /signup".return = "403";
        locations."~* \"^/(forgot-password|reset-password|signup)([/.;]|$)\"".return = "403";
      };
    };
    systemd.services.nginx.serviceConfig.LoadCredential = [
      "terrace-documenso-cert:${cfg.certificateFile}"
      "terrace-documenso-key:${cfg.certificateKeyFile}"
    ];
    networking.firewall.allowedTCPPorts = [443];
    terrace.backup = lib.mkIf config.terrace.backup.enable {
      postgresql.databases = [role];
      extraPaths.documenso-inputs = cfg.inputsDirectory;
      extraPaths.documenso-state = "/var/lib/terrace-documenso";
    };
  };
}
