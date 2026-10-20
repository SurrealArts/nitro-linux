#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import pathlib, subprocess, sys

BASE = pathlib.Path("/sys/devices/platform/nitro")

def read(name):
    try:
        return (BASE / name).read_text().strip()
    except Exception as e:
        return f"MISSING ({e})"

def status():
    for name in [
        "fan_cpu",
        "fan_gpu",
        "ftbl",
        "tkst",
        "opms",
        "kbd_static",
        "kbd_dynamic",
        "kbd_zone1",
        "ec_cpu_temp"]:
        print(f"{name}: {read(name)}")
    for p in ["/sys/class/platform-profile/platform-profile-0/profile"]:
        try:
            print(f"platform_profile: {open(p).read().strip()}")
        except Exception as e:
            print(f"platform_profile: MISSING ({e})")
    print("--- dmesg nitro ---")
    try:
        out = subprocess.run(["sudo", "dmesg"], capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            if "nitro" in line.lower():
                print(line[-220:])
    except Exception as e:
        print(f"dmesg failed: {e}")

def set_static(zone, r, g, b):
    zone = int(zone)
    assert zone in (1, 2, 4, 8), "zone must be 1,2,4,8"
    for v in (r, g, b):
        assert 0 <= int(v) <= 255
    (BASE / "kbd_static").write_text(f"{zone} {r} {g} {b}")
    print(f"set zone {zone} to {r} {g} {b}: {read('kbd_static')}")

def set_fan(which, duty):
    assert which in ("cpu", "gpu"), "which cpu|gpu"
    duty = int(duty)
    assert 0 <= duty <= 100
    if duty < 20:
        print("WARNING: very low duty, overheat risk")
    (BASE / f"fan_{which}").write_text(str(duty))
    print(f"fan_{which}: {read(f'fan_{which}')}")

def set_dyn(mode, speed, brightness, direction, r, g, b):
    for name, v, lo, hi in [
        ("mode", int(mode), 1, 5),
        ("speed", int(speed), 0, 255),
        ("brightness", int(brightness), 0, 100),
        ("direction", int(direction), 1, 2),
        ("r", int(r), 0, 255),
        ("g", int(g), 0, 255),
        ("b", int(b), 0, 255)]:
        assert lo <= v <= hi, f"{name} out of range"
    (BASE / "kbd_dynamic").write_text(f"{mode} {speed} {brightness} {direction} {r} {g} {b}")
    print(f"kbd_dynamic: {read('kbd_dynamic')}")

def usage():
    prog = pathlib.Path(sys.argv[0]).name
    print("usage:")
    print(f"    {prog} [status]")
    print("               | rgb <zone 1|2|4|8> <r> <g> <b>")
    print("               | fan <cpu|gpu> <0-100>")
    print("               | dyn <mode 1-5> <speed> <brightness 0-100> <dir 1-2> <r> <g> <b>")
    print("               | profile <low-power|quiet|balanced|balanced-performance|performance>")
    print("modes:")
    print("    1 breath")
    print("    2 neon")
    print("    3 wave")
    print("    4 shifting")
    print("    5 zoom")
    print("Note:")
    print("    sysfs is root-owned, run with sudo for writes,")
    print(f"    e.g. sudo {prog} rgb 1 255 0 0")

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("status",):
        status()
    elif sys.argv[1] in ("help", "-h", "--help"):
        usage()
    elif sys.argv[1] == "rgb" and len(sys.argv) == 6:
        set_static(*sys.argv[2:6])
    elif sys.argv[1] == "fan" and len(sys.argv) == 4:
        set_fan(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "dyn" and len(sys.argv) == 9:
        set_dyn(*sys.argv[2:9])
    elif sys.argv[1] == "profile" and len(sys.argv) == 3:
        assert sys.argv[2] in ("low-power", "quiet", "balanced", "balanced-performance", "performance"), "bad profile"
        pathlib.Path("/sys/class/platform-profile/platform-profile-0/profile").write_text(sys.argv[2])
        print(f"platform_profile: {open('/sys/class/platform-profile/platform-profile-0/profile').read().strip()}")
    else:
        usage()
