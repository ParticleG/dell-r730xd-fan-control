#!/usr/bin/env python3
"""
Local IPMI fan control adapted for a dual-CPU Dell R730xd running PVE.

Based on https://github.com/greghughespdx/dell-poweredge-fan-control
Copyright (c) 2026 Greg Hughes. Distributed under the accompanying MIT LICENSE.

The default --check command only reads temperatures and fan status. --control
explicitly enables manual PWM control. Missing inputs, unhealthy fans and
thermal limits terminate control and restore the iDRAC automatic policy.
The systemd unit also restores automatic mode after crashes or watchdog expiry.
Neither mechanism can protect against a frozen kernel or an unreachable BMC.

The balanced profile preserves the original 45% floor. Silent and quiet are
unvalidated low-airflow presets: H330 and X540 temperatures are not exposed.
Profile selection persists locally; runtime switching needs no service restart.
"""

import argparse
import math
import subprocess
import time
import sys
import os
import glob
import json
import signal
import socket
import contextlib
import fcntl
import select
import stat
import tempfile
from datetime import datetime

# Local OpenIPMI needs no network session or iDRAC password.
IPMI_CMD = ["/usr/bin/ipmitool", "-I", "open"]

# Sensor names exactly as they appear in `ipmitool sensor` output on this BMC.
SENSOR_INLET = "Inlet Temp"
SENSOR_EXHAUST = "Exhaust Temp"
SENSOR_CPU = "Temp"          # generic per-socket CPU temperature rows
EXPECTED_FANS = {f"Fan{index} RPM" for index in range(1, 7)}
LAST_FAN_RPMS = {}


class SensorError(RuntimeError):
    """A required cooling input is unavailable or unsafe."""


def fan_enable_manual():
    """Dell PowerEdge: switch the BMC out of automatic fan control."""
    subprocess.run(IPMI_CMD + ["raw", "0x30", "0x30", "0x01", "0x00"],
                   capture_output=True, check=True, timeout=5)


def fan_set_percent(percent):
    """Set all fans; never silently accept an unvalidated duty."""
    if not MIN_FAN_PERCENT <= percent <= MAX_FAN_PERCENT:
        raise ValueError(f"PWM outside configured range: {percent}")
    subprocess.run(IPMI_CMD + ["raw", "0x30", "0x30", "0x02", "0xff", hex(percent)],
                   capture_output=True, check=True, timeout=5)


def fan_restore_auto():
    subprocess.run(IPMI_CMD + ["raw", "0x30", "0x30", "0x01", "0x01"],
                   capture_output=True, check=True, timeout=5)


# Each component demands a PWM independently; the highest demand wins.
FAN_PROFILES = {
    "silent": (10, 75),
    "quiet": (25, 75),
    "balanced": (45, 75),
    "performance": (65, 100),
    "full-speed": (100, 100),
}
DEFAULT_PROFILE = "balanced"
MIN_FAN_PERCENT = min(bounds[0] for bounds in FAN_PROFILES.values())
MAX_FAN_PERCENT = max(bounds[1] for bounds in FAN_PROFILES.values())
CONTROL_SOCKET = "/run/ipmi-fan-control/control.sock"
PROFILE_STATE_FILE = "/var/lib/ipmi-fan-control/profile"
HYSTERESIS = 2
RAMP_DOWN_MAX = 2
CYCLE_SECONDS = 15
DRIVE_SAMPLE_SECONDS = 60
DRIVE_SMART_TIMEOUT = 5
EXPECTED_DRIVE_COUNTS = {"hdd": 10, "ssd": 2, "nvme": 2}

# (start increasing PWM, return to the iDRAC automatic policy), in Celsius.
AIR_PROFILES = {"inlet": (26, 32), "exhaust": (38, 50), "cpu": (50, 70)}
DRIVE_PROFILES = {"hdd": (35, 45), "ssd": (45, 60), "nvme": (50, 65)}


