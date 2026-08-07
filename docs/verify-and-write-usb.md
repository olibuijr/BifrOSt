# Verify a release and write USB media

> **AÐVÖRUN / WARNING:** Writing an image erases the entire destination drive. Never paste a generic device such as `/dev/sdX` into an imaging command. Identify the physical USB by model, serial/WWN, capacity, and transport immediately before writing.

## Release artifact status

The `main` branch is at released BifrOSt 0.2.2, a patch release delivered only through the signed `[bifrost]` pacman repository; no `0.2.2` ISO exists. The latest published GitHub release with an installation ISO is `v0.2.0`; no `v0.2.1` release was ever published, and installation ISOs are produced only for minor releases (`0.X.0`), so the next ISO will be `0.3.0`. Always use the guide from the exact release tag whose artifact you are verifying. The following filenames describe locally generated 0.2.2 candidates only; their presence does not mean a `0.2.2` ISO has been published.

BifrOSt 0.2.2 tooling uses these exact names for the standard ISO `bifrost-0.2.2-x86_64.iso`:

- Unsigned build: `bifrost-0.2.2-x86_64.iso.sha256.unsigned`. This can detect an accidental download error, but it does **not** authenticate the publisher.
- Signed build: `bifrost-0.2.2-x86_64.iso.sha256` plus detached armored signature `bifrost-0.2.2-x86_64.iso.sha256.asc`.
- Inspection metadata: `bifrost-0.2.2-x86_64.packages.json` and `bifrost-0.2.2-x86_64.build.json`. Metadata records the full source revision and build epoch, but is not proof of reproducibility.

Do not rename an `.unsigned` file or describe it as signed. BifrOSt does not claim a signed release unless the detached signature verifies against a release key whose full fingerprint you obtained through an independent trusted channel.

## Verify a signed checksum

Download the ISO, its `.sha256` file, and its `.sha256.asc` signature into a new directory. Obtain [`keys/bifrost-release-key.asc`](../keys/bifrost-release-key.asc) through an established project channel you already trust and independently confirm its full primary fingerprint: `B2E0 9853 D23E 5DB6 21C6 123B FC13 D6D6 3D06 E8D2`. A key downloaded only beside a compromised ISO provides no independent authentication. Signing keys were rotated on 2026-08-07; artifacts and tags released before the rotation (`v0.2.0`, `v0.2.1`) verify only against the previous key preserved at the matching tag, so verify them with the guide and key from that tag.

Inspect the key without importing it into your normal keyring:

```bash
gpg --show-keys --with-fingerprint --with-subkey-fingerprint ./bifrost-release-key.asc
```

Compare the complete primary-key fingerprint character for character with `B2E0 9853 D23E 5DB6 21C6 123B FC13 D6D6 3D06 E8D2` and with the fingerprint published through an independent trusted channel. If they differ, stop: authenticity has not been established.

Create a temporary verification keyring and verify the detached signature:

```bash
ISO_NAME='bifrost-0.2.2-x86_64.iso'
VERIFY_HOME="$(mktemp -d)"
chmod 700 "$VERIFY_HOME"
gpg --homedir "$VERIFY_HOME" --import ./bifrost-release-key.asc
gpgv --keyring "$VERIFY_HOME/pubring.kbx" \
  "./${ISO_NAME}.sha256.asc" \
  "./${ISO_NAME}.sha256"
```

Proceed only when `gpgv` reports a good signature from the expected full fingerprint. Then verify the ISO bytes from the directory containing the downloads:

```bash
sha256sum --check "./${ISO_NAME}.sha256"
rm -rf -- "$VERIFY_HOME"
```

If a later release uses a different exact ISO basename, change only `ISO_NAME`. The check must report `OK` for the ISO. A valid digest without a valid trusted signature proves integrity against the checksum file, not publisher identity.

For an explicitly unsigned 0.2.2 candidate, the limited integrity-only check is:

```bash
ISO_NAME='bifrost-0.2.2-x86_64.iso'
sha256sum --check "./${ISO_NAME}.sha256.unsigned"
```

Treat that result as unauthenticated.

## Installed system package trust

The 0.2.2 installer registers the tracked installed-system payload as the `bifrost-system` package. Before changing the target, it verifies the staged package and manifest against a pinned full ALPM signing-key fingerprint; after installation, `pacman -Qo` reports BifrOSt executables, desktop files, autostart entries, recovery text, and maintenance components as owned by `bifrost-system`. The installed `[bifrost]` repository is limited to that package, follows the official Arch repositories, requires both package and repository-database signatures, and uses only `https://olibuijr.github.io/BifrOSt/alpm/$arch`.

The ALPM public key embedded in an ISO is authentic only to the extent that the ISO itself was authenticated. A correctly verified ISO signature ties that pinned key and bootstrap package to the signed release; an unsigned ISO does not establish publisher identity merely because its internally consistent package signature verifies.

Apply Arch and BifrOSt system updates only as one complete transaction:

```bash
sudo pacman -Syu
```

Never refresh only the sync databases with `pacman -Sy`, and do not perform partial upgrades. BifrOSt does not auto-apply system maintenance, claim Btrfs or operating-system rollback, or support Secure Boot.

The current release, 0.2.2, ships through that repository as a patch release with no new ISO. Systems installed from 0.2.0 or 0.2.1 media trust only the pre-rotation ALPM key and require a one-time key adoption before the first upgrade (see `STATUS.md` in the repository root):

```bash
curl -fsSLo /tmp/bifrost-key.asc \
  https://olibuijr.github.io/BifrOSt/alpm/x86_64/alpm-repository-key.asc
sudo pacman-key --add /tmp/bifrost-key.asc
sudo pacman-key --lsign-key F5CE992078EA20EA8469A05FC68D23E4208D553F
sudo pacman -Syu
```

Confirm that ALPM fingerprint through an independent trusted channel before locally signing the key. Fresh 0.3.0+ media will carry and locally sign the new key automatically through the installer bootstrap.

## Write the USB

A graphical image writer that displays model and capacity—KDE ISO Image Writer, GNOME Disks, or Rufus—is preferred. Select the verified ISO, then compare the destination's model, capacity, and serial with the physical USB before approving the destructive operation.

For a command-line write on Linux, first inspect whole disks:

```bash
lsblk -d -o NAME,PATH,SIZE,MODEL,SERIAL,WWN,TRAN,RM,RO
```

Locate the USB with `TRAN=usb` and match its model, serial/WWN, and capacity. Use its stable `/dev/disk/by-id/` link; never substitute a guessed `/dev/sdX` path:

```bash
ls -l /dev/disk/by-id/
ISO='/absolute/path/to/verified-bifrost.iso'
TARGET='/dev/disk/by-id/<exact-verified-usb-disk-id>'
readlink -f -- "$TARGET"
lsblk -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,RM,RO,MOUNTPOINTS -- "$TARGET"
```

Do not continue unless `TARGET` resolves to the same physical USB and the output shows the expected identity. It must be the whole-disk link, not a `-part1` link. Unmount its mounted child partitions through the desktop file manager. Re-run the identity command after any unplug/replug.

Only after those checks, write and flush the image:

```bash
sudo dd if="$ISO" of="$TARGET" bs=4M status=progress conv=fsync
sync
```

`dd` does not ask for confirmation and a wrong `TARGET` destroys that disk. Eject the USB after the command completes. Boot it in UEFI mode with Secure Boot disabled.
