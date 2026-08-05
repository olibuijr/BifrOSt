#!/usr/bin/env bash
# shellcheck disable=SC2034

iso_name="bifrost"
iso_label="BIFROST_$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%y%m)"
iso_publisher="BifrOSt project"
iso_application="BifrOSt Icelandic developer live/install ISO"
iso_version="$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y.%m.%d)"
install_dir="bifrost"
buildmodes=('iso')
bootmodes=('bios.syslinux'
           'uefi.systemd-boot')
pacman_conf="pacman.conf"
airootfs_image_type="squashfs"
airootfs_image_tool_options=('-comp' 'xz' '-Xbcj' 'x86' '-b' '1M' '-Xdict-size' '1M' '-processors' '4')
bootstrap_tarball_compression=('zstd' '-c' '-T0' '--auto-threads=logical' '--long' '-19')
file_permissions=(
  ["/etc/shadow"]="0:0:400"
  ["/etc/sudoers.d/liveuser"]="0:0:440"
  ["/root"]="0:0:750"
  ["/root/.automated_script.sh"]="0:0:755"
  ["/root/.gnupg"]="0:0:700"
  ["/usr/local/bin/choose-mirror"]="0:0:755"
  ["/usr/local/bin/Installation_guide"]="0:0:755"
  ["/usr/local/bin/livecd-sound"]="0:0:755"
  ["/usr/local/bin/bifrost-installer"]="0:0:755"
  ["/usr/local/lib/bifrost-installer-backend"]="0:0:755"
  ["/usr/local/bin/apply-bifrost-live-branding"]="0:0:755"
)
