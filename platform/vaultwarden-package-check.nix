let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  release = import ./vaultwarden/release.nix {inherit pkgs;};
in
  pkgs.runCommand "terrace-vaultwarden-release-proof-${release.pin.version}" {
    nativeBuildInputs = [pkgs.coreutils pkgs.gnugrep];
  } ''
    mkdir -m 0700 "$TMPDIR/empty-inputs"
    env -i ENV_FILE=/dev/null CONFIG_FILE="$TMPDIR/empty-inputs/absent.json" \
      DATA_FOLDER="$TMPDIR/empty-inputs" \
      WEB_VAULT_FOLDER=${release.webvault}/share/vaultwarden/vault \
      ${release.server}/bin/vaultwarden --version > "$out"
    grep -Fxq 'Vaultwarden ${release.pin.version}' "$out"
    grep -Fxq -e 'Web-Vault ${release.pin.webvaultVersion}' -e 'Web-Vault v${release.pin.webvaultVersion}' "$out"
    test -s ${release.webvault}/share/vaultwarden/vault/index.html
  ''
