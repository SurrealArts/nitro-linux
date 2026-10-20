#!/bin/bash
set -e
cd "$(dirname "$0")/.."
VER=$(grep PACKAGE_VERSION dkms.conf | cut -d'"' -f2)
echo "[*] rmmod"
rmmod nitro 2>/dev/null || true
echo "[*] disable service + files"
systemctl disable --now nitrod.service 2>/dev/null || true
systemctl disable nitro-apply.service 2>/dev/null || true
rm -f /etc/systemd/system/nitro-apply.service /etc/systemd/system/nitrod.service
rm -f /usr/local/bin/nitro-apply /usr/local/bin/nitrod /usr/local/bin/nitro-linux /usr/local/bin/nitro
rm -f /usr/local/bin/nitro-tui  # remove the pre-rename command
rm -f /etc/modules-load.d/nitro.conf
systemctl daemon-reload
echo "[*] dkms remove (keeps /etc/nitro.conf and /var/lib/nitro)"
dkms remove -m nitro -v "$VER" --all 2>/dev/null || true
rm -rf "/usr/src/nitro-$VER"
echo "[*] rebuild initramfs (purge nitro)"
if command -v dracut-rebuild >/dev/null 2>&1; then
  dracut-rebuild
else
  echo "[!] dracut-rebuild not found: rebuild your initramfs manually"
fi
echo "[*] done"
