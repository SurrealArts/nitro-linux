#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

SYS = pathlib.Path(os.environ.get("NITRO_SYS", "/sys/devices/platform/nitro"))
HWMON = pathlib.Path(os.environ.get("NITRO_HWMON", "/sys/class/hwmon"))
POWER = pathlib.Path(os.environ.get("NITRO_POWER", "/sys/class/power_supply"))
LEDS = pathlib.Path("/sys/class/leds")
PROFILE = pathlib.Path("/sys/class/platform-profile")
SUSPEND = os.environ.get("NITRO_SUSPEND", "systemctl suspend").split()

SETTINGS = (
    "fan_cpu",
    "fan_gpu",
    "fan_mode",
    "coolboost",
    "kbd_timeout",
    "kbd_bright",
    "ftbl",
    "kbd_static",
    "kbd_dynamic",
)
EC_REGS = {"kbd_timeout_ec": 0x06}
INFO_KEYS = ("ac_online", "ppd")


def read(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return None


def read_ec(reg):
    node = SYS / "ec_raw"
    try:
        node.write_text(f"0x{reg:02x}\n")
        text = node.read_text().strip()
    except OSError:
        return None
    m = re.search(r"val=0x([0-9a-f]+)", text)
    return f"0x{m.group(1)}" if m else text


def capture():
    data = {}
    for name in SETTINGS:
        value = read(SYS / name)
        if value is not None:
            data[name] = value
    for path in sorted(PROFILE.glob("platform-profile-*/profile")):
        data["profile"] = read(path)
        break
    for led in sorted(LEDS.glob("nitro:rgb:zone*")):
        data[f"led_{led.name}"] = read(led / "brightness")
    for hw in HWMON.glob("hwmon*"):
        if read(hw / "name") != "nitro":
            continue
        for pwm in sorted(hw.glob("pwm*_enable")):
            data[pwm.name] = read(pwm)
    ac = read(POWER / "ACAD" / "online")
    if ac is not None:
        data["ac_online"] = "battery" if ac == "0" else "ac"
    ppd = shutil.which("powerprofilesctl")
    if ppd:
        try:
            data["ppd"] = subprocess.run(
                [ppd, "get"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    for key, reg in EC_REGS.items():
        value = read_ec(reg)
        if value is not None:
            data[key] = value
    return data


def save(data, path):
    pathlib.Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load(path):
    return json.loads(pathlib.Path(path).read_text())


def compare(before, after):
    changed = []
    for key in sorted(set(before) | set(after)):
        if key in INFO_KEYS:
            continue
        if before.get(key) != after.get(key):
            changed.append(key)
            print(f"CHANGED {key}: {before.get(key)} -> {after.get(key)}")
    for key in INFO_KEYS:
        if key in before and before.get(key) != after.get(key):
            print(f"note: {key} {before.get(key)} -> {after.get(key)}")
    return changed


def slept_since(stamp):
    if not shutil.which("journalctl"):
        return None
    try:
        out = subprocess.run(
            ["journalctl", "-k", "-b", "0", "--no-pager", "--since", stamp],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return "PM: suspend entry" in out


def run():
    if os.geteuid() != 0:
        print("run needs root: it reads EC registers and suspends the machine")
        print("  sudo python3 tools/check_suspend.py")
        return 2
    if not (SYS / "capabilities").exists():
        print(f"nitro driver not found at {SYS}")
        return 2

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="nitro-suspend-"))
    before = capture()
    save(before, workdir / "before.json")
    print(f"captured {len(before)} values -> {workdir / 'before.json'}")
    if before.get("ac_online"):
        print(f"power: {before['ac_online']}")
    if "kbd_timeout_ec" not in before:
        print("note: EC 0x06 unreadable; the timeout is compared from its cached value")

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"suspending with '{' '.join(SUSPEND)}'; wake the machine to continue")
    subprocess.run(SUSPEND, check=False)
    time.sleep(2)

    after = capture()
    save(after, workdir / "after.json")
    print(f"captured {len(after)} values -> {workdir / 'after.json'}")

    changed = compare(before, after)
    slept = slept_since(stamp)
    if slept is False:
        print("suspend check FAILED: no 'PM: suspend entry' in the kernel log, the machine did not sleep")
        return 2
    if slept is None:
        print("note: could not confirm the sleep from journalctl")
    if changed:
        print(f"suspend check FAILED: {len(changed)} setting(s) changed across the sleep")
        return 1
    print("suspend check OK: all settings retained")
    return 0


def cmd_capture(argv):
    data = capture()
    if argv:
        save(data, argv[0])
        print(f"captured {len(data)} values -> {argv[0]}")
        return 0
    for key in sorted(data):
        print(f"{key}={data[key]}")
    return 0


def cmd_compare(argv):
    if len(argv) != 2:
        print("usage: check_suspend.py compare <before.json> <after.json>")
        return 2
    changed = compare(load(argv[0]), load(argv[1]))
    if changed:
        print(f"suspend check FAILED: {len(changed)} setting(s) changed")
        return 1
    print("suspend check OK: all settings retained")
    return 0


def main(argv):
    if not argv or argv[0] == "run":
        return run()
    if argv[0] == "capture":
        return cmd_capture(argv[1:])
    if argv[0] == "compare":
        return cmd_compare(argv[1:])
    print("usage: check_suspend.py [run | capture [file] | compare <before> <after>]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
