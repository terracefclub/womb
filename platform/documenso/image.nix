{pkgs}: let
  pin = builtins.fromJSON (builtins.readFile ./image.json);
in
  pkgs.dockerTools.pullImage {
    inherit (pin) imageName imageDigest sha256;
    finalImageName = pin.imageName;
    finalImageTag = pin.version;
    os = "linux";
    arch = "amd64";
  }
