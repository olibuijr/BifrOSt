# BifrOSt Operating System Agent Guide

## Scope

This repository builds and releases the BifrOSt operating system. It owns the ArchISO profile, installer, installed-system payload, maintenance and update utilities, trust roots, signed ALPM and Flatpak catalog tooling, release evidence, and publication paths.

First-party sandboxed application development belongs in [`../BifrOSt-Apps`](../BifrOSt-Apps) locally and [`olibuijr/BifrOSt-Apps`](https://github.com/olibuijr/BifrOSt-Apps) on GitHub. That repository owns app source, Flatpak manifests, metadata, translations, tests, and unsigned candidate bundles. Do not add independent end-user app source or manifests here.

Keep the installer, Update Assistant, maintenance utility, welcome utility, privileged helpers, OS configuration, embedded public keys, admission policy, and catalog publication code in this repository. Changes to the app namespace, branch, catalog URL, trust key, signing process, or publication contract require coordinated changes and verification in both repositories.

## Safety invariants

- Treat installer target selection, source validation, destructive confirmation, partitioning, encryption, bootloader installation, and pre-wipe revalidation as load-bearing safety code.
- Never weaken target identity checks, live-media ancestry checks, mounted/active-device checks, minimum-size checks, source readiness, or the final exact-path confirmation.
- BifrOSt supports complete Arch Linux upgrades through `pacman -Syu`; do not introduce partial-upgrade paths.
- Do not claim Secure Boot, offline completeness, reproducibility, rollback, or hardware qualification beyond recorded evidence.
- Private release keys never belong in Git, CI artifacts, command-line arguments, logs, or repository fixtures. Tracked public keys and exact fingerprints are trust inputs; update every embedded and published copy together.
- Preserve the separation between the narrowly scoped BifrOSt ALPM repository and official Arch repositories.

## Application catalog contract

The Update Assistant discovers signed applications dynamically. It accepts only `org.bifrost.*` applications from the pinned HTTPS repository and embedded application-release key. Do not add a hard-coded application registry.

At the current `main` revision, the production catalog contains no application refs. An empty Update Assistant is therefore correct until a real candidate from BifrOSt-Apps is reviewed, signed, and published. `org.bifrost.TemplateCheck` is an ephemeral CI identity, not a product or catalog candidate. Update this status text when the first real application is published; do not encode the temporary empty state in updater logic.

Application development and CI produce unsigned reviewed `.flatpak` candidates in BifrOSt-Apps. Final release operators use `dispatch-app-release.py` here to:

1. stage immutable copies and report SHA-256 digests;
2. reject refs outside the trusted namespace;
3. import into a staging OSTree repository;
4. sign commits and repository metadata with the protected key;
5. publish the catalog consumed by installed BifrOSt systems.

Never move the signing key or direct catalog publication into application CI.

## Verification

Run the narrowest check that exercises the changed contract, followed by repository validation when release inputs changed:

```bash
python3 -m unittest discover -s tests -v
python3 validate-build.py
```

For application catalog changes, exercise an actual candidate through a local, non-production repository before publication, then verify list, install, launch, update, and rollback in a BifrOSt VM.

For installer or installed-system behavioral changes, build the ISO and follow `docs/qemu-test-plan.md`. Build success alone is not installation verification. UI changes require launching the affected interface and exercising the changed path.

Release metadata is generated evidence. Do not hand-edit digests, provenance fields, signatures, or qualification results to make validation pass.

## Repository hygiene

Generated ISO, VM, work, release, cache, and signing material must remain untracked. Treat unexpected working-tree changes as user work and do not overwrite them. Reuse the existing installed-root and packaging patterns rather than introducing a second source for the same shipped file.
