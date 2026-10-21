# nitro-linux

Kernel driver, daemon, and btop-style TUI for the Acer Nitro AN515-57: keyboard backlight effects, fan monitoring, thermal policies. Coexists with in-tree `acer-wmi`.

Target: AN515-57 / Scala_TLS, BIOS V1.22. Other AN515 models are still in planning.

Minimum terminal size: **120x47**

![nitro-linux dashboard](docs/screenshot-tui.png)

## Install

### Package

```ini
[nitro-linux]
SigLevel = Required DatabaseOptional
Server = https://surrealarts.github.io/nitro-linux/$arch
```

```bash
sudo pacman-key --keyserver hkps://keyserver.ubuntu.com --recv-keys 7EA2338784E083123CC077F2E29A80B4FDD576B1
sudo pacman-key --lsign-key 7EA2338784E083123CC077F2E29A80B4FDD576B1
```

Install the headers for your running kernel (`linux-headers` or `linux-lts-headers`), then:

```bash
sudo pacman -Syu nitro-linux
```

Reboot once. `/etc/nitro.conf` is created only if absent. Updates are normal `pacman -Syu`.

```bash
sudo pacman -Rns nitro-linux
```

### Source

```bash
sudo bash deploy/install.sh
sudo bash deploy/uninstall.sh
```

## Use

`nitro-linux` opens the TUI. `nitro help` lists CLI commands. `?` shows the full key map.

- Fans: `A` auto, `S` max, `D` custom; `.`/`/` and `;`/`'` adjust CPU/GPU duty.
- CoolBoost: `i` toggles it in Auto only; retained while in Max/Custom.
- Power: `Z` `X` `C` `V` `B`; `o`/`p` switch AC/battery.
- Thermal: `F` toggles °C/°F.
- Keyboard: `[`/`]` static/dynamic; `1`-`4` zones; `1`-`5` effects; `Enter`/`#` color picker; `l`/`L` brightness; `R` reapply.
- `-`/`+` refresh; `q` quit; `Q` stop daemon.

Config: `/etc/nitro.conf` for defaults (`sudo systemctl restart nitro-apply`), `/sys/devices/platform/nitro/` for direct sysfs, `docs/REVERSE.md` for the EC/WMI interface. Optional config line: `coolboost on|off` (applied only while both fans are Auto). CLI: `sudo nitro coolboost on|off`.

Sensors: `nitro-isa-0000`. LEDs: `/sys/class/leds/nitro:rgb:zone*`. Profile: `/sys/class/platform-profile/platform-profile-0/profile`.

## Secure Boot

Enroll the DKMS key once before installing:

```bash
sudo pacman -S mokutil
sudo mokutil --import /var/lib/dkms/mok.pub
```

If no keypair exists:

```bash
sudo openssl req -new -x509 -newkey rsa:2048 \
  -keyout /var/lib/dkms/mok.key -outform DER \
  -out /var/lib/dkms/mok.pub -nodes -days 36500 \
  -subj '/CN=DKMS module signing key/'
```

## Dependencies

Package: `python`, `dkms`, `systemd`

- optional: `nvidia-utils`

Source build: `linux-headers`, `dkms`, `make`, `gcc`, `sudo`

- secure boot: `mokutil` + `openssl`

Runtime: Python 3.8+, `nvidia-smi` for GPU sensors

## Notes

- EC via WMI GUID `7A4DDFE7-5B5D-40B4-8595-4408E0CC7F56` (`WMBH`); see [REVERSE](docs/REVERSE.md).
- Unload restores the previous fan/policy/lighting state.
- Kernel logs: `journalctl -k | grep nitro` or `sudo dmesg | grep nitro`.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
