# Test-only. Feeds a synthetic CA into the guest's system trust bundle WITHOUT a cached
# copy: security.pki would copy the CA into a separately cached bundle derivation, which a
# CA rebuilt with a new key under the same store path leaves stale. Instead this oneshot
# assembles the bundle at boot inside the guest, from the public roots and the CA's store
# path, and replaces the /etc bundle links with that file. Used only where a proof must
# keep exercising a product default that reads the system bundle (Vaultwarden's and
# Windmill's default identityCaFile). Never imported by a production profile.
{
  ca,
  units ? [],
}: {pkgs, ...}: {
  systemd.services.terrace-test-system-trust = {
    description = "Assemble the test system trust bundle at run time from the synthetic CA store path";
    wantedBy = ["multi-user.target"];
    requiredBy = units;
    before = ["multi-user.target"] ++ units;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -euo pipefail
      for file in /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt /etc/pki/tls/certs/ca-bundle.crt; do
        test -e "$file"
        cat ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt ${ca} >"$file.terrace-new"
        chmod 0444 "$file.terrace-new"
        mv -f "$file.terrace-new" "$file"
      done
    '';
  };
}
