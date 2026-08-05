<p align="center">
  <img src="profile/airootfs/usr/share/bifrost/branding/bifrost-mark.svg" width="150" alt="BifrOSt geometric bridge mark">
</p>

<h1 align="center">BifrOSt</h1>

<p align="center"><strong>Íslenskt · frjálst · byggt á Arch Linux</strong></p>

BifrOSt is an independent Icelandic developer workstation and live/install ISO based on Arch Linux. It keeps Arch's official kernel, package manager, repositories, and rolling-release model, then adds Icelandic defaults, the COSMIC desktop, a Rust-focused toolset, and original visual identity.

![BifrOSt aurora wallpaper](profile/airootfs/usr/share/backgrounds/bifrost/bifrost-aurora.png)

## Download

Download the current x86_64 ISO and its checksum from the
[GitHub releases page](https://github.com/olibuijr/BifrOSt/releases/latest).

The image supports BIOS and UEFI systems. Verify the published SHA-256 digest,
then write it with a trusted image writer such as KDE ISO Image Writer, GNOME
Disks, or Rufus. Back up the destination drive first; imaging erases it.

## What ships

- Icelandic locale (`is_IS.UTF-8`), `is-latin1` console keymap, Icelandic-first XKB layout, and `Atlantic/Reykjavik` timezone
- COSMIC desktop with the original BifrOSt aurora, glacier, basalt, and longship artwork
- Quiet Plymouth startup with a BifrOSt mark, smoothly animated progress bar, and retained splash during the COSMIC handoff
- A native GTK/libadwaita graphical installer with a focused four-step, Icelandic-first workflow
- Icelandic Firefox language pack with English available as a fallback
- Automatic Btrfs layout, Linux and Linux LTS kernels, NetworkManager, PipeWire, COSMIC, and systemd-boot
- Rust developer setup: `rustup`, `rust-analyzer`, Clang, LLD, CMake, Ninja, Just, Git, and `base-devel`
- Podman and Distrobox for isolated development environments
- Official Arch `scx-scheds` and `scx-tools`; sched-ext remains opt-in
- Snapper plus `snap-pac` for package-transaction snapshots on supported Btrfs layouts

BifrOSt uses official Arch repositories only. It does not replace pacman, ship a custom kernel, or enable the AUR automatically.

## Icelandic desktop translations

BifrOSt drives the COSMIC desktop toward complete Icelandic coverage. The
project completed the Icelandic (`is_IS`) localization of the core COSMIC
components to full parity with English and contributed the work upstream to
System76, so every COSMIC user benefits — not just BifrOSt. Terminology is
grounded in the [Íðorðabankinn](https://idordabanki.arnastofnun.is/) TOLVA
computing dictionary and kept consistent with COSMIC's existing Icelandic
strings.

Upstream pull requests (each bringing a component to 100% key parity):

| Component | Pull request |
| --- | --- |
| cosmic-settings | [#2136](https://github.com/pop-os/cosmic-settings/pull/2136) |
| cosmic-applets | [#1517](https://github.com/pop-os/cosmic-applets/pull/1517) |
| cosmic-term | [#897](https://github.com/pop-os/cosmic-term/pull/897) |
| cosmic-files | [#1958](https://github.com/pop-os/cosmic-files/pull/1958) |
| cosmic-player | [#319](https://github.com/pop-os/cosmic-player/pull/319) |
| cosmic-initial-setup | [#147](https://github.com/pop-os/cosmic-initial-setup/pull/147) |
| cosmic-store | [#587](https://github.com/pop-os/cosmic-store/pull/587) |
| cosmic-greeter | [#508](https://github.com/pop-os/cosmic-greeter/pull/508) |
| cosmic-edit | [#605](https://github.com/pop-os/cosmic-edit/pull/605) |
| cosmic-osd | [#214](https://github.com/pop-os/cosmic-osd/pull/214) |

Because COSMIC embeds translations into its binaries at build time, these
strings appear in BifrOSt automatically once the pull requests merge and the
official Arch `cosmic` packages are rebuilt — no profile changes required.

## Screenshots

### Graphical installer

![BifrOSt graphical installer](screenshots/bifrost-installer.png)

### Installed COSMIC first run

![Installed BifrOSt COSMIC desktop](screenshots/bifrost-installed-cosmic.png)


## Build on Arch Linux

Install the official build and VM tools:

```bash
sudo pacman -S --needed archiso qemu-desktop edk2-ovmf
```

Build from the repository root:

```bash
sudo mkarchiso -v -r \
  -w work \
  -o out \
  profile
```

The result is `out/bifrost-YYYY.MM.DD-x86_64.iso`.

Boot it in a UEFI QEMU VM:

```bash
run_archiso -i out/bifrost-*.iso
```

## Install

1. Boot the ISO and choose **BifrOSt live/install medium**.
2. COSMIC starts automatically as the passwordless live user.
3. Connect to the network.
4. Open the application launcher and choose **Setja upp BifrOSt** / **Install BifrOSt**.
5. Choose the target disk, create your administrator account, and confirm the destructive disk operation in the graphical installer.
6. When the completion screen appears, remove the ISO and reboot.

The installer downloads current packages from official Arch mirrors. No default password is embedded in the installed system.

## Screenshots

### Live desktop

![BifrOSt live COSMIC desktop](screenshots/bifrost-live-desktop.png)

### Graphical installer

![BifrOSt graphical installer welcome screen](screenshots/bifrost-installer-welcome.png)

### Installed system

![Installed BifrOSt COSMIC desktop](screenshots/bifrost-installed-cosmic.png)

## Verification checks

This release was booted in accelerated QEMU, installed through the graphical workflow onto a blank 24 GiB virtual disk, and booted from that disk without the ISO. The installed COSMIC session, BifrOSt identity, Icelandic defaults, Btrfs layout, and branded desktop were verified.

## Identity and attribution

The BifrOSt mark, boot art, and wallpaper are original vector-generated project artwork. The longship is shown without the ahistorical horned-helmet motif; the bridge palette refers to Bifröst and Iceland's aurora, ice, sea, and volcanic basalt.

BifrOSt is an independent project, not an official Arch Linux or System76 product. Arch Linux and its marks belong to their respective owners. COSMIC is developed by System76.

## License

Project-authored configuration, scripts, and artwork are released under the [MIT License](LICENSE). Upstream ArchISO files and installed software retain their own licenses.
