# Rehearsal-only harness for the production relay role: the installer stand-in
# and the marker the production profile refuses. Nothing else differs.
{
  imports = [./rehearsal-stage.nix];
  terrace.production.rehearsalHarness = true;
}
