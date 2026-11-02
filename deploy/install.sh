#!/bin/bash
set -e
cd "$(dirname "$0")/.."

MANUAL_MODULE=0
PROBE_MODE=0
MOK_CERT_OVERRIDE=""

usage() {
  cat <<'EOF'
usage: sudo bash deploy/install.sh [--manual-module] [--probe] [--mok-cert PATH]

  --manual-module   skip DKMS; install the userspace tools and services only.
                    Use this when you built and signed the kernel module yourself.
  --probe           load the module read-only for a probe-listed model. Writes
                    'options nitro probe=1' to /etc/modprobe.d/nitro-linux.conf.
  --mok-cert PATH   certificate Secure Boot should have enrolled, when it is not
                    the one DKMS is configured with.
  -h, --help        show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --manual-module) MANUAL_MODULE=1 ;;
    --probe) PROBE_MODE=1 ;;
    --mok-cert)
      shift
      if [ $# -eq 0 ]; then
        echo "[!] --mok-cert needs a path" >&2
        exit 2
      fi
      MOK_CERT_OVERRIDE="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[!] unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if pacman -Qq nitro-linux >/dev/null 2>&1; then
  echo "[!] nitro-linux is installed as a package."
  echo "    Use pacman to upgrade, or remove the package before a source install."
  exit 1
fi

if ! command -v setfacl >/dev/null 2>&1; then
  echo "[!] per-user ACLs disabled; group 'nitro' access still works. Install 'acl' to grant access to the invoking user without a re-login."
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

ensure_nitro_group() {
  getent group nitro >/dev/null 2>&1 || groupadd -r nitro
}

tighten_state() {
  install -d -m700 -o root -g root /var/lib/nitro
  chmod 700 /var/lib/nitro 2>/dev/null || true
  chmod 600 /var/lib/nitro/state.json /var/lib/nitro/history.db* 2>/dev/null || true
  rm -f /var/lib/nitro/state.json.tmp 2>/dev/null || true
}

add_user_nitro() {
  local u
  ensure_nitro_group
  if ! u="$(invoking_user)"; then
    echo "[*] Add your desktop user to group nitro, then log out and back in:"
    echo "    sudo usermod -aG nitro \"\$USER\""
    return 0
  fi
  if id -nG "$u" | grep -qw nitro; then
    return 0
  fi
  usermod -aG nitro "$u"
  echo "[*] Added $u to group nitro. Log out and back in so the TUI can connect."
}

run_as_user() {
  local u="$1"; shift
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$u" -- "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$u" -- "$@"
  else
    return 1
  fi
}

stop_daemon() {
  local pid tries=0
  systemctl stop nitrod.service 2>/dev/null || true
  [ -f /run/nitro.sock.lock ] || return 0
  pid=$(tr -dc '0-9' < /run/nitro.sock.lock)
  [ -n "$pid" ] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  grep -qs nitrod "/proc/$pid/cmdline" 2>/dev/null || return 0
  echo "[*] stopping leftover nitrod (pid $pid)"
  kill "$pid" 2>/dev/null || true
  while kill -0 "$pid" 2>/dev/null && [ "$tries" -lt 5 ]; do
    sleep 1
    tries=$((tries + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
  fi
  rm -f /run/nitro.sock /run/nitro.sock.lock
}

auto_key() {
  local u uid home
  if ! u="$(invoking_user)"; then
    echo "[*] Nitro key: run 'nitro key auto' as your desktop user."
    return 0
  fi
  uid="$(id -u "$u")"
  home="$(getent passwd "$u" | cut -d: -f6)"
  if [ -z "$home" ]; then
    echo "[*] Nitro key: run 'nitro key auto' as $u."
    return 0
  fi
  if [ -e "$home/.config/nitro/key.json" ]; then
    echo "[*] Nitro key: already configured; run 'nitro key auto' as $u to refresh the listener."
    return 0
  fi
  echo "[*] Nitro key: configuring for $u"
  if run_as_user "$u" env \
      HOME="$home" \
      XDG_RUNTIME_DIR="/run/user/$uid" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
      /usr/local/bin/nitro key auto --if-unconfigured; then
    return 0
  fi
  echo "[!] Nitro key: setup incomplete; run 'nitro key auto' as $u to finish."
}

secure_boot_on() {
  # The SecureBoot efivar is 5 bytes; the last one is 1 when enforcement is on.
  local efivar=/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c val=""
  [ -r "$efivar" ] && val=$(od -An -tu1 "$efivar" 2>/dev/null | awk '{print $NF}')
  [ "$val" = "1" ]
}

mok_cert() {
  # DKMS signs with whatever key it is configured with, not necessarily the
  # conventional /var/lib/dkms pair. Honour an explicit override, then the
  # mok_certificate setting from dkms.conf or the DKMS framework config.
  local conf cert
  if [ -n "${MOK_CERT_OVERRIDE:-${NITRO_MOK_CERT:-}}" ]; then
    printf '%s\n' "${MOK_CERT_OVERRIDE:-${NITRO_MOK_CERT:-}}"
    return 0
  fi
  for conf in dkms.conf "${NITRO_DKMS_CONF:-/etc/dkms/framework.conf}"; do
    [ -r "$conf" ] || continue
    cert=$(sed -n 's/^[[:space:]]*mok_certificate[[:space:]]*=[[:space:]]*//p' "$conf" | tail -n 1)
    cert="${cert%\"}"; cert="${cert#\"}"; cert="${cert%\'}"; cert="${cert#\'}"
    if [ -n "$cert" ]; then
      printf '%s\n' "$cert"
      return 0
    fi
  done
  printf '%s\n' /var/lib/dkms/mok.pub
}

sb_key_state() {
  # mokutil's exit status is not an "is enrolled" signal: 0 means the key can
  # still be enrolled, 1 means it is already present or blocked, and -1 means
  # the file could not be tested. Read the message instead, which is stable.
  local out
  out=$(mokutil --test-key "$1" 2>&1) || true
  case "$out" in
    *"already in the enrollment request"*) printf 'pending\n' ;;
    *"blocked in"*) printf 'blocked\n' ;;
    *"already in db"*|*"is already enrolled"*) printf 'enrolled\n' ;;
    *"already in the built-in trusted keyring"*) printf 'trusted\n' ;;
    *"is not enrolled"*) printf 'not-enrolled\n' ;;
    *) printf 'unknown\n' ;;
  esac
}

