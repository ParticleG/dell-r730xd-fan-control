# Dell R730xd Fan Control

A Python, local-IPMI fan controller adapted for a dual-CPU Dell PowerEdge R730xd running Proxmox VE. It combines CPU, inlet, exhaust and per-class disk temperature demands, taking the highest required PWM rather than averaging unrelated components.

This repository starts from a validated deployment, not a generic configuration for every Dell server. The controller, regression tests and service were imported without runtime changes. Local Git changes do not automatically update an installed service.

## Safety first

**Do not run `--control` or start the supplied service on a non-PVE desktop.** Local development only needs the mocked tests and `--help` shown below.

- Manual control disables the iDRAC automatic fan policy. A process that disappears without restoring automatic mode can leave the last commanded duty in effect.
- The controller's cleanup and the service's `ExecStopPost` attempt to restore automatic control. They cannot guarantee recovery if the host kernel freezes or the BMC becomes inaccessible.
- H330 controller and X540 NIC temperatures are not available through the existing host interfaces used here. CPU and disk temperatures do not establish those components' cooling margins. Keep the cooling shroud, blanks and normal chassis airflow intact.
- The 45% floor is a setting tested on one machine, **not a Dell airflow certification or a universal safe minimum**. Different cards, disks, ambient conditions and workloads require their own assessment.
- Run only one fan controller at a time. Do not issue competing manual fan commands while this service is active.
- This is not a replacement for SMART health monitoring or iDRAC hardware alerts.

## Requirements and local development

The deployment was exercised on PVE 8.3 with Python 3.11. Use Python 3.11 or newer. There are no third-party Python dependencies.

Hardware operation requires root access on the PVE host, local OpenIPMI (`/dev/ipmi0`), `/usr/bin/ipmitool`, `/usr/sbin/smartctl`, `lsblk`, systemd and GNU `timeout`. No Docker, network IPMI session, iDRAC password or `.env` file is needed.

From the repository directory on a development machine:

```sh
python3 -B -m unittest -v
python3 -B ipmi_fan_control.py --help
```

The tests replace command execution with mocks; they do not access IPMI or disks. Hardware trials are separate from the regression suite and must not run in ordinary CI.

## Current host-specific profile

The authoritative settings are constants in [ipmi_fan_control.py](ipmi_fan_control.py). There is no external configuration file or live reload.

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
| `FAN_FLOOR` | 45% |
| `FAN_CEILING` | 75% |
| `CYCLE_SECONDS` | 15 seconds |
| `DRIVE_SAMPLE_SECONDS` | 60 seconds |
| `DRIVE_SMART_TIMEOUT` | 5 seconds per disk |
| `RAMP_DOWN_MAX` | 2 percentage points per control cycle |
| `HYSTERESIS` | 2 percentage points |

At startup, the first write goes directly to the computed target after a complete valid sample. It is **not** ramped down from the BMC's live duty. Subsequent increases are immediate; decreases are limited to two points per cycle. Downward differences smaller than the hysteresis are held, so the commanded duty can remain slightly above the computed target.

### Temperature curves and automatic-control handoff

Each component independently requests a linear increase from 45% toward 75% between the two temperatures below. The highest request wins. At or above the handoff threshold, the controller exits manual control and attempts to restore the iDRAC automatic policy instead of keeping a fixed emergency PWM.

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

Each cycle emits JSON. `pwm` is the last successfully commanded percentage, **not a BMC mode/duty readback**. `fan_rpm` contains measured values sampled before that cycle's write; a speed change can appear in the following sample. Read-only mode reports `pwm: null`. Always consider measured RPM alongside the requested duty, and never overlap controllers or fault-injection runs.

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

Leave the permanent service stopped. Use a host-side transient service so a lost SSH connection is not the only thing supervising recovery. This example has a 9-minute program duration and a separate 10-minute systemd runtime limit:

```sh
systemd-run --unit=ipmi-fan-control-trial --wait --pipe \
  --property=Type=simple \
  --property=NotifyAccess=main \
  --property=WatchdogSec=90s \
  --property=RuntimeMaxSec=10min \
  --property=TimeoutStopSec=20s \
  --property=Restart=no \
  '--property=ExecStopPost=/usr/bin/timeout --kill-after=2s 10s /usr/bin/ipmitool -I open raw 0x30 0x30 0x01 0x01' \
  /usr/bin/python3 -B /opt/ipmi-fan-control/ipmi_fan_control.py --control --duration 540
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

Edit `EXPECTED_DRIVE_COUNTS`, `AIR_PROFILES`, `DRIVE_PROFILES`, PWM limits and timing constants only after assessing the actual host. Commit and test local changes, stop the installed service, deploy the reviewed files, validate inputs, and run another supervised trial before enabling the service again. Keep this profile documentation in sync with changed constants.

The original deployment was checked at 23°C inlet with a bounded five-minute workload using 12 CPU workers and 18.75 GiB of read-only disk I/O across 14 drives. The controller raised PWM under load and reduced it afterward. Normal stop, missing CPU temperature, a successful SMART response without temperature, rejected PWM writes and a `SIGSTOP` watchdog recovery were exercised. These hardware fault-injection helpers are intentionally not shipped as ordinary developer tests.

That validation did not establish safety for all-core saturation, full 10Gbps NIC load, high ambient temperatures or every unobservable component. Lower RPM is not a measured reduction in decibels. The included regression tests do not certify thermal safety on another machine.

## Origin and license

Adapted from [greghughespdx/dell-poweredge-fan-control](https://github.com/greghughespdx/dell-poweredge-fan-control). The upstream copyright notice, Copyright (c) 2026 Greg Hughes, is retained in the [MIT LICENSE](LICENSE).

This is an independently adapted deployment, not an official Dell product or a claim of universal PowerEdge compatibility. The Dell OEM raw fan commands are undocumented interfaces; verify support on the exact model and firmware before use.
