#!/usr/bin/env bash
set -euo pipefail

pacman -Syu --noconfirm --needed git sudo gnupg

if ! id builder >/dev/null 2>&1; then
  useradd -m builder
fi
echo 'builder ALL=(ALL) NOPASSWD: ALL' >/etc/sudoers.d/builder
chmod 440 /etc/sudoers.d/builder

mkdir -p /src/public/x86_64
chown -R builder:builder /src/packaging /src/public

printf '%s' "${GPG_PRIVATE_KEY:?GPG_PRIVATE_KEY is not set}" >/home/builder/repo-key.asc
chmod 600 /home/builder/repo-key.asc
chown builder:builder /home/builder/repo-key.asc
trap 'rm -f /home/builder/repo-key.asc' EXIT

su - builder -c 'bash /src/.github/scripts/build-repo-user.sh'
