# BifrOSt 0.2.2 release status

Updated 2026-08-07. This file supersedes every earlier continuation note.

## Release policy

- 0.2.2 is a **patch release**: it ships exclusively through the signed
  `[bifrost]` pacman repository (`https://olibuijr.github.io/BifrOSt/alpm/$arch`).
  Installed systems receive it with a complete `sudo pacman -Syu`.
- **No ISO is built for patch releases.** Installation media are produced only
  when the minor version increases (next: 0.3.0). The 0.2.1 ISO-size finding
  (seeded ISO ~4.0 GB vs the 2 GiB GitHub asset limit) is deferred to 0.3.0.
- The signed tag `v0.2.1` remains where it is; it predates the keyring and
  ownership fixes and must never be moved. 0.2.2 gets a new signed tag at the
  release commit.

## State

- `VERSION` is `0.2.2`. `bifrost-system` is `0.2.2-1` (pkgrel reset; the
  0.2.1-x pkgrel series ended at 8 and is obsolete).
- Installed provenance template is back to `unsigned-development` placeholder
  form for version 0.2.2 (the prepared 0.2.1 build-input provenance was
  discarded with the abandoned ISO attempt).
- Static validation passes (`python3 validate-build.py`), including the new
  airootfs hygiene check. All 114 unit tests pass.
- An **unsigned** `bifrost-system-0.2.2-1-any.pkg.tar.zst` build passed on
  2026-08-07, containing the 0.2.2 payload without official-package path
  conflicts.

## What 0.2.2 contains

Release-pipeline and installer hardening (all merged in the working tree for
the release commit):

- `publish-release.py`: immutable O_NOFOLLOW asset staging (validation,
  upload, and re-hash all use the same staged read-only copies); idempotent
  draft-resume that verifies tag target and asset digests, never touching a
  published release; QEMU evidence is produced locally, bound to the exact
  ISO digest, and must carry per-case `install_seconds`.
- `generate-release-metadata.py`: every cached package archive requires its
  detached signature, verified against the pinned keyring; `.PKGINFO`
  name/version/arch must match the ALPM database record; VALIDSIG is parsed
  positionally and the PRIMARY key fingerprint must equal the trusted one.
- `dispatch-app-release.py`: signing requires a reviewed candidate manifest
  (bundle SHA-256, source revision, app identity); the imported ref set must
  match exactly; `org.bifrost.TemplateCheck` is denylisted.
- `prepare-installer-cache.py` + installer backend: seed staging is
  root-owned before verification, every archive signature is verified, and the
  backend validates the complete manifest (sizes, hashes, signatures) before
  enabling the seed, falling back online with one logged warning. New
  `--require` flag gates release builds.
- `vm/qemu-release-candidate.py`: per-case install/cold-boot wall times in
  result.json, bounded serial-socket drain on all exit paths, and an
  `--overall-deadline` that writes failure evidence before expiry.
- GitHub Actions removed entirely; validation, qualification, and publication
  all run locally (`validate-build.py` now rejects a `.github` directory).
- Live ISO pacman policy: `SigLevel = Required DatabaseRequired`,
  `LocalFileSigLevel = Required`.
- New regression tests: pacstrap `-K`/keyring wrapper, bifrost-system path
  ownership disjointness, release-pipeline behavioral contracts, seed
  verification, dispatch admission, provenance, publish staging.
- `profile/airootfs/usr/share/bifrost/os-release` bumped to 0.2.2.

## Signing keys (rotated 2026-08-07)

The previous card-held keys (`A306…EFB6` evidence, `69D9…E1C2` ALPM) were
retired with operator authorization because the card is unavailable. New
passphrase-protected keys were generated in isolated GnuPG homedirs under
`~/.local/state/bifrost-release/`:

- Release evidence (tag signing): `B2E09853D23E5DB621C6123BFC13D6D63D06E8D2`
- ALPM packages/repository: `F5CE992078EA20EA8469A05FC68D23E4208D553F`

`keys/bifrost-release-key.asc`, `keys/bifrost-alpm-key.asc`, the pinned
`ALPM_PRIMARY_FINGERPRINT`, and the README commands reference the new keys.
Tags signed with the old evidence key (`v0.2.0`, `v0.2.1`) verify only against
the old public key preserved in Git history.

## Rollout to installed systems

Systems installed from 0.2.0/0.2.1 media trust only the old ALPM key, so a
one-time key adoption is required before the upgrade:

```bash
curl -fsSLo /tmp/bifrost-key.asc \
  https://olibuijr.github.io/BifrOSt/alpm/x86_64/alpm-repository-key.asc
sudo pacman-key --add /tmp/bifrost-key.asc
sudo pacman-key --lsign-key F5CE992078EA20EA8469A05FC68D23E4208D553F
sudo pacman -Syu
```

Fresh 0.3.0+ media will carry and lsign the new key automatically through the
installer bootstrap. No USB has been written and no physical installation has
been performed for 0.2.2; none is required for a patch release.
