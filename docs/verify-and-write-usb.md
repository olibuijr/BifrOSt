# Verify a release and write USB media

> **AÐVÖRUN / WARNING:** Writing an image erases the entire destination drive. Never paste a generic device such as `/dev/sdX` into an imaging command. Identify the physical USB by model, serial/WWN, capacity, and transport immediately before writing.

## Release artifact status

BifrOSt 0.2.0 release metadata uses these exact names for the standard ISO `bifrost-0.2.0-x86_64.iso`:

- Unsigned build: `bifrost-0.2.0-x86_64.iso.sha256.unsigned`. This can detect an accidental download error, but it does **not** authenticate the publisher.
- Signed build: `bifrost-0.2.0-x86_64.iso.sha256` plus detached armored signature `bifrost-0.2.0-x86_64.iso.sha256.asc`.
- Inspection metadata: `bifrost-0.2.0-x86_64.packages.json` and `bifrost-0.2.0-x86_64.build.json`. Metadata records the full source revision and build epoch, but is not proof of reproducibility.

Do not rename an `.unsigned` file or describe it as signed. BifrOSt does not claim a signed release unless the detached signature verifies against a release key whose full fingerprint you obtained through an independent trusted channel.

## Verify a signed checksum

Download the ISO, its `.sha256` file, and its `.sha256.asc` signature into a new directory. Obtain the release public key and expected full fingerprint independently of those three files—for example from an established project channel you already trust. A key downloaded only beside a compromised ISO provides no independent authentication.

Inspect the key without importing it into your normal keyring:

```bash
gpg --show-keys --with-fingerprint --with-subkey-fingerprint ./bifrost-release-key.asc
```

Compare the complete primary-key fingerprint character for character with the independently published fingerprint. If no trusted fingerprint is published, stop: authenticity has not been established.

Create a temporary verification keyring and verify the detached signature:

```bash
ISO_NAME='bifrost-0.2.0-x86_64.iso'
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

For an explicitly unsigned 0.2.0, the limited integrity-only check is:

```bash
ISO_NAME='bifrost-0.2.0-x86_64.iso'
sha256sum --check "./${ISO_NAME}.sha256.unsigned"
```

Treat that result as unauthenticated.

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
