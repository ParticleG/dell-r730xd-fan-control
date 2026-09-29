# Repository Guidelines

## Project Overview

This is a host-specific, standard-library Python fan controller for a dual-CPU Dell PowerEdge R730xd running Proxmox VE. It reads local IPMI, CPU, inlet, exhaust, fan, and disk temperatures; selects the highest cooling demand; and controls Dell OEM fan PWM through `ipmitool`.

Treat safety behavior as part of the public contract. Invalid or missing input must fail closed and restore iDRAC automatic fan control. Do not run `--control` or start the service on a development workstation. The `silent` and `quiet` profiles are explicitly unvalidated low-airflow presets, not safe defaults for other hardware.

## Architecture & Data Flow

The repository is intentionally flat: `ipmi_fan_control.py` contains the CLI, hardware adapters, policy, persistence, IPC, and control loop.

1. `main()` parses one mutually exclusive mode and resolves profile/interval precedence: CLI override, saved state, then built-in default.
2. `sample_drive_temperatures()` periodically discovers disks with `lsblk` and reads normalized SMART data with `smartctl`; `read_all_sensors()` samples IPMI temperatures and all six fans each control cycle.
3. Pure helpers such as `component_target()`, `compute_fan_target()`, `compute_drive_fan_target()`, and `apply_ramping()` calculate the highest PWM demand, thermal handoff, hysteresis, and bounded ramp-down.
4. Manual mode is enabled only after a complete valid sample. The loop writes PWM, atomically persists profile/interval changes, emits one JSON status line, and notifies the systemd watchdog.
5. Runtime profile and interval commands use JSON over an `AF_UNIX` `SOCK_SEQPACKET` socket. A file lock prevents a second controller from owning the same endpoint.
6. Failures after the process acquires the control socket and enters the loop exit nonzero and attempt the `finally` restore path. Socket-ownership failure returns before that path and must not restore automatic control underneath the active owner. `ipmi-fan-control.service` adds restart/watchdog behavior and a separate `ExecStopPost` restore attempt.

The process is synchronous and single-threaded. It uses `select.select()` to service control commands between serial hardware samples; there is no `asyncio`, worker thread, or overlapping scan.

## Key Directories

- Repository root: `ipmi_fan_control.py`, `test_ipmi_fan_control.py`, `ipmi-fan-control.service`, `README.md`, and `LICENSE`.
- `debian/`: native Debian package metadata, file layout, and service lifecycle policy. `debian/changelog` is the package version source.
- `.github/workflows/release.yml`: CI build and tag-triggered GitHub Release.

Runtime/deployment paths:

- `/opt/ipmi-fan-control/`: packaged Python source, tests, and license.
- `/run/ipmi-fan-control/control.sock`: private runtime control socket; the adjacent `.lock` file guards ownership.
- `/var/lib/ipmi-fan-control/{profile,interval}`: independent persisted settings.
- `/lib/systemd/system/ipmi-fan-control.service`: packaged unit. An older manually installed unit at `/etc/systemd/system/ipmi-fan-control.service` would override it; the package's first-install check requires relocating that unit.

## Development Commands

Run local commands from the repository root:

```sh
python3 -B -m unittest -v
python3 -B ipmi_fan_control.py --help
python3 -B ipmi_fan_control.py --list-profiles
```

Only on the target PVE host, as root, use read-only hardware validation before manual control:

```sh
ipmitool -I open mc info
python3 -B /opt/ipmi-fan-control/ipmi_fan_control.py --check
python3 -B /opt/ipmi-fan-control/ipmi_fan_control.py --monitor --duration 120
```

On Debian/Ubuntu, build a package from the checkout with `dpkg-buildpackage -b -us -uc` after installing `build-essential`, `debhelper`, and `dpkg-dev`. The version comes from `debian/changelog`; the output is `../ipmi-fan-control_<version>_all.deb`. A `v<version>` tag triggers GitHub Actions to publish the tested `.deb` and checksums to GitHub Releases. There is no Python package build step, linter, formatter, type checker, or coverage command.

On the target host, use `apt install ./ipmi-fan-control_<version>_all.deb` only after stopping other controllers and addressing any legacy `/etc/systemd/system/ipmi-fan-control.service`. Installation does not auto-start the service. After installing, run `systemctl daemon-reload` and check the packaged unit with `systemd-analyze verify /lib/systemd/system/ipmi-fan-control.service`; follow `README.md` for read-only validation, a supervised trial, enablement, and recovery. A Git checkout change does not update the installed service.

## Code Conventions & Common Patterns

