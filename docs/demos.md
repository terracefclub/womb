# Synthetic Home Assistant and Frigate demonstrations

On November 2, 2026, the Linux check passed all nine scenarios with the measured
limits in 317.38 seconds. The resulting VM test artifact on the builder is
`/nix/store/hlf1l4q4d902yfycj9fgkbyxs74m34x0-vm-test-run-terrace-synthetic-demos`.
Deployment and individual access are tracked in [issue #10](https://gitlab.com/terrace/tech/womb/-/issues/10).
Real hardware and device integration are tracked in [issue #9](https://gitlab.com/terrace/tech/womb/-/issues/9).

`platform/demos.nix` is an optional NixOS module (`terrace.demos.enable`, default
off) that runs the kept home services against made-up data so people can see them
working before any on-premises hardware exists. It is independent of K3s, the
website and the access relay.

## What runs

| Service | What it does | Listens on (loopback only) |
| --- | --- | --- |
| Home Assistant | Six template entities computed from the clock: `sensor.synthetic_{kitchen,pool,solarium}_temperature` and `binary_sensor.synthetic_{kitchen,pool,solarium}_occupancy`. Names start with "Synthetic" and carry a `synthetic: true` attribute; the instance is named "Terrace SYNTHETIC demo". | 8123 |
| go2rtc | Generates a black 320x240, 5 fps H.264 stream (`synthetic_black`) with its built-in ffmpeg source, encoded only while a consumer is attached. | 1984 API, 8554 RTSP, 8555 WebRTC |
| Frigate | One camera, `synthetic_black`, reading that stream. Detection, recording, snapshots, birdseye, audio, notifications, MQTT and version checks are all off. | 80 (Frigate login), 5000 (unauthenticated, upstream design), 5001 |
| nginx | Frigate's NixOS module requires it as the front end. | 80, 5000 |

Home Assistant has only its built-in default integrations plus `template`;
there is no `default_config`, discovery (zeroconf, SSDP, DHCP, USB, Bluetooth),
`cloud`, MQTT or mobile app. The pinned Home Assistant includes local conversation
and analytics as built-in dependencies; all analytics preferences remain off and
are checked through its authenticated API before and after restart. Loading the
integration does not opt in to [analytics](https://www.home-assistant.io/integrations/analytics/).
The Home Assistant HTTP client can open mDNS sockets even with discovery off.
Its private network namespace supplies only the loopback interface. A systemd
socket proxy forwards up to 32 connections from host loopback to this namespace.
The four application units also deny IP traffic outside localhost.

Access for now is an SSH local forward, as for the K3s API (see
[platform/README.md](../platform/README.md)):

```sh
ssh -L 8123:127.0.0.1:8123 operator@HOST
# Save one synthetic frame from Frigate through your operator account.
ssh operator@HOST 'curl -fsS http://127.0.0.1:5000/api/synthetic_black/latest.jpg' > synthetic_black.jpg
```

Home Assistant onboarding (the first owner account) happens in that forwarded
browser session. The Frigate demo suppresses its generated administrator password
in the journal. Operators can check the frame through SSH. Interactive Frigate
administration requires provisioning individual accounts before wider access.

## Bounds

The 2-vCPU, 4-GiB guest ran one generated camera and six changing sensors.
The sizing run produced the following startup and ingest measurements, used to
select the memory and task limits.
CPU time is cumulative, so it cannot be read as steady-state CPU usage.

| Service | Peak memory (MiB) | CPU time (s) | Tasks at sample | MemoryHigh / MemoryMax (MiB) | CPUQuota / TasksMax |
| --- | ---: | ---: | ---: | ---: | ---: |
| Home Assistant | 348 | 6.9 | 14 | 448 / 640 | 100% / 96 |
| Frigate | 939 | 18.3 | 175 | 1152 / 1536 | 100% / 320 |
| go2rtc | 33 | 0.4 | 22 | 64 / 96 | 50% / 64 |
| nginx | 53 | 0.4 | 69 | 80 / 128 | 50% / 128 |

Memory and task limits are host-overridable defaults. All four units disable
swap. Frigate caps shared memory at 128 MiB and leaves recording off. At the
sample, the Home Assistant, Frigate and Frigate cache directories used 648,
300 and 16 KiB respectively. Home Assistant retains one day of recorder history.
Long-run disk use and a quota for the application host's volume remain deployment
checks.

## Verification scope

The two-machine VM test checks six labelled sensors, disabled analytics before
and after restart, black pixels in the frame served by Frigate, loopback-only
host listeners and blocked traffic from each application cgroup. It exercises
both services together, each service alone, restart, teardown and disabling.
Unrelated files in the normal service directories must survive all of them.
During shutdown, Frigate 0.18.2 can log Python resource tracker `KeyError`
messages for released synthetic frame buffers. The test checks that ingest
resumes after a restart and again after a full teardown.

The demos use disposable data and exclude it from backups. Real cameras, radios,
accelerators, detection, recording, individual Frigate accounts and capacity on
the eventual application host remain separate work.

## Run the test

On the Linux builder, never on a Mac (see `AGENTS.md`). From a clean checkout
synced to a task-specific directory on the builder:

```sh
BUILD_LOCK="$HOME/.falcon-heavy.lock" ./platform/check demos
```

Set `BUILD_LOCK` to that builder's shared heavy-job lock (the example uses the
current builder's convention). The runner requires x86_64 Linux and KVM, queues
behind other heavy work and leaves build parallelism bounded. For a remote run
that survives a disconnected terminal:

```sh
( nohup setsid env BUILD_LOCK="$HOME/.falcon-heavy.lock" \
    ./platform/check demos >demos-test.log 2>&1 </dev/null & echo $! >demos-test.pid )
```

The guest-only `platform/demos-ha-test.py` client keeps throwaway onboarding
credentials in a root-only `/run` file; they never enter driver commands or logs.
The test VM filters Frigate's generated-password message before journal output.

## Operate, disable, tear down

```sh
terrace-demo status            # unit state, memory use against its limit, API probes
terrace-demo teardown --yes    # stop the units and delete all synthetic state
```

To disable the demos, remove `terrace.demos.enable` or set it to `false` and
switch the configuration. The services stop and the demo directories are
retained. To remove their contents, run `terrace-demo teardown --yes` while
the module is enabled. It only cleans the selected demos' state under
`/var/lib/terrace-demo-hass`, `/var/lib/terrace-demo-frigate` and
`/var/cache/terrace-demo-frigate`. Frigate and nginx see their usual paths
through private bind mounts. Enable the module explicitly in a demo host
profile to run it.

`terrace.demos.homeAssistant.enable` and `terrace.demos.frigate.enable` select
individual demonstrations; status and teardown operate only on those selected.
The module rejects a configuration with neither selected.

The Frigate demo requires a dedicated host nginx service. An evaluation assertion
rejects other host nginx vhosts because the demo's global loopback-only policy
would prevent them serving outside clients. Other applications may use a separate
ingress (for example, in K3s). Do not remove this guard merely to combine vhosts;
isolate the demo proxy first.
