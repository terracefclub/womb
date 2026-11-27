#!/usr/bin/env bash
# Confinement probe for the throwaway SFTP endpoint (infra/cloud/rehearsal_sftp.py), run on the build machine.
#
# Usage: rehearsal_sftp_probe.sh <run-dir> <client-private-key> <evidence-dir> <openssh-bin-dir> <restic>
# The endpoint must already be prepared and started for <client-private-key>'s public half. The script only reads
# the receipt, plants synthetic files OUTSIDE the repository, then records (in <evidence-dir>) that the permitted
# restic cycle works and that every probe a holder of the key could try is refused. It prints no key material.
#
# Layer mode (LAYER=1, APPDIR=<repository dir>): the server was started by hand WITHOUT the namespace, serving the
# directory that contains APPDIR, to show that the SFTP server's own rooting confines the client independently of
# the mount namespace. The restic cycle is skipped; creations inside the served root are expected and listed.
set -u
RUN=$1 KEY=$2 EVID=$3 SSHBIN=$4 RESTIC=$5
umask 077
mkdir -p "$EVID"
PORT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["port"])' "$RUN/sftp-listener.json")
HOSTKEY=$(cat "$RUN/sftp-host-key.pub")
KH="$EVID/known_hosts"; echo "[127.0.0.1]:$PORT $HOSTKEY" > "$KH"
export PATH="$SSHBIN:$PATH"
OUT="$EVID/probe.log"; : > "$OUT"
say() { printf '%s\n' "$*" | tee -a "$OUT"; }
SSHO=(-i "$KEY" -p "$PORT" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KH" -o ConnectTimeout=10)
SFTPO=(-i "$KEY" -P "$PORT" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KH" -o ConnectTimeout=10)
APPDIR=${APPDIR:-$RUN/app}; LAYER=${LAYER:-0}
PROBEWORK=$(mktemp -d "$EVID/work.XXXXXX")
OUTSIDE="$EVID/outside-secret.txt"; echo "synthetic outside secret $RANDOM$RANDOM" > "$OUTSIDE"
OUTSIDE_SUM=$(sha256sum "$OUTSIDE" | cut -d' ' -f1)
ESCAPE="$EVID/outside-write-target"; rm -rf "$ESCAPE"
FAIL=0
expect_fail() { # name, then command: must exit non-zero
  local name=$1; shift
  local o; o=$(timeout 40 "$@" 2>&1 </dev/null); local rc=$?
  say "--- $name: exit $rc"; say "$(printf '%s' "$o" | head -20)"
  if [ $rc -eq 0 ]; then say "!!! UNEXPECTED SUCCESS: $name"; FAIL=1; fi
}
leak_check() { # name, file that must not contain the outside secret
  if grep -q "synthetic outside secret" "$2" 2>/dev/null; then say "!!! LEAK: $1"; FAIL=1; else say "no leak: $1"; fi
}

say "== endpoint: port $PORT, host key $HOSTKEY"
say "== restic cycle through the endpoint (production invocation shape)"
printf 'synthetic-repository-password\n' > "$PROBEWORK/pw"
export RESTIC_PASSWORD_FILE="$PROBEWORK/pw" RESTIC_CACHE_DIR="$PROBEWORK/cache"
REPO="sftp:terrace@127.0.0.1:app"
ROPT=(-o "sftp.args=-i $KEY -p $PORT -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KH -o ServerAliveInterval=30 -o ServerAliveCountMax=10")
mkdir -p "$PROBEWORK/data/sub"; head -c 3000000 /dev/urandom > "$PROBEWORK/data/blob"; echo hello > "$PROBEWORK/data/sub/note"
( cd "$PROBEWORK/data" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) > "$EVID/data.sha256"
r() { say "\$ restic $1"; "$RESTIC" "${ROPT[@]}" -r "$REPO" "$@" >> "$OUT" 2>&1; local rc=$?; say "exit $rc"; [ $rc -eq 0 ] || FAIL=1; }
if [ "$LAYER" != 1 ]; then
r init
r backup "$PROBEWORK/data"
r snapshots
r check
r restore latest --target "$PROBEWORK/restored"
( cd "$PROBEWORK/restored$PROBEWORK/data" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) > "$EVID/restored.sha256"
if cmp -s "$EVID/data.sha256" "$EVID/restored.sha256"; then say "restore checksums match"; else say "!!! restore mismatch"; FAIL=1; fi
fi

say "== probes that must fail"
expect_fail "shell request (ssh -tt, no command)" ssh -tt "${SSHO[@]}" terrace@127.0.0.1
expect_fail "exec request: id" ssh "${SSHO[@]}" terrace@127.0.0.1 id
expect_fail "exec request: cat outside file" ssh "${SSHO[@]}" terrace@127.0.0.1 cat "$OUTSIDE"
expect_fail "exec request: sh -c" ssh "${SSHO[@]}" terrace@127.0.0.1 sh -c 'echo pwned'

# -L: forward to the endpoint's own port; if forwarding were allowed a connection would return an SSH banner.
LP=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
timeout 12 ssh -N -L "127.0.0.1:$LP:127.0.0.1:$PORT" "${SSHO[@]}" terrace@127.0.0.1 >"$PROBEWORK/L.out" 2>&1 </dev/null &
LPID=$!; sleep 3
GOT=$(python3 - "$LP" <<'E'
import socket, sys
try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=4) as c:
        c.settimeout(4); print(repr(c.recv(32)))
except OSError as e:
    print("error", type(e).__name__)
E
)
wait $LPID 2>/dev/null
say "--- -L forward through the key: connection result via the forward: $GOT"
case "$GOT" in *SSH-2*) say "!!! -L FORWARD WORKED"; FAIL=1;; *) say "-L forward carried no data (refused)";; esac
say "$(head -5 "$PROBEWORK/L.out")"

