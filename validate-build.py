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

VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
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


def read_version(root: Path) -> str:
    path = root / "VERSION"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValidationError(f"cannot read canonical VERSION: {error}") from error
    if len(lines) != 1 or not VERSION_PATTERN.fullmatch(lines[0]):
        raise ValidationError("VERSION must contain exactly one semantic release version")
    return lines[0]


def profile_values(root: Path, epoch: int | None) -> list[str]:
    profile = root / "profile/profiledef.sh"
    command = [
        "bash",
        "-c",
        'declare -A file_permissions=(); source "$1" && printf "%s\\n" "$iso_name" "$iso_label" "$iso_version" "${buildmodes[*]}" "${bootmodes[*]}" "${file_permissions[/usr/local/bin/bifrost-installer]}" "${file_permissions[/usr/local/lib/bifrost-installer-backend]}" "${file_permissions[/usr/share/bifrost/installed-root/usr/share/bifrost/release.json]}"',
        "validate-profile",
        str(profile),
    ]
    environment = os.environ.copy()
    environment.pop("BIFROST_VERSION", None)
    environment.pop("SOURCE_DATE_EPOCH", None)
    if epoch is not None:
        environment["SOURCE_DATE_EPOCH"] = str(epoch)
    return run_checked(command, cwd=root, env=environment).splitlines()


def validate_profile(root: Path) -> None:
    version = read_version(root)
    version_token = re.sub(r"[^A-Z0-9]", "", version.upper())[:12]
    default = profile_values(root, None)
    expected_default = [
        "bifrost",
        f"BIFROST_{version_token}_19700101",
        version,
        "iso",
        "uefi.systemd-boot",
        "0:0:755",
        "0:0:755",
        "0:0:644",
    ]
    if default != expected_default:
        raise ValidationError(f"unexpected default profile identity: {default!r}")
    override = profile_values(root, 86_400)
    if override[:3] != ["bifrost", f"BIFROST_{version_token}_19700102", version]:
        raise ValidationError(f"SOURCE_DATE_EPOCH override is not deterministic: {override[:3]!r}")
    profile_source = (root / "profile/profiledef.sh").read_text(encoding="utf-8")
    if "/VERSION" not in profile_source or re.search("bifrost_version=[\"'][0-9]", profile_source):
        raise ValidationError("profiledef.sh must consume canonical VERSION without a hardcoded fallback")

    os_release = read_os_release(root / "profile/airootfs/usr/share/bifrost/os-release")
    for key in ("BUILD_ID", "VERSION_ID", "IMAGE_VERSION"):
        if os_release.get(key) != version:
            raise ValidationError(f"os-release {key} must be {version}")
    if version not in os_release.get("PRETTY_NAME", "") or version not in os_release.get("VERSION", ""):
        raise ValidationError("os-release display metadata does not contain the canonical version")

    live_packages = read_package_names(root / "profile/packages.x86_64")
    required_live = {"archinstall", "cosmic", "gtk4", "linux", "linux-firmware", "networkmanager", "python-gobject"}
    missing = sorted(required_live - live_packages)
    if missing:
        raise ValidationError(f"profile package list is missing: {', '.join(missing)}")
    bootstrap = read_package_names(root / "profile/bootstrap_packages")
    if not {"arch-install-scripts", "base"}.issubset(bootstrap):
        raise ValidationError("bootstrap package list must include arch-install-scripts and base")
    pacman_init_link = (
        root
        / "profile/airootfs/etc/systemd/system/multi-user.target.wants/pacman-init.service"
    )
    if not pacman_init_link.is_symlink() or pacman_init_link.readlink() != Path("../pacman-init.service"):
        raise ValidationError("live pacman keyring initialization service must be enabled")


def validate_release_contract(root: Path) -> None:
    version = read_version(root)
    release_path = (
        root
        / "profile/airootfs/usr/share/bifrost/installed-root/usr/share/bifrost/release.json"
    )
    try:
        with release_path.open(encoding="utf-8") as source:
            release = json.load(source, object_pairs_hook=unique_json_object)
    except (OSError, UnicodeError, ValueError) as error:
        raise ValidationError(f"invalid installed release provenance: {error}") from error
    if release.get("schema_version") != 1:
        raise ValidationError("installed release provenance must use schema_version 1")
    if release.get("provenance_status") != "unsigned-development":
        raise ValidationError("source release provenance must be explicitly unsigned-development")
    if release.get("version") != version:
        raise ValidationError("installed release provenance version must equal canonical VERSION")
    if release.get("source_revision") is not None or release.get("build_id") is not None:
        raise ValidationError("source release template must not claim a finalized revision/build identity")
    if release.get("source_date_epoch") != 0:
        raise ValidationError("source release template must use deterministic epoch zero")
    iso = release.get("iso")
    if not isinstance(iso, dict) or iso.get("file") != f"bifrost-{version}-x86_64.iso":
        raise ValidationError("source release template has an inconsistent ISO identity")
    for key in ("bytes", "sha256", "volume_id"):
        if iso.get(key) is not None:
            raise ValidationError(f"source release template ISO {key} must remain unfinalized")
    evidence = release.get("evidence")
    required_evidence = {
        "attestation_file",
        "build_metadata_file",
        "checksum_file",
        "checksum_signature_file",
        "detached_signature_file",
        "package_manifest_file",
        "signer_fingerprint",
        "toolchain_manifest_file",
    }
    if not isinstance(evidence, dict) or not required_evidence.issubset(evidence):
        raise ValidationError("source release template is missing evidence identity fields")
    if any(evidence[key] is not None for key in required_evidence):
        raise ValidationError("source release template must not claim finalized evidence")

    generator = (root / "generate-release-metadata.py").read_text(encoding="utf-8")
    publisher = (root / "publish-release.py").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/publish-final-release.yml").read_text(encoding="utf-8")
    if "--force" in generator:
        raise ValidationError("release evidence generation must never expose a replacement override")
    for source_name, source in (("metadata generator", generator), ("publisher", publisher)):
        if "VERSION_FILE" not in source or '"0.2.0"' in source or "'0.2.0'" in source:
            raise ValidationError(f"{source_name} must consume VERSION without an old hardcoded fallback")
    required_generator_contract = (
        "--final",
        "--prepare-installed",
        "--source-tag",
        "--toolchain-manifest",
        "checksum_signature_file",
        "detached_signature_file",
        "profile_sha256",
    )
    if any(token not in generator for token in required_generator_contract):
        raise ValidationError("release metadata generator is missing final provenance/signing controls")
    forbidden_publication = ("release delete", "git tag -f", "--force", "--clobber")
    if any(token in publisher for token in forbidden_publication):
        raise ValidationError("release publisher contains a destructive replacement path")
    required_publication = (
        "verify_remote_tag",
        "require_no_release",
        "verify_signature",
        "provenance_status",
        "toolchain_manifest_sha256",
        "--signer-fingerprint",
        "VALIDSIG",
    )
    if any(token not in publisher for token in required_publication):
        raise ValidationError("release publisher is missing fail-closed identity/evidence checks")
    if "workflow_dispatch:" not in workflow or "contents: write" not in workflow:
        raise ValidationError("final release workflow must be operator-dispatched with explicit write permission")
    if '--signer-fingerprint "${{ inputs.signer_fingerprint }}"' not in workflow:
        raise ValidationError("final release workflow must pass the operator-trusted signer fingerprint")


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
        validate_release_contract(root)
    except (OSError, ValidationError) as error:
        print(f"validate-build.py: {error}", file=sys.stderr)
        return 1
    print("BifrOSt static validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
