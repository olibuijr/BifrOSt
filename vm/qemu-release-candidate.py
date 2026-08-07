#!/usr/bin/env python3
"""Run destructive installer qualification only against harness-created QEMU disks."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT = Path.home() / ".local/state/bifrost/qemu-rc"
OVMF_CODE = Path("/usr/share/edk2/x64/OVMF_CODE.4m.fd")
OVMF_VARS = Path("/usr/share/edk2/x64/OVMF_VARS.4m.fd")
DISK_SIZE = "64G"
USER_PASSWORD = "Bifr0st-RC-user-only"
LUKS_PASSWORD = "Bifr0st-RC-luks-only"
WRONG_LUKS_PASSWORD = "Bifr0st-RC-wrong-only"

CASES = {
    "standard": {
        "serial": "BIFROST-RC-STANDARD",
        "encrypted": False,
        "profiles": ["base"],
        "language": "en",
        "locale": "en_US.UTF-8",
        "keymap": "us",
    },
    "luks2": {
        "serial": "BIFROST-RC-LUKS2",
        "encrypted": True,
        "profiles": ["base", "dev-rust"],
        "language": "is",
        "locale": "is_IS.UTF-8",
        "keymap": "is-latin1",
    },
}


class QualificationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_output(arguments: list[str]) -> str:
    return subprocess.run(arguments, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.strip()


def safe_work_dir(requested: Path | None, iso_digest: str) -> Path:
    if requested is None:
        requested = DEFAULT_STATE_ROOT / f"candidate-{iso_digest[:16]}"
    requested = requested.expanduser().absolute()
    if any(character in str(requested) for character in (",", "\n", "\r")):
        raise QualificationError("work directory must not contain commas or line breaks")
    if requested == Path("/") or requested.exists():
        raise QualificationError(f"work directory must not already exist: {requested}")
    parent = requested.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if parent.is_symlink():
        raise QualificationError(f"work directory parent must not be a symlink: {parent}")
    requested.mkdir(mode=0o700)
    return requested


class SerialConsole:
    def __init__(self, path: Path, log_path: Path, process: subprocess.Popen[bytes], timeout: int = 30):
        deadline = time.monotonic() + timeout
        while not path.exists():
            if process.poll() is not None:
                raise QualificationError(f"QEMU exited before creating serial socket (rc={process.returncode})")
            if time.monotonic() >= deadline:
                raise QualificationError(f"QEMU did not create serial socket: {path}")
            time.sleep(0.1)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(str(path))
        self.process = process
        self.log = log_path.open("ab", buffering=0)
        self.buffer = bytearray()

    def close(self) -> None:
        self.socket.close()
        self.log.close()

    def drain(self, timeout: float = 10.0) -> None:
        """After QEMU exits, read buffered serial bytes through EOF so log tails survive."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(deadline - time.monotonic(), 0.0)
            ready, _, _ = select.select([self.socket], [], [], min(1.0, remaining))
            if not ready:
                continue
            try:
                data = self.socket.recv(65536)
            except OSError:
                return
            if not data:
                return
            self.log.write(data)

    def send(self, value: str) -> None:
        self.socket.sendall(value.encode())

    def read(self, seconds: float = 0.5) -> bytes:
        ready, _, _ = select.select([self.socket], [], [], seconds)
        if not ready:
            return b""
        data = self.socket.recv(65536)
        if data:
            self.log.write(data)
            self.buffer.extend(data)
            if len(self.buffer) > 1024 * 1024:
                del self.buffer[: len(self.buffer) - 1024 * 1024]
        return data

    def wait_for(self, needles: list[bytes], timeout: int, label: str) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for needle in needles:
                if needle in self.buffer:
                    return needle
            if self.process.poll() is not None:
                raise QualificationError(
                    f"QEMU exited with rc={self.process.returncode} while waiting for {label}"
                )
            if not self.read(1.0):
                continue
        rendered = ", ".join(repr(item.decode(errors="replace")) for item in needles)
        raise QualificationError(f"timed out waiting for {label}: {rendered}")

    def clear(self) -> None:
        self.buffer.clear()

    def discard_through(self, needle: bytes) -> None:
        end = self.buffer.rfind(needle) + len(needle)
        del self.buffer[:end]


    def send_script(self, script: str) -> None:
        encoded = base64.b64encode(script.encode()).decode()
        self.send(": > /run/bifrost-rc.b64\n")
        for offset in range(0, len(encoded), 2800):
            chunk = encoded[offset : offset + 2800]
            self.send(f"printf %s {shlex.quote(chunk)} >> /run/bifrost-rc.b64\n")
        self.send("base64 -d /run/bifrost-rc.b64 > /run/bifrost-rc.sh && chmod 700 /run/bifrost-rc.sh && /run/bifrost-rc.sh\n")


