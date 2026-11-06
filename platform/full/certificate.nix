# Synthetic VM trust only. Never an ACME account or production certificate.
{
  pkgs,
  hostnames,
}:
assert hostnames != [] && builtins.length hostnames <= 16;
assert builtins.all (name: builtins.isString name && builtins.stringLength name <= 253 && builtins.match "[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?([.][a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+" name != null) hostnames;
assert pkgs.lib.unique hostnames == hostnames;
  pkgs.runCommand "terrace-rehearsal-certificate" {nativeBuildInputs = [pkgs.openssl];} ''
    mkdir $out
    openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 3650 \
      -keyout ca-key.pem -out $out/ca.pem -subj /CN=TerraceRehearsalSyntheticCA \
      -addext basicConstraints=critical,CA:TRUE \
      -addext keyUsage=critical,keyCertSign,cRLSign >/dev/null 2>&1
    openssl req -new -nodes -newkey rsa:2048 -sha256 -keyout $out/key.pem \
      -out request.pem -subj /CN=${builtins.head hostnames} >/dev/null 2>&1
    cat >extensions <<'EXTENSIONS'
    basicConstraints=critical,CA:FALSE
    keyUsage=critical,digitalSignature,keyEncipherment
    extendedKeyUsage=serverAuth
    subjectAltName=${pkgs.lib.concatStringsSep "," (map (name: "DNS:${name}") hostnames)}
    EXTENSIONS
    openssl x509 -req -in request.pem -CA $out/ca.pem -CAkey ca-key.pem -CAcreateserial \
      -out $out/cert.pem -days 3650 -sha256 -extfile extensions >/dev/null 2>&1
    ${pkgs.lib.concatMapStringsSep "\n" (name: ''openssl verify -purpose sslserver -verify_hostname ${pkgs.lib.escapeShellArg name} -CAfile $out/ca.pem $out/cert.pem >/dev/null'') hostnames}
  ''
