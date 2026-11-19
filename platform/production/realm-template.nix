# The Keycloak realm as a sops-nix template file. Placeholders, never values,
# are passed to the hash-pinned TFC producer, which validates the origins.
{
  pkgs,
  contract,
  webOrigin,
  cmsOrigin,
  placeholders,
}:
pkgs.runCommand "terrace-realm-template.json" {
  nativeBuildInputs = [pkgs.bun pkgs.jq];
  credentials = builtins.toJSON placeholders;
  inherit webOrigin cmsOrigin;
} ''
  export HOME=$PWD
  cp -r ${contract}/. producer
  chmod -R u+w producer
  cp ${./realm-template.ts} producer/realm-template.ts
  bun --no-env-file producer/realm-template.ts "$webOrigin" "$cmsOrigin" "$credentials" >realm.json
  # The production realm may never allow registration, carry users, accept
  # plain HTTP or contain anything but the four reviewed placeholders as secrets.
  jq -e --argjson p "$credentials" '
    .registrationAllowed == false and .sslRequired == "all" and (has("users") | not)
    and ([.clients[].secret] | sort) == ([$p[]] | sort)
  ' realm.json >/dev/null
  mv realm.json $out
''
