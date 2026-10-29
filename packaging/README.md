# Packaging

Builds `nitro-linux` and publishes the pacman repository at <https://surrealarts.github.io/nitro-linux/x86_64>.

`PKGBUILD` uses `git+$url.git#tag=v$pkgver`; do not replace it with a tag-archive checksum, since `packaging/PKGBUILD` is inside the archive.

```bash
cd packaging
makepkg -sf
```

Publishing is handled by `.github/workflows/publish-repo.yml` on `v*` tags. It builds in a pinned `archlinux:base-devel` image digest, signs with `GPG_PRIVATE_KEY` from the protected `release` environment, runs `repo-add --include-sigs`, and deploys `public/` to Pages. Manual dispatch only re-publishes an existing tag; branch runs are refused. Pages must be enabled once in the repository settings, and the pushed tag must be signed with a key published in `keys/`.

Release:

1. Bump every version location from the repo root:

    ```bash
    make bump VERSION=X.Y.Z
    ```

    The script updates `VERSION`, `dkms.conf`, `packaging/PKGBUILD`, `packaging/.SRCINFO`, the Python fallbacks, `MODULE_VERSION()` in `src/nitro.c` only while it is still a literal (the kernel normally uses the `NITRO_VERSION` define from `VERSION`), and the docs/issue-template version hints. It refuses to run on a dirty tracked tree unless you pass `--allow-dirty`.

2. Review the diff and commit it on its own as `"chore(release): X.Y.Z"`.
3. Create and push a signed tag on that commit: `git tag -s vX.Y.Z` then `git push origin vX.Y.Z`. The tag must verify against a public key in `keys/`; unsigned or lightweight tags are rejected by the publish workflow.
4. Bump `pkgrel` for packaging-only rebuilds (`.SRCINFO`, `PKGBUILD`).

Key: [keys/nitro-linux-repo.asc](../keys/nitro-linux-repo.asc). Private half: `GPG_PRIVATE_KEY` Actions secret. Store it as an environment secret on the protected `release` environment (repository settings → Environments), with required reviewers, not as a plain repository secret.
