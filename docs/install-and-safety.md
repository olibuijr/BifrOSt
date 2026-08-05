# Uppsetning og öryggi / Installation and safety

> **AÐVÖRUN — ÖLL GÖGN Á VALDA DISKINUM EYÐAST.** BifrOSt 0.2 styður aðeins uppsetningu á heilum diski. Það er hvorki tvíræsing né handvirk disksneiðing. Taktu afrit og aftengdu diska sem ekki má eyða.
>
> **WARNING — ALL DATA ON THE SELECTED DISK WILL BE ERASED.** BifrOSt 0.2 is a whole-disk installer only. It does not offer dual boot or manual partitioning. Back up your data and disconnect every disk that must not be erased.

## Before booting

- Use a UEFI x86_64 computer. Secure Boot is not supported; disable it in firmware before booting the medium.
- Keep the computer on reliable power.
- Back up the target and verify the backup from another device.
- Disconnect non-target storage where practical. This reduces risk but does not replace identity checks.
- For an online installation, connect to a reliable network before opening the installer.
- Use a non-removable whole-disk target of at least 20 GiB. The UI applies that suitability threshold; the privileged geometry guard independently refuses any target below 16 GiB.

The installer refuses read-only, removable, too-small, mounted, active-swap, holder-backed, live-media, and otherwise active targets it can identify. It also rejects any path, model, serial, WWN, capacity, or sector-size change. Those checks are safeguards, not a substitute for checking the physical disk yourself.

## Identify the target

The installer shows the device path, model, capacity, serial number, and WWN when available. Compare at least two stable facts—preferably model plus serial or WWN—with the drive label, firmware setup, or vendor documentation. Capacity alone is not a unique identity.

Device paths such as `/dev/sda` and `/dev/nvme0n1` can change after unplugging, rebooting, or adding hardware. If hardware changes, rescan and repeat the comparison. Do not continue when a serial/WWN is unexpectedly missing or a displayed fact differs from the drive you intend to erase.

The final in-app confirmation repeats the selected disk identity and requires the exact target path shown at that moment. Read the model, size, serial/WWN, and path again before typing it. Cancel if any field is wrong. The privileged backend revalidates the target immediately before the wipe and refuses identity or topology changes.

## Installation choices

### Encryption

**Encrypt disk (LUKS2)** places the Btrfs `@`, `@home`, and `@log` subvolumes inside a LUKS2 root partition. A separate 1 GiB FAT32 EFI System Partition mounted at `/boot`, plus the metadata needed to start the unlock process, remain unencrypted.

- Use a long, unique passphrase and store it in a separate password manager or physical recovery record.
- The login password and disk passphrase are separate secrets.
- Losing the LUKS2 passphrase means the encrypted data cannot be recovered by BifrOSt.
- The installer passes secrets in a separate mode-`0600` temporary file and removes it after consumption. It does not intentionally retain plaintext passwords or passphrases in installer logs.
- Encryption protects data at rest; it does not make an already unlocked or compromised running system safe.

### Profiles

`base` is always installed. The 0.2 profile manifests are:

| ID | Purpose | 0.2 manifest packages |
| --- | --- | --- |
| `base` | Mandatory installed-system base | `btrfs-progs`, `cryptsetup`, `firefox`, `firefox-i18n-is`, `fwupd`, `gtk4`, `libadwaita`, `linux-firmware`, `networkmanager`, `noto-fonts`, `noto-fonts-emoji`, `plymouth`, `python-gobject` |
| `dev-rust` | Rust development | `base-devel`, `clang`, `cmake`, `git`, `just`, `lld`, `ninja`, `rust-analyzer`, `rustup` |
| `dev-containers` | Container development | `distrobox`, `podman` |
| `dev-web` | Web development | `base-devel`, `git`, `nodejs`, `npm` |
| `dev-python` | Python development | `base-devel`, `git`, `python-pip`, `python-pipx`, `python-virtualenv` |

These are package names, not frozen package versions. Shared dependencies are installed once. The review page is the authority for the choices applied to that run; resolution follows current signed official Arch repositories and can change with Arch's rolling release.

### Language and system defaults

The installer offers Icelandic and English for critical installation messages. The installed product remains Icelandic-first with `is_IS.UTF-8`, Icelandic keyboard defaults, and `Atlantic/Reykjavik` as its documented defaults. Review the values displayed by the installer before confirming.

## Online and offline truth

**Online is the normal 0.2 mode.** Before the wipe, the backend refreshes signed official Arch package databases, resolves every selected and implicit package, and probes the actual source URL. It repeats source and target readiness immediately before handing the plan to archinstall. Because BifrOSt keeps Arch's rolling repositories, package versions can differ between installations and a source checkout is not a bit-for-bit reproducible package set.

**Offline is offered only when the medium contains the signed schema-v2 manifest, trusted keyring, local repository, and every package required by the selected and implicit profiles below `/usr/share/bifrost/offline/`.** The backend verifies the manifest signature, completeness, and each package SHA-256 before the wipe. The mere presence of cached packages, a bootable ISO, or an “offline” label is not proof of completeness. If the installer does not explicitly report a validated offline source, assume the installation requires network access. The 0.2.0 source tree does not itself make an offline-completeness claim.

## Install and first boot

1. Boot the verified USB in UEFI mode and open **Setja upp BifrOSt / Install BifrOSt**.
2. Select Icelandic or English, connect to the network when using online mode, and wait for source readiness.
3. Rescan disks, select the target, and verify its complete identity as described above.
4. Choose encryption and optional profiles; enter account details and, if enabled, a dedicated LUKS2 passphrase.
5. Review every choice. In the destructive dialog, verify the disk again and type only the exact path displayed there.
6. Wait for a success result. Do not power off while installation is running.
7. On success, shut down, remove the USB, and boot the installed disk. An encrypted installation prompts for its LUKS2 passphrase before the system can start.
8. The BifrOSt welcome appears once per user after the desktop becomes available. It explains the recorded installation choices and writes its completion marker only after the window has appeared and is dismissed.

If installation fails, do not assume the disk was restored. See [Recovery and log collection](recovery.md).

## Known platform limits

- **Secure Boot:** unsupported. BifrOSt 0.2 does not claim a signed Secure Boot chain. Disable Secure Boot to boot or install.
- **NVIDIA:** proprietary NVIDIA drivers are not bundled or configured by the installer. The live session may rely on the kernel's available open driver and may have limited display support on some GPUs. Do not infer proprietary-driver, hybrid-graphics, suspend, or external-display qualification from a successful live boot. Consult current Arch Linux NVIDIA guidance after installation and test before relying on the machine.
- **Hardware support:** only rows marked Pass in the release-specific [hardware qualification matrix](hardware-qualification.md) are qualified. Unlisted hardware is untested, not supported by implication.
