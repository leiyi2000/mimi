#!/usr/bin/env python3
"""Capture a real device fingerprint over ADB and write data/device_profile.json.

Optional: run against a connected phone to pin the QIMEI36 to that device. Without
it, the plugin fabricates and persists a plausible profile on first login. The
field set matches auth.device.DeviceProfile.
"""
from __future__ import annotations

import os
import re
import sys
import json
import shutil
import argparse
import subprocess
from pathlib import Path


class CaptureError(RuntimeError):
    pass


GETPROP_FIELDS = {
    "android_release": "ro.build.version.release",
    "model": "ro.product.model",
    "brand": "ro.product.brand",
    "board": "ro.product.board",
    "device": "ro.product.device",
    "first_api_level": "ro.product.first_api_level",
    "manufacturer": "ro.product.manufacturer",
    "product_name": "ro.product.name",
    "build_host": "ro.build.host",
}

# Matches auth.qimei.DEFAULT_PROFILE_PATH: the plugin's own data/ dir, which is
# also the compose mount source, so a host-side capture lands where the plugin
# (host or container) reads it by default.
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "device_profile.json"


def _serials(binary: str) -> list[str]:
    done = subprocess.run([binary, "devices"], text=True, capture_output=True, check=False)
    if done.returncode != 0:
        raise CaptureError(f"adb devices failed: {done.stderr.strip()}")
    serials = []
    for line in done.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _connect(serial: str | None) -> tuple[str, str]:
    binary = os.environ.get("ADB") or shutil.which("adb")
    if not binary:
        raise CaptureError("adb not found; install platform-tools or set ADB")
    serials = _serials(binary)
    if not serials:
        raise CaptureError("no authorized device connected")
    if serial is not None:
        if serial not in serials:
            raise CaptureError(f"device {serial} is not connected")
        return binary, serial
    if len(serials) > 1:
        raise CaptureError(f"multiple devices; pass --serial ({', '.join(serials)})")
    return binary, serials[0]


def _shell(binary: str, serial: str, command: str) -> str:
    done = subprocess.run(
        [binary, "-s", serial, "shell", command], text=True, capture_output=True, check=False
    )
    if done.returncode != 0:
        detail = done.stderr.strip() or done.stdout.strip()
        raise CaptureError(f"adb shell {command!r} failed: {detail}")
    return done.stdout.strip()


def _clean(value: str) -> str:
    return "" if value in {"", "null"} else value


def capture(binary: str, serial: str) -> dict:
    pattern = re.compile(r"^\[(.+?)\]: \[(.*)\]$")
    props: dict[str, str] = {}
    for line in _shell(binary, serial, "getprop").splitlines():
        matched = pattern.match(line)
        if matched:
            props[matched.group(1)] = matched.group(2)

    profile: dict = {field: props.get(prop, "") for field, prop in GETPROP_FIELDS.items()}
    try:
        profile["android_api"] = int(props.get("ro.build.version.sdk") or "")
    except ValueError as exc:
        raise CaptureError(f"non-numeric SDK level: {props.get('ro.build.version.sdk')!r}") from exc
    profile["android_id"] = _clean(_shell(binary, serial, "settings get secure android_id"))
    profile["boot_id"] = _clean(_shell(binary, serial, "cat /proc/sys/kernel/random/boot_id"))
    profile["kernel"] = _clean(_shell(binary, serial, "uname -a"))
    try:
        route = _shell(binary, serial, "ip route get 1")
        matched = re.search(r"src (\S+)", route)
        profile["local_ip"] = matched.group(1) if matched else ""
    except CaptureError:
        profile["local_ip"] = ""
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a device profile via ADB.")
    parser.add_argument("--serial", help="target serial when several devices are connected")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="destination JSON")
    args = parser.parse_args()

    try:
        binary, serial = _connect(args.serial)
        profile = capture(binary, serial)
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(profile, indent=2, ensure_ascii=True) + "\n")
    print(f"wrote {args.output} ({profile['brand']} {profile['model']}, API {profile['android_api']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
