{pkgs, ...}: {
  # Disposable capture only: no relay/forward configuration, no provider.
  # The native app reaches the host through the private Podman bridge.
  systemd.services.documenso-test-mailpit = {
    serviceConfig = {
      ExecStart = "${pkgs.mailpit}/bin/mailpit --smtp 10.88.0.1:1025 --listen 127.0.0.1:8025 --database /var/lib/documenso-test-mailpit/mail.db --max 0 --disable-version-check --smtp-disable-rdns --smtp-allowed-recipients ^local-member@terrace[.]invalid$ --smtp-max-recipients 1 --quiet";
      StateDirectory = "documenso-test-mailpit";
      StateDirectoryMode = "0700";
      UMask = "0077";
      MemoryMax = "128M";
      CPUQuota = "100%";
      TasksMax = 64;
      TimeoutStartSec = 30;
      TimeoutStopSec = 10;
      NoNewPrivileges = true;
      StandardOutput = "null";
      StandardError = "null";
    };
  };
}
