# Contributing a model report

This project supports verified models: **AN515-57**, **AN515-45**, and **AN515-58**. Adding another model is not a config change — it requires evidence and code.

If you want your Nitro/Predator supported, the most valuable thing you can do is **run the read-only report below and open an issue**. Willingness to run follow-up probes is even better.

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

## Probe (candidate models)

A candidate probe entry is added only after a Model Support Report is triaged, and it matches `DMI_SYS_VENDOR` + `DMI_PRODUCT_NAME`. The current table lives in [MODELS.md](MODELS.md#candidates-probe-entry), which `tools/check_models.py` checks against the driver's DMI tables; this page does not repeat it.

If your model is listed there, load the module in read-only mode. The source installer does that in one step and keeps it that way across reboots:

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

Then collect the read-only evidence in one block:

```bash
sudo python3 tools/probe_survey.py > survey.txt
```

It prints a markdown block to paste into the report: DMI, module version and parameters, the WMI device list, every typed attribute, the nitro hwmon channels, the platform profile, the read-only `wmi_raw` GET matrix, the APGE reply and its EC `0x06` cross-check, an EC register sweep run twice with a diff so registers that latch on read stand out, and the last nitro kernel lines. It exits 0 when complete, 1 when a section was unavailable and 2 when it cannot run, and `--no-ec-sweep` skips the 512 EC reads. It sends no EC writes and no WMI SET calls; the one side effect is the sysinfo poll (`0x05`), which sets EC `PSEE` and is the same call the driver makes at load.

The individual reads behind it are:

```bash
cat /sys/devices/platform/nitro/capabilities
cat /sys/devices/platform/nitro/ec_cpu_temp
cat /sys/devices/platform/nitro/fan_cpu
cat /sys/devices/platform/nitro/fan_gpu
nitro report
```

Probe mode exposes read attributes for the capabilities the probe entry advertises; every write attribute returns `EPERM`. `fan_cpu`, `fan_gpu`, `fan_mode`, RGB, CoolBoost and `kbd_timeout` are all gated this way, so fan duty and RGB changes cannot be made or verified while `probe=1` alone, which is what the write-back node below is for. It registers the read-only hwmon class (fan tachos and whichever temperatures the firmware reports), but no LED class, no platform profile and no PWM channels, and it limits `wmi_raw` to read methods. `CAP_HWMON` still gates the `ec_cpu_temp` attribute and the thermal pane in the TUI. Do not use `modprobe -f`, `insmod`, or patched DMI strings.

### ACPI tables (only when a maintainer asks)

Some capabilities do not fail because a register is missing, but because the driver and that machine's firmware disagree about how to talk to an ACPI method — the keyboard timeout goes through `WMAA`, and a machine may number or lay out its arguments differently. The survey cannot see ACPI tables, so a maintainer will ask for them when a reply looks wrong rather than absent.

```bash
# Arch: sudo pacman -S --needed acpica. Debian/Ubuntu: sudo apt install acpica-tools
cd /tmp && sudo acpidump -b && iasl -d /tmp/dsdt.dat /tmp/ssdt*.dat >/dev/null 2>&1
grep -l "Method (WMAA" /tmp/*.dsl
```

Attach the `.dsl` file that matches, or paste its `Method (WMAA ...)` block plus the objects it calls (`CTID`, `WMIB`, `AAGT`, `AAST`) if they are defined nearby. `iasl` reports namespace errors and exits non-zero on most machines; that is normal for a table decompiled without the rest of the firmware around it, and the `.dsl` files are still written. Judge by `grep` finding a file, not by exit status. If `acpidump` is not available, there is no shortcut: `/sys/firmware/acpi/tables/SSDT*` is the compiled table, so it still needs `acpidump` or another acpica tool to read it.

These commands copy and decompile tables into `/tmp`; they do not load anything into firmware and do not touch the EC. The method layout the driver expects is in [REVERSE.md](REVERSE.md#keyboard-backlight-timeout-wmi-apge), and that is what a dump gets compared against.

### Same-value write-back (opt-in)

Probe mode cannot verify a write path, so promotion otherwise needs the SET call tested by editing the quirks table and rebuilding. The `writeback` node closes that gap without ever writing a value that was not read from the same machine first. Add `probe_writes=1` to the load:

```bash
sudo modprobe -r nitro
sudo modprobe nitro probe=1 probe_writes=1
```

If `deploy/install.sh --probe` already wrote `options nitro probe=1` to
`/etc/modprobe.d/`, `modprobe` merges that with the command line, so adding
`probe_writes=1` is enough.

The node only exists with that flag (root-only, `0600`). Write a path name, then
read the report back:

```bash
echo fan_cpu > /sys/devices/platform/nitro/writeback
cat /sys/devices/platform/nitro/writeback
```

Supported paths: `fan_cpu`, `fan_gpu`, `fan_mode`, `ftbl`, `opms`, `kbd_timeout`, `kbd_static`, `kbd_dynamic`, `coolboost`. Each one reads the current value, writes that exact value back, re-reads it, and restores the mode it had to switch — the fan paths go through `custom` like the normal control and put the previous mode back afterwards. `kbd_static` is the first exception: it reads the four zone colours, writes their bitwise inverse through the full static path (`0x02` zones-on, per-zone `0x06`, `0x14` brightness), verifies the read-back, then restores the original colours, because a same-value colour write cannot distinguish a working path from firmware that ignores writes. `kbd_dynamic` is the second exception: when `0x15` reports no effect state there is no same value to write, so it first enables the static path (`0x02` zones-on, the current zone colours and brightness), then writes a known effect (mode 1, red), reads `0x15` back, and restores the static colours. The report prints the reply even when a status-prefixed reply returns a non-zero status; 0-based replies are shifted into the same layout first. It is a visual test, not a promotion by itself; the reporter has to confirm the keyboard changed. A report from an AN515-57:

```text
path=fan_cpu
read fan_mode cpu=auto gpu=auto
read fan_cpu=50
set fan_cpu mode=custom ret=0
set fan_cpu=50 ret=0
verify fan_cpu=5
restore fan_mode cpu=auto gpu=auto ret=0
verify fan_mode cpu=auto gpu=auto
result=ok ret=0
```

The rules that make this safe to hand to a reporter:

- The value always comes from a read on that model. Nothing here accepts a typed value. The two visual tests are the exceptions: `kbd_static` writes the inverse of the colours it read, and `kbd_dynamic` writes a known effect when the machine reports no effect state; both restore the previous static colours.
- A path that cannot read first does not write at all, so a machine whose APGE GET fails never receives an APGE SET through this node.
- A fan path refuses when `fan_mode` reads back a value outside auto/max/custom, because it could not guarantee the restore.
- `coolboost` refuses unless the model advertises the CoolBoost backend and the fans it can see report `auto`, the same rules the normal control enforces.
- The fan paths restore the previous fan mode, so a test leaves the machine as it found it. Confirm with `cat fan_mode` afterwards.
- Run one path at a time, and stop `nitrod` first if it applies a fan profile, so a timer cannot race the restore. FTBL, OPMS, and CoolBoost can spin the fans up briefly while the value is re-applied.
- The store returns success when the test ran; the outcome is the `result=` line, so `echo fan_cpu > writeback` exits 0 even when the report says `result=error`.

### Adding a probe entry (maintainers)

- `src/nitro.c`: a quirks struct with read capabilities only, and a row in
  `nitro_probe_dmi[]` matching `DMI_SYS_VENDOR` + `DMI_PRODUCT_NAME`.
  `nitro_validate_probe_quirks()` rejects `COOLBOOST`, `PLATFORM_PROFILE`,
  `LED_CLASS` and `KBD_TIMEOUT` and requires the `NONE` backends, so a probe
  entry cannot advertise a write path.
- `docs/MODELS.md`: a Candidates row with the expected reads and status
  `probe shipped`. `tools/check_models.py` fails when the two tables disagree.
- Run `python3 tools/check_models.py` and `make test`.
- Then ask for the survey block before proposing any capability change.

A capability moves into the quirks struct only with its evidence: reads from the survey, and for a SET path a passing `probe_writes=1` write-back run on that machine. A sibling model is not evidence for a write path.

---

## What happens next

1. Your report goes into the Candidates table in `docs/MODELS.md`
2. After triage we may add a **read-only probe** entry for your model; you run it with `probe=1`
3. You collect read evidence with `sudo python3 tools/probe_survey.py`: EC temperature, fan tachos and duty, fan mode, current RGB/lighting state, `wmi_raw` reads, the EC register sweep and the kernel log. Nothing in this phase writes a setting: every write attribute returns `EPERM`, and the sysinfo poll's EC `PSEE` side effect is the same one the driver makes at load
4. If the reads are sane, the write paths are verified one at a time (fan duty → fan mode → profile → RGB → effects → CoolBoost → keyboard timeout), with the maintainer in the loop. For most of them this is a `probe_writes=1` write-back run: the module writes that model's own read-back value and reports whether the SET was accepted
5. With recorded evidence, the model gets a verified quirk entry and moves to the supported-models table

CoolBoost needs direct EC evidence on **that specific model**. The 30-second keyboard timeout needs its APGE GET replies on that model (`0x1E0000080000` on after a SET, `0x1E0000000000` on before the first SET of the boot, `0x80000` off; some boards also carry the keyboard brightness in bits 32-39, which the driver masks) and a visible ON → OFF transition. A same-value `writeback kbd_timeout` only proves that the SET call is accepted; it does not prove that writing the opposite value changes the timeout, so do not treat a passing same-value run as ON/OFF evidence. When the APGE GET does not parse, the EC `0x06` value must differ between the two states and agree with the visible timeout; if it is the same in both, the setting never reached the firmware and the run is not evidence. `apge_raw` reads the APGE reply back one at a time: `echo "2 0x88401" > /sys/devices/platform/nitro/apge_raw`, then `cat` the same file for the status, reply type and value. Collect all ON-state observations first, then all OFF-state observations. Changing the firmware setting is the only step that needs a reboot; `modprobe -r nitro` / `modprobe nitro ...` is enough to reload the driver. If the APGE replies are rejected or parse into an unexpected value, the fix depends on that machine's firmware, so the next step is the ACPI table: [ACPI tables](#acpi-tables-only-when-a-maintainer-asks) above.

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

When the kernel drops a capability at load, `/sys/devices/platform/nitro/pruned` lists `name=reason` lines such as `kbd_timeout=APGE WMI GUID missing` (or `none`). The CLI, `nitrod`, and the TUI read that file and print `unavailable (<reason>)` instead of silently omitting the control.

### Identity and evidence

A model is `sys_vendor` + `product_name`; that is the only DMI the kernel matches. Board name and BIOS version are diagnostic metadata, not identity: collect them in reports, never key on them. Board name is a platform codename shared across retail names, so it is stable but not unique; BIOS version changes with every firmware release and is not comparable across models, so it is neither stable nor unique. Evidence is collected per machine and recorded once per model rather than per model/board/BIOS combination.

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

## Working on the driver

```bash
make            # build against the running kernel
make test       # guard checks, no hardware and no kernel headers needed
make install    # load the freshly built module
```

The write-back decisions live in `src/writeback.h`: whether a model gets a fan path at all, whether a fan switch may run when `fan_mode` reads back something unexpected, and whether CoolBoost may be touched outside `auto`. The module and `tools/test_writeback_guards.c` include the same header, so `make test` exercises the predicates the driver actually runs. A new guard belongs in that header with a row in the test; the CI `guards` job runs the same target.

The WMI traffic, the restore order and the lines around them can only be checked on hardware — `make test` covers the decisions, not the calls.

### Testing capability pruning

`inject_read_failures` makes baseline reads fail on purpose, so the degradation path can be exercised on a supported machine without waiting for a firmware difference. The value is a bitmask:

| Bit | Baseline read     |
| --- | ----------------- |
| 1   | CPU fan duty      |
| 2   | GPU fan duty      |
| 4   | fan mode          |
| 8   | CoolBoost         |
| 16  | keyboard timeout  |
| 32  | static RGB zones  |
| 64  | dynamic RGB state |
| 128 | platform profile  |

Load the module with the read you want to fail, then check the log:

```bash
sudo modprobe -r nitro
sudo modprobe nitro inject_read_failures=4
sudo dmesg | tail -n 20
```

The driver must still load, and the log contains one line naming what was removed and which read failed, for example:

```text
nitro: pruned capabilities: fan_mode, coolboost (fan mode read failed)
```

The pruned attributes disappear or return `ENODEV`; everything else keeps working. Reload without the parameter to get the full capability set back. This is a development aid for supported models, not probe mode: a real failed read is what the summary reports on a machine whose firmware disagrees with the quirk table.

---

## Other contributions

Other contributions are welcome. This includes:

- Bug fixes, CI, packaging, TUI/CLI improvements
- `acer-wmi` coexistence testing (especially on AN515-58)
- Documentation fixes
- Upstreaming missing pieces to `acer-wmi` (long term)