preflight_sb() {
  local cert state
  secure_boot_on || return 0
  echo "[!] Secure Boot is ON."
  cert="$(mok_cert)"
  if ! command -v mokutil >/dev/null; then
    echo "Install mokutil, then enroll the signing key so the module loads:"
    echo "  sudo pacman -S mokutil"
    echo "  sudo mokutil --import $cert"
    echo "Reboot, confirm enrollment in MokManager, then re-run this script."
    return 1
  fi
  if [ ! -f "$cert" ]; then
    echo "No signing certificate at $cert."
    if [ "$cert" = /var/lib/dkms/mok.pub ]; then
      echo "Create the DKMS keypair, then re-run:"
      echo "  sudo openssl req -new -x509 -newkey rsa:2048 -keyout /var/lib/dkms/mok.key \\"
      echo "    -outform DER -out $cert -nodes -days 36500 \\"
      echo "    -subj '/CN=DKMS module signing key/'"
    else
      echo "Point --mok-cert or mok_certificate in dkms.conf at the certificate you sign with,"
      echo "or create a keypair at that path."
    fi
    echo "Then enroll it and reboot:"
    echo "  sudo mokutil --import $cert"
    echo "Signing the module yourself? Use: sudo bash deploy/install.sh --manual-module"
    return 1
  fi
  state="$(sb_key_state "$cert")"
  case "$state" in
    enrolled)
      echo "[*] signing key $cert is enrolled, continuing."
      return 0
      ;;
    trusted)
      echo "[*] signing key $cert is in the kernel trusted keyring, continuing."
      return 0
      ;;
    pending)
      echo "Enrollment of $cert is already queued but not active."
      echo "Reboot, confirm it in MokManager, then re-run this script."
      return 1
      ;;
    blocked)
      echo "$cert is blocked by the firmware (dbx/MokListX). Remove the block first."
      return 1
      ;;
    not-enrolled)
      echo "The signing key $cert is not enrolled. Enroll it, then re-run:"
      echo "  sudo mokutil --import $cert"
      echo "Reboot, confirm enrollment in MokManager, then re-run this script."
      echo "Signing the module yourself? Use: sudo bash deploy/install.sh --manual-module"
      return 1
      ;;
    *)
      echo "[!] could not read the enrollment state of $cert; continuing." >&2
      echo "    check it with: mokutil --test-key $cert" >&2
      return 0
      ;;
  esac
}
if [ "$MANUAL_MODULE" -eq 0 ]; then
  if ! command -v dkms >/dev/null 2>&1; then
    echo "[!] dkms is not installed; it builds and signs the module for each kernel." >&2
    echo "    install dkms and the headers for $(uname -r), or use --manual-module." >&2
    exit 1
  fi
  preflight_sb
  VER=$(grep PACKAGE_VERSION dkms.conf | cut -d'"' -f2)
else
  VER=$(tr -d '[:space:]' < VERSION)
