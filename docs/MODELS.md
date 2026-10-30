# Supported models

**As of v2.3.4, only one configuration is verified:**

| Model    | Board     | BIOS  | Kernel                     | Status       | Notes                                                                          |
| -------- | --------- | ----- | -------------------------- | ------------ | ------------------------------------------------------------------------------ |
| AN515-57 | Scala_TLS | V1.22 | built per installed kernel | **Verified** | All features: fan, profile, 4-zone RGB, 5 dynamic effects, CoolBoost (EC 0x10) |

---

## Candidates (read-only probe)

No model is currently in the probe table. A probe entry is added only after a
[Model Support Report](https://github.com/SurrealArts/nitro-linux/issues/new?template=model-report.yml) is triaged.

| Product | Board | BIOS | Issue | Probe entry | Status | Last update |
| ------- | ----- | ---- | ----- | ----------- | ------ | ----------- |

---

## Capability-based support

Starting with the multi-model refactor, the driver exposes a **capabilities** sysfs file:

```bash
cat /sys/devices/platform/nitro/capabilities
# Example output: probe=0 fan=1 fan_count=2 fan_mode=1 hwmon=1 profile=ftbl rgb=4 dyn=5 coolboost=ec10 led=1
```

Userspace (daemon, TUI, CLI) reads this at startup and **only enables features that are present**. If a capability is missing, the corresponding controls are hidden/disabled.

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
- `CAP_PLATFORM_PROFILE` requires `profile_backend == FTBL`
- `CAP_COOLBOOST` requires `CAP_FAN_MODE` and `coolboost_backend == EC10`
- Backends are `NONE` or the single supported type (`FTBL` / `EC10`)

A probe quirk (used with `probe=1`) is validated separately. It may advertise
read-only capabilities (`CAP_FAN`, `CAP_FAN_MODE`, `CAP_HWMON`, `CAP_RGB`,
`CAP_RGB_DYNAMIC`) so the probe can read sensors, fan state, and lighting, but
it must use `NONE` backends and must not set `CAP_COOLBOOST`,
`CAP_PLATFORM_PROFILE`, or `CAP_LED_CLASS`: those are unverified write paths,
and probe mode does not register LED class or platform profile devices.

---

### Fan duty 0 note

On AN515-57, duty 0 does **not** stop the fans; firmware maps it to ~2k–3k RPM. Duties 1–19 are real low duties (RPM increases with duty). The driver logs duty 0 at info level and warns for 1–19; both messages are rate-limited.

---

## How to report a new model

**Read-only only.** Do not force-load the module, patch DMI strings, or write EC/WMI registers.

Run this and paste the output into a **Model support report** issue:

```bash
{
  echo "[uname]"; uname -r
  echo "[DMI]"
  cat /sys/class/dmi/id/{sys_vendor,product_name,board_name,board_vendor,bios_version,bios_date}
  echo "[WMI]"; ls /sys/bus/wmi/devices/
  echo "[nitro]"; ls /sys/devices/platform/nitro/ 2>/dev/null || true
  echo "[modules]"; lsmod | grep -E 'nitro|acer_wmi' || true
} 2>/dev/null
```

Also include:

- Distro, kernel, whether `nitro-linux` is installed (package/source/none)
- What you observe: fans, temperatures, backlight, profile, suspend behavior
- **Safety acknowledgement:** you have not force-loaded or written raw registers

`nitro report` automates this. It is read-only and redacts UUIDs, MAC addresses, and long digit-only strings; review the output before posting.

---

## How a model becomes verified

One cooperative owner + recorded evidence. Do not skip or reorder:

1. **Report**: DMI, board, BIOS, kernel, WMI devices, current behavior
2. **Read-only probe** (`probe=1`): no hardware write attrs (`wmi_raw` accepts read methods only), no profile/LED registration, no EC writes
3. **Sensors**: CPU/GPU temps and fan RPM match independent source
4. **Fan duty**: one fan at a time; confirm RPM change, no oscillation
5. **Fan mode**: Auto, Max, Custom accepted by EC
6. **Platform profile**: confirm backend; no `acer-wmi` conflict
7. **Static RGB**: zone count, color order
8. **Dynamic effects**: effect count, speed, direction, brightness
9. **CoolBoost**: **only with direct EC evidence on that model**; never infer from AN515-57
10. **Record & release**: add to this table, update quirk table, add regression tests

---

## What not to do

- Do not add a DMI match for a model you cannot test
- Do not use `modprobe -f`, `insmod`, or patched DMI to bypass the gate
- Do not write `wmi_raw` on an unknown model
- Do not claim "it works" based only on module load
- Do not assume AN515-58 should replace `acer-wmi`; complement is safer

See [Contributing](CONTRIBUTING.md) for the one-page user guide.
