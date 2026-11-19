# Evaluation-check fixture: a private module whose path imitates nixpkgs'
# `nixos/modules/`. The production profile must still refuse its free-form sshd
# configuration (modules are trusted only under the pinned nixpkgs store path).
{services.openssh.extraConfig = "PermitTunnel yes";}
