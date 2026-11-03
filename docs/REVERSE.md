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

### Sensors (sysinfo 0x05)

`WMBH` `Arg1` = `0x05` takes the same 64-bit command word the in-tree
`acer-wmi` uses: bits 7-0 select the command, bits 15-8 the sensor. The reply
carries the status in bits 7-0, the reading in bits 23-8 and the
supported-sensor bitmap in bits 39-24.

| Command | Selector | Reply                          |
| ------- | -------- | ------------------------------ |
| `0x00`  | none     | supported-sensor bitmap        |
| `0x01`  | sensor   | reading (degrees C or RPM)     |
| `0x02`  | none     | battery status                 |

Bitmap bit `n` means sensor `n + 1` is supported. AN515-57 returns `0x0227`:
CPU temperature (1), CPU fan (2), external temperature 2 (3), GPU fan (6) and
GPU temperature (`0x0A`). At idle `0x01` read 83 °C, `0x03` 74 °C and `0x0A`
53 °C, matching the `acer-wmi` `predator_v4` sensor list. The driver polls
this command as step 1 of the static-lighting enable sequence, so the bitmap
costs no extra call at probe.

The bitmap is read once at load and hides the hwmon channels the firmware does
not report. A zero bitmap falls back to the quirk's `fan_count` and the single
CPU temperature.

| hwmon attribute                            | Channel               | Source         |
| ------------------------------------------ | --------------------- | -------------- |
| `temp1_input` `temp2_input` `temp3_input`  | CPU / GPU / external 2 | sensors 1, `0x0A`, 3 |
| `fan1_input` `fan2_input`                  | CPU / GPU             | sensors 2, 6   |
| `pwm1` `pwm2`                              | CPU / GPU duty 0-255  | `0x10` / `0x11` |
| `pwm1_enable` `pwm2_enable`                | 0 max, 1 custom, 2 auto | `0x0E` / `0x0F` |

`pwmN` reads `round(duty * 255 / 100)`; a write converts back to a percentage
and switches that fan to custom, the same path `fan_cpu` and `fan_gpu` use.
`pwmN_enable` writes only that fan's mode and leaves the other fan alone. The
PWM channels are not created in probe mode.

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
- The value read at module load is saved and restored on module unload; probe mode reads it without saving.
- `apge_raw` is the root-only (`0600`) diagnostic for a board whose reply does not parse: `echo "2 0x88401" > /sys/devices/platform/nitro/apge_raw`, then read it back for `status`, the reply type and length, and the 64-bit value. Only method `2` (GET) is accepted, so the node cannot change firmware state. ACPI integers and buffers of 1-8 bytes are accepted and zero-extended; anything else fails with `-EPROTO`.
- A GET that returns an unparsable value, or a call that the ACPI method rejects, is logged at `pr_info` (`APGE keyboard timeout reply 0x...`, `keyboard timeout query failed <errno>`) before the capability is dropped, so a model report can carry the raw reply without dynamic debug.

### CoolBoost (direct EC)

CoolBoost is not a WMI command on this machine. It is direct EC register
`0x10`: **0x00 = off, 0x01 = on**. Windows NitroSense sets it directly; a
`GetGamingMiscSetting` scan found no selector that tracks it.

- Driver: `coolboost` RW sysfs, accepts `0`/`1`/`on`/`off`.
- Reads always work. Writes are allowed only while both fans report `auto`;
  in `max`/`custom` the driver returns `-EPERM` and leaves the EC value
  retained. This matches NitroSense, where the toggle is greyed outside Auto.
- The value read at module load is saved and restored on module unload. Probe entries never expose CoolBoost.
- Evidence: captured Windows EC traces (not stored in this repository). Auto
  idle effect at matched temperatures: +418 CPU RPM / +451 GPU RPM on Windows at 92 °C.
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
- `writeback` is the opt-in same-value write-back probe for unverified boards (`probe_writes=1`, root-only, hidden otherwise). `echo fan_cpu > writeback` reads the current mode and duty, writes that duty back through `0x0E`/`0x10`, verifies it through `0x11`, then restores the previous `0x0E` modes; `fan_mode`, `ftbl` (`0x13`), `kbd_timeout` (APGE SET then GET) and `coolboost` (EC `0x10`) work the same way. The node takes a path name, not a value, so every byte it writes was read from that board first and a path that cannot read does not write. The fan and mode guards live in `src/writeback.h`, shared with `tools/test_writeback_guards.c` so `make test` covers them without hardware.
- `wmi_raw` sysfs node sends raw method calls for probing, accepting space-separated method and argument bytes in base 0 (decimal, `0x` hex, or leading-`0` octal, e.g. `17`, `0x11`, `021`). It is root-only (`0600`) and rejects values above `0xFF` and more than 17 tokens. In probe mode, write methods are rejected, but the selector bytes are not validated: `0x05` and `0x17` can have firmware side effects and some `0x17` selectors are refused by the typed attributes. Treat `wmi_raw` as a root-only engineering escape hatch, not a guaranteed read-only path.