def notify_watchdog():
    address = os.environ.get("NOTIFY_SOCKET")
    if not address or not os.environ.get("WATCHDOG_USEC"):
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
        notifier.settimeout(2)
        notifier.sendto(b"WATCHDOG=1", address)


def lerp(value, in_low, in_high, out_low, out_high):
    if value <= in_low:
        return out_low
    if value >= in_high:
        return out_high
    ratio = (value - in_low) / (in_high - in_low)
    return out_low + ratio * (out_high - out_low)


def compute_fan_target(inlet, cpu_max, exhaust, profile):
    return max(component_target(value, AIR_PROFILES[name], name, profile)
               for name, value in (("inlet", inlet), ("cpu", cpu_max),
                                   ("exhaust", exhaust)))


def component_target(temperature, limits, name, profile):
    low, high = limits
    if temperature >= high:
        raise SensorError(f"{name} reached the automatic-control limit: "
                          f"{temperature:g}C >= {high}C")
    return lerp(temperature, low, high, *FAN_PROFILES[profile])


def apply_ramping(current, target, allow_decrease=True):
    target = math.ceil(target)
    if current is None or target >= current:
        return target
    if not allow_decrease or current - target < HYSTERESIS:
        return current
    return max(target, current - RAMP_DOWN_MAX)


def temperature_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not -20 <= value <= 125:
        return None
    return value


