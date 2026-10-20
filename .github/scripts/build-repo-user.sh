#!/usr/bin/env bash
set -euo pipefail

gpg --batch --import /home/builder/repo-key.asc
key_id="$(gpg --list-secret-keys --with-colons | awk -F: '/^fpr:/ {print $10; exit}')"
if [ -z "$key_id" ]; then
  echo "No signing key was imported." >&2
  exit 1
fi

cd /src/packaging
makepkg -sf --noconfirm --sign --key "$key_id"

cd /src
pkg_file="$(ls -t packaging/nitro-linux-*.pkg.tar.zst | head -n1)"
sig_file="${pkg_file}.sig"
if [ ! -f "$sig_file" ]; then
  echo "Package signature not found: $sig_file" >&2
  exit 1
fi

cp "$pkg_file" "$sig_file" public/x86_64/
repo-add --include-sigs public/x86_64/nitro-linux.db.tar.gz "$pkg_file"

rm -f public/x86_64/nitro-linux.db public/x86_64/nitro-linux.files
cp public/x86_64/nitro-linux.db.tar.gz public/x86_64/nitro-linux.db
cp public/x86_64/nitro-linux.files.tar.gz public/x86_64/nitro-linux.files
cp /src/packaging/index.html public/index.html
touch public/.nojekyll
