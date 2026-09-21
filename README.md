# Dell R730xd Fan Control

A Python, local-IPMI fan controller adapted for a dual-CPU Dell PowerEdge R730xd running Proxmox VE. It combines CPU, inlet, exhaust and per-class disk temperature demands, taking the highest required PWM rather than averaging unrelated components.

This repository starts from a validated deployment, not a generic configuration for every Dell server. The initial Git import preserved that implementation; runtime-selectable profiles were added afterward. Local Git changes do not automatically update an installed service.

## Safety first

**Do not run `--control` or start the supplied service on a non-PVE desktop.** Local development only needs the mocked tests, `--help` and `--list-profiles` shown below.

- Manual control disables the iDRAC automatic fan policy. A process that disappears without restoring automatic mode can leave the last commanded duty in effect.
- The controller's cleanup and the service's `ExecStopPost` attempt to restore automatic control. They cannot guarantee recovery if the host kernel freezes or the BMC becomes inaccessible.
- H330 controller and X540 NIC temperatures are not available through the existing host interfaces used here. CPU and disk temperatures do not establish those components' cooling margins. Keep the cooling shroud, blanks and normal chassis airflow intact.
- The `balanced` profile preserves the original 45% floor tested on one machine, **not a Dell airflow certification or a universal safe minimum**. `silent` (10%) and `quiet` (25%) are deliberately aggressive, unvalidated low-airflow presets. Normal CPU/disk temperatures do not prove that the H330, X540 or other unobserved components are adequately cooled.
- Run only one fan controller at a time. Do not issue competing manual fan commands while this service is active.
- There is no fan-stop profile. Even at a 10% command the BMC may enforce a different physical minimum; an actual fan reading at or below 600 RPM still triggers automatic-control recovery. Low-airflow selections persist across restarts, so they require supervision and a deliberate return to `balanced` after experiments.
- This is not a replacement for SMART health monitoring or iDRAC hardware alerts.

## Requirements and local development

The deployment was exercised on PVE 8.3 with Python 3.11. Use Python 3.11 or newer. There are no third-party Python dependencies.

Hardware operation requires root access on the PVE host, local OpenIPMI (`/dev/ipmi0`), `/usr/bin/ipmitool`, `/usr/sbin/smartctl`, `lsblk`, systemd and GNU `timeout`. No Docker, network IPMI session, iDRAC password or `.env` file is needed.

From the repository directory on a development machine:

```sh
python3 -B -m unittest -v
python3 -B ipmi_fan_control.py --help
python3 -B ipmi_fan_control.py --list-profiles
```

The tests replace command execution with mocks; they do not access IPMI or disks. Hardware trials are separate from the regression suite and must not run in ordinary CI.

## Profiles and runtime commands

Presets share the inlet, exhaust and disk temperature limits, disk requirements and sensor validity checks. `silent` alone uses a later CPU ramp and handoff (60°C / 75°C); all other presets keep 50°C / 70°C. PWM endpoints are:

| Profile | PWM floor | PWM ceiling | Purpose |
| --- | ---: | ---: | --- |
| `silent` | 10% | 75% | Experimental minimum airflow; not thermally validated |
| `quiet` | 25% | 75% | Experimental reduced airflow; not thermally validated |
| `balanced` | 45% | 75% | Original policy; default when no selection has been saved |
| `performance` | 65% | 100% | More airflow at the same temperatures |
| `full-speed` | 100% | 100% | Fixed maximum command, still subject to safety handoff |

New presets have software-level checks only, not new hardware thermal validation. A profile name is not a safety or acoustic certification.

With the updated service already running **on PVE**, use these root commands:

```sh
python3 -B /opt/ipmi-fan-control/ipmi_fan_control.py --get-profile
python3 -B /opt/ipmi-fan-control/ipmi_fan_control.py --set-profile performance
# Return to the original policy:
python3 -B /opt/ipmi-fan-control/ipmi_fan_control.py --set-profile balanced
# Set and persist the detection interval in seconds (0.5-10):
python3 -B /opt/ipmi-fan-control/ipmi_fan_control.py --set-interval 3
```

`--set-profile silent` and `--set-profile quiet` use the same interface, but require assessment of the low-airflow risks above before any hardware trial.

