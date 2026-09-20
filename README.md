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

Each preset uses the same component temperature curves, disk requirements and automatic-control handoff thresholds. Only the PWM endpoints differ:

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
```

`--set-profile silent` and `--set-profile quiet` use the same interface, but require assessment of the low-airflow risks above before any hardware trial.

- A changed selection wakes the control loop without restarting the process. The loop completes any in-progress SMART/IPMI operation and performs a control cycle before acknowledging the change. Existing SMART sampling/cache rules are retained. Switching is not a hard-real-time operation; the CLI waits up to 120 seconds.
- A successful response includes `profile`, `pwm` (last commanded duty) and `profile_target_pwm` (the temperature curve's demand before downward ramping). `--get-profile` returns the last completed cycle, not a new hardware measurement. Selecting the already active profile returns that status without another sample or state write.
- Increases are immediate once the control cycle succeeds. Decreases remain limited to two percentage points per 15 seconds, including across repeated switch commands. Switching to a lower ceiling does **not** abruptly clamp the current duty to it; duty can temporarily exceed the new ceiling while ramping down.
- The controller atomically saves the selected name in `/var/lib/ipmi-fan-control/profile` after the successful cycle. It is reused after service or machine restarts. A save failure is an error and triggers automatic-control recovery rather than reporting a successful switch.
- Startup selection order is explicit `--profile NAME`, then the saved name, then `balanced`. In control mode an explicit startup override is saved after its first successful cycle. A corrupt saved name prevents manual control; a deliberate `--profile balanced` override can replace it. Read-only `--check`/`--monitor` never save their selections.
- The default endpoint is `/run/ipmi-fan-control/control.sock`, accessible only to root under the supplied service. The endpoint lock prevents two instances using that same socket; it cannot protect against a separate IPMI writer or an instance deliberately using another socket.
- The service must be running to use `--get-profile` or `--set-profile`. The client neither starts it nor changes fan hardware directly. On timeout or a lost connection, the result may be uncertain: query the running controller before retrying.

The systemd unit creates private runtime and persistent directories. A manually supervised instance creates its own directories if needed and requires them to be private and owned by its effective user. `--socket PATH` selects an alternative endpoint for both controller and CLI; `--state-file PATH` selects the state file for a sampling/control process. Do not expose the endpoint to untrusted users.

## Current host-specific policy

The authoritative preset endpoints, sensor limits and sampling settings are in [ipmi_fan_control.py](ipmi_fan_control.py). The state file stores only a selected name, not arbitrary editable curves; editing source still requires deployment and a service restart.

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
| `CYCLE_SECONDS` | 15 seconds |
| `DRIVE_SAMPLE_SECONDS` | 60 seconds |
| `DRIVE_SMART_TIMEOUT` | 5 seconds per disk |
| `RAMP_DOWN_MAX` | 2 percentage points; decreases at least 15 seconds apart |
| `HYSTERESIS` | 2 percentage points |

At startup, the first write goes directly to the computed target after a complete valid sample. It is **not** ramped down from the BMC's live duty, including when reloading a saved `silent` profile. Subsequent increases are immediate; decreases are limited to two points with at least one control interval between them. Downward differences smaller than the hysteresis are held, so the commanded duty can remain slightly above the computed target.

### Temperature curves and automatic-control handoff

Each component independently requests a linear increase between the selected profile's PWM endpoints over the two temperatures below. The highest request wins. At or above the handoff threshold, every profile exits manual control and attempts to restore the iDRAC automatic policy instead of keeping a fixed emergency PWM. This includes `full-speed`: its 100% command is not an exception to the handoff checks.

**These are conservative policy thresholds, not manufacturer temperature limits.**

| Component | Start increasing PWM | Handoff to iDRAC at or above |
| --- | ---: | ---: |
| Inlet | 26°C | 32°C |
| Exhaust | 38°C | 50°C |
| Hottest CPU | 50°C | 70°C |
| Hottest HDD | 35°C | 45°C |
| Hottest non-NVMe SSD | 45°C | 60°C |
| Hottest NVMe | 50°C | 65°C |

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

Each cycle emits JSON, including the selected `profile`. `pwm` is the last successfully commanded percentage, **not a BMC mode/duty readback**. `profile_target_pwm` is the curve's demand; `target_pwm` includes ramping and hysteresis. `fan_rpm` contains measured values sampled before that cycle's write; a speed change can appear in the following sample. Read-only mode reports `pwm: null`. Always consider measured RPM alongside the requested duty, and never overlap controllers or fault-injection runs.

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

Edit `FAN_PROFILES`, `EXPECTED_DRIVE_COUNTS`, `AIR_PROFILES`, `DRIVE_PROFILES` and timing constants only after assessing the actual host. All fan profiles deliberately share the temperature safety limits and sensor validity checks. Commit and test local changes, stop the installed service, deploy the reviewed files, validate inputs, and run another supervised trial before enabling the service again. Keep this policy documentation in sync with changed constants. Selecting an existing profile is the only operation that takes effect without a restart.

The original deployment was checked at 23°C inlet with a bounded five-minute workload using 12 CPU workers and 18.75 GiB of read-only disk I/O across 14 drives. The controller raised PWM under load and reduced it afterward. Normal stop, missing CPU temperature, a successful SMART response without temperature, rejected PWM writes and a `SIGSTOP` watchdog recovery were exercised. These hardware fault-injection helpers are intentionally not shipped as ordinary developer tests.

That validation did not establish safety for all-core saturation, full 10Gbps NIC load, high ambient temperatures or every unobservable component. Lower RPM is not a measured reduction in decibels. The included regression tests do not certify thermal safety on another machine.

## Origin and license

Adapted from [greghughespdx/dell-poweredge-fan-control](https://github.com/greghughespdx/dell-poweredge-fan-control). The upstream copyright notice, Copyright (c) 2026 Greg Hughes, is retained in the [MIT LICENSE](LICENSE).

This is an independently adapted deployment, not an official Dell product or a claim of universal PowerEdge compatibility. The Dell OEM raw fan commands are undocumented interfaces; verify support on the exact model and firmware before use.
