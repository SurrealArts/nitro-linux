# Contributing a model report

This project supports verified models: **AN515-57** (Scala_TLS / BIOS V1.22) and **AN515-45** (Scala_CAS / BIOS V1.14).
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

A candidate probe entry is added only after a Model Support Report is triaged, and it keeps the full `sys_vendor` + `product_name` + `board_name` triad. Current probe table:

| Product        | Board     | BIOS  | Probe reads                                   |
| -------------- | --------- | ----- | --------------------------------------------- |
| Nitro AN515-58 | Jimny_ADH | V2.21 | 2x fans, fan mode, ec_cpu_temp, 4x static RGB |

If your model is in the table, load the module in read-only mode. The source
installer does that in one step and keeps it that way across reboots:

```bash
sudo bash deploy/install.sh --probe
```

It writes `options nitro probe=1` to `/etc/modprobe.d/nitro-linux.conf`, so
`modprobe`, `systemd-modules-load` and every later boot use probe mode. For a
one-off load of an already installed module instead:

```bash
sudo modprobe -r nitro 2>/dev/null || true
sudo modprobe nitro probe=1
```

Then collect the read-only evidence:

```bash
cat /sys/devices/platform/nitro/capabilities
cat /sys/devices/platform/nitro/ec_cpu_temp
cat /sys/devices/platform/nitro/fan_cpu
cat /sys/devices/platform/nitro/fan_gpu
nitro report
```

Probe mode exposes read attributes for the capabilities the probe entry
advertises; every write attribute returns `EPERM`. `fan_cpu`, `fan_gpu`,
`fan_mode`, RGB, CoolBoost and `kbd_timeout` are all gated this way, so fan duty
and RGB changes cannot be made or verified while `probe=1`. It registers the
read-only hwmon class (fan tachos and whichever temperatures the firmware
reports), but no LED class, no platform profile and no PWM channels, and it
limits `wmi_raw` to read methods. `CAP_HWMON` still
gates the `ec_cpu_temp` attribute and the thermal pane in the TUI. Do not use
`modprobe -f`, `insmod`, or patched DMI strings.

### Same-value write-back (opt-in)

Probe mode cannot verify a write path, so promotion otherwise needs the SET call
tested by editing the quirks table and rebuilding. The `writeback` node closes
that gap without ever writing a value that was not read from the same machine
first. Add `probe_writes=1` to the load:

```bash
sudo modprobe -r nitro
sudo modprobe nitro probe=1 probe_writes=1
```

The node only exists with that flag (root-only, `0600`). Write a path name, then
read the report back:

```bash
echo fan_cpu > /sys/devices/platform/nitro/writeback
cat              /sys/devices/platform/nitro/writeback
```

Supported paths: `fan_cpu`, `fan_gpu`, `fan_mode`, `ftbl`, `kbd_timeout`,
`coolboost`. Each one reads the current value, writes that exact value back,
re-reads it, and restores the mode it had to switch — the fan paths go through
`custom` like the normal control and put the previous mode back afterwards. A
report from an AN515-57:

```text
path=fan_cpu
read fan_mode cpu=auto gpu=auto
read fan_cpu=50
set fan_cpu mode=custom ret=0
set fan_cpu=50 ret=0
verify fan_cpu=50
restore fan_mode cpu=auto gpu=auto ret=0
verify fan_mode cpu=auto gpu=auto
result=ok ret=0
```

The rules that make this safe to hand to a reporter:

- The value always comes from a read on that model. Nothing here accepts a typed
  value, and no path writes a constant.
- A path that cannot read first does not write at all, so a board whose APGE GET
  fails never receives an APGE SET through this node.
- `coolboost` refuses unless both fans report `auto`, the same rule the normal
  control enforces.
- The fan paths restore the previous fan mode, so a test leaves the machine as it
  found it. Confirm with `cat fan_mode` afterwards.
- Run one path at a time. FTBL and CoolBoost can spin the fans up briefly while
  the value is re-applied.

---

## What happens next

1. Your report goes into the Candidates table in `docs/MODELS.md`
2. After triage we may add a **read-only probe** entry for your model; you run it with `probe=1`
3. You collect read evidence: EC temperature, fan tachos and duty, fan mode, current RGB/lighting state, `wmi_raw` reads. Nothing is written in this phase — write attributes return `EPERM`
4. If the reads are sane, the write paths are verified one at a time (fan duty → fan mode → profile → RGB → effects → CoolBoost → keyboard timeout), with the maintainer in the loop. For most of them this is a `probe_writes=1` write-back run: the module writes that model's own read-back value and reports whether the SET was accepted
5. With recorded evidence, the model gets a verified quirk entry and moves to **Verified**

CoolBoost needs direct EC evidence on **that specific model**. The 30-second keyboard timeout needs its APGE GET replies on that model (`0x1E0000080000` on after a SET, `0x1E0000000000` on before the first SET of the boot, `0x80000` off). `apge_raw` reads them back one at a time: `echo "2 0x88401" > /sys/devices/platform/nitro/apge_raw`, then `cat` the same file for the status, reply type and value.

---

## Capability-based support (how it works)

The driver exposes `/sys/devices/platform/nitro/capabilities` — a space-separated `key=value` list:

```text
probe=0 fan=1 fan_count=2 fan_mode=1 hwmon=1 profile=ftbl rgb=4 dyn=5 coolboost=ec10 led=1 kbd_timeout=1
```

Userspace (daemon, TUI, CLI, `nitro-apply`) reads this at startup and **only enables features that are present**. If a capability is missing, the corresponding controls are hidden/disabled.

This means:

- Adding a model = adding a quirk table entry in the kernel, not rewriting userspace
- The same package works across models with different feature sets
- Unsupported features are gracefully hidden, not broken (unsupported features will not fail module initialization)

---

## Safety rules (non-negotiable)

- **Only load the module on verified hardware.** The DMI gate is a safety boundary, not a bug. The exceptions are read-only probe mode (`probe=1`) on a model in the probe table, and `probe_writes=1`, which is only used when a maintainer asks for a same-value write-back run.
- **Never force-load** (`modprobe -f`, `insmod`, patched DMI, edited source).
- **No blind writes.** A successful module load ≠ working fan/RGB/CoolBoost. The write-back node exists so a SET can be proven with that model's own value; anything else is still a blind write.
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
