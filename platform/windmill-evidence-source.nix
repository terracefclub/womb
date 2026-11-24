# The Windmill evidence programs (./windmill) as copied into the proofs, without what .gitignore ignores below the
# repository root (__pycache__ the unit tests write, node_modules, result links). The same commit gives the same store
# path, so the same check derivations, whether or not those exist in the checkout; the name and content are those of a
# clean tree's ./windmill copy. tests/platform/test_clean_sources.py checks this with a planted cache.
builtins.path {
  path = ./windmill;
  name = "windmill";
  filter = path: _type: let
    name = baseNameOf path;
  in
    !(builtins.elem name ["__pycache__" "node_modules" "result"] || builtins.substring 0 7 name == "result-");
}
