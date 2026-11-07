let
  platform = builtins.getFlake "path:${toString ../../platform}";
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  python = pkgs.python3.withPackages (packages: [packages.playwright]);
  toolPath = pkgs.lib.makeBinPath [pkgs.coreutils pkgs.openssh (pkgs.tesseract.override {enableLanguages = ["eng"];})];
  sources = pkgs.runCommand "terrace-console-fingerprint-source" {} ''
    mkdir -p "$out/infra/nixos" "$out/infra/cloud"
    cp ${./console_fingerprint.py} "$out/infra/nixos/console_fingerprint.py"
    cp ${./rescue_install.py} "$out/infra/nixos/rescue_install.py"
    cp ${../cloud/rehearsal_lease.py} "$out/infra/cloud/rehearsal_lease.py"
  '';
in
  pkgs.writeShellApplication {
    name = "terrace-console-fingerprint";
    text = ''
      umask 077
      scratch=$(${pkgs.coreutils}/bin/mktemp -d /tmp/terrace-console.XXXXXXXX)
      trap '${pkgs.coreutils}/bin/rm -rf -- "$scratch"' EXIT
      if ${pkgs.coreutils}/bin/env -i PATH=${toolPath} LANG=C.UTF-8 HOME="$scratch" TMPDIR="$scratch" \
          SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt \
          TERRACE_NOVNC_ROOT=${pkgs.novnc}/share/webapps/novnc \
          TERRACE_CONSOLE_BROWSER=${pkgs.chromium}/bin/chromium \
          timeout --kill-after=5 110 ${python}/bin/python3 -B ${sources}/infra/nixos/console_fingerprint.py "$@" \
          >/dev/null 2>&1; then
        echo 'Provider console authenticated the exact Rescue SSH host key.'
      else
        status=$?
        echo 'Rescue console authentication failed. Private diagnostics suppressed.' >&2
        exit "$status"
      fi
    '';
  }
