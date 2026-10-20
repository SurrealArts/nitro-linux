# Packaging

Builds `nitro-linux` and publishes the pacman repository at <https://surrealarts.github.io/nitro-linux/x86_64>.

`PKGBUILD` uses `git+$url.git#tag=v$pkgver`; do not replace it with a tag-archive checksum, since `packaging/PKGBUILD` is inside the archive.

```bash
cd packaging
makepkg -sf
```

Publishing is handled by `.github/workflows/publish-repo.yml` on `v*` tags or manual dispatch. It builds in `archlinux:base-devel`, signs with `GPG_PRIVATE_KEY`, runs `repo-add --include-sigs`, and deploys `public/` to Pages.

Release:

1. Bump `VERSION`, `dkms.conf`, `pkgver`, and regenerate `.SRCINFO`.
2. Commit, tag `vX.Y.Z`, and push the tag.
3. Bump `pkgrel` for packaging-only rebuilds.

Key: [keys/nitro-linux-repo.asc](../keys/nitro-linux-repo.asc). Private half: `GPG_PRIVATE_KEY` Actions secret.
