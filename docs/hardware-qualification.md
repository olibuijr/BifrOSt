# Hardware qualification

This document is the qualification record and per-release template for BifrOSt 0.2. A row is a support claim only when it names a release artifact, exact hardware/firmware, a completed result, and retained evidence. QEMU results do not qualify physical hardware.

**Current source status (`0.2.0`): no physical-hardware rows have been verified in this repository. All rows below remain Not tested until evidence is recorded for a released ISO.**

Secure Boot is outside the 0.2 qualification boundary and must remain disabled. Proprietary NVIDIA, hybrid-graphics switching, and NVIDIA external-display paths are also outside the default qualification boundary unless an explicitly named configuration is added and tested.

## Result vocabulary

- **Pass:** every stated check passed on the exact release artifact and configuration.
- **Fail:** one or more checks failed; link the issue or failure log.
- **Partial:** the completed subset and missing checks are explicit. Partial is not a qualification pass.
- **Not tested:** no evidence. Never infer Pass from a similar model, chipset, kernel, or VM.

## Release summary

| Release | ISO SHA-256 | Firmware mode | Standard install | LUKS2 install | USB-written boot | Evidence | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0.2.0 | Not recorded | UEFI, Secure Boot off | Not tested | Not tested | Not tested | None | Not tested |

## Required physical matrix

Duplicate rows as needed. Record exact identifiers rather than only “Intel laptop” or “AMD desktop.”

| Release | System/vendor model | BIOS/UEFI version | CPU | GPU + driver | Network device + driver | Target bus/model/firmware | Install mode | Result | Evidence/issue |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.2.0 | Not recorded | Not recorded | Intel class | Intel graphics class | Intel Wi-Fi class | NVMe class | Standard | Not tested | None |
| 0.2.0 | Not recorded | Not recorded | AMD class | AMD graphics class | AMD/MediaTek Wi-Fi class | NVMe class | LUKS2 | Not tested | None |
| 0.2.0 | Not recorded | Not recorded | Any | Any | Ethernet class | SATA SSD class | Standard | Not tested | None |
| 0.2.0 | Not recorded | Not recorded | Any | Any | Broadcom Wi-Fi class | Internal disk | Standard | Not tested | None |
| 0.2.0 | Not recorded | Not recorded | Any | Any | Any | USB-attached target class | Standard | Not tested | None |
| 0.2.0 | Not recorded | Not recorded | Any | NVIDIA, open driver | Any | Internal disk | Standard | Not tested | None |

The Broadcom and NVIDIA rows are deliberate caveat probes, not support claims. A failure or Not tested result must remain visible in release notes.

## Per-machine checklist

Record Pass/Fail/Not tested and an evidence reference for each item:

| Check | Result | Evidence/notes |
| --- | --- | --- |
| Signed checksum verified, or release explicitly recorded as unsigned | Not tested | |
| ISO written to USB and write completed without error | Not tested | |
| USB boots in UEFI mode with Secure Boot off | Not tested | |
| Live graphics reaches a usable COSMIC session | Not tested | |
| Internal display resolution and brightness controls | Not tested | |
| Keyboard, touchpad/mouse, and Icelandic layout | Not tested | |
| Wired networking, if present | Not tested | |
| Wi-Fi discovery, association, DNS, and reconnect | Not tested | |
| Audio output/input and volume controls | Not tested | |
| Installer shows correct target model, serial/WWN, size, and sector size | Not tested | |
| Live-media, mounted, swap, and non-target disks are refused | Not tested | |
| Online source readiness completes before destructive confirmation | Not tested | |
| Standard whole-disk install completes | Not tested | |
| LUKS2 install completes and cold-boot unlock succeeds | Not tested | |
| Wrong LUKS2 passphrase is rejected without bypass | Not tested | |
| Installed UEFI entry and systemd-boot start correctly without USB | Not tested | |
| First-boot welcome appears once and reflects recorded choices | Not tested | |
| Selected optional profiles are present; unselected profiles are absent | Not tested | |
| Suspend/resume, shutdown, and cold boot | Not tested | |
| Post-install `pacman -Syu`, reboot, and second boot | Not tested | |
| Installer logs persist and contain no observed plaintext secret | Not tested | |
| Recovery/log collection instructions work from live media | Not tested | |

## Evidence record template

```text
Release: 0.2.0
ISO filename:
ISO SHA-256:
Checksum signature: verified / unsigned / not checked
Release-key primary fingerprint (if verified):
Test date (UTC):
Tester:
System vendor/model:
Mainboard:
UEFI version/settings (Secure Boot must be off):
CPU:
GPU(s), kernel driver(s), display topology:
Network controller(s), kernel driver(s):
Audio controller/codec:
Target model, serial/WWN (redact public copy if needed), capacity, bus, firmware:
Installer language:
Source mode and source identity:
Install mode: standard / LUKS2
Profiles:
Result: Pass / Fail / Partial
Failed or omitted checks:
Evidence paths/URLs:
Known issues:
```

Store serial logs, installer run logs, `journalctl -b`, `lsblk -O`, `lspci -nnk`, firmware version, and screenshots only after reviewing them for secrets and personal identifiers. A release summary should link immutable evidence; an unverifiable prose assertion is not qualification.