RP=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
expect_fail "-R forward (ExitOnForwardFailure)" timeout 12 ssh -N -o ExitOnForwardFailure=yes -R "127.0.0.1:$RP:127.0.0.1:$PORT" "${SSHO[@]}" terrace@127.0.0.1
if python3 -c 'import socket,sys;s=socket.socket();s.settimeout(1);sys.exit(0 if s.connect_ex(("127.0.0.1",int(sys.argv[1]))) == 0 else 1)' "$RP"; then say "!!! -R port $RP is listening"; FAIL=1; else say "-R port $RP is not listening"; fi
expect_fail "agent forwarding + X11 (-A -X with a command)" ssh -A -X "${SSHO[@]}" terrace@127.0.0.1 true

say "== reads outside the repository (sftp)"
for target in "$OUTSIDE" /etc/passwd ../outside-secret.txt ../../../../etc/passwd "app/../../outside-secret.txt"; do
  rm -f "$PROBEWORK/got"
  printf 'get %s %s\n' "$target" "$PROBEWORK/got" | timeout 40 sftp "${SFTPO[@]}" -b - terrace@127.0.0.1 >"$PROBEWORK/sftp.out" 2>&1; rc=$?
  say "--- get $target: exit $rc; $(tr '\n' ' ' < "$PROBEWORK/sftp.out" | head -c 300)"
  if [ -s "$PROBEWORK/got" ]; then
    if [ "$target" = /etc/passwd ] || [ "$target" = ../../../../etc/passwd ]; then
      if grep -q '^root:' "$PROBEWORK/got"; then say "!!! LEAK: real /etc/passwd served"; FAIL=1; fi
    fi
    leak_check "get $target" "$PROBEWORK/got"
  fi
done
say "--- listing of the served root and of its parent, as the client sees them"
printf 'ls -la /\nls -la ..\nls -la /app\npwd\n' | timeout 40 sftp "${SFTPO[@]}" -b - terrace@127.0.0.1 2>&1 | tee -a "$OUT" | head -30 >/dev/null

