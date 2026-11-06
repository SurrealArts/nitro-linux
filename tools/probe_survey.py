#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import pathlib
import re
import subprocess
import sys

SYS = pathlib.Path(os.environ.get("NITRO_SYS", "/sys/devices/platform/nitro"))
DMI = pathlib.Path(os.environ.get("NITRO_DMI", "/sys/class/dmi/id"))
WMI = pathlib.Path(os.environ.get("NITRO_WMI", "/sys/bus/wmi/devices"))
HWMON = pathlib.Path(os.environ.get("NITRO_HWMON", "/sys/class/hwmon"))
PROFILE = pathlib.Path(os.environ.get("NITRO_PROFILE", "/sys/class/platform-profile"))
MODULE = pathlib.Path(os.environ.get("NITRO_MODULE", "/sys/module/nitro"))

BH_GUID = "7a4ddfe7-5b5d-40b4-8595-4408e0cc7f56"
APGE_GUID = "61ef69ea-865c-4bc3-a502-a0deba0cb531"

DMI_KEYS = (
    "sys_vendor",
    "product_name",
    "board_name",
    "board_vendor",
    "bios_version",
    "bios_date",
)

TYPED = (
    "fan_cpu",
    "fan_gpu",
    "fan_mode",
    "coolboost",
    "ftbl",
    "kbd_timeout",
    "kbd_bright",
    "kbd_zone1",
    "kbd_static",
    "kbd_dynamic",
    "ec_cpu_temp",
    "tkst",
    "opms",
)

GETS = (
    ("0x05 sysinfo", "5 0 0 0 0 0 0 0"),
    ("0x07 static", "7 0 0 0 0 0 0 0"),
    ("0x0f fan_behavior", "15 0 0 0 0 0 0 0"),
    ("0x11 fan cpu", "17 1 0 0 0 0 0 0"),
    ("0x11 fan gpu", "17 4 0 0 0 0 0 0"),
    ("0x13 ftbl", "19 0 0 0 0 0 0 0"),
    ("0x15 dyn", "21 0 0 0 0 0 0 0"),
    ("0x17 misc tkst", "23 1 0 0 0 0 0 0"),
    ("0x17 misc opms", "23 11 0 0 0 0 0 0"),
)


