# Hardware qualification

This document is the qualification record and per-release template for BifrOSt 0.2. A row is a support claim only when it names a release artifact, exact hardware/firmware, a completed result, and retained evidence. QEMU results do not qualify physical hardware.

**Current source status (`0.2.2`): 0.2.2 is a patch release delivered only through the signed pacman repository — it produced no ISO, so no physical-hardware qualification is required or claimed for it. No `0.2.1` ISO was ever published, and no `0.2.1` physical-hardware row was tested. The historical user-reported `0.2.0` run remains recorded as Partial below; no full physical-hardware Pass is claimed without the exact tested ISO digest and completed checklist.**

Secure Boot is outside the 0.2 qualification boundary and must remain disabled. Proprietary NVIDIA, hybrid-graphics switching, and NVIDIA external-display paths are also outside the default qualification boundary unless an explicitly named configuration is added and tested.

## Result vocabulary

- **Pass:** every stated check passed on the exact release artifact and configuration.
- **Fail:** one or more checks failed; link the issue or failure log.
- **Partial:** the completed subset and missing checks are explicit. Partial is not a qualification pass.
- **Not tested:** no evidence. Never infer Pass from a similar model, chipset, kernel, or VM.

## Release summary

| Release | ISO SHA-256 | Firmware mode | Standard install | LUKS2 install | USB-written boot | Evidence | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0.2.2 | No ISO produced (patch release; pacman delivery only) | Not applicable | Not applicable | Not applicable | Not applicable | Local QEMU release-pipeline evidence only; QEMU does not qualify hardware | Not required |
| 0.2.1 | No ISO published | UEFI, Secure Boot off | Not tested | Not tested | Not tested | None | Not tested |
| 0.2.0 | Not retained for tested media | UEFI, Secure Boot off | Partial | Not tested | Partial | User report and locally inspected system identity | Partial |

## Reported 0.2.0 physical run

On 2026-08-05, the user reported that BifrOSt worked on the current PC and USB key and designated that tested build as `0.2.0` for release. Direct inspection found the earlier `0.2.0-rc1` label in the running installation (`/etc/os-release`) and USB volume (`BIFROST_020RC1_20260805`). Because the tested ISO SHA-256 and detailed checklist were not retained, this is a Partial `0.2.0` observation rather than a full qualification Pass.

| Artifact | System/vendor model | BIOS/UEFI | CPU | GPU + driver | Network device + driver | Installed target | Install mode | Observation | Qualification |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0.2.0` designation; tested media carried the earlier `0.2.0-rc1` label; ISO SHA-256 not retained | Lenovo ThinkPad T14 Gen 4 (`21HES0BG00`) | `N3QET48W` 1.48; UEFI; Secure Boot disabled | 13th Gen Intel Core i5-1345U | Intel Iris Xe (`8086:a7a1`), `i915` | Intel CNVi Wi-Fi (`8086:51f1`), `iwlwifi`; Intel I219-LM Ethernet (`8086:0dc5`), `e1000e` | WD PC SN740 512 GB NVMe; standard Btrfs installation | Standard | User reports the PC installation and USB key work; local hardware and release identity inspected 2026-08-05; detailed checklist and immutable logs not retained | Partial |

## Required physical matrix

Duplicate rows as needed. Record exact identifiers rather than only “Intel laptop” or “AMD desktop.”

| Release | System/vendor model | BIOS/UEFI version | CPU | GPU + driver | Network device + driver | Target bus/model/firmware | Install mode | Result | Evidence/issue |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.2.0 | Lenovo ThinkPad T14 Gen 4 (`21HES0BG00`) | `N3QET48W` 1.48; UEFI, Secure Boot disabled | Intel Core i5-1345U | Intel Iris Xe (`8086:a7a1`), `i915` | Intel CNVi Wi-Fi (`8086:51f1`), `iwlwifi`; Intel I219-LM Ethernet (`8086:0dc5`), `e1000e` | NVMe, WD PC SN740 512 GB | Standard | Partial | User-reported working PC installation and USB; tested-media SHA-256 and detailed checklist not retained |
| 0.2.1 | Lenovo ThinkPad T14 Gen 4 (`21HES0BG00`) | `N3QET48W` 1.48; UEFI, Secure Boot disabled | Intel Core i5-1345U | Intel Iris Xe (`8086:a7a1`), `i915` | Intel CNVi Wi-Fi (`8086:51f1`), `iwlwifi`; Intel I219-LM Ethernet (`8086:0dc5`), `e1000e` | NVMe, WD PC SN740 512 GB | Standard | Not tested | No `0.2.1` ISO was published or physically installed |
| 0.3.0 (next ISO) | Not recorded | Not recorded | AMD class | AMD graphics class | AMD/MediaTek Wi-Fi class | NVMe class | LUKS2 | Not tested | None |
| 0.3.0 (next ISO) | Not recorded | Not recorded | Any | Any | Ethernet class | SATA SSD class | Standard | Not tested | None |
| 0.3.0 (next ISO) | Not recorded | Not recorded | Any | Any | Broadcom Wi-Fi class | Internal disk | Standard | Not tested | None |
| 0.3.0 (next ISO) | Not recorded | Not recorded | Any | Any | Any | USB-attached target class | Standard | Not tested | None |
| 0.3.0 (next ISO) | Not recorded | Not recorded | Any | NVIDIA, open driver | Any | Internal disk | Standard | Not tested | None |

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
Release:
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
