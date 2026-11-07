{pkgs}:
pkgs.dockerTools.buildImage {
  name = "terrace-persistence-probe";
  tag = "v1";
  copyToRoot = pkgs.buildEnv {
    name = "terrace-rehearsal-probe-root";
    paths = [pkgs.pkgsStatic.busybox];
    pathsToLink = ["/bin"];
  };
}
