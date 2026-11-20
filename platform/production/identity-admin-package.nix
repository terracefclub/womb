# terrace-identity-admin for the production app host: Python standard library plus the
# exact tools it calls, with a fixed PATH (nothing from the caller's environment). The
# launcher is an ELF binary wrapper: NixOS security.wrappers refuses script targets.
{
  lib,
  stdenvNoCC,
  makeBinaryWrapper,
  python3,
  jq,
  coreutils,
  systemd,
  util-linux,
  postgresql,
}:
stdenvNoCC.mkDerivation {
  pname = "terrace-identity-admin";
  version = "1";
  dontUnpack = true;
  nativeBuildInputs = [makeBinaryWrapper];
  installPhase = ''
    install -Dm0644 ${./terrace-identity-admin} $out/libexec/terrace-identity-admin/terrace-identity-admin.py
    makeWrapper ${python3}/bin/python3 $out/bin/terrace-identity-admin \
      --add-flags "-I -B $out/libexec/terrace-identity-admin/terrace-identity-admin.py" \
      --set PATH ${lib.makeBinPath [jq coreutils systemd util-linux postgresql]}
  '';
  meta.mainProgram = "terrace-identity-admin";
}
