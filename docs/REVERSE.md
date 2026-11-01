# Implementation notes (AN515-57, BIOS V1.22)

The EC is driven over WMI GUID `7A4DDFE7-5B5D-40B4-8595-4408E0CC7F56` (ACPI method `WMBH`, `Arg1` = sub-command, `Arg2` = input buffer).

Instance 0.

Buffers: `BHSK` 4B in, `BHGK` 8B / `BHLK` 16B out.

| Arg1 | Dir | Effect                                                                                                                                    |
| ---- | --- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| 0x02 | SET | LED/zones-on bitmap (`BIT(3)` zones-on + zone mask from quirk; AN515-57 uses `0xF<<40`)                                                   |
| 0x05 | GET | sysinfo via EC GGSI (also sets `PSEE=1` as a side effect). Selectors: 1 temp, 2 cpu fan RPM, 6 gpu fan RPM                                |
| 0x06 | SET | static zone RGB: `[zone 1/2/4/8, R, G, B]`                                                                                                |
| 0x07 | GET | static zone RGB                                                                                                                           |
| 0x0E | SET | fan behavior (`bitmap, 0, cpu \| gpu<<6`)                                                                                                 |
| 0x0F | GET | fan behavior (status, modes)                                                                                                              |
| 0x10 | SET | fan duty 0-100 (`[1=cpu/4=gpu, duty]`; firmware range-checks)                                                                             |
| 0x11 | GET | fan duty                                                                                                                                  |
| 0x12 | SET | fan-table/thermal policy byte (no firmware range check — driver clamps 0-4)                                                               |
| 0x13 | GET | fan-table byte                                                                                                                            |
| 0x14 | SET | 16B effect payload: `[mode, speed, brightness, x, dir, R, G, B, ...]`                                                                     |
| 0x15 | GET | 16B effect state (shifted by one leading zero byte; driver checks status byte at offset 0)                                                |
| 0x16 | SET | misc, selector in `[0]`: 1 = TKST byte, 0x0B = OPMS (touches overdrive + notifies — not written by this driver)                           |
| 0x17 | GET | misc: only selectors 1 (TKST) and 0x0B (OPMS) return clean status+value; others return stale bytes or call into SMI — driver refuses them |

### Keyboard backlight timeout (WMI APGE)

The 30-second timeout is not on the gaming GUID. It uses the APGE interface: GUID `61EF69EA-865C-4BC3-A502-A0DEBA0CB531`, ACPI object id `AA` -> method `WMAA` in SSDT12. Method `1` is SET and `2` is GET; both take an 8-byte little-endian payload that `WMAA` forwards to firmware through `WSMI`.

| Action  | Payload                            | Reply                                  |
| ------- | ---------------------------------- | -------------------------------------- |
| get     | `0x00088401`                       | `0x1E0000080000` = on, `0x80000` = off |
| set off | `0x00088402`                       | `0x0` = accepted                       |
| set on  | `0x1E0000088402` (`0x1E` = 30-sec) | `0x0` = accepted                       |

The GET reply mirrors the last SET payload: bits 40-47 are the timeout in seconds (`0x1E` = 30), and firmware sets bit 19 (`0x80000`) once a SET has run in that boot. SET itself returns `0x0`, so the driver confirms by GET.

Verified on AN515-57 (module reload + `/sys/.../kbd_timeout`):

| State                           | GET reply                                 | `kbd_timeout` |
| ------------------------------- | ----------------------------------------- | ------------- |
| set on                          | `0x1E0000080000`                          | 1             |
| set off                         | `0x0000000000080000`                      | 0             |
| before the first SET after boot | `0x1E0000000000` (duration, bit 19 clear) | 1             |

WMI APGE GUID: `61EF69EA-865C-4BC3-A502-A0DEBA0CB531`

The boot-time value is read as **on**, since the 30-sec duration is what the firmware stores for an armed timeout; `0x80000` alone is off. A GET reply of `0x0` or one with unexpected bits is treated as `-EPROTO` and drops the capability at load.

EC cross-check (AN515-57, `ec_read()`): the setting is backed by EC `0x06`, which reads `0x1E` while the timeout is on and `0x00` while it is off.

- Driver: `kbd_timeout` RW sysfs, accepts `0`/`1`/`on`/`off`; SET is verified by a follow-up GET.
- Gated by `NITRO_CAP_KBD_TIMEOUT`. If the APGE GUID is missing the capability is dropped at load and the module still probes.
- The probe-time value is saved and restored on module unload.

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
- `wmi_raw` sysfs node sends raw method calls for probing, accepting space-separated method and argument bytes in base 0 (decimal, `0x` hex, or leading-`0` octal, e.g. `17`, `0x11`, `021`). It is root-only (`0600`) and rejects values above `0xFF` and more than 17 tokens. In probe mode, write methods are rejected.
