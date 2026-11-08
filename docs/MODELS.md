# Supported models

**As of v2.4.0, verified models:**

| Model    | Fan control | Fan mode  | Power plan | RGB        | Dynamic effects | CoolBoost  | Keyboard timeout | hwmon |
| -------- | ----------- | --------- | ---------- | ---------- | --------------- | ---------- | ---------------- | ----- |
| AN515-57 | ✅          | ✅        | ✅ FTBL    | ✅ 4 zones | ✅ 5 effects    | ✅ EC 0x10 | ✅ APGE          | ✅    |
| AN515-45 | ✅          | ✅        | ✅ FTBL    | ✅ 4 zones | ✅ 5 effects    | ✅ EC 0x10 | ✅ APGE          | ✅    |
| AN515-58 | ✅          | ✅ 0=auto | ✅ OPMS    | ✅ 4 zones | ✅ 5 effects    | ❌[^1]     | ✅ APGE          | ✅    |

[^1]: Model hardware does not support toggle-able CoolBoost.

---

## Candidates (probe entry)

A probe entry is added only after a [Model Support Report](https://github.com/SurrealArts/nitro-linux/issues/new?template=model-report.yml) is triaged. Probe entries bind only with `probe=1`, expose read attributes only, register a read-only hwmon class (fan tachos and whichever temperatures the firmware reports) but no LED class, platform profile or PWM channels, and reject non-read `wmi_raw` methods. Every write attribute returns `EPERM`.

A maintainer can ask for a same-value write-back run with `probe_writes=1`, which exercises one SET path with the value that machine just reported and restores it afterwards. It is how a write path is usually proven before the probe entry is promoted; see `docs/CONTRIBUTING.md`.

No probe candidates at present.

### Model notes

- **AN515-58** uses OPMS (`GET/SET_MISC` selector `0x0B`) for the platform profile; FTBL reads on that model but does not track the active profile (it read 0 after `acer-wmi` set `performance`). The machine reports fan mode 0 at boot while the fans are auto, so its quirk sets `fan_mode_zero_auto`. Static RGB is four zones and dynamic effects work: the `0x15` reply is 0-based (mode first, no status byte), unlike the AN515-57/45 `BHLK` layout, so the driver shifts it into the status-prefixed layout.

Escalating a probe entry (for example enabling `dyn`) requires the previous probe output and a passing `writeback` for each write path being enabled: `ftbl` or `opms` for the platform profile, `kbd_timeout` for the APGE path or the EC 0x06 path (plus the visible 30-second timeout in both states for EC 0x06), `kbd_static` for static RGB, `kbd_dynamic` plus a confirmed visible effect for dynamic RGB, and direct EC evidence for CoolBoost.

---

## Capability-based support

Starting with the multi-model refactor, the driver exposes a **capabilities** sysfs file:

```bash
cat /sys/devices/platform/nitro/capabilities
# Example output: probe=0 fan=1 fan_count=2 fan_mode=1 hwmon=1 profile=ftbl rgb=4 dyn=5 coolboost=ec10 led=1 kbd_timeout=1
```

Userspace (daemon, TUI, CLI) reads this at startup and **only enables features that are present**. If a capability is missing, the corresponding controls are hidden/disabled. `/sys/devices/platform/nitro/pruned` lists `name=reason` lines for capabilities the load had to drop, or `none`; `nitro status` and the TUI show those reasons next to the unavailable control.

This means:

- Adding a model = adding a quirk table entry in the kernel, not rewriting userspace
- The same package works across models with different feature sets
- Unsupported features are gracefully hidden, not broken (optional subsystems degrade at init; module load succeeds)

### Quirk invariants (code-enforced)

Every quirk entry is validated at module load (`nitro_validate_quirks()`). The invariants are:

- `capabilities` uses only defined bits
- `rgb_zones` [0, 4]; if `CAP_RGB` then `rgb_zones > 0`
- `dynamic_effects` [0, 5]; if `CAP_RGB_DYNAMIC` then `dynamic_effects > 0`
- `fan_count` [0, 2]; if `CAP_FAN` then `fan_count > 0`
- `CAP_FAN_MODE` requires `CAP_FAN`
- `CAP_LED_CLASS` requires `CAP_RGB`
- `CAP_RGB_DYNAMIC` requires `CAP_RGB`
- `CAP_PLATFORM_PROFILE` requires `profile_backend` `FTBL` or `OPMS`; `OPMS` also requires `has_misc`
- `CAP_COOLBOOST` requires `CAP_FAN_MODE` and `coolboost_backend == EC10`
- Backends are `NONE` or the single supported type (`FTBL` / `OPMS` / `EC10`)

A probe quirk (used with `probe=1`) is validated separately. It may advertise read-only capabilities (`CAP_FAN`, `CAP_FAN_MODE`, `CAP_HWMON`, `CAP_RGB`, `CAP_RGB_DYNAMIC`) so the probe can read sensors, fan state, and lighting, but it must use `NONE` backends and must not set `CAP_COOLBOOST`, `CAP_PLATFORM_PROFILE`, `CAP_LED_CLASS`, or `CAP_KBD_TIMEOUT`: those are unverified write paths, and probe mode does not register LED class or platform-profile devices. `CAP_HWMON` in a probe entry gates the `ec_cpu_temp` attribute, the thermal pane in userspace, and the read-only hwmon class (fan tachos + EC temperature).

---

### Fan duty 0 note

On AN515-57, duty 0 does **not** stop the fans; firmware maps it to ~2k–3k RPM. Duties 1–19 are real low duties (RPM increases with duty). The driver logs duty 0 at info level and warns for 1–19; both messages are rate-limited.

---

## How to report a new model

**Read-only only.** Do not force-load the module, patch DMI strings, or write EC/WMI registers.

Run `nitro report` and paste the full output into a **Model support report** issue. It is read-only and redacts UUIDs, MAC addresses, and long digit-only strings from DMI and sysfs output; model names stay readable and the WMI GUID list is kept (those are hardware-family identifiers). Review the output before posting. If `nitro-linux` is not installed, paste this instead:

```bash
{
  echo "[uname]"; uname -r
  echo "[DMI]"
  cat /sys/class/dmi/id/{sys_vendor,product_name,bios_version,bios_date}
  echo "[WMI]"; ls /sys/bus/wmi/devices/
  echo "[modules]"; lsmod | grep -E 'nitro|acer_wmi' || true
} 2>/dev/null
```

Also include:

- Distro, kernel, whether `nitro-linux` is installed (package/source/none)
- What you observe: fans, temperatures, backlight, profile, suspend behavior
- **Safety acknowledgement:** you have not force-loaded or written raw registers

---

## How a model becomes verified

One cooperative owner + recorded evidence. Do not skip or reorder:

1. **Report**: DMI, kernel, WMI devices, current behavior — the survey block from `tools/probe_survey.py` covers most of it
2. **Probe** (`probe=1`): no hardware write attrs (`wmi_raw` accepts read methods only), no profile/LED registration, no EC writes. The probe phase collects the baseline reads, and a `probe_writes=1` write-back run can prove a SET path with that machine's own value before promotion
3. **Sensors**: CPU/GPU temps and fan RPM match independent source
4. **Fan duty**: one fan at a time; confirm RPM change, no oscillation
5. **Fan mode**: Auto, Max, Custom accepted by EC
6. **Platform profile**: confirm backend; no `acer-wmi` conflict
7. **Static RGB**: zone count, color order
8. **Dynamic effects**: effect count, speed, direction, brightness
9. **CoolBoost**: **only with direct EC evidence on that model**; never infer from AN515-57
10. **Keyboard timeout**: APGE GET replies on that model (`0x1E0000080000` on after a SET, `0x1E0000000000` on before the first SET of the boot, `0x80000` off)
11. **Record & release**: add to this table, update quirk table, add regression tests

---

## What not to do

- Do not add a DMI match for a model you cannot test
- Do not use `modprobe -f`, `insmod`, or patched DMI to bypass the gate
- Do not write `wmi_raw` on an unknown model
- Do not claim "it works" based only on module load
- Do not assume AN515-58 should replace `acer-wmi`; complement is safer

See [Contributing](CONTRIBUTING.md) for the one-page user guide.
