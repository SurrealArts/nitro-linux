#!/bin/bash
set -e
cd "$(dirname "$0")/.."

if pacman -Qq nitro-linux >/dev/null 2>&1; then
  echo "[!] nitro-linux is installed as a package."
  echo "    Remove it with: sudo pacman -Rns nitro-linux"
  exit 1
fi

echo "[*] disable services"
systemctl disable --now nitrod.service 2>/dev/null || true
systemctl disable --now nitro-apply.service 2>/dev/null || true
echo "[*] rmmod"
rmmod nitro 2>/dev/null || true
echo "[*] remove source files"
rm -f /etc/systemd/system/nitro-apply.service /etc/systemd/system/nitrod.service
rm -f /usr/local/bin/nitro-apply /usr/local/bin/nitrod /usr/local/bin/nitro-linux /usr/local/bin/nitro
rm -f /usr/local/bin/nitro-tui  # remove the pre-rename command
rm -f /etc/modules-load.d/nitro.conf
systemctl daemon-reload
echo "[*] dkms remove all nitro versions (keeps /etc/nitro.conf and /var/lib/nitro)"
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