def read(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return None


def run(argv, timeout=15):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip()


def write_node(path, text):
    try:
        fd = os.open(str(path), os.O_WRONLY)
    except OSError as e:
        return str(e)
    try:
        os.write(fd, text.encode())
    except OSError as e:
        return str(e)
    finally:
        os.close(fd)
    return None


def read_node(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError as e:
        return f"error: {e}"


def wmi_raw(payload):
    node = SYS / "wmi_raw"
    if not node.exists():
        return "(wmi_raw not present)"
    error = write_node(node, payload + "\n")
    return f"error: {error}" if error else read_node(node)


def ec_read(reg):
    node = SYS / "ec_raw"
    if not node.exists():
        return "(ec_raw not present)", None
    error = write_node(node, f"0x{reg:02x}\n")
    if error:
        return f"error: {error}", None
    text = read_node(node)
    m = re.search(r"val=0x([0-9a-f]{2})", text)
    return text, m.group(1) if m else None


def section_dmi():
    lines = []
    for key in DMI_KEYS:
        value = read(DMI / key)
        lines.append(f"{key}: {value if value is not None else '?'}")
    kernel = run(["uname", "-r"])
    lines.append(f"kernel: {kernel if kernel else '?'}")
    return lines


def section_nitro():
    version = run(["modinfo", "-F", "version", "nitro"])
    lines = [
        f"version: {version if version else '?'}",
        f"capabilities: {read(SYS / 'capabilities') or '?'}",
    ]
    params = [
        f"{param.name}={read(param)}"
        for param in sorted(MODULE.glob("parameters/*"))
    ]
    lines.append(f"params: {' '.join(params) if params else '(none)'}")
    return lines


def section_wmi():
    if not WMI.is_dir():
        return [f"{WMI} not found"]
    lines = []
    for entry in sorted(WMI.iterdir()):
        lines.append(entry.name)
    return lines or ["(empty)"]


def section_typed():
    lines = []
    for name in TYPED:
        if not (SYS / name).exists():
            continue
        value = read(SYS / name)
        lines.append(f"{name}: {value if value is not None else 'unreadable'}")
    return lines or ["(no attributes)"]


def section_hwmon():
    lines = []
    for hw in sorted(HWMON.glob("hwmon*")):
        if read(hw / "name") != "nitro":
            continue
        lines.append(hw.name)
        for entry in sorted(hw.iterdir()):
            if entry.name.startswith(("temp", "fan", "pwm")):
                value = read(entry)
                if value is not None:
                    lines.append(f"  {entry.name}: {value}")
    return lines or ["(no nitro hwmon device)"]


def section_profile():
    lines = []
    for pp in sorted(PROFILE.glob("platform-profile-*")):
        lines.append(
            f"{pp.name}: profile={read(pp / 'profile')} choices={read(pp / 'choices')}"
        )
    return lines or ["(not registered; probe mode does not create it)"]


def section_get():
    return [f"{label}: {wmi_raw(payload)}" for label, payload in GETS]


def section_apge():
    lines = []
    ok = True

    present = set()
    if WMI.is_dir():
        present = {entry.name.lower() for entry in WMI.iterdir()}
    if not any(APGE_GUID in name for name in present):
        lines.append(f"GUID {APGE_GUID} not present; the APGE path cannot work")
        ok = False
    if not any(BH_GUID in name for name in present):
        lines.append(f"GUID {BH_GUID} not present; the machine is not using WMBH")
        ok = False

    node = SYS / "apge_raw"
    if node.exists():
        error = write_node(node, "2 0x88401\n")
        if error:
            lines.append(f"get: error: {error}")
            ok = False
        else:
            lines.append(f"get: {read_node(node)}")
    else:
        lines.append("apge_raw: not present in this build")
        ok = False

    text, _ = ec_read(0x06)
    lines.append(f"ec 0x06: {text}")
    return lines, ok


def sweep_ec():
    values = {}
    failed = []
    for reg in range(256):
        _, value = ec_read(reg)
        if value is None:
            failed.append(f"{reg:02x}")
        else:
            values[reg] = value
    return values, failed


def section_ec(values, failed):
    lines = ["format: reg=value in hex, 8 per row"]
    items = [f"{reg:02x}={values[reg]}" for reg in sorted(values)]
    for start in range(0, len(items), 8):
        lines.append(" ".join(items[start : start + 8]))
    if failed:
        lines.append(f"failed: {' '.join(failed)}")
    return lines or ["(no registers read)"]


def section_ec_diff(first, second):
    changed = [
        f"{reg:02x}: {first[reg]} -> {second[reg]}"
        for reg in sorted(first)
        if reg in second and first[reg] != second[reg]
    ]
    return ["registers that moved between the two sweeps"] + (changed or ["(none)"])


def section_log():
    out = run(["journalctl", "-k", "-b", "0", "--no-pager"])
    if out is None:
        out = run(["dmesg"])
    if out is None:
        return ["(kernel log unreadable)"]
    lines = [line for line in out.splitlines() if "nitro: " in line]
    return lines[-25:] or ["(no nitro lines)"]


def collect(do_sweep):
    sections = []
    skipped = []

    sections.append(("dmi", section_dmi()))
    sections.append(("nitro", section_nitro()))
    sections.append(("wmi", section_wmi()))
    sections.append(("typed", section_typed()))
    sections.append(("hwmon", section_hwmon()))
    sections.append(("profile", section_profile()))

    if (SYS / "wmi_raw").exists():
        sections.append(("get", section_get()))
    else:
        skipped.append("get")
        sections.append(("get", ["(wmi_raw not present)"]))

    apge_lines, apge_ok = section_apge()
    if not apge_ok:
        skipped.append("apge")
    sections.append(("apge", apge_lines))

    if not do_sweep:
        sections.append(("ec", ["(skipped with --no-ec-sweep)"]))
        sections.append(("ec-diff", ["(skipped with --no-ec-sweep)"]))
    elif not (SYS / "ec_raw").exists():
        skipped.append("ec")
        sections.append(("ec", ["(ec_raw not present)"]))
        sections.append(("ec-diff", ["(ec_raw not present)"]))
    else:
        print("sweeping 256 EC registers twice, this takes a moment", file=sys.stderr)
        first, first_failed = sweep_ec()
        second, second_failed = sweep_ec()
        sections.append(("ec", section_ec(first, first_failed)))
        sections.append(("ec-diff", section_ec_diff(first, second)))
        if first_failed or second_failed:
            skipped.append("ec-partial")

    if MODULE.exists():
        sections.append(("log", section_log()))
    else:
        sections.append(("log", ["(module not loaded)"]))
    return sections, skipped


def main(argv):
    do_sweep = True
    for arg in argv:
        if arg == "--no-ec-sweep":
            do_sweep = False
        elif arg in ("-h", "--help"):
            print("usage: probe_survey.py [--no-ec-sweep]")
            print("")
            print("Collects the read-only evidence for a Model Support Report.")
            print("Needs root for wmi_raw, ec_raw and apge_raw.")
            print("Exit 0 complete, 1 incomplete, 2 cannot run.")
            return 0
        else:
            print(f"unknown argument: {arg}")
            return 2

    if os.geteuid() != 0:
        print("survey needs root: it reads wmi_raw, ec_raw and apge_raw")
        print("  sudo python3 tools/probe_survey.py")
        return 2
    if not (SYS / "capabilities").exists():
        print(f"nitro driver not found at {SYS}")
        print("load it first: sudo modprobe nitro probe=1")
        return 2

    sections, skipped = collect(do_sweep)

    print("## nitro survey")
    print("")
    print("```text")
    for name, lines in sections:
        print(f"[{name}]")
        for line in lines:
            print(line)
        print("")
    if skipped:
        print(f"survey: incomplete (missing: {' '.join(sorted(set(skipped)))})")
    else:
        print("survey: complete")
    print("```")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
