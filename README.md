# nitro-linux

Kernel driver, daemon, and btop-style TUI for the Acer Nitro AN515-57:
keyboard backlight effects, fan monitoring, thermal policies. Coexists with
in-tree `acer-wmi`.

Target: AN515-57 / Scala_TLS, BIOS V1.22
(Other models to be reviewed)

Minimum terminal size: **120x47**

![Dashboard of nitro-tui. Image shows Fan Control and Fan Monitor (CPU/GPU fan speed) on top; Power Plan, Thermal (CPU/GPU temps), and Keyboard (controls) on the bottom](docs/screenshot-tui.png)

## Install

```bash
sudo bash deploy/install.sh   # DKMS driver + nitrod + nitro-tui + services
nitro-tui                   # dashboard
```

Edit defaults in `/etc/nitro.conf`, apply with
`sudo systemctl restart nitro-apply`

Uninstall:
`sudo bash deploy/uninstall.sh`

## Secure Boot

If OFF: nothing to do

If ON: enroll the DKMS key once, then install normally:

```bash
sudo pacman -S mokutil
sudo mokutil --import /var/lib/dkms/mok.pub
# reboot, confirm in MokManager, then:
sudo bash deploy/install.sh
```

If no keypair yet, create one with the following command then enroll as above:

```bash
sudo openssl req -new -x509 -newkey rsa:2048 -keyout /var/lib/dkms/mok.key -outform DER -out /var/lib/dkms/mok.pub -nodes -days 36500 -subj '/CN=DKMS module signing key/'
```

`install.sh` checks all of this up front and stops with instructions instead of failing obscurely at `modprobe`

Without installing, however requiring `led-class-multicolor` to be loaded first:

```bash
sudo modprobe led-class-multicolor && sudo insmod src/nitro.ko
```

## Dependencies

**Build (driver, `deploy/install.sh`)**

- `linux-headers`, `dkms`, `make`, `gcc`
- `led-class-multicolor` module (for the keyboard RGB zones)
- `sudo` (you _know_ you are installing a daemon)
- `mokutil` + `openssl` (to enroll a DKMS key for secure boot systems)

**Runtime (`nitrod`, `nitro-tui`, `tools/nitro.py`)**

- Python 3.8+
    - `curses`, `sqlite3`, `socket`, `json`, `subprocess`, `threading`, `glob`, `re`
    - Uses `#!/usr/bin/env python3`
- `nvidia-smi`
- Monospace Font
- minimum terminal size **120x47** cells

**System**

- `systemd` (units `nitrod.service`, `nitro-apply.service`)
- `nitro` kernel module loaded (`/sys/devices/platform/nitro` present)

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

## Use

One screen, every control clickable and showing its key. Minimum size 120x47.

- Fans: `A` auto, `S` max, `D` custom.
- Custom reveals two vertical sliders: `.`/`/` CPU duty (-/+)5, `;`/`'` GPU duty (-/+)5.
- Power: `Z` `X` `C` `V` `B`, the panel edits the plan of the current power source; AC and battery keep separate plans and switch automatically; `o`/`p` switch the AC/Battery tabs
- Thermal: `F` toggles °C / °F; shows CPU and GPU temps + model names.
- Keyboard: `[` static, `]` dynamic
    - static:
        - `1`-`4` zone
        - `Enter`/`#` colour picker
        - `x` zone on/off
    - dynamic:
        - `1`-`5` effect
        - `t`/`T` speed
        - `f` direction
        - `Enter`/`#` colour picker
    - picker:
        - `↑↓←→`/tab highlight cycling
        - `r`/`g`/`b` select channel (press again to edit)
        - `#` edits hex
        - `Enter` acts on the highlight
            - field -> edit
            - swatch -> select
            - apply ->apply
            - cancel -> close
        - `Shift+Enter` jumps to `[ apply ]` (repeat confirms selection); uses the kitty keyboard protocol (works in kitty but others fall back to plain `Enter`)
        - `Esc` jumps to `[ cancel ]` (repeat confirms selection)
        - mouse clicks select
        - swatches show `●` (selected), `○`/`◉` (highlight traversal);
    - `l`/`L` brightness (-/+)5
    - `R` reapply saved state
    - `?` help
- `-`/`+` refresh interval
- `q`/`Esc`/`Ctrl+q` close UI
- `Q` stop daemon

CLI: `sudo python tools/nitro.py status` (also `rgb`, `fan`, `dyn`, `profile`)

Sensors: `nitro-isa-0000`

LEDs: `/sys/class/leds/nitro:rgb:zone*`

Policy: `/sys/class/platform-profile/platform-profile-0/profile` (`low-power quiet balanced balanced-performance performance`).

## Notes

- How it _should_ work: the EC is driven over WMI GUID `7A4DDFE7-5B5D-40B4-8595-4408E0CC7F56` (method `WMBH`, `Arg1` = sub-command). See `docs/REVERSE.md` for the table.
- Effect modes 1-5 light the keyboard. Per-zone static colours display via the enable sequence (`0x05` poll -> `0x02` zones-on -> `0x06` colours -> `0x14` select).
- Unload restores previous fan/policy/lighting. Keep fans above 0.

## Logs

Kernel driver logs via `printk`:

```bash
journalctl -k | grep nitro
sudo dmesg | grep nitro
```

which may be restricted depending on `kernel.dmesg_restrict`.

This also dumps recent `dmesg nitro` lines:

```bash
sudo python tools/nitro.py status
```

`nitrod` and `nitro-tui` have no userspace logging.

## TODO

- [ ] CoolBoost
