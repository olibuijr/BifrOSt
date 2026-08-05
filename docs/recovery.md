# Endurheimt og annálar / Recovery and logs

> **AÐVÖRUN / WARNING:** A failed install may already have erased or repartitioned the target. BifrOSt does not provide transactional rollback. Do not write more data to a disk from which you may need professional data recovery.

## What the installer retains

Each run has a directory below:

```text
/var/log/bifrost-installer/<run-id>/
```

The run directory contains `plan.json`, a redacted `archinstall.json`, `backend.log`, `status.json`, and—after apply begins—an available redacted copy of `archinstall.log`. Secret input is separate and must not be present in retained logs. The installer reports the exact live log path and any available target log path; use the paths it displays rather than guessing.

These records help diagnose or resume manual recovery; they do not restore the old partition table or files.

## Collect logs from the live environment

Keep the installer failure page open and note its run ID and log path. Attach a separate, trusted USB storage device. Identify it by stable facts before mounting:

```bash
lsblk -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,FSTYPE,MOUNTPOINTS
```

Use the actual partition path shown for the separate log USB—not the failed installation target—and mount it through the desktop file manager. Then copy the reported run directory. The placeholder below is intentionally not a device name:

```bash
RUN_ID='<run-id-shown-by-installer>'
DEST="/run/media/$USER/<mounted-log-usb-name>"
sudo cp -a -- "/var/log/bifrost-installer/$RUN_ID" "$DEST/"
sync
```

Replace both angle-bracket placeholders from the installer and mounted-volume display. Confirm that `DEST` is the separate USB before running the copy. Do not publish logs until you have reviewed them for hostnames, usernames, disk serials, network details, or other personal information.

If the graphical session is unavailable, the same run directory can be inspected from a console:

```bash
sudo ls -la /var/log/bifrost-installer
sudo journalctl -b --no-pager > /tmp/bifrost-live-journal.txt
```

The journal can contain private system information. Review it before sharing.

## After a failed installation

1. Read the failure phase and recovery actions shown by the installer.
2. Save the run logs before rebooting; the live environment is normally temporary.
3. If failure occurred before the wipe, rescan and correct the reported readiness issue.
4. If failure occurred during or after partitioning, assume the target contains a partial system. Do not report it as installed and do not expect rollback.
5. Correct the underlying cause, rerun source readiness, re-verify the complete disk identity, and start a new whole-disk installation only if erasing the partial target is acceptable.

For data that existed before the attempted installation, stop using the disk. Reinstalling, formatting, filesystem repair, or opening LUKS mappings can overwrite recoverable evidence.

## Encrypted-system recovery facts

- BifrOSt cannot recover a lost LUKS2 passphrase.
- The installer does not claim to create an escrowed key or automatic recovery key.
- Keep any passphrase record separate from the computer and installation USB.
- Before changing LUKS key slots after installation, make a tested backup and follow current `cryptsetup` documentation. A key-slot mistake can make the data permanently inaccessible.

## Installed-system diagnostics

After a successful boot, the read-only `bifrost-recovery-info` helper summarizes installed recovery and log locations without changing the system. The first-boot welcome also points to the persistent journal and installer record. Useful read-only checks include:

```bash
sudo bifrost-recovery-info
sudo journalctl -b --no-pager
sudo ls -la /var/log/bifrost-installer
```

A Btrfs snapshot, when present, is not by itself a verified rollback mechanism. BifrOSt 0.2 makes no rollback claim.
