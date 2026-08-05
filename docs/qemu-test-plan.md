# Exact QEMU installation test plan

This is a manual release-candidate plan, not a claim that a given artifact passed. Record the ISO SHA-256, QEMU version, OVMF package version, installer run ID, logs, and every deviation. Both cases use UEFI with Secure Boot disabled because that is the supported 0.2 boundary.

## Host preparation

On Arch Linux, install official packages:

```bash
sudo pacman -S --needed qemu-desktop edk2-ovmf
```

Set the ISO to one exact, already verified artifact. Do not use a wildcard that could select a different build:

```bash
ISO='/absolute/path/to/bifrost-0.2.0-x86_64.iso'
WORKDIR="$HOME/.local/state/bifrost-qemu-0.2.0"
test -f "$ISO"
install -d -m 700 -- "$WORKDIR"
sha256sum -- "$ISO" | tee "$WORKDIR/iso.sha256"
qemu-system-x86_64 --version | tee "$WORKDIR/qemu.version"
pacman -Q edk2-ovmf | tee "$WORKDIR/ovmf.version"
```

If the release uses a different exact ISO basename, change only `ISO`. Compare `iso.sha256` with the previously authenticated checksum before continuing.

The commands below use Arch's 4 MiB OVMF images at `/usr/share/edk2/x64/OVMF_CODE.4m.fd` and `/usr/share/edk2/x64/OVMF_VARS.4m.fd`, KVM, 8 GiB RAM, four vCPUs, a 64 GiB blank VirtIO disk, user-mode networking, and a GTK display. If KVM is unavailable, record the substitution rather than silently changing the test environment.

## Case A: standard unencrypted installation

Create a fresh disk and independent writable firmware state:

```bash
cp -- /usr/share/edk2/x64/OVMF_VARS.4m.fd "$WORKDIR/standard_VARS.fd"
qemu-img create -f qcow2 "$WORKDIR/standard.qcow2" 64G
```

Boot the ISO:

```bash
qemu-system-x86_64 \
  -name bifrost-0.2-standard \
  -enable-kvm -machine q35,accel=kvm -cpu host \
  -smp 4 -m 8192 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/x64/OVMF_CODE.4m.fd \
  -drive if=pflash,format=raw,file="$WORKDIR/standard_VARS.fd" \
  -drive id=system,if=none,format=qcow2,file="$WORKDIR/standard.qcow2" \
  -device virtio-blk-pci,drive=system,serial=BIFROSTSTD001 \
  -cdrom "$ISO" \
  -device virtio-vga \
  -display gtk \
  -nic user,model=virtio-net-pci \
  -serial file:"$WORKDIR/standard-install.serial.log" \
  -boot menu=on
```

Perform these exact UI actions:

1. Boot **BifrOSt live/install medium** and open **Setja upp BifrOSt / Install BifrOSt**.
2. Select English for this case. Confirm the source readiness page identifies **online** mode and becomes ready before any destructive confirmation is available. Do not accept an offline claim.
3. Rescan. Select only the 64 GiB VirtIO disk with serial `BIFROSTSTD001`. Record its displayed path, model, serial, WWN state, byte size, and logical-sector size.
4. Leave **Encrypt disk (LUKS2)** off. Keep only mandatory `base`; leave every optional development profile unselected.
5. Enter a disposable VM-only user and hostname. Never reuse a real password.
6. On review, confirm mode is whole disk, encryption is off, `base` is selected, and the source/language/defaults match the earlier pages.
7. Open the final styled confirmation. Verify model, 64 GiB size, serial `BIFROSTSTD001`, and current path. Type the exact path displayed by the dialog. Confirm the wipe.
8. Wait for a terminal success event. Record the run ID and live/target log paths. Shut down from the live environment; do not merely reset the VM.

Cold-boot the installed disk with the ISO detached:

```bash
qemu-system-x86_64 \
  -name bifrost-0.2-standard-installed \
  -enable-kvm -machine q35,accel=kvm -cpu host \
  -smp 4 -m 8192 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/x64/OVMF_CODE.4m.fd \
  -drive if=pflash,format=raw,file="$WORKDIR/standard_VARS.fd" \
  -drive id=system,if=none,format=qcow2,file="$WORKDIR/standard.qcow2" \
  -device virtio-blk-pci,drive=system,serial=BIFROSTSTD001 \
  -device virtio-vga \
  -display gtk \
  -nic user,model=virtio-net-pci \
  -serial file:"$WORKDIR/standard-boot.serial.log"
```

Pass criteria:

- systemd-boot starts from the virtual disk without the ISO;
- no LUKS unlock prompt appears;
- login reaches a usable COSMIC session;
- Icelandic defaults are present and English critical installer text was usable;
- the welcome appears once, reports `base` and unencrypted storage from recorded state, and does not reappear after explicit dismissal plus logout/login;
- networking works and a repository refresh can be attempted without changing the test result into an update qualification claim;
- the following read-only checks show a FAT EFI System Partition, Btrfs root/subvolumes, persistent installer logs, and no selected optional-profile record:

