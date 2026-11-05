{pkgs}: let
  pin = builtins.fromJSON (builtins.readFile ./release.json);
in
  pkgs.dockerTools.pullImage {
    inherit (pin) imageName imageDigest;
    sha256 = pin.archiveHash;
    finalImageName = pin.imageName;
    finalImageTag = pin.version;
    os = "linux";
    arch = "amd64";
  }
