# Implementation notes (AN515-57, BIOS V1.22)

The EC is driven over WMI GUID `7A4DDFE7-5B5D-40B4-8595-4408E0CC7F56` (ACPI method `WMBH`, `Arg1` = sub-command, `Arg2` = input buffer).

Instance 0.

Buffers: `BHSK` 4B in, `BHGK` 8B / `BHLK` 16B out.

| Arg1 | Dir | Effect                                                                                                                                    |
| ---- | --- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| 0x05 | GET | sysinfo via EC GGSI (also sets `PSEE=1` as a side effect). Selectors: 1 temp, 2 cpu fan RPM, 6 gpu fan RPM                                |
| 0x06 | SET | static zone RGB: `[zone 1/2/4/8, R, G, B]`                                                                                                |
| 0x07 | GET | static zone RGB                                                                                                                           |
| 0x10 | SET | fan duty 0-100 (`[1=cpu/4=gpu, duty]`; firmware range-checks)                                                                             |
| 0x11 | GET | fan duty                                                                                                                                  |
| 0x12 | SET | fan-table/thermal policy byte (no firmware range check — driver clamps 0-4)                                                               |
| 0x13 | GET | fan-table byte                                                                                                                            |
| 0x14 | SET | 16B effect payload: `[mode, speed, brightness, x, dir, R, G, B, ...]`                                                                     |
| 0x15 | GET | 16B effect state (shifted by one leading zero byte)                                                                                       |
| 0x16 | SET | misc, selector in `[0]`: 1 = TKST byte, 0x0B = OPMS (touches overdrive + notifies — not written by this driver)                           |
| 0x17 | GET | misc: only selectors 1 (TKST) and 0x0B (OPMS) return clean status+value; others return stale bytes or call into SMI — driver refuses them |

Notable findings:

- FTBL values match the predator_v4 EC enum:
    - 0 balanced
    - 1 quiet
    - 2 balanced-performance
    - 3 performance
    - 4 eco
- Effect modes 1-5 (breath, neon, wave, shifting, zoom) light the keyboard.
- Static zones (mode `0 = KBLE`):
    - a bare `0x14` with mode 0 blanks the backlight.
    - Mode 0 only displays after the enable sequence:
        1. sysinfo `0x05` poll
        2. `0x02` `BIT(3)|0xF<<40` (zones on)
        3. per-zone `0x06` colours
        4. `0x14` `[2]=brightness, [9]=1`
- `wmi_raw` sysfs node (0600) sends raw `method + bytes` calls for probing.

## Backlogs & notes

- **CoolBoost**: not implemented. Acer's NitroSense exposes a CoolBoost™ toggle on the Fan Control panel; purpose/mapping unknown (no sub-command identified in the table above). Plan: reverse-engineer via WMI/EC probing (risky, needs the user present). No code shipped.
- **CPU+GPU temp combo**: done — the thermal pane shows CPU (package, coretemp hwmon) and GPU (discrete NVIDIA, nvidia-smi) temps; model names read live from `/proc/cpuinfo` + `nvidia-smi` (short forms). The older "cpu package + ec board" combo is replaced.
- **Logs**: the kernel driver logs via printk (`pr_info`/`pr_err`/`pr_warn`) → `dmesg` / `journalctl -k` (also dumped by `sudo python tools/nitro.py status`). `nitrod` and `nitro-tui` are silent (no userspace logging).
