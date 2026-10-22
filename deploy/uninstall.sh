#!/bin/bash
set -e
cd "$(dirname "$0")/.."

if pacman -Qq nitro-linux >/dev/null 2>&1; then
  echo "[!] nitro-linux is installed as a package."
  echo "    Remove it with: sudo pacman -Rns nitro-linux"
  exit 1
fi

invoking_user() {
  local u="${SUDO_USER:-}"
  if [ -z "$u" ] || [ "$u" = root ]; then
    u="$(logname 2>/dev/null || true)"
  fi
  if [ -z "$u" ] || [ "$u" = root ]; then
    return 1
  fi
  id "$u" >/dev/null 2>&1 || return 1
  printf '%s\n' "$u"
}

disable_key() {
  local u uid home
  if ! u="$(invoking_user)"; then
    return 0
  fi
  uid="$(id -u "$u")"
  home="$(getent passwd "$u" | cut -d: -f6)"
  [ -n "$home" ] || return 0
  rm -f "$home/.config/systemd/user/default.target.wants/nitro-key.service"
  rm -f "$home/.config/autostart/nitro-key.desktop"
  if [ -d "/run/user/$uid" ] && [ -S "/run/user/$uid/bus" ] && command -v runuser >/dev/null 2>&1; then
    runuser -u "$u" -- env HOME="$home" XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user disable --now nitro-key.service >/dev/null 2>&1 || true
  fi
}

echo "[*] disable services"
systemctl disable --now nitrod.service 2>/dev/null || true
systemctl disable --now nitro-apply.service 2>/dev/null || true
echo "[*] rmmod"
rmmod nitro 2>/dev/null || true
echo "[*] remove source files"
disable_key
rm -f /etc/systemd/system/nitro-apply.service /etc/systemd/system/nitrod.service
rm -f /usr/local/bin/nitro-apply /usr/local/bin/nitrod /usr/local/bin/nitro-linux /usr/local/bin/nitro
rm -f /usr/local/lib/systemd/user/nitro-key.service
rm -f /etc/modules-load.d/nitro.conf
systemctl daemon-reload
echo "[*] dkms remove all nitro versions (keeps /etc/nitro.conf, /var/lib/nitro, and ~/.config/nitro)"
for v in $(dkms status 2>/dev/null | sed -n 's#^nitro/\([^,]*\),.*#\1#p' | sort -u); do
  echo "[*] dkms remove nitro/$v"
  dkms remove -m nitro -v "$v" --all 2>/dev/null || true
done
for d in /usr/src/nitro-[0-9]*; do
  [ -d "$d" ] || continue
  echo "[*] remove $d"
  rm -rf "$d"
done
echo "[*] rebuild initramfs (purge nitro)"
if command -v dracut-rebuild >/dev/null 2>&1; then
  dracut-rebuild
else
  echo "[!] dracut-rebuild not found: rebuild your initramfs manually"
fi
echo "[*] done"