fi
if ! [[ $VER =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "[!] invalid version '$VER' (expected X.Y.Z)" >&2
  exit 1
fi
echo "[*] version $VER"
PRELOADED_VER=""
if [ "$MANUAL_MODULE" -eq 1 ]; then
  MODULE_VER=$(modinfo -F version nitro 2>/dev/null || true)
  PRELOADED_VER=$(cat /sys/module/nitro/version 2>/dev/null || true)
  # A module loaded with insmod need not be installed in the module tree yet.
  [ -n "$MODULE_VER" ] || MODULE_VER=$PRELOADED_VER
  if [ -z "$MODULE_VER" ]; then
    echo "[!] no nitro module for $(uname -r): not in the module tree and not loaded" >&2
    echo "    build and install (or insmod) the module for the running kernel first." >&2
    exit 1
  fi
  if [ "$MODULE_VER" != "$VER" ]; then
    if [ "$PRELOADED_VER" = "$MODULE_VER" ]; then
      echo "[!] loaded nitro is $MODULE_VER, expected $VER" >&2
    else
      echo "[!] nitro in the $(uname -r) module tree is $MODULE_VER, expected $VER" >&2
      if [ "$PRELOADED_VER" = "$VER" ]; then
        echo "    $VER is loaded now, but the next boot would pick $MODULE_VER." >&2
      fi
    fi
    echo "    rebuild it from this checkout for $(uname -r), install it in /lib/modules" >&2
    echo "    (depmod -a), then retry --manual-module. Sign it again if Secure Boot is on." >&2
    exit 1
  fi
  echo "[*] manual module mode: skipping the DKMS Secure Boot preflight"
  if secure_boot_on; then
    echo "    Secure Boot is ON: only an enrolled key can load the module."
    echo "    verify yours with: mokutil --test-key <certificate.pub>"
  fi
fi
if [ "$MANUAL_MODULE" -eq 0 ]; then
  SRC=/usr/src/nitro-$VER
  echo "[*] copy source to $SRC"
  rm -rf "$SRC"
  mkdir -p "$SRC"
  cp -a Makefile dkms.conf VERSION src tools docs deploy README.md "$SRC/"
  rm -rf "$SRC/src/"*.o "$SRC/src/"*.ko "$SRC/src/"*.mod "$SRC/src/"*.mod.c "$SRC/src/".*.cmd "$SRC/.tmp_versions" "$SRC/Module.symvers" "$SRC/modules.order" 2>/dev/null || true
  echo "[*] dkms add/build/install"
  for v in $(dkms status 2>/dev/null | sed -n 's#^nitro/\([^,]*\),.*#\1#p' | sort -u); do
    dkms remove -m nitro -v "$v" --all 2>/dev/null || true
  done
  dkms add -m nitro -v "$VER"
  for k in /lib/modules/*; do
    [ -d "$k/build" ] || continue
    k="${k##*/}"
    echo "[*] build/install for $k"
    dkms build -m nitro -v "$VER" -k "$k"
    dkms install -m nitro -v "$VER" -k "$k"
  done
else
  echo "[*] manual module mode: skipping DKMS build and install"
fi
echo "[*] modules-load + config + service"
install -m644 deploy/nitro-modules.conf /etc/modules-load.d/nitro.conf
install -m755 deploy/nitro-apply /usr/local/bin/nitro-apply
install -m644 deploy/nitro-apply.service /etc/systemd/system/nitro-apply.service
install -m755 daemon/nitrod.py /usr/local/bin/nitrod
install -m755 tui/nitro-linux.py /usr/local/bin/nitro-linux
install -m755 tools/nitro.py /usr/local/bin/nitro
install -Dm644 tools/model.py /usr/local/lib/nitro/model.py
install -Dm644 deploy/nitro-key.service /usr/local/lib/systemd/user/nitro-key.service
VFILE="VERSION"; [ -f "$VFILE" ] || VFILE="$(dirname "$0")/../VERSION"
if [ -f "$VFILE" ]; then
  install -Dm644 "$VFILE" /usr/local/lib/nitro/VERSION
  V="$(tr -d '[:space:]' < "$VFILE")"
  sed -i "s/^VERSION = .*/VERSION = \"$V\"/" /usr/local/bin/nitro-linux
  sed -i "s/^VERSION = .*/VERSION = \"$V\"/" /usr/local/bin/nitrod
fi
install -m644 deploy/nitrod.service /etc/systemd/system/nitrod.service
[ -f /etc/nitro.conf ] || install -m644 deploy/nitro.conf /etc/nitro.conf
if [ "$PROBE_MODE" -eq 1 ]; then
  install -d -m755 /etc/modprobe.d
  printf '# nitro-linux: read-only probe mode, added by deploy/install.sh --probe\noptions nitro probe=1\n' \
    > /etc/modprobe.d/nitro-linux.conf
  chmod 644 /etc/modprobe.d/nitro-linux.conf
  echo "[*] probe mode: options nitro probe=1 in /etc/modprobe.d/nitro-linux.conf"
elif [ -f /etc/modprobe.d/nitro-linux.conf ] &&
     grep -qs 'deploy/install.sh --probe' /etc/modprobe.d/nitro-linux.conf; then
  rm -f /etc/modprobe.d/nitro-linux.conf
  echo "[*] probe mode: drop-in removed"
fi
tighten_state
add_user_nitro
systemctl daemon-reload
stop_daemon
if [ "$MANUAL_MODULE" -eq 1 ] && [ "$PRELOADED_VER" = "$VER" ]; then
  echo "[*] load: nitro $VER is already loaded, keeping it"
else
  echo "[*] load"
  rmmod nitro 2>/dev/null || true
  sleep 1
  modprobe led-class-multicolor 2>/dev/null || true
  if [ "$MANUAL_MODULE" -eq 1 ]; then
    depmod -a "$(uname -r)" 2>/dev/null || true
    echo "[*] manual module mode: loading $(uname -r) module via modprobe"
  fi
  modprobe nitro || {
    LOG=$(dmesg 2>/dev/null | grep -i 'nitro:' | tail -5 || true)
    [ -n "$LOG" ] && printf '%s\n' "$LOG" >&2
    echo "[!] modprobe failed." >&2
    case "$LOG" in
      *"DMI match failed"*)
        if [ "$PROBE_MODE" -eq 1 ]; then
          echo "    probe=1 is already set, so this model has no DMI entry at all." >&2
          echo "    run 'nitro report' and open a model support issue." >&2
        else
          echo "    this model is not in the verified DMI list. If it has a probe entry," >&2
          echo "    re-run with --probe to load it read-only." >&2
        fi
        ;;
      *"WMI GUID"*)
        echo "    the Acer WMI GUID is not exposed by this firmware; nitro cannot bind." >&2
        ;;
      *)
        if secure_boot_on; then
          if [ "$MANUAL_MODULE" -eq 1 ]; then
            echo "    Secure Boot is ON: the key that signed your module must be enrolled (mokutil --import), then reboot." >&2
          else
            echo "    Secure Boot is ON: enroll the signing key $(mok_cert) and reboot." >&2
          fi
        fi
        ;;
    esac
    exit 1
  }
