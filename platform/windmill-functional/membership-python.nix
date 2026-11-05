{pkgs}: let
  inherit (pkgs) lib;
  pins = builtins.fromJSON (builtins.readFile ./membership-wheels.json);
  lock =
    lib.filter (line: line != "" && !(lib.hasPrefix "#" line))
    (lib.splitString "\n" (builtins.readFile ./membership-script.lock));
  valid = row:
    builtins.match "[A-Za-z0-9_.-]+\\.whl" row.filename
    != null
    && builtins.match "https://files\\.pythonhosted\\.org/packages/[A-Za-z0-9/_.-]+" row.url != null
    && builtins.match "[a-f0-9]{64}" row.sha256 != null
    && row.size > 0
    && row.size <= 32 * 1024 * 1024;
  sources =
    map (row: {
      inherit row;
      source = pkgs.fetchurl {
        inherit (row) url;
        name = row.filename;
        sha256 = row.sha256;
      };
    })
    pins.wheels;
  installer = pkgs.python312.withPackages (ps: [ps.installer]);
in
  assert lib.assertMsg (pins.schema
    == 1
    && pins.python == "3.12"
    && builtins.length pins.wheels == 20
    && map (row: "${row.name}==${row.version}") pins.wheels == lock
    && lib.all valid pins.wheels)
  "Windmill membership wheels must match the reviewed exact Python lock.";
    pkgs.runCommand "terrace-windmill-membership-python312" {nativeBuildInputs = [installer];} ''
      mkdir -p "$TMPDIR/wheel-sources"
      ${lib.concatMapStringsSep "\n" ({
        row,
        source,
      }: "ln -s ${source} \"$TMPDIR/wheel-sources/${row.filename}\"")
      sources}
      python -m installer --destdir "$TMPDIR/installed" --prefix / --no-compile-bytecode --validate-record all "$TMPDIR"/wheel-sources/*.whl
      test -d "$TMPDIR/installed/lib/python3.12/site-packages"
      mkdir -p "$out"
      cp -R "$TMPDIR/installed/lib/python3.12/site-packages"/. "$out/"
    ''
