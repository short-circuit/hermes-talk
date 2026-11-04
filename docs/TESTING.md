# Testing

## Layers

- **Unit** — parsers, event mapping, attachment validation, approval policy.
- **Integration** — relay against a stub Hermes API (recorded SSE fixtures); no real agent needed.
- **End-to-end** — relay + Hermes + local models, driven by scripted audio fixtures.
- **Android** — instrumentation tests for the call state machine; emulator for UI flows.
- **Soak** — 30-minute call with induced network loss and reconnection.

## Emulator (archive-hosted AVD)

The Android layers run on an API 34 x86_64 AVD whose storage lives outside the
home partition:

| Part | Location |
|---|---|
| emulator binary, platform-tools, system image | `$ANDROID_SDK_ROOT` (default `$HOME/Android/Sdk`) |
| AVD `hermes_talk_34` (config, 6 GiB data partition, snapshots) | `$ANDROID_AVD_HOME` (default `/store/archive/android-avd`) |

The split is deliberate: the home partition runs at ~94% capacity while the
archive volume has over a terabyte free, and the userdata image grows with
installed builds, snapshots and app data — exactly what should not land on the
small disk. The system image is read-only and stays with the SDK so that
`avdmanager` and `emulator` can resolve `image.sysdir.1`.

`scripts/emulator-archive.sh` is the supported entry point. Every subcommand
exports the same `ANDROID_AVD_HOME` before invoking `avdmanager` or `emulator`,
so the two tools cannot disagree about where the AVD lives.

```bash
scripts/emulator-archive.sh headless        # boot without a window
scripts/emulator-archive.sh wait-for-boot   # block until sys.boot_completed=1
scripts/emulator-archive.sh stop            # shut the emulator down
scripts/emulator-archive.sh start           # same as headless, with a window
```

The script is idempotent: it reuses an existing AVD and refuses to start a
second emulator while one is attached, so repeated runs never fork a duplicate
AVD. `COLD_BOOT=yes` skips the quickboot snapshot when a reproducible boot is
needed. `ANDROID_SDK_ROOT`, `ANDROID_AVD_HOME`, `AVD_NAME`, `SYSTEM_IMAGE`,
`DEVICE_PROFILE`, `DATA_PARTITION_SIZE` and `BOOT_TIMEOUT` are overridable, so
no personal path is hard-coded in the repository.

Hardware acceleration is required. `emulator -accel-check` reports
`KVM (version 12) is installed and usable` on a host where `/dev/kvm` is
readable by the build user; without it the x86_64 image falls back to
emulation and boots several times slower.

Measured cold boot on this host (API 34 x86_64, 4 vCPU, KVM):

| Case | Time to `sys.boot_completed=1` |
|---|---|
| first boot of a freshly created AVD | 20.1 s (emulator log: `Boot completed in 20146 ms`) |
| cold boot with existing userdata (`COLD_BOOT=yes`) | 14.8 s (emulator log: `Boot completed in 14807 ms`), 16.1 s from process launch |

Verification after boot:

```bash
adb devices                                   # emulator-5554	device
adb shell getprop ro.build.version.sdk        # 34
```

## Fixtures


Synthetic only. Use short generated tones or a TTS-generated corpus; never commit personal recordings.
Keep fixtures under 2 MB.

## Latency SLOs

| Metric | Target |
|---|---|
| Endpointing | 300–500 ms |
| STT final | < 300 ms |
| Brain first token | < 300 ms |
| TTS first audio | < 250 ms |
| Perceived response | < 1.2 s p50 |

Failures are reported with the measured distribution, not a single sample.