fi
LOADED_VER=$(cat /sys/module/nitro/version 2>/dev/null || true)
if [ -z "$LOADED_VER" ]; then
  LOADED_VER=$(modinfo -F version nitro 2>/dev/null || true)
fi
if [ -z "$LOADED_VER" ]; then
  echo "[!] cannot read the loaded nitro version; expected $VER"
  echo "    check 'lsmod | grep nitro' and 'modinfo nitro'."
  exit 1
fi
if [ "$LOADED_VER" != "$VER" ]; then
  echo "[!] loaded nitro is $LOADED_VER, expected $VER"
  echo "    modprobe picked up a stale copy; install your module for $(uname -r) and retry."
  exit 1
fi
sleep 1
echo "[*] enable and start services"
systemctl enable nitro-apply.service nitrod.service
systemctl restart nitro-apply.service 2>/dev/null || true
systemctl restart nitrod.service 2>/dev/null || true
if [ -d /run/systemd/system ]; then
  sleep 1
  if ! systemctl is-active --quiet nitrod.service; then
    echo "[!] nitrod.service failed to start" >&2
    systemctl --no-pager --full status nitrod.service 2>/dev/null | tail -12 || true
    journalctl -u nitrod.service -n 20 --no-pager 2>/dev/null || true
    echo "    inspect: journalctl -u nitrod.service" >&2
    exit 1
  fi
fi
if [ "$MANUAL_MODULE" -eq 0 ]; then
  echo "[*] rebuild initramfs (bake current nitro, purge stale copies)"
  if command -v dracut-rebuild >/dev/null 2>&1; then
    dracut-rebuild
  else
    echo "[!] dracut-rebuild not found: rebuild your initramfs manually"
  fi
else
  echo "[*] manual module mode: skipping initramfs rebuild"
fi
echo "[*] status"
dkms status | grep nitro || true
ls /sys/devices/platform/nitro/ | tr '\n' ' '; echo
sensors 2>/dev/null | grep -A4 nitro || true
for led in /sys/class/leds/*nitro*; do
  [ -e "$led" ] || continue
  echo "${led##*/}"
done
echo "[*] done. 'nitro-linux' panel and 'nitro' CLI are ready."
auto_key
