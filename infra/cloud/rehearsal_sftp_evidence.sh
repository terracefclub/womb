#!/usr/bin/env bash
# Rerunnable evidence for the throwaway SFTP endpoint: everything the tests cannot show, on the build machine.
#
# Usage: rehearsal_sftp_evidence.sh <scratch-dir> <evidence-dir> <rclone> <openssh-bin-dir> <restic>
# <scratch-dir> must not exist (it is created 0700, should be on non-snapshotted scratch storage) and is deleted at
# the end, with the throwaway client key and every run directory in it. <evidence-dir> receives logs and public keys.
# Needs a systemd user manager and /var/empty. Takes about 7 minutes (it waits out a 300 s runtime limit).
# Its leftover-units checks are machine-wide (any `terrace-lease-sftp-*` unit of this user counts): run it with no
# other endpoint unit present, or it reports RESULT: FAIL.
set -u
SCRATCH=$1 EVID=$2 RCLONE=$3 OPENSSH=$4 RESTIC=$5
HERE=$(cd "$(dirname "$0")" && pwd)
H="python3 -B $HERE/rehearsal_sftp.py"
mkdir -m 700 "$SCRATCH" "$EVID" || exit 2
umask 077
LOG="$EVID/lifecycle.log"; : > "$LOG"
say() { printf '%s\n' "$*" | tee -a "$LOG"; }
mkdir -m 700 "$SCRATCH/client"
"$OPENSSH/ssh-keygen" -q -t ed25519 -N "" -f "$SCRATCH/client/id" -C throwaway-probe-client
cp "$SCRATCH/client/id.pub" "$EVID/client-public-key.pub"
newrun() { mkdir -m 700 "$SCRATCH/$1"; $H prepare --directory "$SCRATCH/$1" --ssh-keygen "$OPENSSH/ssh-keygen" --client-public-key "$SCRATCH/client/id.pub" >/dev/null; }
listener() { python3 -c 'import socket,sys
try: socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=2); print("STILL LISTENING")
except OSError as e: print("refused:", type(e).__name__)' "$1"; }
field() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }
leftovers() { systemctl --user list-units 'terrace-lease-sftp-*' --all --no-legend | awk '{print $1}' | tr '\n' ' '; }
FAIL=0

say "## A. start on a fresh run (namespace verified by start), probe, stop"
newrun runA
$H start --directory "$SCRATCH/runA" --rclone "$RCLONE" --seconds 900 --limit-mib 2048 | tee "$EVID/start-receipt.json" >> "$LOG"
cp "$SCRATCH/runA/sftp-host-key.pub" "$EVID/host-public-key.pub"
say "status: $($H status --directory "$SCRATCH/runA")"
say "forward option: $($H forward --directory "$SCRATCH/runA")"
UA=$(field "$SCRATCH/runA/sftp-listener.json" unit); PA=$(field "$SCRATCH/runA/sftp-listener.json" pid)
say "server pid $PA; /proc/$PA/root view of the served root: $(ls -A /proc/$PA/root/var/empty | tr '\n' ' ')"
say "/proc/$PA/root view of the run directory: $(ls -A /proc/$PA/root$(cd "$SCRATCH/runA" && pwd -P) | tr '\n' ' ')"
bash "$HERE/rehearsal_sftp_probe.sh" "$SCRATCH/runA" "$SCRATCH/client/id" "$EVID/probe-namespace" "$OPENSSH" "$RESTIC" >/dev/null 2>&1 || { say "!!! probe (namespace) FAILED"; FAIL=1; }
tail -1 "$EVID/probe-namespace/probe.log" | tee -a "$LOG"
$H stop --directory "$SCRATCH/runA" | tee -a "$LOG" || FAIL=1
say "after stop: $(systemctl --user is-active "$UA" 2>&1 | head -1); units left: [$(leftovers)]; $(listener "$(field "$SCRATCH/runA/sftp-listener.json" port)")"

say "## B. same probe with the namespace off (the server's own rooting only)"
newrun runB; mkdir -m 700 "$SCRATCH/runB/serve"; mv "$SCRATCH/runB/app" "$SCRATCH/runB/serve/app"
PB=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
echo "{\"port\": $PB}" > "$SCRATCH/runB/sftp-listener.json"
systemd-run --user --quiet --collect --unit=terrace-lease-sftp-layer-probe.service --property=RuntimeMaxSec=300 "$RCLONE" serve sftp ":local:$SCRATCH/runB/serve" --addr "127.0.0.1:$PB" --key "$SCRATCH/runB/sftp-host-key" --authorized-keys "$SCRATCH/runB/sftp-authorized-keys" --config /dev/null --vfs-cache-mode off --no-modtime --umask 077 --log-level ERROR
sleep 2
LAYER=1 APPDIR="$SCRATCH/runB/serve/app" bash "$HERE/rehearsal_sftp_probe.sh" "$SCRATCH/runB" "$SCRATCH/client/id" "$EVID/probe-no-namespace" "$OPENSSH" /bin/true >/dev/null 2>&1 || { say "!!! probe (no namespace) FAILED"; FAIL=1; }
tail -1 "$EVID/probe-no-namespace/probe.log" | tee -a "$LOG"
systemctl --user stop terrace-lease-sftp-layer-probe.service
say "after stop: units left: [$(leftovers)]"

say "## C. start refuses a server without the namespace"
newrun runC
python3 -B "$HERE/rehearsal_sftp_nonamespace_check.py" --directory "$SCRATCH/runC" --rclone "$RCLONE" 2>&1 | tee -a "$LOG"; [ "${PIPESTATUS[0]}" = 0 ] || FAIL=1

