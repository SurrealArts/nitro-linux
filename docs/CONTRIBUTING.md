# Contributing a model report

This project supports **one verified model** (AN515-57 / Scala_TLS / BIOS V1.22).
Adding another model is not a config change — it requires evidence and code.

If you want your Nitro/Predator supported, the most valuable thing you can do
is **run the read-only report below and open an issue**. Willingness to run
follow-up probes is even better.

---

## Quick report (copy-paste this)

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

Open a **Model support report** issue and paste the output. Also include:

- Distro and kernel (`uname -r`)
- Whether you installed `nitro-linux` (package / source / not installed)
- What you observe: fans, temperatures, keyboard backlight, platform profile, suspend/resume
- **Acknowledgement:** you have not force-loaded the module, patched DMI strings, or written raw EC/WMI registers

---

## Read-only probe (candidate models)

Current probe table: **AN515-58, ANV15-51, ANV15-41**. If your model is listed,
load the module in read-only mode:

```bash
sudo modprobe -r nitro 2>/dev/null || true
sudo modprobe nitro probe=1
cat /sys/devices/platform/nitro/capabilities
nitro report
```

Probe mode creates read-only hardware attributes, does not register hwmon,
LEDs, or platform profile, and limits `wmi_raw` to read methods. Do not use
`modprobe -f`, `insmod`, or patched DMI strings.

---

## What happens next

1. Your report goes into the candidate table in `docs/MODELS.md`
2. If the DMI/WMI matches a known platform, we may add a **read-only probe** entry; you run it with `probe=1`
3. You run the probe; we verify sensors → fan duty → fan mode → profile → RGB → effects → CoolBoost, one at a time
4. With recorded evidence, the model gets a quirk entry and moves to **Verified**

CoolBoost is never enabled without direct EC evidence on **that specific model**.

---

## Capability-based support (how it works)

The driver exposes `/sys/devices/platform/nitro/capabilities` — a space-separated `key=value` list:

```text
probe=0 fan=1 fan_count=2 fan_mode=1 hwmon=1 profile=ftbl rgb=4 dyn=5 coolboost=ec10 led=1
```

Userspace (daemon, TUI, CLI, `nitro-apply`) reads this at startup and **only enables features that are present**. If a capability is missing, the corresponding controls are hidden/disabled.

This means:

- Adding a model = adding a quirk table entry in the kernel, not rewriting userspace
- The same package works across models with different feature sets
- Unsupported features are gracefully hidden, not broken

---

## Safety rules (non-negotiable)

- **Only load the module on verified hardware.** The DMI gate is a safety boundary, not a bug. The only exception is read-only probe mode (`probe=1`) on a model in the probe table.
- **Never force-load** (`modprobe -f`, `insmod`, patched DMI, edited source).
- **No blind writes.** A successful module load ≠ working fan/RGB/CoolBoost.
- **CoolBoost requires direct EC evidence** (register offset + values) on that model.
- **One change at a time.** If fans stop, temps climb, or backlight misbehaves: stop, unload, reboot.

If something goes wrong:

```bash
sudo systemctl stop nitrod.service
sudo modprobe -r nitro   # restores saved state on verified model
```

If unload fails or the machine overheats, power off and let it cool. Do not write more EC values to recover.

---

## Other contributions

Other contributions are welcome. This includes:

- Bug fixes, CI, packaging, TUI/CLI improvements
- `acer-wmi` coexistence testing (especially on AN515-58)
- Documentation fixes
- Upstreaming missing pieces to `acer-wmi` (long term)
