# Release and repository signing keys

## Package repository key

Fingerprint:

```text
C260B0176C622E63CA781D975D8990EE71D2A96C
```

Keyserver: `hkps://keyserver.ubuntu.com` (full fingerprint required)

Fallback: [`nitro-linux-repo.asc`](nitro-linux-repo.asc)

Private half: `GPG_PRIVATE_KEY` GitHub Actions secret in the protected `release`
environment. This key signs packages only; Secure Boot uses the separate
DKMS/MOK key.

## Release tag key

`.github/workflows/publish-repo.yml` imports every `keys/*.asc` file and runs
`git verify-tag` on the pushed `v*` tag. Release tags must therefore be signed
with a key whose public half is committed here. If the tag signing key differs
from the package repository key, add it as `keys/<name>.asc`.

Lightweight or unsigned tags are rejected; use `git tag -s vX.Y.Z`.
