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

The 45% floor is a conservative local trial setting, not a Dell airflow
certification. H330 and X540 temperatures are not exposed on this host.
Do not lower it based solely on CPU or disk temperatures.
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
    if not FAN_FLOOR <= percent <= FAN_CEILING:
        raise ValueError(f"PWM outside configured range: {percent}")
    subprocess.run(IPMI_CMD + ["raw", "0x30", "0x30", "0x02", "0xff", hex(percent)],
                   capture_output=True, check=True, timeout=5)


def fan_restore_auto():
    subprocess.run(IPMI_CMD + ["raw", "0x30", "0x30", "0x01", "0x01"],
                   capture_output=True, check=True, timeout=5)


# Each component demands a PWM independently; the highest demand wins.
FAN_FLOOR = 45
FAN_CEILING = 75
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


def compute_fan_target(inlet, cpu_max, exhaust):
    return max(component_target(value, AIR_PROFILES[name], name)
               for name, value in (("inlet", inlet), ("cpu", cpu_max),
                                   ("exhaust", exhaust)))


def component_target(temperature, limits, name):
    low, high = limits
    if temperature >= high:
        raise SensorError(f"{name} reached the automatic-control limit: "
                          f"{temperature:g}C >= {high}C")
    return lerp(temperature, low, high, FAN_FLOOR, FAN_CEILING)


def apply_ramping(current, target):
    target = math.ceil(target)
    if current is None or target >= current:
        return target
    if current - target < HYSTERESIS:
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




def compute_drive_fan_target(summary):
    return max(component_target(temp, DRIVE_PROFILES[profile], profile)
               for profile, temp in summary["max_by_profile"].items())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="one read-only sample (default)")
    mode.add_argument("--control", action="store_true", help="enable manual PWM control")
    mode.add_argument("--monitor", action="store_true", help="repeat read-only samples")
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    args = parser.parse_args()
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("--duration must be finite and positive")

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.monotonic() + args.duration if args.duration is not None else None
    current_fan = None
    last_sample = None
    drive_summary = None
    exit_code = 0
    try:
        while deadline is None or time.monotonic() < deadline:
            cycle_start = time.monotonic()
            # Never enter manual mode before the first complete valid sample.
            if last_sample is None or cycle_start - last_sample >= DRIVE_SAMPLE_SECONDS:
                drive_summary = sample_drive_temperatures()
                last_sample = cycle_start
            inlet, exhaust, temp1, temp2 = read_all_sensors()
            target = max(compute_fan_target(inlet, max(temp1, temp2), exhaust),
                         compute_drive_fan_target(drive_summary))
            requested_fan = apply_ramping(current_fan, target)
            if args.control and requested_fan != current_fan:
                if current_fan is None:
                    fan_enable_manual()
                fan_set_percent(requested_fan)
                current_fan = requested_fan
            print(json.dumps({
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "mode": "manual" if args.control else "monitor",
                "pwm": current_fan, "target_pwm": requested_fan,
                "inlet": inlet, "exhaust": exhaust, "cpu": [temp1, temp2],
                "fan_rpm": LAST_FAN_RPMS,
                "drive_age_seconds": round(time.monotonic() - last_sample, 1),
                "drives": drive_summary,
            }), flush=True)
            notify_watchdog()
            if not args.control and not args.monitor:
                break
            wait = max(0, CYCLE_SECONDS - (time.monotonic() - cycle_start))
            if deadline is not None:
                wait = min(wait, max(0, deadline - time.monotonic()))
            time.sleep(wait)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"[ERROR] {error}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        if args.control:
            try:
                fan_restore_auto()
                print("[INFO] Restored iDRAC automatic fan control", flush=True)
            except Exception as error:
                print(f"[CRITICAL] Automatic control restore failed: {error}",
                      file=sys.stderr, flush=True)
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