```bash
findmnt /
findmnt /boot
lsblk -o NAME,PATH,TYPE,FSTYPE,SIZE,MODEL,SERIAL,WWN,MOUNTPOINTS
sudo btrfs subvolume list /
sudo bootctl status
cat /etc/bifrost/install-state.json
sudo bifrost-recovery-info
sudo find /var/log/bifrost-installer -maxdepth 2 -type f -printf '%m %u:%g %p\n'
```

Review retained intent/config/output manually and fail the case if a plaintext login password is present.

## Case B: encrypted LUKS2 installation

Create a separate blank disk and firmware state; never reuse Case A:

```bash
cp -- /usr/share/edk2/x64/OVMF_VARS.4m.fd "$WORKDIR/encrypted_VARS.fd"
qemu-img create -f qcow2 "$WORKDIR/encrypted.qcow2" 64G
```

Boot the ISO:

```bash
qemu-system-x86_64 \
  -name bifrost-0.2-encrypted \
  -enable-kvm -machine q35,accel=kvm -cpu host \
  -smp 4 -m 8192 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/x64/OVMF_CODE.4m.fd \
  -drive if=pflash,format=raw,file="$WORKDIR/encrypted_VARS.fd" \
  -drive id=system,if=none,format=qcow2,file="$WORKDIR/encrypted.qcow2" \
  -device virtio-blk-pci,drive=system,serial=BIFROSTLUKS001 \
  -cdrom "$ISO" \
  -device virtio-vga \
  -display gtk \
  -nic user,model=virtio-net-pci \
  -serial file:"$WORKDIR/encrypted-install.serial.log" \
  -boot menu=on
```

Perform these exact UI actions:

1. Boot the live medium, open the installer, and select Icelandic for this case.
2. Confirm online source readiness completes before destructive confirmation.
3. Rescan and select only the 64 GiB VirtIO disk with serial `BIFROSTLUKS001`; record all displayed identity and geometry facts.
4. Enable **Encrypt disk (LUKS2)**. Enter and confirm a unique disposable VM-only passphrase that differs from the disposable login password. Do not put either secret in notes or shell history.
5. Select `base` plus `dev-rust`; leave `dev-containers`, `dev-web`, and `dev-python` off.
6. Verify the review identifies LUKS2 and exactly those profiles. In the final dialog, recheck serial `BIFROSTLUKS001`, model, 64 GiB size, and path; type the exact displayed path and confirm.
7. Wait for success, record run/log paths, and shut down cleanly.

Cold-boot with the ISO detached:

```bash
qemu-system-x86_64 \
  -name bifrost-0.2-encrypted-installed \
  -enable-kvm -machine q35,accel=kvm -cpu host \
  -smp 4 -m 8192 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/x64/OVMF_CODE.4m.fd \
  -drive if=pflash,format=raw,file="$WORKDIR/encrypted_VARS.fd" \
  -drive id=system,if=none,format=qcow2,file="$WORKDIR/encrypted.qcow2" \
  -device virtio-blk-pci,drive=system,serial=BIFROSTLUKS001 \
  -device virtio-vga \
  -display gtk \
  -nic user,model=virtio-net-pci \
  -serial file:"$WORKDIR/encrypted-boot.serial.log"
```

At the LUKS prompt, enter one intentionally wrong value and confirm it is rejected; then enter the correct disposable passphrase. Pass criteria:

- unlock is required on every cold boot and a wrong passphrase never bypasses it;
- correct unlock reaches systemd-boot/system startup and a usable COSMIC login;
- first-boot welcome reports encrypted storage and exactly `base` plus `dev-rust`, then remains dismissed after logout/login;
- every `dev-rust` manifest package is installed and the three unselected profile IDs are absent from recorded state; installing a Rust toolchain through `rustup` is a separate post-install action;
- the EFI System Partition is FAT and unencrypted, while the root partition reports `crypto_LUKS` with Btrfs inside the active mapping;
- retained logs contain no plaintext login password or LUKS passphrase.

Run the read-only checks:

```bash
findmnt /
findmnt /boot
lsblk -o NAME,PATH,TYPE,FSTYPE,SIZE,MODEL,SERIAL,WWN,MOUNTPOINTS
CRYPT_PART="$(lsblk -rpo PATH,FSTYPE | awk '$2 == "crypto_LUKS" { print $1; exit }')"
test -n "$CRYPT_PART"
sudo cryptsetup luksDump "$CRYPT_PART"
sudo btrfs subvolume list /
sudo bootctl status
cat /etc/bifrost/install-state.json
pacman -Q base-devel clang cmake git just lld ninja rust-analyzer rustup
sudo bifrost-recovery-info
sudo find /var/log/bifrost-installer -maxdepth 2 -type f -printf '%m %u:%g %p\n'
```

`cryptsetup luksDump` must report LUKS2. Review retained files manually for secret leakage without copying the secret into a search command.

## Required result record

For each case retain:

- exact ISO basename and SHA-256;
- host QEMU/OVMF versions and launch command;
- disk serial, source mode/identity, chosen language, profiles, and encryption mode;
- installer run ID, event/output log, and reported live/target log paths;
- screenshots of final review, success, first boot, and relevant storage checks;
- Pass/Fail for every criterion above and links to any failure issue.

Do not call 0.2 qualified from only one mode, a boot-only check, an install that still has the ISO attached, or a run whose evidence was not retained.
