{pkgs}: let
  pin = builtins.fromJSON (builtins.readFile ./release.json);
  archive = pkgs.fetchurl {
    url = "https://github.com/dani-garcia/bw_web_builds/releases/download/v${pin.webvaultVersion}/bw_web_v${pin.webvaultVersion}.tar.gz";
    hash = pin.webvaultArchiveHash;
  };
  webvault =
    pkgs.runCommand "vaultwarden-webvault-${pin.webvaultVersion}" {
      nativeBuildInputs = [pkgs.gnutar pkgs.gzip];
      meta = pkgs.vaultwarden.webvault.meta;
    } ''
      mkdir -p $out/share/vaultwarden/vault
      tar --extract --gzip --file ${archive} --no-same-owner --strip-components=1 --directory $out/share/vaultwarden/vault
      test -f $out/share/vaultwarden/vault/index.html
    '';
  server = (pkgs.vaultwarden.override {dbBackend = "postgresql";}).overrideAttrs (final: previous: {
    version = pin.version;
    src = pkgs.fetchFromGitHub {
      owner = "dani-garcia";
      repo = "vaultwarden";
      tag = final.version;
      hash = pin.sourceHash;
    };
    cargoHash = pin.cargoHash;
    cargoDeps = pkgs.rustPlatform.fetchCargoVendor {
      inherit (final) src;
      hash = pin.cargoHash;
    };
    env = (previous.env or {}) // {VW_VERSION = pin.version;};
    passthru = (previous.passthru or {}) // {inherit webvault;};
  });
in {inherit pin server webvault;}
