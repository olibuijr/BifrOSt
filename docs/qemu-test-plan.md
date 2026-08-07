# Automated QEMU release-candidate qualification

This is a release-candidate test procedure, not a claim that an ISO passed. It exercises the exact ISO named on the command line in two independent UEFI/KVM virtual machines. Secure Boot remains disabled: BifrOSt does not support Secure Boot in the 0.2 series. This procedure also makes no system or Btrfs rollback claim.

## Host requirements

Use an Arch Linux x86-64 host with `/dev/kvm` available to the invoking user and these official packages installed:

```bash
sudo pacman -Syu --needed qemu-desktop edk2-ovmf
```

The harness uses the 4 MiB firmware images at:

- `/usr/share/edk2/x64/OVMF_CODE.4m.fd`
- `/usr/share/edk2/x64/OVMF_VARS.4m.fd`

It refuses to fall back silently when KVM, QEMU, or those images are unavailable.

## Run both cases

Name one exact, already-built release-candidate ISO. Do not use a wildcard or a moving symlink:

```bash
ISO=/absolute/path/to/bifrost-0.2.1-x86_64.iso
python3 vm/qemu-release-candidate.py --iso "$ISO" --case all
```

The default evidence root is a newly created mode-`0700` directory below `~/.local/state/bifrost/qemu-rc/`. The command prints its exact path only after both cases pass. For a deterministic CI location, provide a path that does not exist yet:

```bash
python3 vm/qemu-release-candidate.py \
  --iso /srv/bifrost-candidates/bifrost-0.2.1-x86_64.iso \
  --case all \
  --work-dir /srv/bifrost-qemu-evidence/0.2.1-rc1
```

`--case standard` and `--case luks2` are useful for diagnosis, but a release candidate is not qualified unless `--case all` passes in one recorded run.

> **Credential warning:** The harness credentials are fixed, public, disposable test values. Never reuse them, replace them with real credentials, or enter any real secret into these VMs. A retained QCOW2 must contain test data only; successful log-redaction checks are not proof that a disk is safe to handle as non-confidential evidence.

### Disk safety

The command accepts no disk-device argument. It creates a new 64 GiB sparse `disk.qcow2` inside each new case directory and refuses an existing work directory, a symlink ISO, or a missing ISO. The guest payload independently requires exactly one virtual disk at `/dev/vda`, checks the case-specific `BIFROST-RC-*` VirtIO serial, and refuses installation if any of those facts differ. Never modify the harness to pass through a host block device for release qualification.

Each case receives its own writable copy of `OVMF_VARS.4m.fd`; firmware state and disks are never shared between cases.

## What is exercised

For each case, the harness:

1. records the exact ISO path, byte size, SHA-256, QEMU version, OVMF package version, and firmware-image hashes;
2. creates an isolated disk and OVMF variable store;
3. attaches the exact ISO and cold-boots it with UEFI/KVM;
4. waits for the live image's serial root console and transfers mode-`0600` schema-v2 intent and secrets;
5. invokes `/usr/local/lib/bifrost-installer-backend` noninteractively and requires its terminal success event;
6. powers off the live environment completely;
7. starts a new QEMU process with no CD-ROM or ISO argument; and
8. runs read-only installed-system assertions before powering off again.

The standard case selects only `base`, disables encryption, and requests `en_US.UTF-8` with the `us` keymap. The LUKS2 case selects exactly `base` and `dev-rust`, requests `is_IS.UTF-8` with `is-latin1`, and requires a LUKS2 root.

Before the encrypted disk is unlocked successfully, the harness submits an intentionally wrong disposable passphrase. It must observe rejection or a repeated unlock prompt before it sends the correct disposable passphrase. Failure to observe that rejection fails the case.

The installed-system probe requires:

- `/usr/share/bifrost/release.json` to have `build-input` provenance and identify the root `VERSION`, source revision, build ID, and profile digest;
- `/etc/bifrost/install-state.json` to reference that file's exact hash and the same version, revision, build ID, and provenance status;
- both `linux` and `linux-lts` packages, kernels, and initramfs images;
- exactly the selected profile IDs, every package in their installed manifests, and no unselected profile ID in recorded state;
- the requested deterministic locale and console keymap;
- BifrOSt OS identity;
- exactly one retained installer run whose recorded run ID/path agree with install state and whose status, backend, archinstall, plan, and generated-config evidence files are present; and
- absence of the disposable login and encryption passwords from retained installer logs.

A serial-only one-shot assertion service is added to the isolated test image after the production backend succeeds. It makes cold-boot results observable and powers off; it is test instrumentation, not installed BifrOSt payload.

## Evidence layout

The top-level evidence directory contains:

- `manifest.json` — ISO identity plus host QEMU/OVMF versions and hashes;
- `iso.sha256` — the exact ISO digest;
- `result.json` — terminal result and paths for every selected case.

Each `standard/` or `luks2/` directory contains:

- `disk.qcow2` — retained installed system and `/var/log/bifrost-installer/<run-id>` evidence;
- `OVMF_VARS.fd` — the case's retained firmware state;
- `install.command.json` and `boot.command.json` — exact QEMU argument vectors;
- `install.serial.log` and `boot.serial.log` — complete serial transcripts;
- `install.qemu.log` and `boot.qemu.log` — QEMU diagnostics; and
- `result.json` — the case result, wrong-passphrase result, and evidence paths.

The boot serial log includes normalized release/install-state JSON, package versions, retained evidence paths and permissions, and boot-file hashes. Preserve the entire evidence directory on the controlled qualification machine, including the potentially large QCOW2 disks.

## Execution

This repository does not use GitHub Actions; qualification runs locally on a
controlled machine with KVM access. Place the exact ISO on that machine, then
run:

```bash
python3 vm/qemu-release-candidate.py \
  --iso /srv/bifrost-candidates/bifrost-<version>-x86_64.iso \
  --case all \
  --work-dir /srv/bifrost-qemu-evidence/<version>-rc1 \
  --overall-deadline 26700
```

The invoking account must already have read/write access to `/dev/kvm` and write access to the new work-directory parent. Do not run the harness with `sudo`, do not expose host disks to QEMU, and do not treat a single-case or boot-only result as release qualification. The publisher consumes the evidence directory directly and requires both cases to have passed with recorded per-case install durations.