def assertion_script(case: str, config: dict[str, object], version: str) -> str:
    expected_profiles = json.dumps(config["profiles"], separators=(",", ":"))
    all_profiles = json.dumps([p.stem for p in sorted((ROOT / "profile/airootfs/usr/share/bifrost/installed-root/usr/share/bifrost/profiles").glob("*.json"))], separators=(",", ":"))
    return f'''#!/usr/bin/env bash
set -Eeuo pipefail
exec > >(tee /var/log/bifrost-qemu-rc.log /dev/ttyS0) 2>&1
fail() {{ echo "BIFROST_RC_ASSERTION_FAILED case={case} line=$1"; systemctl poweroff --no-block; exit 1; }}
trap 'fail $LINENO' ERR
python3 - <<'PY'
import hashlib, json
from pathlib import Path
expected_version = {version!r}
expected_profiles = {expected_profiles}
all_profiles = {all_profiles}
release_path = Path('/usr/share/bifrost/release.json')
state_path = Path('/etc/bifrost/install-state.json')
release = json.loads(release_path.read_text())
state = json.loads(state_path.read_text())
assert release['schema_version'] == 1
assert release['version'] == expected_version
assert release['provenance_status'] == 'build-input'
assert isinstance(release.get('build_id'), str) and release['build_id']
assert release.get('source_revision')
assert release.get('profile_sha256')
assert state['schema_version'] == 2
assert state['profiles'] == expected_profiles
assert state['encryption'] is {str(config['encrypted'])}
installed_release = state['release']
for key in ('version', 'source_revision', 'build_id'):
    assert installed_release[key] == release[key]
assert installed_release['provenance_status'] == release['provenance_status']
assert installed_release['path'] == str(release_path)
assert installed_release['sha256'] == hashlib.sha256(release_path.read_bytes()).hexdigest()
assert set(state['profiles']).isdisjoint(set(all_profiles) - set(expected_profiles))
locale = Path('/etc/locale.conf').read_text().splitlines()
assert 'LANG={config['locale']}' in locale
vconsole = Path('/etc/vconsole.conf').read_text().splitlines()
assert 'KEYMAP={config['keymap']}' in vconsole
assert 'ID=bifrost' in Path('/etc/os-release').read_text().splitlines()
run_dirs = [p for p in Path('/var/log/bifrost-installer').iterdir() if p.is_dir()]
assert len(run_dirs) == 1
run_dir = run_dirs[0]
assert state['run_id'] == run_dir.name
assert state['source_mode'] == 'online'
assert state['installer_evidence'] == str(run_dir)
status = json.loads((run_dir / 'status.json').read_text())
assert status['state'] == 'success'
for retained in ('status.json', 'backend.log', 'archinstall.log', 'plan.json', 'archinstall.json'):
    assert (run_dir / retained).is_file()
print('BIFROST_RC_RELEASE', json.dumps(release, sort_keys=True))
print('BIFROST_RC_INSTALL_STATE', json.dumps(state, sort_keys=True))
print('BIFROST_RC_EVIDENCE_PATH', run_dirs[0])
PY
for kernel in linux linux-lts; do
  pacman -Q "$kernel"
  test -s "/boot/vmlinuz-$kernel"
  test -s "/boot/initramfs-$kernel.img"
done
if [[ {str(bool(config['encrypted'])).lower()} == true ]]; then
  cryptsetup luksDump /dev/vda2 | tee /run/bifrost-rc-luks.txt
  grep -Eq '^Version:[[:space:]]+2$' /run/bifrost-rc-luks.txt
else
  test "$(lsblk -dnro FSTYPE /dev/vda2)" = btrfs
fi
python3 - <<'PY'
import json, subprocess
from pathlib import Path
for profile in {expected_profiles}:
    manifest = json.loads(Path('/usr/share/bifrost/profiles', profile + '.json').read_text())
    for package in manifest['packages']:
        subprocess.run(['pacman', '-Q', package], check=True, stdout=subprocess.DEVNULL)
PY
find /var/log/bifrost-installer -maxdepth 2 -type f -printf 'BIFROST_RC_RETAINED_LOG %m %u:%g %p\n' | sort
find /boot -maxdepth 2 -type f -exec sha256sum -- {{}} + | sort
sync
echo "BIFROST_RC_ASSERTIONS_PASSED case={case}"
systemctl poweroff --no-block
'''


