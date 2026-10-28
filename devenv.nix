{
  config,
  pkgs,
  ...
}: {
  name = "terrace-womb";
  packages = with pkgs; [git glab jq curl openssh kubectl hcloud restic gitleaks pulumi];
  languages.javascript = {
    enable = true;
    bun.enable = true;
  };
  languages.python = {
    enable = true;
    uv.enable = true;
  };
  env = {
    CLOUDSDK_CONFIG = "${config.devenv.state}/gcloud";
    PULUMI_HOME = "${config.devenv.state}/pulumi";
    GLAB_CONFIG_DIR = "${config.devenv.state}/glab";
    DOCKER_CONFIG = "${config.devenv.state}/docker";
    REGISTRY_AUTH_FILE = "${config.devenv.state}/containers/auth.json";
  };
  git-hooks.hooks = {
    check-merge-conflicts.enable = true;
    check-added-large-files.enable = true;
    end-of-file-fixer = {
      enable = true;
      excludes = ["^windmill/"];
    };
    trim-trailing-whitespace = {
      enable = true;
      excludes = ["^windmill/"];
    };
    alejandra.enable = true;
    gitleaks-staged = {
      enable = true;
      name = "gitleaks (staged changes, redacted)";
      entry = "${pkgs.gitleaks}/bin/gitleaks git --pre-commit --staged --redact --no-banner";
      pass_filenames = false;
      always_run = true;
    };
  };
  scripts.check-shell.exec = ''
    set -eu
    command -v git >/dev/null
    command -v glab >/dev/null
    command -v bun >/dev/null
    command -v kubectl >/dev/null
    command -v pulumi >/dev/null
    test "$CLOUDSDK_CONFIG" = "${config.devenv.state}/gcloud"
    test "$PULUMI_HOME" = "${config.devenv.state}/pulumi"
    test "$GLAB_CONFIG_DIR" = "${config.devenv.state}/glab"
    test -e "${config.devenv.root}/.pre-commit-config.yaml"
    echo "PASS: tools, project-local state, and hooks are available"
  '';
  enterTest = config.scripts.check-shell.exec;
}
