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
      export HCLOUD_TOKEN="$(cat "${config.devenv.root}/secrets/hcloud_token.txt")"
      export WINDMILL_TOKEN="$(cat "${config.devenv.root}/secrets/windmill_token.txt")"

      if ! gcloud auth application-default print-access-token &>/dev/null; then
        echo "Google Cloud application-default credentials not found or expired. Logging in..."
        gcloud auth application-default login \
          --scopes=https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/cloud-platform
      fi
      export GOOGLE_CLOUD_QUOTA_PROJECT="tfc-womb"
      if ! gcloud auth print-access-token &>/dev/null; then
        echo "Google Cloud credentials not found or expired. Logging in..."
        gcloud auth login
      fi
      export CLOUDSDK_CORE_PROJECT="tfc-womb"

      export PULUMI_ACCESS_TOKEN="$(cat "${config.devenv.root}/secrets/pulumi_token.txt")"
      pulumi stack select prod
    '';
    languages.python.enable = true;
    languages.yaml.enable = true;
    editors.zed.enable = true;
    editors.helix.enable = true;
    # TODO incorporate into SDLC
    git-hooks.excludes = ["windmill"];
    git-hooks.hooks.no-commit-to-branch.settings.branch = lib.mkForce [];
    # TODO why do I have to set this?
    treefmt.config.settings.formatter.dprint.options = ["--allow-no-files"];

    # Only way to download windmill-cli right now unfortunately without entire application package
    languages.javascript = {
      enable = true;
      bun.enable = true;
      bun.install.enable = true;
    };
  };
}
