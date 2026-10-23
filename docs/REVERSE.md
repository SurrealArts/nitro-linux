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

### CoolBoost (direct EC)

CoolBoost is not a WMI command on this machine. It is direct EC register
`0x10`: **0x00 = off, 0x01 = on**. Windows NitroSense sets it directly; a
`GetGamingMiscSetting` scan found no selector that tracks it.

- Driver: `coolboost` RW sysfs, accepts `0`/`1`/`on`/`off`.
- Reads always work. Writes are allowed only while both fans report `auto`;
  in `max`/`custom` the driver returns `-EPERM` and leaves the EC value
  retained. This matches NitroSense, where the toggle is greyed outside Auto.
- The probe-time value is saved and restored on module unload.
- Evidence: `windows-captures/coolboost/FINDINGS.md` (ignored). Auto idle effect
  at matched temperatures: +418 CPU RPM / +451 GPU RPM on Windows at 92 °C.
  Linux spot check at 62 °C CPU / 53 °C GPU: about 2040/2307 RPM off and
  2255/2521 RPM on, i.e. roughly +215/+215; the direction matches.

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
- `wmi_raw` sysfs node sends raw `method + bytes` calls for probing.