- A changed profile or interval wakes the control loop without restarting the process. The loop completes any in-progress SMART/IPMI operation and performs a control cycle before acknowledging the change. Existing SMART sampling/cache rules are retained. Changes are not hard-real-time operations; the CLI waits up to 120 seconds.
- A successful response includes `profile`, `interval` (configured seconds), `pwm` (last commanded duty) and `profile_target_pwm` (the temperature curve's demand before downward ramping). `--get-profile` returns this last completed status, not a new hardware measurement. Selecting an already active value returns that status without another sample or state write.
- Increases are immediate once the control cycle succeeds. All decreases are limited to **20 percentage points per normal detection cycle**, for both temperature changes and manual profile transitions. Extra command-triggered cycles cannot bypass the current interval or take extra downward steps. Switching to a lower ceiling does **not** abruptly clamp the current duty to it.
- Every downward step uses the current temperature-derived demand, not simply the preset floor. Ordinary decreases retain the two-point hysteresis; a manual profile transition may take a final one-point step to finish at the rounded-up target. Reselecting the active profile does not restart a transition.
- The controller atomically saves the profile name in `/var/lib/ipmi-fan-control/profile` and the interval in `/var/lib/ipmi-fan-control/interval` after successful cycles. Both survive service and machine restarts. These are separate settings, not an atomic multi-setting transaction. A save failure triggers automatic-control recovery rather than acknowledging success. Existing profile files need no migration.
- Startup precedence is explicit `--profile NAME` / `--interval SECONDS`, then each saved value, then `balanced` / **3 seconds**. In control mode startup overrides are saved after the first successful cycle. Invalid saved settings prevent manual control; an explicit valid override can replace the corresponding bad value. Read-only `--check`/`--monitor` never save either setting.
- The default endpoint is `/run/ipmi-fan-control/control.sock`, accessible only to root under the supplied service. The endpoint lock prevents two instances using that same socket; it cannot protect against a separate IPMI writer or an instance deliberately using another socket.
- The service must be running to use `--get-profile`, `--set-profile` or `--set-interval`. The client neither starts it nor changes fan hardware directly. On timeout or a lost connection, the result may be uncertain: query the running controller before retrying.

For example, with a constant 10% temperature demand, a switch from 45% to `silent` descends through `25 -> 10` over two normal detection cycles. With the default 3-second interval, budget roughly 6 seconds plus command and sampling latency; overruns can take longer. Warmer components can require a higher target or an immediate increase at any step; `silent` does not mean a fixed 10% command.

The systemd unit creates private runtime and persistent directories. A manually supervised instance creates its own directories if needed and requires them to be private and owned by its effective user. `--socket PATH` selects an alternative endpoint for both controller and CLI; `--state-file PATH` and `--interval-file PATH` select the independent profile and interval state files for a sampling/control process. Use separate files and override both paths for an isolated instance. Do not expose the endpoint to untrusted users.

## Current host-specific policy

The authoritative preset endpoints, sensor limits and interval bounds are in [ipmi_fan_control.py](ipmi_fan_control.py). The state files store only the selected profile name and detection interval, not arbitrary editable curves; editing source still requires deployment and a service restart.

`EXPECTED_DRIVE_COUNTS` requires at least:

| Class | Required count |
| --- | ---: |
| `hdd` | 10 |
| `ssd` (non-NVMe; SATA in the validated machine) | 2 |
| `nvme` | 2 |

Additional discovered disks are also sampled. A missing required class/count, an unreadable disk or a disk without a usable temperature prevents continued manual control. Disks passed through to another OS may no longer meet this host-side requirement.

The IPMI parser also requires:

- One `Inlet Temp` and one `Exhaust Temp` reading.
- Exactly two CPU rows named `Temp`; both are retained.
- Six healthy fan rows named `Fan1 RPM` through `Fan6 RPM`, with `ok` status and readings above 600 RPM. This validity check is not an airflow certification.

### PWM and sampling

| Setting | Current value |
| --- | ---: |
| PWM endpoints | Selected from `FAN_PROFILES`; see the preset table |
| Detection interval | Default 3 seconds; configurable and persistent from 0.5 to 10 seconds |
| `DRIVE_SAMPLE_SECONDS` | 60 seconds |
| `DRIVE_SMART_TIMEOUT` | 5 seconds per disk |
| `RAMP_DOWN_MAX` | 20 percentage points per normal detection cycle |
| `HYSTERESIS` | 2 percentage points |

At startup, the first write goes directly to the computed target after a complete valid sample. It is **not** ramped down from the BMC's live duty, including when reloading a saved `silent` profile. Subsequent increases are immediate. Downward steps follow the configured detection cycle without an additional post-write cooldown. Ordinary downward differences smaller than the hysteresis are held, so the commanded duty can remain slightly above the computed target after a later temperature change.

The interval schedules CPU, inlet/exhaust and fan-health sampling; it does not change the independent 60-second SMART sweep. Sampling and fan writes remain serial: the configured 0.5-second minimum is a scheduling request, not a guarantee of fresh BMC data every 500 ms. IPMI reads, SMART sweeps and BMC sensor refresh can limit the effective rate. When a cycle overruns, the controller polls queued commands and starts the next cycle without an extra sleep; it does not queue overlapping hardware scans. Changing the interval reschedules from the last normal cycle's start, without granting extra downward steps inside the newly selected interval.

### Temperature curves and automatic-control handoff

Each component independently requests a linear increase between the selected profile's PWM endpoints over the two temperatures below. The highest request wins. At or above the handoff threshold, every profile exits manual control and attempts to restore the iDRAC automatic policy instead of keeping a fixed emergency PWM. This includes `full-speed`: its 100% command is not an exception to the handoff checks.

**These are software policy thresholds, not manufacturer temperature limits or target temperatures.**

| Component | Start increasing PWM | Handoff to iDRAC at or above |
| --- | ---: | ---: |
| Inlet | 26°C | 32°C |
| Exhaust | 38°C | 50°C |
| Hottest CPU (`silent`) | 60°C | 75°C |
| Hottest CPU (all other profiles) | 50°C | 70°C |
| Hottest HDD | 35°C | 45°C |
| Hottest non-NVMe SSD | 45°C | 60°C |
| Hottest NVMe | 50°C | 65°C |

The `silent` CPU curve requests its 10% floor through 60°C, then rises linearly toward 75% just below 75°C. At 75°C it hands back to iDRAC instead of holding that temperature. This higher handoff threshold is experimental and has not been thermally validated under high load; it does not modify iDRAC warning or critical thresholds. Other components can demand more airflow even with the CPUs below 60°C.

SMART uses automatic device detection. Do not reintroduce a forced `-d scsi` for all SATA disks: that returned success without temperature on the validated H330 setup. ATA temperature comes from normalized `temperature.current`, never the packed SMART `raw.value`; NVMe includes the hottest valid normalized sensor.

Non-NVMe SMART reads use `-n standby`. Sleeping disks are not deliberately spun up to obtain a temperature. If a required temperature is unavailable, the controller hands back to iDRAC rather than treating it as 0°C, silently omitting it or indefinitely reusing an old reading.

## Failure handling and logs

The supplied [systemd unit](ipmi-fan-control.service) uses:

- `WatchdogSec=90s` with a heartbeat after a completed control cycle.
- `Restart=on-failure` and `RestartSec=30s`.
- `ExecStopPost` running the automatic-control command under a 10-second timeout, with a further 2-second kill grace period.
- `TimeoutStopSec=20s`.

A sensor or command failure exits nonzero. The service retries after 30 seconds; this is **not a latched shutdown**. While a required input remains invalid, a retry does not enter manual control. Exceptionally slow but still successful sampling can also exceed the watchdog deadline, so a watchdog event is not by itself proof of a deadlock. Do not feed the watchdog from an unrelated timer that would hide a stalled control loop.

Normal control-mode exit, `SIGINT` and `SIGTERM` run the program's restore path. `SIGKILL` cannot run that cleanup; systemd provides a separate restore attempt. An unmanaged invocation has no systemd fallback.

Once exit cleanup begins, further `SIGINT` and `SIGTERM` requests are ignored so they cannot interrupt the bounded automatic-control restore attempt. Forced termination and the systemd fallback retain their existing behavior.

Each cycle emits JSON, including the selected `profile`, configured `interval` in seconds, and a millisecond-resolution timestamp. `pwm` is the last successfully commanded percentage, **not a BMC mode/duty readback**. `profile_target_pwm` is the curve's demand; `target_pwm` includes ramping and hysteresis. `fan_rpm` contains measured values sampled before that cycle's write; a speed change can appear in the following sample. Read-only mode reports `pwm: null`. Always consider measured RPM alongside the requested duty, and never overlap controllers or fault-injection runs.

## Install or update on PVE

Transfer the reviewed checkout to the PVE host. **Run the following commands there as root, from that checkout, not on the development desktop.** Stop other fan controllers first. Do not proceed past a failed command or an invalid sensor check.

Begin with iDRAC automatic control active. If its state is uncertain, use the manual recovery procedure below after stopping all controllers.

If updating an existing installation, first disable and stop it so its restore path runs:

```sh
systemctl disable --now ipmi-fan-control.service
```

Install missing dependencies and copy the files. The PVE base system supplies systemd, coreutils and util-linux.

```sh
apt-get install --no-install-recommends python3 ipmitool smartmontools
install -d -m 0755 /opt/ipmi-fan-control
install -m 0644 ipmi_fan_control.py test_ipmi_fan_control.py LICENSE /opt/ipmi-fan-control/
install -m 0644 ipmi-fan-control.service /etc/systemd/system/ipmi-fan-control.service
systemd-analyze verify /etc/systemd/system/ipmi-fan-control.service
systemctl daemon-reload
```

Check the real inputs without changing fan settings:

```sh
ipmitool -I open mc info
python3 -B /opt/ipmi-fan-control/ipmi_fan_control.py --check
python3 -B /opt/ipmi-fan-control/ipmi_fan_control.py --monitor --duration 120
```

Confirm both CPUs, all six fans and every required disk are present. Review the profile against the actual hardware before taking manual control; changing counts solely to conceal failed reads is not a fix.

### First controlled trial

Leave the permanent service stopped. Use a host-side transient service so a lost SSH connection is not the only thing supervising recovery. This example explicitly starts with `balanced`, overriding any saved experimental selection after the first successful cycle. It has a 9-minute program duration and a separate 10-minute systemd runtime limit:

```sh
systemd-run --unit=ipmi-fan-control-trial --wait --pipe \
  --property=Type=simple \
  --property=RuntimeDirectory=ipmi-fan-control \
  --property=RuntimeDirectoryMode=0700 \
  --property=StateDirectory=ipmi-fan-control \
  --property=StateDirectoryMode=0700 \
  --property=UMask=0077 \
  --property=NotifyAccess=main \
  --property=WatchdogSec=90s \
  --property=RuntimeMaxSec=10min \
  --property=TimeoutStopSec=20s \
  --property=Restart=no \
  '--property=ExecStopPost=/usr/bin/timeout --kill-after=2s 10s /usr/bin/ipmitool -I open raw 0x30 0x30 0x01 0x01' \
  /usr/bin/python3 -B /opt/ipmi-fan-control/ipmi_fan_control.py --control --profile balanced --duration 540
```

To abort an active trial from another terminal:

```sh
systemctl stop ipmi-fan-control-trial.service
```

After the trial exits, confirm that automatic control was restored and check measured fan status:

```sh
ipmitool -I open sdr type Fan
```

Do not start the permanent service, another trial or a fault-injection run until the previous trial has stopped and completed its restore. Its delayed cleanup would otherwise interfere with the next controller. Evaluate bounded, non-destructive workloads and temperature trends, not just one idle reading; never run raw-device write benchmarks against live VM storage.

After diagnosing a failed transient trial, `systemctl reset-failed ipmi-fan-control-trial.service` may be needed before reusing that unit name. This does not itself restore fan control.

During a supervised trial, the profile commands above address the transient controller through the same default socket. Inspect the reported duty and measured RPM while switching; return to `balanced` before ending low-airflow experiments. The last selection is saved for the permanent service too. Stopping the trial restores automatic fan control but does not erase its saved selection.

After successful validation, enable the permanent service:

```sh
systemctl enable --now ipmi-fan-control.service
systemctl status ipmi-fan-control.service --no-pager
journalctl -u ipmi-fan-control.service -f
```

## Revert to iDRAC automatic control

Stop and disable the permanent service; its stop handling attempts the restore:

```sh
systemctl disable --now ipmi-fan-control.service
```

If manual recovery is needed, first ensure all controllers and any active transient trial are stopped, then run:

```sh
ipmitool -I open raw 0x30 0x30 0x01 0x01
ipmitool -I open sdr type Fan
```

Do not resume manual control if restoration fails. Investigate local IPMI access and monitor hardware temperatures. To revert a software change, select and test the desired Git revision locally, then repeat the stopped-service update and read-only validation sequence above.

## Tuning and validation limits

Edit `FAN_PROFILES`, `EXPECTED_DRIVE_COUNTS`, `AIR_PROFILES`, `CPU_PROFILE_OVERRIDES`, `DRIVE_PROFILES` and interval bounds only after assessing the actual host. `CPU_PROFILE_OVERRIDES` changes only the named presets' CPU curves; the inlet, exhaust and disk limits and all sensor validity checks remain shared. Commit and test local changes, stop the installed service, deploy the reviewed files, validate inputs, and run another supervised trial before enabling the service again. Keep this policy documentation in sync with changed constants. Selecting an existing profile or setting an interval within the supported range takes effect without a restart.

The original deployment was checked at 23°C inlet with a bounded five-minute workload using 12 CPU workers and 18.75 GiB of read-only disk I/O across 14 drives. The controller raised PWM under load and reduced it afterward. Normal stop, missing CPU temperature, a successful SMART response without temperature, rejected PWM writes and a `SIGSTOP` watchdog recovery were exercised. These hardware fault-injection helpers are intentionally not shipped as ordinary developer tests.

That validation did not establish safety for all-core saturation, full 10Gbps NIC load, high ambient temperatures or every unobservable component. Lower RPM is not a measured reduction in decibels. The included regression tests do not certify thermal safety on another machine.

## Origin and license

Adapted from [greghughespdx/dell-poweredge-fan-control](https://github.com/greghughespdx/dell-poweredge-fan-control). The upstream copyright notice, Copyright (c) 2026 Greg Hughes, is retained in the [MIT LICENSE](LICENSE).

This is an independently adapted deployment, not an official Dell product or a claim of universal PowerEdge compatibility. The Dell OEM raw fan commands are undocumented interfaces; verify support on the exact model and firmware before use.