- Use 4-space indentation, `snake_case` functions/locals, `UPPER_SNAKE_CASE` policy constants, and descriptive safety comments/docstrings. Keep code and documentation in English.
- Preserve the flat-module design unless a real boundary requires a split. Keep hardware I/O in small adapters and cooling calculations in pure functions.
- Use `SensorError` for unavailable or unsafe cooling inputs. Never replace a missing sensor with a cold/default value, silently omit a required disk, or weaken expected counts to hide read failures.
- Chain lower-level failures with `raise ... from error`. CLI errors go to stderr with `[ERROR]` or `[CRITICAL]`; cycle status remains JSON on stdout. `main()` returns an integer, and only the module footer calls `sys.exit(main())`.
- Keep subprocess calls as argument lists through module-level `subprocess.run`; never use `shell=True`. Tests patch `controller.subprocess.run` and rely on module-level patch seams.
- Preserve normalized SMART temperature handling: never read packed ATA `raw.value`, never force `-d scsi`, and retain `-n standby` for non-NVMe drives.
- Persistence uses private directories and atomic `mkstemp` → `fsync` → `os.replace` writes. Profile and interval are independent state files, not one transaction.
- Dependency injection is lightweight: CLI path overrides (`--socket`, `--state-file`, `--interval-file`) and patchable module functions. There is no DI framework or global service container.
- Preserve lifecycle invariants: validate before enabling manual mode, allow immediate PWM increases, rate-limit decreases, and restore automatic mode after a controller-loop exit. Do not restore on a competing socket-ownership failure, and do not feed the watchdog from an unrelated timer.
- When changing `FAN_PROFILES`, temperature curves, drive counts, interval bounds, hysteresis, ramping, or sampling behavior, update the matching policy tables and safety text in `README.md` and the module docstring used by `--help`.

## Important Files

- `ipmi_fan_control.py`: application entry point, policy constants, hardware I/O, control loop, state persistence, and socket protocol.
- `test_ipmi_fan_control.py`: mocked safety regression suite using `unittest` and `unittest.mock`.
- `README.md`: authoritative operational guidance, safety limitations, policy tables, package deployment, trials, and recovery.
- `ipmi-fan-control.service`: root-run service, watchdog, restart policy, private runtime/state directories, and automatic-control fallback.
- `debian/control`, `debian/changelog`, `debian/rules`, `debian/ipmi-fan-control.install`: package dependencies, version, build behavior, and installed layout; `debian/ipmi-fan-control.preinst`/`.prerm` guard migration and removal.
- `.github/workflows/release.yml`: tests, package build/smoke check, artifact upload, and GitHub Release creation.
- `.gitignore`: ignores build outputs, bytecode, virtual environments, IDE files, and `.env*`; the application does not use `.env`.
- `LICENSE`: MIT license and retained upstream notice.

## Runtime/Tooling Preferences

- Use Python 3.11 or newer with the standard library only. Debian packaging uses `debhelper`/`dpkg-buildpackage` and APT; there is no Python package manager, virtual-environment requirement, or dependency lockfile.
- Target platform: Proxmox VE with root access, local OpenIPMI (`/dev/ipmi0`), systemd, GNU `timeout`, `lsblk`, `/usr/bin/ipmitool`, and `/usr/sbin/smartctl`.
- Do not introduce Docker, network IPMI credentials, an `.env` workflow, or third-party Python dependencies without an explicit requirement.
- Keep the existing absolute binary paths unless deployment requirements change. Hardware operation assumes the exact sensor names and drive inventory documented in `README.md`.
- Runtime and state directories must remain owned by the effective user with no group/other permissions; the socket remains mode `0600` under the supplied service.

## Testing & QA

- Framework: standard-library `unittest`; the only test module is `test_ipmi_fan_control.py`.
- Naming: `<Area>Tests` classes and behavior-focused `test_*` methods, for example `test_missing_cpu_is_not_treated_as_cold`.
- Tests must remain deterministic and hardware-free. Mock every subprocess path; the suite intentionally fails on unexpected real command execution. Use temporary state/socket paths and fake monotonic time for loop behavior.
- Preserve tests around sensor validation, thermal handoff, ramping/hysteresis, persistence failure, socket ownership/protocol, signal handling, and automatic-control restoration.
- Hardware trials and fault injection are separate, supervised PVE procedures and must not run in ordinary CI. Unit tests do not certify thermal safety on another machine.
- GitHub Actions runs tests and builds the package on `main`, pull requests, and tag pushes; only a tag matching `debian/changelog` releases it. No coverage threshold is configured. Run `python3 -B -m unittest -v` and inspect the built `.deb` for source changes; for service changes, also verify the packaged unit on the target PVE host.