def install_script(case: str, config: dict[str, object], version: str) -> str:
    serial = config["serial"]
    profiles = json.dumps(config["profiles"], separators=(",", ":"))
    assertion = base64.b64encode(assertion_script(case, config, version).encode()).decode()
    encryption_json = "true" if config["encrypted"] else "false"
    encryption_python = str(bool(config["encrypted"]))
    luks_secret = f'"encryption_password": {json.dumps(LUKS_PASSWORD)},' if config["encrypted"] else ""
    mount_source = "/dev/mapper/bifrost-rc-root" if config["encrypted"] else "/dev/vda2"
    open_luks = f"printf %s {shlex.quote(LUKS_PASSWORD)} | cryptsetup open --key-file=- /dev/vda2 bifrost-rc-root" if config["encrypted"] else ":"
    return f'''#!/usr/bin/env bash
set -Eeuo pipefail
trap 'rc=$?; echo "BIFROST_RC_INSTALL_FAILED case={case} rc=$rc line=$LINENO" >/dev/ttyS0; exit "$rc"' ERR
export EXPECTED_SERIAL={shlex.quote(str(serial))}
export USER_PASSWORD={shlex.quote(USER_PASSWORD)}
export LUKS_PASSWORD={shlex.quote(LUKS_PASSWORD)}
python3 - <<'PY'
import json, os, subprocess
rows = json.loads(subprocess.run(['lsblk', '--json', '--bytes', '--nodeps', '--output', 'PATH,TYPE,SIZE,MODEL,SERIAL,WWN,LOG-SEC'], check=True, text=True, stdout=subprocess.PIPE).stdout)['blockdevices']
disks = [row for row in rows if row['type'] == 'disk']
assert len(disks) == 1, f'refusing VM with {{len(disks)}} disks'
disk = disks[0]
assert disk['path'] == '/dev/vda'
assert disk.get('serial') == os.environ['EXPECTED_SERIAL'], 'refusing non-test disk serial'
assert int(disk['size']) >= 16 * 1024**3
intent = {{
  'schema_version': 2,
  'target': {{
    'path': disk['path'], 'model': disk.get('model') or '', 'serial': disk.get('serial') or '',
    'wwn': disk.get('wwn') or '', 'size': int(disk['size']), 'logical_sector': int(disk['log-sec']),
  }},
  'options': {{
    'encryption': {encryption_python}, 'profiles': {profiles}, 'installer_language': {config['language']!r},
    'source_mode': 'online',
    'system_defaults': {{
      'hostname': 'bifrost-rc-{case}', 'timezone': 'Atlantic/Reykjavik', 'locale': {config['locale']!r},
      'keyboard_layout': {config['keymap']!r}, 'username': 'bifrost', 'full_name': 'BifrOSt RC',
    }},
  }},
}}
secrets = {{'schema_version': 2, 'user_password': os.environ['USER_PASSWORD'], {luks_secret}}}
for path, value in (('/run/bifrost-intent.json', intent), ('/run/bifrost-secrets.json', secrets)):
    with open(path, 'w') as stream:
        json.dump(value, stream)
    os.chmod(path, 0o600)
PY
/usr/local/lib/bifrost-installer-backend /run/bifrost-intent.json /run/bifrost-secrets.json | tee /run/bifrost-backend.events
python3 - <<'PY'
import json
lines = [json.loads(line) for line in open('/run/bifrost-backend.events') if line.strip()]
assert lines[-1]['event'] == 'success', lines[-1]
PY
{open_luks}
mount -o subvol=@ {mount_source} /mnt
mount /dev/vda1 /mnt/boot
# Add serial visibility and a read-only assertion service to this isolated test image.
sed -i '/^options / {{ /console=ttyS0/! s/$/ console=ttyS0,115200/; }}' /mnt/boot/loader/entries/*.conf
install -d -m 0755 /mnt/usr/local/lib /mnt/etc/systemd/system/multi-user.target.wants
printf %s {assertion} | base64 -d > /mnt/usr/local/lib/bifrost-rc-assert
chmod 0755 /mnt/usr/local/lib/bifrost-rc-assert
cat > /mnt/etc/systemd/system/bifrost-rc-assert.service <<'UNIT'
[Unit]
Description=BifrOSt release-candidate assertions
After=local-fs.target
Before=display-manager.service

[Service]
Type=oneshot
ExecStart=/usr/local/lib/bifrost-rc-assert
StandardInput=tty
TTYPath=/dev/ttyS0

[Install]
WantedBy=multi-user.target
UNIT
ln -s ../bifrost-rc-assert.service /mnt/etc/systemd/system/multi-user.target.wants/bifrost-rc-assert.service
if grep -R -F -- "$USER_PASSWORD" /mnt/var/log/bifrost-installer; then exit 1; fi
if [[ {encryption_json} == true ]] && grep -R -F -- "$LUKS_PASSWORD" /mnt/var/log/bifrost-installer; then exit 1; fi
sync
umount -R /mnt
[[ {encryption_json} == true ]] && cryptsetup close bifrost-rc-root
rm -f /run/bifrost-secrets.json
echo "BIFROST_RC_INSTALL_PASSED case={case}" >/dev/ttyS0
poweroff
'''


