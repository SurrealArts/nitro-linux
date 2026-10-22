#!/bin/bash
set -e
cd "$(dirname "$0")/.."

if pacman -Qq nitro-linux >/dev/null 2>&1; then
  echo "[!] nitro-linux is installed as a package."
  echo "    Remove it with: sudo pacman -Rns nitro-linux"
  exit 1
fi

invoking_user() {
  local u sid uid users=
  for u in "${SUDO_USER:-}" "$(logname 2>/dev/null || true)"; do
    if [ -n "$u" ] && [ "$u" != root ] && id "$u" >/dev/null 2>&1; then
      printf '%s\n' "$u"
      return 0
    fi
  done
  command -v loginctl >/dev/null 2>&1 || return 1
  for sid in $(loginctl list-sessions --no-legend 2>/dev/null | awk '$1 ~ /^[0-9]+$/ {print $1}'); do
    [ "$(loginctl show-session "$sid" -p Class --value 2>/dev/null)" = user ] || continue
    [ "$(loginctl show-session "$sid" -p Remote --value 2>/dev/null)" = no ] || continue
    [ "$(loginctl show-session "$sid" -p Active --value 2>/dev/null)" = yes ] || continue
    uid="$(loginctl show-session "$sid" -p User --value 2>/dev/null)"
    [ -n "$uid" ] || continue
    u="$(id -nu "$uid" 2>/dev/null)" || continue
    [ "$u" != root ] || continue
    case " $users " in
      *" $u "*) ;;
      *) users="$users $u" ;;
    esac
  done
  users="${users# }"
  [ -n "$users" ] || return 1
  case "$users" in
    *" "*) return 1 ;;
  esac
  printf '%s\n' "$users"
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
