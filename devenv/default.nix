{
  perSystem.canivete.devenv.shells.default = {
    config,
    lib,
    pkgs,
    ...
  }: let
    pulumi = pkgs.pulumi.withPackages (ps: [ps.pulumi-python]);
  in {
    packages = [pulumi pkgs.hcloud pkgs.google-cloud-sdk];
    enterShell = ''
      export PULUMI_ACCESS_TOKEN="$(cat "${config.devenv.root}/secrets/pulumi_token.txt")"
      export HCLOUD_TOKEN="$(cat "${config.devenv.root}/secrets/hcloud_token.txt")"
      ${lib.getExe pulumi} stack select prod
    '';
    languages.python.enable = true;
    languages.yaml.enable = true;
    editors.zed.enable = true;
    editors.helix.enable = true;
    git-hooks.hooks.no-commit-to-branch.settings.branch = lib.mkForce [];
    # TODO why do I have to set this?
    treefmt.config.settings.formatter.dprint.options = ["--allow-no-files"];
  };
}
