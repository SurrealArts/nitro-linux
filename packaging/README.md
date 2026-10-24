# Packaging

Builds `nitro-linux` and publishes the pacman repository at <https://surrealarts.github.io/nitro-linux/x86_64>.

`PKGBUILD` uses `git+$url.git#tag=v$pkgver`; do not replace it with a tag-archive checksum, since `packaging/PKGBUILD` is inside the archive.

```bash
cd packaging
makepkg -sf
```

Publishing is handled by `.github/workflows/publish-repo.yml` on `v*` tags or manual dispatch. It builds in `archlinux:base-devel`, signs with `GPG_PRIVATE_KEY`, runs `repo-add --include-sigs`, and deploys `public/` to Pages.

Release:

1. Bump:
    - `dkms.conf`: PACKAGE_VERSION
    - `VERSION`
    - `.github/ISSUE_TEMPLATE/bug-report.yml`: placeholder
    - `daemon/nitrod.py`: VERSION
    - `docs/MODELS.md`
    - `packaging/.SRCINFO`: pkgver, source
    - `packaging/PKGBUILD`: pkgver
    - `src/nitro.c`: MODULE_VERSION, pr_info
    - `tui/nitro-linux.py`: `VERSION`,
2. Commit the version bump on its own commit `"chore(release): X.Y.Z"`, tag that commit `vX.Y.Z`, and push the tag.
3. Bump `pkgrel` for packaging-only rebuilds (.SRCINFO, PKGBUILD).

Key: [keys/nitro-linux-repo.asc](../keys/nitro-linux-repo.asc). Private half: `GPG_PRIVATE_KEY` Actions secret.
