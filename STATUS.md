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

## Remaining steps (blocked on the signing card)

Both protected keys live on the OpenPGP card, which is **not connected**
(`gpg --card-status`: no device). Do not weaken verification or substitute
keys. With the card connected:

1. Commit the working tree as the 0.2.2 release commit and create the signed
   annotated tag `v0.2.2` with the release evidence key
   `A306D3537F1538306CB3A23B2C4A62768746EFB6`.
2. Build and sign the package and repository with
   `packaging/alpm/build-repository.py` using the ALPM key
   `69D95C1EA4E97AB5FB9580AAFED54F3B9691E1C2` in an isolated GnuPG homedir
   (the script refuses the personal homedir).
3. Publish the staged repository to the `gh-pages` `alpm/x86_64` path consumed
   by installed systems.
4. Verify on an installed 0.2.1 system (or chroot) that a complete
   `sudo pacman -Syu` installs `bifrost-system 0.2.2-1` with valid package and
   database signatures.

No USB has been written and no physical installation has been performed for
0.2.2; none is required for a patch release.