def qemu_arguments(case_dir: Path, config: dict[str, object], iso: Path | None, serial_socket: Path) -> list[str]:
    arguments = [
        "qemu-system-x86_64", "-name", f"bifrost-rc-{case_dir.name}",
        "-enable-kvm", "-machine", "q35,accel=kvm", "-cpu", "host", "-smp", "4", "-m", "8192",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
        "-drive", f"if=pflash,format=raw,file={case_dir / 'OVMF_VARS.fd'}",
        "-drive", f"id=system,if=none,format=qcow2,cache=none,file={case_dir / 'disk.qcow2'}",
        "-device", f"virtio-blk-pci,drive=system,serial={config['serial']}",
        "-nic", "user,model=virtio-net-pci", "-display", "none", "-monitor", "none", "-no-reboot",
        "-serial", f"unix:{serial_socket},server=on,wait=off",
    ]
    if iso is not None:
        arguments += ["-cdrom", str(iso), "-boot", "order=d,once=d"]
    else:
        arguments += ["-boot", "order=c"]
    return arguments


def launch(case_dir: Path, config: dict[str, object], iso: Path | None, phase: str, timeout: int) -> tuple[subprocess.Popen[bytes], SerialConsole]:
    socket_path = case_dir / f"{phase}.serial.sock"
    log_path = case_dir / f"{phase}.serial.log"
    stderr = (case_dir / f"{phase}.qemu.log").open("wb")
    arguments = qemu_arguments(case_dir, config, iso, socket_path)
    (case_dir / f"{phase}.command.json").write_text(json.dumps(arguments, indent=2) + "\n")
    process = subprocess.Popen(arguments, stdout=stderr, stderr=subprocess.STDOUT)
    stderr.close()
    console = SerialConsole(socket_path, log_path, process)
    return process, console

