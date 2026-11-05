{image}: [
  # Podman U sets tmpfs ownership from the container user, without recursively
  # changing the separate read-only image subpath. Explicit /tmp totals 512MiB.
  "--tmpfs=/tmp:rw,nodev,nosuid,mode=1777,size=128m,notmpcopyup"
  "--mount=type=tmpfs,destination=/tmp/windmill,tmpfs-mode=0700,tmpfs-size=128m,U=true,notmpcopyup,nodev,nosuid"
  "--mount=type=tmpfs,destination=/tmp/windmill/cache,tmpfs-mode=0700,tmpfs-size=256m,U=true,notmpcopyup,nodev,nosuid"
  "--mount=type=image,source=${image},destination=/tmp/windmill/cache/py_runtime,subpath=/tmp/windmill/cache/py_runtime,rw=false"
]