say "== symlinks"
ln -s "$OUTSIDE" "$PROBEWORK/mylink"
printf 'symlink %s app/link-abs\nsymlink ../outside-secret.txt app/link-rel\nsymlink / app/link-root\n' "$OUTSIDE" | timeout 40 sftp "${SFTPO[@]}" -b - terrace@127.0.0.1 >"$PROBEWORK/sftp.out" 2>&1
say "--- client-created symlinks: $(tr '\n' ' ' < "$PROBEWORK/sftp.out" | head -c 400)"
for l in link-abs link-rel link-root; do
  if [ -L "$APPDIR/$l" ]; then say "!!! symlink $l exists on disk"; FAIL=1; else say "symlink $l was not created on disk"; fi
done
# Stronger: a symlink planted by the operator inside the repository (the client cannot make one) must not be followed.
ln -s "$OUTSIDE" "$APPDIR/planted-link"
ln -s "$EVID" "$APPDIR/planted-dir"
for target in app/planted-link app/planted-dir/outside-secret.txt; do
  rm -f "$PROBEWORK/got"
  printf 'get %s %s\n' "$target" "$PROBEWORK/got" | timeout 40 sftp "${SFTPO[@]}" -b - terrace@127.0.0.1 >"$PROBEWORK/sftp.out" 2>&1; rc=$?
  say "--- get planted $target: exit $rc; $(tr '\n' ' ' < "$PROBEWORK/sftp.out" | head -c 300)"
  [ -s "$PROBEWORK/got" ] && leak_check "planted $target" "$PROBEWORK/got"
done
rm -f "$APPDIR/planted-link" "$APPDIR/planted-dir"

say "== writes outside the repository"
echo "client wrote this" > "$PROBEWORK/payload"
for dest in "$ESCAPE" ../escape-write /tmp/escape-write-$PORT "app/../../escape-write2" /escape-root-write; do
  printf 'put %s %s\n' "$PROBEWORK/payload" "$dest" | timeout 40 sftp "${SFTPO[@]}" -b - terrace@127.0.0.1 >"$PROBEWORK/sftp.out" 2>&1; rc=$?
  say "--- put $dest: exit $rc; $(tr '\n' ' ' < "$PROBEWORK/sftp.out" | head -c 300)"
done
printf 'mkdir ../escape-dir\nmkdir /escape-dir-abs\nrename app/config ../moved\nchmod 777 /\n' | timeout 40 sftp "${SFTPO[@]}" -b - terrace@127.0.0.1 >"$PROBEWORK/sftp.out" 2>&1
say "--- mkdir/rename outside: $(tr '\n' ' ' < "$PROBEWORK/sftp.out" | head -c 400)"
ROOTD=$(dirname "$APPDIR")  # the served root: client paths such as ../x and /x land here at most
OUTER=$(dirname "$ROOTD")   # anything here or elsewhere on the machine would be an escape
CHECK=("$ESCAPE" "$ROOTD/../escape-write" "$OUTER/escape-write" "$OUTER/escape-write2" "$OUTER/escape-dir" "$OUTER/moved" "/tmp/escape-write-$PORT" /escape-root-write /escape-dir-abs "$RUN/../escape-write")
if [ "$LAYER" != 1 ]; then CHECK+=("$ROOTD/escape-write" "$ROOTD/escape-write2" "$ROOTD/escape-dir" "$ROOTD/moved"); fi
for p in "${CHECK[@]}"; do
  if [ -e "$p" ] || [ -L "$p" ]; then say "!!! found on the builder: $p"; FAIL=1; fi
done
say "no probe wrote outside the served root (run directory holds: $(ls "$RUN" | tr '\n' ' '))"
if [ "$LAYER" = 1 ]; then say "inside the served root (expected in layer mode): $(ls -A "$ROOTD" | tr '\n' ' ')"; fi
say "outside secret unchanged: $([ "$(sha256sum "$OUTSIDE" | cut -d' ' -f1)" = "$OUTSIDE_SUM" ] && echo yes || echo NO)"

