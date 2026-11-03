{
  pkgs,
  webArchive,
  cmsArchive,
}: let
  pin = builtins.fromJSON (builtins.readFile ./website-release.json);
  bundle = component: archive:
    pkgs.runCommand "terrace-${component}-${builtins.substring 0 12 pin.revision}" {
      nativeBuildInputs = [pkgs.coreutils pkgs.gnutar pkgs.gzip pkgs.jq];
      # Reject a mistaken private input before copying any bytes into the store.
      inputArchive =
        if builtins.hashFile "sha256" archive != pin.${component}
        then throw "Terrace ${component} archive hash mismatch; use the reviewed secret-free release."
        else
          builtins.path {
            path = archive;
            name = "terrace-${component}.tar.gz";
          };
    } ''
      printf '%s  %s\n' '${pin.${component}}' "$inputArchive" | sha256sum --check --status
      mkdir -p "$out"
      tar -xzf "$inputArchive" -C "$out"
      jq -e '.schemaVersion == 1 and .component == "${component}" and .revision == "${pin.revision}" and
        .runtime.node == "${pin.node}" and .runtime.platform == "linux" and .runtime.arch == "x64" and
        .runtime.libc == "glibc" and .entrypoints.start == "bin/start"' "$out/release.json" >/dev/null
      (cd "$out"; jq -r '.files | to_entries[] | "\(.value)  \(.key)"' release.json | sha256sum --check --status)
      test -x "$out/bin/start"
      ${pkgs.lib.optionalString (component == "cms") ''
        test -x "$out/bin/migrate"
        test -d "$out/.next/cache"
        test -z "$(ls -A "$out/.next/cache")"
      ''}
    '';
in {
  inherit (pin) revision;
  web = bundle "web" webArchive;
  cms = bundle "cms" cmsArchive;
}