say "## D. size limit: 16 MiB limit, client writes 64 MiB"
newrun runD
$H start --directory "$SCRATCH/runD" --rclone "$RCLONE" --seconds 300 --limit-mib 16 | tee -a "$LOG" >/dev/null
PD=$(field "$SCRATCH/runD/sftp-listener.json" port); UD=$(field "$SCRATCH/runD/sftp-listener.json" unit)
head -c 67108864 /dev/zero > "$SCRATCH/big"
KH="$SCRATCH/kh"; echo "[127.0.0.1]:$PD $(cat "$SCRATCH/runD/sftp-host-key.pub")" > "$KH"
T0=$(date +%s.%N)
printf 'put %s app/big\n' "$SCRATCH/big" | timeout 120 "$OPENSSH/sftp" -i "$SCRATCH/client/id" -P "$PD" -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile="$KH" -b - terrace@127.0.0.1 >/dev/null 2>&1
say "client write ended with exit $? after $(python3 -c "import time;print(round(time.time()-$T0,1))") s"
for i in $(seq 1 15); do [ "$(systemctl --user is-active "$UD" 2>&1 | head -1)" = active ] || break; sleep 1; done
say "server unit $(systemctl --user is-active "$UD" 2>&1 | head -1) $(python3 -c "import time;print(round(time.time()-$T0,1))") s after the write started; units left: [$(leftovers)]; $(listener "$PD")"
say "limit marker: $(cat "$SCRATCH/runD/sftp-limit-tripped.json" | tr -d '\n ')"
say "status: $($H status --directory "$SCRATCH/runD")"
$H stop --directory "$SCRATCH/runD" | tee -a "$LOG" || FAIL=1

say "## D2. empty files count: 16 MiB limit, client creates 6000 empty files (about 0 bytes of data)"
newrun runF
$H start --directory "$SCRATCH/runF" --rclone "$RCLONE" --seconds 300 --limit-mib 16 | tee -a "$LOG" >/dev/null
PF=$(field "$SCRATCH/runF/sftp-listener.json" port); UF=$(field "$SCRATCH/runF/sftp-listener.json" unit)
KHF="$SCRATCH/khf"; echo "[127.0.0.1]:$PF $(cat "$SCRATCH/runF/sftp-host-key.pub")" > "$KHF"; : > "$SCRATCH/empty"
seq 1 6000 | sed "s|.*|put $SCRATCH/empty app/e&|" > "$SCRATCH/batch"
timeout 200 "$OPENSSH/sftp" -i "$SCRATCH/client/id" -P "$PF" -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile="$KHF" -b "$SCRATCH/batch" terrace@127.0.0.1 >/dev/null 2>&1
for i in $(seq 1 15); do [ "$(systemctl --user is-active "$UF" 2>&1 | head -1)" = active ] || break; sleep 1; done
say "server unit $(systemctl --user is-active "$UF" 2>&1 | head -1); units left: [$(leftovers)]; $(listener "$PF")"
say "limit marker: $(cat "$SCRATCH/runF/sftp-limit-tripped.json" | tr -d '\n ')"
$H stop --directory "$SCRATCH/runF" | tee -a "$LOG" || FAIL=1

say "## D3. entry cap: --max-entries 1000, client creates 3000 empty files"
newrun runG
$H start --directory "$SCRATCH/runG" --rclone "$RCLONE" --seconds 300 --max-entries 1000 | tee -a "$LOG" >/dev/null
PG=$(field "$SCRATCH/runG/sftp-listener.json" port); UG=$(field "$SCRATCH/runG/sftp-listener.json" unit)
KHG="$SCRATCH/khg"; echo "[127.0.0.1]:$PG $(cat "$SCRATCH/runG/sftp-host-key.pub")" > "$KHG"
seq 1 3000 | sed "s|.*|put $SCRATCH/empty app/e&|" > "$SCRATCH/batch"
timeout 200 "$OPENSSH/sftp" -i "$SCRATCH/client/id" -P "$PG" -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile="$KHG" -b "$SCRATCH/batch" terrace@127.0.0.1 >/dev/null 2>&1
for i in $(seq 1 15); do [ "$(systemctl --user is-active "$UG" 2>&1 | head -1)" = active ] || break; sleep 1; done
say "server unit $(systemctl --user is-active "$UG" 2>&1 | head -1); units left: [$(leftovers)]; $(listener "$PG")"
say "limit marker: $(cat "$SCRATCH/runG/sftp-limit-tripped.json" | tr -d '\n ')"
say "status: $($H status --directory "$SCRATCH/runG")"
$H stop --directory "$SCRATCH/runG" | tee -a "$LOG" || FAIL=1

say "## E. runtime limit: 300 s, nobody stops it"
newrun runE
$H start --directory "$SCRATCH/runE" --rclone "$RCLONE" --seconds 300 --limit-mib 2048 | tee -a "$LOG" >/dev/null
PE=$(field "$SCRATCH/runE/sftp-listener.json" port); UE=$(field "$SCRATCH/runE/sftp-listener.json" unit)
sleep 310
say "310 s later: server $(systemctl --user is-active "$UE" 2>&1 | head -1); units left: [$(leftovers)]; $(listener "$PE"); rclone processes: $(ps -eo args | grep -c "[r]clone serve sftp.*127.0.0.1:$PE")"
$H stop --directory "$SCRATCH/runE" | tee -a "$LOG" || FAIL=1

say "## cleanup: delete the scratch directory (client private key, host keys, repositories) and verify"
rm -rf "$SCRATCH"
[ ! -e "$SCRATCH" ] && say "scratch directory gone" || { say "!!! scratch directory remains"; FAIL=1; }
say "final: units left [$(leftovers)]; rclone serve processes: $(ps -eo args | grep -c '[r]clone serve sftp')"
[ $FAIL -eq 0 ] && say "RESULT: PASS" || say "RESULT: FAIL"
exit $FAIL