def read_all_sensors():
    """Require both CPUs, inlet/exhaust, and all six healthy fan readings."""
    global LAST_FAN_RPMS
    try:
        result = subprocess.run(IPMI_CMD + ["sensor"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        raise SensorError(f"IPMI sensor read failed: {error}") from error
    if result.returncode:
        raise SensorError(f"IPMI sensor read failed: rc={result.returncode}")

    temperatures = {SENSOR_INLET: [], SENSOR_EXHAUST: [], SENSOR_CPU: []}
    fans = {}
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split("|")]
        name = parts[0]
        if name not in temperatures and name not in EXPECTED_FANS:
            continue
        if len(parts) < 4 or parts[3] != "ok":
            raise SensorError(f"Required sensor is not healthy: {line.strip()}")
        try:
            value = float(parts[1])
        except ValueError as error:
            raise SensorError(f"Invalid reading for {name}: {parts[1]}") from error
        if name in temperatures:
            if parts[2] != "degrees C" or temperature_value(value) is None:
                raise SensorError(f"Invalid temperature for {name}: {parts[1]}")
            temperatures[name].append(value)
        else:
            if (parts[2] != "RPM" or not math.isfinite(value)
                    or value <= 600 or name in fans):
                raise SensorError(f"Invalid fan reading: {line.strip()}")
            fans[name] = int(value)

    for name, count in ((SENSOR_INLET, 1), (SENSOR_EXHAUST, 1), (SENSOR_CPU, 2)):
        if len(temperatures[name]) != count:
            raise SensorError(f"Expected {count} valid {name} readings, "
                              f"got {len(temperatures[name])}")
    if fans.keys() != EXPECTED_FANS:
        raise SensorError(f"Missing fan readings: {sorted(EXPECTED_FANS - fans.keys())}")
    LAST_FAN_RPMS = fans
    return (temperatures[SENSOR_INLET][0], temperatures[SENSOR_EXHAUST][0],
            *temperatures[SENSOR_CPU])




# =============================================================================
# Drive temperature sampling (smartctl)
# =============================================================================
# Use smartctl device autodetection: forcing SCSI loses SATA temperatures on
# this PERC H330. A sleeping or unreadable drive causes fallback to automatic
# control instead of waking it, pretending it is cold, or retaining stale data.

def stable_byid_path(kernel_path):
    prefixes = ("wwn-", "scsi-", "ata-", "nvme-eui.", "nvme-")
    matches = []
    for path in glob.glob("/dev/disk/by-id/*"):
        name = os.path.basename(path)
        if "-part" in name or not name.startswith(prefixes):
            continue
        if os.path.realpath(path) == kernel_path:
            matches.append(path)
    priority = ("wwn-", "scsi-", "nvme-eui.", "ata-", "nvme-")
    matches.sort(key=lambda p: next((i for i, pre in enumerate(priority) if os.path.basename(p).startswith(pre)), 99))
    return matches[0] if matches else kernel_path


def discover_drive_devices():
    try:
        result = subprocess.run(
            ["lsblk", "-d", "-J", "-o", "NAME,TYPE,ROTA,TRAN,MODEL,PATH"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise SensorError(f"Drive discovery failed: {error}") from error

    devices = []
    counts = dict.fromkeys(EXPECTED_DRIVE_COUNTS, 0)
    for dev in payload.get("blockdevices", []):
        name = dev.get("name", "")
        if dev.get("type") != "disk":
            continue
        kernel_path = dev.get("path") or f"/dev/{name}"
        if name.startswith("nvme"):
            profile = "nvme"
        elif dev.get("rota") is True or dev.get("rota") == 1:
            profile = "hdd"
        elif dev.get("rota") is False or dev.get("rota") == 0:
            profile = "ssd"
        else:
            raise SensorError(f"Unknown drive class: {name}")
        counts[profile] += 1
        devices.append({
            "path": stable_byid_path(kernel_path),
            "name": name,
            "profile": profile,
            "model": dev.get("model") or "unknown",
        })
    for profile, expected in EXPECTED_DRIVE_COUNTS.items():
        if counts[profile] < expected:
            raise SensorError(f"Expected at least {expected} {profile} drives, "
                              f"found {counts[profile]}")
    return devices


def extract_drive_temp(payload, profile):
    """Read normalized Celsius, never packed ATA SMART raw attributes."""
    values = [payload.get("temperature", {}).get("current")]
    if profile == "nvme":
        log = payload.get("nvme_smart_health_information_log", {})
        values.append(log.get("temperature"))
        values.extend(value for value in log.get("temperature_sensors", []) or []
                      if value != 0)
    temperatures = [value for value in values if temperature_value(value) is not None]
    return max(temperatures) if temperatures else None


def read_drive_temperature(device):
    cmd = ["/usr/sbin/smartctl", "-A", "-j"]
    if device["profile"] != "nvme":
        cmd += ["-n", "standby"]
    cmd.append(device["path"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=DRIVE_SMART_TIMEOUT)
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return {"status": "error", "temp": None, "detail": str(error)}
    # Bits 0..2 indicate command/device/SMART-read failures, including standby.
    # Higher bits report health/history warnings, not invalid temperature units.
    if result.returncode < 0 or result.returncode & 0x07:
        return {"status": "error", "temp": None,
                "detail": f"smartctl-rc={result.returncode}"}
    temp = extract_drive_temp(payload, device["profile"])
    if temp is None:
        return {"status": "no-temp", "temp": None, "detail": "no normalized Celsius"}
    return {"status": "ok" if result.returncode == 0 else "warning",
            "temp": temp, "detail": f"smartctl-rc={result.returncode}"}




def sample_drive_temperatures():
    """Rediscover each sweep so added/removed disks cannot be silently skipped."""
    entries = []
    for device in discover_drive_devices():
        reading = read_drive_temperature(device)
        if reading["temp"] is None:
            raise SensorError(f"{device['name']} temperature unavailable: "
                              f"{reading['detail']}")
        entries.append({"name": device["name"], "profile": device["profile"],
                        "temp": reading["temp"], "status": reading["status"]})
    return summarize_drive_temperatures(entries)


def summarize_drive_temperatures(entries):
    max_by_profile = {}
    for entry in entries:
        profile = entry["profile"]
        max_by_profile[profile] = max(max_by_profile.get(profile, entry["temp"]),
                                      entry["temp"])
    return {"max_by_profile": max_by_profile, "count": len(entries), "devices": entries}




def compute_drive_fan_target(summary, profile):
    return max(component_target(temp, DRIVE_PROFILES[kind], kind, profile)
               for kind, temp in summary["max_by_profile"].items())


def private_directory(path, create=False):
    directory = os.path.dirname(os.path.abspath(path))
    if create:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    info = os.stat(directory)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError(f"Directory must be owned by this user and private: {directory}")
    return directory


def read_profile(path):
    try:
        private_directory(path)
        with open(path) as state:
            profile = state.read(64).strip()
    except FileNotFoundError:
        return None
    if profile not in FAN_PROFILES:
        raise ValueError(f"Invalid saved profile in {path}: {profile!r}")
    return profile


def save_profile(path, profile):
    directory = private_directory(path, create=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".profile-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as state:
            state.write(profile + "\n")
            state.flush()
            os.fsync(state.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def control_socket(path):
    private_directory(path, create=True)
    # Keep ownership through automatic restoration; never unlink a live socket.
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode):
                raise ValueError(f"Refusing to remove a non-socket file: {path}")
            os.unlink(path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.bind(path)
            try:
                os.chmod(path, 0o600)
                listener.listen(4)
                yield listener
            finally:
                os.unlink(path)


def reply_profile(connection, response):
    with connection:
        try:
            connection.sendall(json.dumps(response).encode())
        except OSError:
            # A disconnected CLI must not stop cooling or undo an applied choice.
            pass


def wait_for_profile(listener, timeout, status):
    until = time.monotonic() + timeout
    # An overrun leaves no idle time, but queued commands still need one poll.
    first_poll = True
    while first_poll or time.monotonic() < until:
        first_poll = False
        ready, _, _ = select.select([listener], [], [], max(0, until - time.monotonic()))
        if not ready:
            break
        connection, _ = listener.accept()
        connection.settimeout(1)
        try:
            request = json.loads(connection.recv(1024))
            if not isinstance(request, dict):
                raise ValueError("Expected a profile command object")
            command = request.get("command")
            if command == "get":
                reply_profile(connection, status)
                continue
            profile = request.get("profile")
            if command != "set" or not isinstance(profile, str) or profile not in FAN_PROFILES:
                raise ValueError("Unknown profile command or profile name")
            if profile == status["profile"]:
                reply_profile(connection, status)
                continue
            return connection, profile
        except (OSError, ValueError) as error:
            reply_profile(connection, {"error": str(error)})
    return None


def request_profile(path, profile):
    request = {"command": "get"} if profile is None else {"command": "set", "profile": profile}
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        # The main loop may be finishing a SMART sweep; never interrupt its checks.
        connection.settimeout(120)
        connection.connect(path)
        connection.sendall(json.dumps(request).encode())
        response = json.loads(connection.recv(4096))
    if "error" in response:
        raise RuntimeError(response["error"])
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="one read-only sample (default)")
    mode.add_argument("--control", action="store_true", help="enable manual PWM control")
    mode.add_argument("--monitor", action="store_true", help="repeat read-only samples")
    mode.add_argument("--list-profiles", action="store_true", help="list presets without hardware access")
    mode.add_argument("--get-profile", action="store_true", help="query the running controller")
    mode.add_argument("--set-profile", choices=FAN_PROFILES, help="switch and persist the running profile")
    parser.add_argument("--profile", choices=FAN_PROFILES,
                        help="startup override; otherwise saved profile, then balanced")
    parser.add_argument("--socket", default=CONTROL_SOCKET, help="local control socket path")
    parser.add_argument("--state-file", default=PROFILE_STATE_FILE, help="controller profile state path")
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    args = parser.parse_args()
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("--duration must be finite and positive")
    client_mode = args.list_profiles or args.get_profile or args.set_profile is not None
    if client_mode and (args.profile is not None or args.duration is not None):
        parser.error("--profile and --duration apply only to sampling/control modes")
    if args.list_profiles:
        print(json.dumps({"default": DEFAULT_PROFILE, "profiles": {
            name: {"floor": low, "ceiling": high}
            for name, (low, high) in FAN_PROFILES.items()
        }}))
        return 0
    if args.get_profile or args.set_profile is not None:
        try:
            print(json.dumps(request_profile(args.socket, args.set_profile)))
            return 0
        except (OSError, ValueError, RuntimeError) as error:
            print(f"[ERROR] Profile request failed: {error}", file=sys.stderr, flush=True)
            return 1

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    resources = contextlib.ExitStack()
    listener = None
    try:
        if args.control:
            listener = resources.enter_context(control_socket(args.socket))
    except Exception as error:
        resources.close()
        print(f"[ERROR] Cannot own control socket: {error}", file=sys.stderr, flush=True)
        # A competing controller must not restore auto underneath the owner.
        return 1

    deadline = time.monotonic() + args.duration if args.duration is not None else None
    current_fan = None
    next_cycle = 0
    next_decrease = 0
    last_sample = None
    drive_summary = None
    pending = None
    failure = "Controller stopped before the profile switch completed"
    exit_code = 0
    try:
        saved_profile = read_profile(args.state_file) if args.profile is None else None
        profile = args.profile or saved_profile or DEFAULT_PROFILE
        while deadline is None or time.monotonic() < deadline:
            cycle_start = time.monotonic()
            if cycle_start >= next_cycle:
                next_cycle = cycle_start + CYCLE_SECONDS
            # Never enter manual mode before the first complete valid sample.
            if last_sample is None or cycle_start - last_sample >= DRIVE_SAMPLE_SECONDS:
                drive_summary = sample_drive_temperatures()
                last_sample = cycle_start
            inlet, exhaust, temp1, temp2 = read_all_sensors()
            target = max(compute_fan_target(inlet, max(temp1, temp2), exhaust, profile),
                         compute_drive_fan_target(drive_summary, profile))
            requested_fan = apply_ramping(current_fan, target, cycle_start >= next_decrease)
            if args.control and requested_fan != current_fan:
                if current_fan is None:
                    fan_enable_manual()
                fan_set_percent(requested_fan)
                if current_fan is None or requested_fan < current_fan:
                    # Repeated CLI requests cannot accelerate downward ramping.
                    next_decrease = time.monotonic() + CYCLE_SECONDS
                current_fan = requested_fan
            if args.control and profile != saved_profile:
                save_profile(args.state_file, profile)
                saved_profile = profile
            status = {"profile": profile, "pwm": current_fan,
                      "profile_target_pwm": math.ceil(target)}
            print(json.dumps({
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "mode": "manual" if args.control else "monitor",
                **status, "target_pwm": requested_fan,
                "inlet": inlet, "exhaust": exhaust, "cpu": [temp1, temp2],
                "fan_rpm": LAST_FAN_RPMS,
                "drive_age_seconds": round(time.monotonic() - last_sample, 1),
                "drives": drive_summary,
            }), flush=True)
            notify_watchdog()
            if pending is not None:
                reply_profile(pending, status)
                pending = None
            if not args.control and not args.monitor:
                break
            wait = max(0, next_cycle - time.monotonic())
            if deadline is not None:
                wait = min(wait, max(0, deadline - time.monotonic()))
            if listener is None:
                time.sleep(wait)
            else:
                request = wait_for_profile(listener, wait, status)
                if request is not None:
                    pending, profile = request
    except KeyboardInterrupt:
        pass
    except Exception as error:
        failure = str(error)
        print(f"[ERROR] {error}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        # Repeated stop requests must not interrupt the bounded restore attempt.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if args.control:
            try:
                fan_restore_auto()
                print("[INFO] Restored iDRAC automatic fan control", flush=True)
            except Exception as error:
                print(f"[CRITICAL] Automatic control restore failed: {error}",
                      file=sys.stderr, flush=True)
                exit_code = 1
        if pending is not None:
            reply_profile(pending, {"error": failure})
        resources.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
