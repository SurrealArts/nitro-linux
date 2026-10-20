# Repository signing key

Fingerprint:

```text
7EA2338784E083123CC077F2E29A80B4FDD576B1
```

Keyserver: `hkps://keyserver.ubuntu.com` (full fingerprint required)

Fallback: [`nitro-linux-repo.asc`](nitro-linux-repo.asc)

Private half: `GPG_PRIVATE_KEY` GitHub Actions secret. This key signs packages only; Secure Boot uses the separate DKMS/MOK key.
