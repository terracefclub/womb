# terrace-secrets for operators (via the flake's packages output) and hosts.
{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
  sops,
  age,
  ssh-to-age,
  openssh,
  openssl,
}:
stdenvNoCC.mkDerivation {
  pname = "terrace-secrets";
  version = "1";
  dontUnpack = true;
  nativeBuildInputs = [makeWrapper];
  installPhase = ''
    install -Dm0644 ${./terrace-secrets} $out/libexec/terrace-secrets/terrace-secrets.py
    install -Dm0644 ${./inventory.json} $out/share/terrace-secrets/inventory.json
    makeWrapper ${python3}/bin/python3 $out/bin/terrace-secrets \
      --add-flags "-I -B $out/libexec/terrace-secrets/terrace-secrets.py" \
      --set TERRACE_SECRETS_INVENTORY $out/share/terrace-secrets/inventory.json \
      --prefix PATH : ${lib.makeBinPath [sops age ssh-to-age openssh openssl]}
  '';
  meta.mainProgram = "terrace-secrets";
}