def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(20)


def wait_process(process: subprocess.Popen[bytes], console: SerialConsole, timeout: int, label: str) -> None:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        console.read(1.0)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(20)
        except subprocess.TimeoutExpired:
            process.kill()
        raise QualificationError(f"timed out waiting for QEMU {label}")
    if process.returncode != 0:
        raise QualificationError(f"QEMU {label} exited with rc={process.returncode}")


def run_case(name: str, config: dict[str, object], work_dir: Path, iso: Path, version: str, install_timeout: int, boot_timeout: int) -> dict[str, object]:
    case_dir = work_dir / name
    case_dir.mkdir(mode=0o700)
    shutil.copyfile(OVMF_VARS, case_dir / "OVMF_VARS.fd")
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(case_dir / "disk.qcow2"), DISK_SIZE], check=True)

    install_started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    install_started_monotonic = time.monotonic()
    process, console = launch(case_dir, config, iso, "install", install_timeout)
    try:
        live_boot_timeout = min(boot_timeout, 120)
        console.wait_for([b"Boot in", b"BifrOSt live/install medium"], 30, "systemd-boot menu")
        console.send("e")
        time.sleep(1)
        console.send("console=ttyS0,115200 \n")
        shell = console.wait_for(
            [b"root@archiso", b"root :", b"# ", b" login:"],
            live_boot_timeout,
            "live serial login",
        )
        if shell == b" login:":
            console.clear()
            console.send("root\n")
            console.wait_for([b"root@archiso", b"root :", b"# "], 60, "live serial root shell")
        console.send("stty -echo\n")
        time.sleep(0.5)
        console.send_script(install_script(name, config, version))
        outcome = console.wait_for(
            [
                f"BIFROST_RC_INSTALL_PASSED case={name}".encode(),
                f"BIFROST_RC_INSTALL_FAILED case={name}".encode(),
            ],
            install_timeout,
            "installation result",
        )
        if outcome.endswith(f"FAILED case={name}".encode()):
            console.send(
                "find /var/log/bifrost-installer -maxdepth 2 -name backend.log "
                "-exec tail -n 200 {} \\; >/dev/ttyS0; poweroff\n"
            )
            wait_process(process, console, 180, "failed-install shutdown")
            raise QualificationError(f"{name} installation failed; see install.serial.log")
        wait_process(process, console, 180, "install shutdown")
    finally:
        stop_process(process)
        console.drain()
        console.close()
    install_completed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    install_seconds = time.monotonic() - install_started_monotonic

    cold_boot_started_monotonic = time.monotonic()
    process, console = launch(case_dir, config, None, "boot", boot_timeout)
    wrong_rejected = not bool(config["encrypted"])
    try:
        if config["encrypted"]:
            unlock_prompts = [
                b"Enter passphrase",
                b"Passphrase for",
                b"password for",
                b"Password for",
                b"password is required",
            ]
            console.wait_for(unlock_prompts, 300, "LUKS2 unlock prompt")
            console.clear()
            console.send(WRONG_LUKS_PASSWORD + "\n")
            rejection = console.wait_for(
                [b"No key available", b"Incorrect", b"incorrect", b"Failed to activate"],
                120,
                "wrong LUKS2 passphrase rejection",
            )
            wrong_rejected = True
            console.discard_through(rejection)
            console.wait_for(unlock_prompts, 120, "LUKS2 retry prompt")
            console.clear()
            console.send(LUKS_PASSWORD + "\n")
        console.wait_for([f"BIFROST_RC_ASSERTIONS_PASSED case={name}".encode()], boot_timeout, "installed-system assertions")
        wait_process(process, console, 180, "cold-boot shutdown")
    finally:
        stop_process(process)
        console.drain()
        console.close()
    cold_boot_seconds = time.monotonic() - cold_boot_started_monotonic

    result = {
        "case": name,
        "status": "passed",
        "wrong_luks_passphrase_rejected": wrong_rejected,
        "install_started_at": install_started_at,
        "install_completed_at": install_completed_at,
        "install_seconds": round(install_seconds, 3),
        "cold_boot_seconds": round(cold_boot_seconds, 3),
        "disk": str(case_dir / "disk.qcow2"),
        "firmware_vars": str(case_dir / "OVMF_VARS.fd"),
        "install_serial_log": str(case_dir / "install.serial.log"),
        "boot_serial_log": str(case_dir / "boot.serial.log"),
        "installed_evidence_path": "/var/log/bifrost-installer/<run-id> (inside retained disk.qcow2)",
    }
    (case_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", required=True, type=Path, help="exact release-candidate ISO path (no globbing)")
    parser.add_argument("--work-dir", type=Path, help="new directory; defaults below ~/.local/state/bifrost/qemu-rc")
    parser.add_argument("--case", choices=("all", *CASES), default="all")
    parser.add_argument("--install-timeout", type=int, default=10800, help="seconds per installation")
    parser.add_argument("--boot-timeout", type=int, default=900, help="seconds per cold boot")
    parser.add_argument(
        "--overall-deadline",
        type=int,
        default=0,
        help="overall wall-clock budget in seconds for all cases; 0 disables. "
        "On expiry a failed result.json plus evidence is written before exiting nonzero",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    iso = args.iso.expanduser().absolute()
    if not iso.is_file() or iso.is_symlink() or iso.stat().st_size == 0:
        raise QualificationError("--iso must name one non-empty, non-symlink regular file")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise QualificationError("/dev/kvm is unavailable; use the explicit self-hosted Arch/KVM runner")
    for executable in ("qemu-system-x86_64", "qemu-img"):
        if shutil.which(executable) is None:
            raise QualificationError(f"missing required executable: {executable}")
    for firmware in (OVMF_CODE, OVMF_VARS):
        if not firmware.is_file():
            raise QualificationError(f"missing OVMF image: {firmware}")
    version_path = ROOT / "VERSION"
    version = version_path.read_text().strip()
    if not version:
        raise QualificationError("VERSION is empty")

    iso_digest = sha256(iso)
    work_dir = safe_work_dir(args.work_dir, iso_digest)
    selected = list(CASES) if args.case == "all" else [args.case]
    manifest = {
        "schema_version": 1,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "version": version,
        "iso": {"path": str(iso), "bytes": iso.stat().st_size, "sha256": iso_digest},
        "host": {
            "qemu": command_output(["qemu-system-x86_64", "--version"]),
            "ovmf_package": command_output(["pacman", "-Q", "edk2-ovmf"]),
            "ovmf_code_sha256": sha256(OVMF_CODE),
            "ovmf_vars_sha256": sha256(OVMF_VARS),
        },
        "cases": selected,
    }
    (work_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (work_dir / "iso.sha256").write_text(f"{iso_digest}  {iso.name}\n")
    results: list[dict[str, object]] = []
    if args.overall_deadline > 0:
        def expire_overall_deadline(signum: int, frame: object) -> None:
            raise QualificationError(f"overall deadline of {args.overall_deadline}s expired")

        signal.signal(signal.SIGALRM, expire_overall_deadline)
        signal.alarm(args.overall_deadline)
    try:
        for name in selected:
            results.append(run_case(name, CASES[name], work_dir, iso, version, args.install_timeout, args.boot_timeout))
        signal.alarm(0)
    except Exception as error:
        summary = {**manifest, "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "failed", "error": str(error), "results": results}
        (work_dir / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
        raise
    summary = {**manifest, "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "passed", "results": results}
    (work_dir / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(work_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QualificationError, OSError, subprocess.CalledProcessError) as error:
        print(f"qemu qualification failed: {error}", file=sys.stderr)
        raise SystemExit(1)
