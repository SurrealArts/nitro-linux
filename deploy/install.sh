#!/bin/bash
set -e
cd "$(dirname "$0")/.."

preflight_sb() {
  # For Secure Boot systems
  local efivar=/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c
  local val="" pub=/var/lib/dkms/mok.pub key=/var/lib/dkms/mok.key
  [ -r "$efivar" ] && val=$(od -An -tu1 "$efivar" 2>/dev/null | awk '{print $NF}')
  [ "$val" != "1" ] && return 0
  echo "[!] Secure Boot is ON."
  if ! command -v mokutil >/dev/null; then
    echo "Install mokutil, then enroll the DKMS key so the module loads:"
    echo "  sudo pacman -S mokutil"
    echo "  sudo mokutil --import $pub"
    echo "Reboot, confirm enrollment in MokManager, then re-run this script."
    return 1
  fi
  if [ ! -f "$key" ] || [ ! -f "$pub" ]; then
    echo "No DKMS signing key found. Create one, enroll it, then re-run:"
    echo "  sudo openssl req -new -x509 -newkey rsa:2048 -keyout $key -outform DER -out $pub -nodes -days 36500 -subj '/CN=DKMS module signing key/'"
    echo "  sudo mokutil --import $pub"
    echo "Reboot, confirm enrollment in MokManager, then re-run this script."
    return 1
  fi
  if mokutil --test-key "$pub" >/dev/null 2>&1; then
    echo "[*] DKMS key is enrolled, continuing."
    return 0
  fi
  echo "The DKMS key is not enrolled. Enroll it, then re-run:"
  echo "  sudo mokutil --import $pub"
  echo "Reboot, confirm enrollment in MokManager, then re-run this script."
  return 1
}
preflight_sb
VER=$(grep PACKAGE_VERSION dkms.conf | cut -d'"' -f2)
SRC=/usr/src/nitro-$VER
echo "[*] version $VER"
echo "[*] copy source to $SRC"
rm -rf "$SRC"
mkdir -p "$SRC"
cp -a Makefile dkms.conf src tools docs deploy README.md "$SRC/"
rm -rf "$SRC/src/"*.o "$SRC/src/"*.ko "$SRC/src/".*.cmd "$SRC/.tmp_versions" "$SRC/Module.symvers" "$SRC/modules.order" 2>/dev/null || true
echo "[*] dkms add/build/install"
dkms remove -m nitro -v "$VER" --all 2>/dev/null || true
dkms add -m nitro -v "$VER"
for k in $(ls /lib/modules); do
  [ -d "/lib/modules/$k/build" ] || continue
  echo "[*] build/install for $k"
  dkms build -m nitro -v "$VER" -k "$k"
  dkms install -m nitro -v "$VER" -k "$k"
done
echo "[*] modules-load + config + service"
install -m644 deploy/nitro-modules.conf /etc/modules-load.d/nitro.conf
install -m755 deploy/nitro-apply /usr/local/bin/nitro-apply
install -m644 deploy/nitro-apply.service /etc/systemd/system/nitro-apply.service
install -m755 daemon/nitrod.py /usr/local/bin/nitrod
install -m755 tui/nitro-linux.py /usr/local/bin/nitro-linux
install -m755 tools/nitro.py /usr/local/bin/nitro
rm -f /usr/local/bin/nitro-tui  # remove the pre-rename command
VFILE="VERSION"; [ -f "$VFILE" ] || VFILE="$(dirname "$0")/../VERSION"
if [ -f "$VFILE" ]; then
  sed -i "s/^VERSION = .*/VERSION = \"$(tr -d '[:space:]' < "$VFILE")\"/" /usr/local/bin/nitro-linux
fi
install -m644 deploy/nitrod.service /etc/systemd/system/nitrod.service
[ -f /etc/nitro.conf ] || install -m644 deploy/nitro.conf /etc/nitro.conf
mkdir -p /var/lib/nitro
systemctl daemon-reload
systemctl enable nitro-apply.service nitrod.service
systemctl stop nitrod.service 2>/dev/null || true
echo "[*] load"
rmmod nitro 2>/dev/null || true
sleep 1
modprobe led-class-multicolor
modprobe nitro || { dmesg | tail -5; echo "[!] modprobe failed. If Secure Boot is on, the DKMS key must be enrolled (see Secure Boot below)."; exit 1; }
sleep 1
echo "[*] apply /etc/nitro.conf"
systemctl restart nitro-apply.service 2>/dev/null || true
systemctl restart nitrod.service 2>/dev/null || true
echo "[*] rebuild initramfs (bake current nitro, purge stale copies)"
if command -v dracut-rebuild >/dev/null 2>&1; then
  dracut-rebuild
else
  echo "[!] dracut-rebuild not found: rebuild your initramfs manually"
fi
echo "[*] status"
dkms status | grep nitro || true
ls /sys/devices/platform/nitro/ | tr '\n' ' '; echo
sensors 2>/dev/null | grep -A4 nitro || true
ls /sys/class/leds/ | grep nitro || true
echo "[*] done. 'nitro-linux' panel and 'nitro' CLI are ready."