say "== hard links, renames out of the root, the endpoint's own key files, other usernames, other subsystems"
printf 'ln %s app/hl-abs\nln app/config ../hl-out\nln app/config /hl-root\nrename app/config ../moved-out\nrename app/config /moved-root\n' "$OUTSIDE" | timeout 40 sftp "${SFTPO[@]}" -b - terrace@127.0.0.1 >"$PROBEWORK/sftp.out" 2>&1
say "--- hard link and rename attempts: $(tr '\n' ' ' < "$PROBEWORK/sftp.out" | head -c 600)"
for p in "$APPDIR/hl-abs" "$OUTER/hl-out" "$OUTER/moved-out" /hl-root /moved-root; do
  if [ -e "$p" ] || [ -L "$p" ]; then say "!!! found on the builder: $p"; FAIL=1; fi
done
if [ "$LAYER" != 1 ] && [ -e "$ROOTD/hl-out" -o -e "$ROOTD/moved-out" -o ! -e "$APPDIR/config" ]; then say "!!! hard link or rename took effect"; FAIL=1; fi
for target in "$RUN/sftp-host-key" "$RUN/sftp-authorized-keys" "$RUN/sftp-listener.json" /proc/self/environ /proc/self/cmdline; do
  rm -f "$PROBEWORK/got"
  printf 'get %s %s\n' "$target" "$PROBEWORK/got" | timeout 40 sftp "${SFTPO[@]}" -b - terrace@127.0.0.1 >"$PROBEWORK/sftp.out" 2>&1; rc=$?
  say "--- get $(basename "$target"): exit $rc; $(tr '\n' ' ' < "$PROBEWORK/sftp.out" | head -c 200 | sed "s|$RUN|RUN|g")"
  if [ -s "$PROBEWORK/got" ]; then say "!!! LEAK: $target served"; FAIL=1; fi
done
for u in root nobody terrace2; do
  out=$(printf 'ls -a /\nget /etc/passwd %s\n' "$PROBEWORK/got" | timeout 40 sftp "${SFTPO[@]}" -b - "$u@127.0.0.1" 2>&1 | tr '\n' ' ' | head -c 300)
  say "--- as user $u (any name authenticates with the one key; same root): $out"
  if [ -s "$PROBEWORK/got" ]; then say "!!! LEAK as $u"; FAIL=1; fi
done
expect_fail "other subsystem (netconf)" ssh -s "${SSHO[@]}" terrace@127.0.0.1 netconf
expect_fail "other subsystem (shell)" ssh -s "${SSHO[@]}" terrace@127.0.0.1 shell

say "== authentication"
ssh-keygen -q -t ed25519 -N "" -f "$PROBEWORK/other" -C unlisted-probe >/dev/null
expect_fail "second, unlisted key" ssh -i "$PROBEWORK/other" -p "$PORT" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KH" -o ConnectTimeout=10 terrace@127.0.0.1 true
printf '#!/bin/sh\necho not-the-password\n' > "$PROBEWORK/askpass"; chmod 700 "$PROBEWORK/askpass"
expect_fail "password authentication" env SSH_ASKPASS="$PROBEWORK/askpass" SSH_ASKPASS_REQUIRE=force DISPLAY=none ssh -p "$PORT" -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive -o NumberOfPasswordPrompts=2 -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KH" -o ConnectTimeout=10 terrace@127.0.0.1 true
expect_fail "no authentication (none)" ssh -p "$PORT" -o PreferredAuthentications=none -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KH" -o ConnectTimeout=10 terrace@127.0.0.1 true
say "--- server's offered methods and host key algorithms (ssh -vv)"
ssh -vv -p "$PORT" -o PreferredAuthentications=none -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KH" -o ConnectTimeout=10 terrace@127.0.0.1 true 2>&1 | grep -E "Authentications that can continue|peer server KEXINIT proposal|host key algorithms:|Server host key" | cut -c1-200 | tee -a "$OUT"
expect_fail "host key other than ed25519 (HostKeyAlgorithms=ssh-rsa)" ssh -p "$PORT" -o HostKeyAlgorithms=ssh-rsa -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 terrace@127.0.0.1 true

rm -rf "$PROBEWORK"
if [ $FAIL -eq 0 ]; then say "RESULT: all permitted operations succeeded and every probe was refused"; else say "RESULT: FAILED (see !!! lines)"; fi
exit $FAIL
