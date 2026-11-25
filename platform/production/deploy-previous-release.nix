# The PREVIOUS website release for the deploy VM proof (deploy-test.nix): the pin Womb carried before
# 5a3cfbc (website 680ad56, eleven migrations; deploy-previous-release.json is that pin, copied unchanged from
# Womb history). Verified like platform/website-release.nix, except that this release predates the operator
# tools (bin/roster-invite, bin/wp-import), so it is not checked for them. Proof input only; never a host's pin.
{
  pkgs,
  webArchive,
  cmsArchive,
}: let
  pin = builtins.fromJSON (builtins.readFile ./deploy-previous-release.json);
  bundle = component: archive:
    pkgs.runCommand "terrace-${component}-${builtins.substring 0 12 pin.revision}" {
      nativeBuildInputs = [pkgs.coreutils pkgs.gnutar pkgs.gzip pkgs.jq];
      inputArchive =
        if builtins.hashFile "sha256" archive != pin.${component}
        then throw "Terrace previous ${component} archive hash mismatch; use the kept ${builtins.substring 0 7 pin.revision} release."
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
        .runtime.node == "${pin.node}" and .entrypoints.start == "bin/start"' "$out/release.json" >/dev/null
      (cd "$out"; jq -r '.files | to_entries[] | "\(.value)  \(.key)"' release.json | sha256sum --check --status)
      test -x "$out/bin/start"
      ${pkgs.lib.optionalString (component == "cms") ''
        test -x "$out/bin/migrate"
        test -x "$out/bin/sweep"
      ''}
    '';
in {
  inherit (pin) revision migrations;
  web = bundle "web" webArchive;
  cms = bundle "cms" cmsArchive;
}
