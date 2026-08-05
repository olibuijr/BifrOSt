#!/usr/bin/env python3
"""Run unprivileged static checks for the BifrOSt ArchISO profile."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tokenize

CURRENT_VERSION = "0.2.0"
SKIP_DIRS = {".git", "__pycache__", "out", "release", "vm", "work"}
FORBIDDEN_TRACKED = (
    "*.iso",
    "*.qcow2",
    "*.img",
    "*.raw",
    "*.vdi",
    "*.vmdk",
    "*.vhd",
    "*.vhdx",
    "*.ova",
    "*.fd",
    "*.p12",
    "*.pfx",
    "*.pem",
    "*.key",
    "secring.gpg",
    "*/private-keys-v1.d/*",
    "private-keys-v1.d/*",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".env",
    ".env.*",
    "secrets.json",
    "*-secrets.json",
)
PACKAGE_NAME = re.compile(r"^[a-z0-9@._+:-]+$")


class ValidationError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="repository root (default: directory containing this script)",
    )
    return parser.parse_args()


def source_kind(path: Path) -> str | None:
    if path.suffix == ".py":
        return "python"
    if path.suffix == ".sh":
        return "shell"
    try:
        with path.open("rb") as source:
            first_line = source.readline(256)
    except OSError:
        return None
    if not first_line.startswith(b"#!"):
        return None
    if b"python" in first_line:
        return "python"
    if b"bash" in first_line or first_line.rstrip().endswith(b"/sh"):
        return "shell"
    return None


def source_files(root: Path) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    shell: list[Path] = []
    python: list[Path] = []
    json_files: list[Path] = []
    desktop: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or SKIP_DIRS.intersection(path.relative_to(root).parts):
            continue
        if path.suffix == ".json":
            json_files.append(path)
        elif path.suffix == ".desktop":
            desktop.append(path)
        kind = source_kind(path)
        if kind == "shell":
            shell.append(path)
        elif kind == "python":
            python.append(path)
    return tuple(sorted(items) for items in (shell, python, json_files, desktop))


def require_commands(*commands: str) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise ValidationError(f"missing required command(s): {', '.join(missing)}")


def run_checked(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValidationError(f"{' '.join(command)} failed:\n{detail}")
    return completed.stdout


def unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate object key: {key}")
        value[key] = item
    return value


def validate_sources(root: Path) -> None:
    shell, python, json_files, desktop = source_files(root)
    require_commands("bash", "shellcheck", "desktop-file-validate", "git")
    if shell:
        run_checked(["bash", "-n", *map(str, shell)], cwd=root)
        run_checked(["shellcheck", "--severity=error", "-x", *map(str, shell)], cwd=root)
    for path in python:
        try:
            with tokenize.open(path) as source:
                compile(source.read(), str(path), "exec")
        except (OSError, SyntaxError, UnicodeError) as error:
            raise ValidationError(f"invalid Python file {path.relative_to(root)}: {error}") from error
    for path in json_files:
        try:
            with path.open(encoding="utf-8") as source:
                json.load(source, object_pairs_hook=unique_json_object)
        except (OSError, UnicodeError, ValueError) as error:
            raise ValidationError(f"invalid JSON file {path.relative_to(root)}: {error}") from error
    for path in desktop:
        run_checked(["desktop-file-validate", str(path)], cwd=root)


def validate_tracked_files(root: Path) -> None:
    output = run_checked(["git", "ls-files", "-z"], cwd=root)
    for name in filter(None, output.split("\0")):
        basename = Path(name).name
        if any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(basename, pattern) for pattern in FORBIDDEN_TRACKED):
            raise ValidationError(f"secret or disk/build artifact must not be tracked: {name}")


def read_package_names(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValidationError(f"cannot read {path}: {error}") from error
    names = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    invalid = [name for name in names if not PACKAGE_NAME.fullmatch(name)]
    if invalid:
        raise ValidationError(f"invalid package name(s) in {path}: {', '.join(invalid)}")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValidationError(f"duplicate package name(s) in {path}: {', '.join(duplicates)}")
    return set(names)


def read_os_release(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValidationError(f"invalid os-release line {number}: {raw_line}")
        key, raw_value = line.split("=", 1)
        try:
            parsed = shlex.split(raw_value, posix=True)
        except ValueError as error:
            raise ValidationError(f"invalid os-release value on line {number}: {error}") from error
        if len(parsed) != 1:
            raise ValidationError(f"invalid os-release value on line {number}: {raw_value}")
        values[key] = parsed[0]
    return values


def profile_values(root: Path, version: str | None, epoch: int | None) -> list[str]:
    profile = root / "profile/profiledef.sh"
    command = [
        "bash",
        "-c",
        'declare -A file_permissions=(); source "$1" && printf "%s\\n" "$iso_name" "$iso_label" "$iso_version" "${buildmodes[*]}" "${bootmodes[*]}" "${file_permissions[/usr/local/bin/bifrost-installer]}" "${file_permissions[/usr/local/lib/bifrost-installer-backend]}"',
        "validate-profile",
        str(profile),
    ]
    environment = os.environ.copy()
    environment.pop("BIFROST_VERSION", None)
    environment.pop("SOURCE_DATE_EPOCH", None)
    if version is not None:
        environment["BIFROST_VERSION"] = version
    if epoch is not None:
        environment["SOURCE_DATE_EPOCH"] = str(epoch)
    return run_checked(command, cwd=root, env=environment).splitlines()


def validate_profile(root: Path) -> None:
    default = profile_values(root, None, None)
    expected_default = [
        "bifrost",
        "BIFROST_020_19700101",
        CURRENT_VERSION,
        "iso",
        "bios.syslinux uefi.systemd-boot",
        "0:0:755",
        "0:0:755",
    ]
    if default != expected_default:
        raise ValidationError(f"unexpected default profile identity: {default!r}")
    override = profile_values(root, "9.8.7-test.1", 86_400)
    if override[:3] != ["bifrost", "BIFROST_020_19700102", CURRENT_VERSION]:
        raise ValidationError(f"SOURCE_DATE_EPOCH override is not deterministic: {override[:3]!r}")
    os_release = read_os_release(root / "profile/airootfs/usr/share/bifrost/os-release")
    for key in ("BUILD_ID", "VERSION_ID", "IMAGE_VERSION"):
        if os_release.get(key) != CURRENT_VERSION:
            raise ValidationError(f"os-release {key} must be {CURRENT_VERSION}")
    if CURRENT_VERSION not in os_release.get("PRETTY_NAME", "") or CURRENT_VERSION not in os_release.get("VERSION", ""):
        raise ValidationError("os-release display metadata does not contain the release version")

    live_packages = read_package_names(root / "profile/packages.x86_64")
    required_live = {"archinstall", "cosmic", "gtk4", "linux", "linux-firmware", "networkmanager", "python-gobject"}
    missing = sorted(required_live - live_packages)
    if missing:
        raise ValidationError(f"profile package list is missing: {', '.join(missing)}")
    bootstrap = read_package_names(root / "profile/bootstrap_packages")
    if not {"arch-install-scripts", "base"}.issubset(bootstrap):
        raise ValidationError("bootstrap package list must include arch-install-scripts and base")


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    if not (root / "profile/profiledef.sh").is_file():
        print(f"validate-build.py: not a BifrOSt repository root: {root}", file=sys.stderr)
        return 2
    try:
        validate_sources(root)
        validate_tracked_files(root)
        validate_profile(root)
    except (OSError, ValidationError) as error:
        print(f"validate-build.py: {error}", file=sys.stderr)
        return 1
    print("BifrOSt static validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
