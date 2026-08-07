#!/usr/bin/env python3
"""Populate the ISO's signed package seed cache for fast installations.

The pacman download phase runs with the staging directory owned by the alpm
user so pacman can drop privileges. Before any verification begins the staging
directory is returned to root:root 0700, every archive and detached signature
is reopened through O_NOFOLLOW file descriptors, and each signature is checked
with gpg against the system ALPM keyring (/etc/pacman.d/gnupg). Only content
that passed signature verification is hashed into manifest.json and atomically
promoted into place.

Pass --require to verify an already prepared seed instead of rebuilding it:
the command exits nonzero unless the cache directory holds a manifest marked
complete whose files all exist with their recorded size and sha256 and whose
packages all carry a detached signature. Release tooling calls this as a gate.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PROFILE_ROOT = ROOT / "profile"
PROFILES_DIR = PROFILE_ROOT / "airootfs/usr/share/bifrost/installed-root/usr/share/bifrost/profiles"
DEFAULT_CACHE = PROFILE_ROOT / "airootfs/usr/share/bifrost/installer-cache"
MANIFEST_NAME = "manifest.json"
ALPM_GNUPG = Path("/etc/pacman.d/gnupg")
BASE_PACKAGES = (
    "base",
    "sudo",
    "linux-firmware",
    "mkinitcpio",
    "btrfs-progs",
    "networkmanager",
    "cosmic",
    "xdg-user-dirs",
    "cosmic-greeter",
    "amd-ucode",
    "intel-ucode",
    "zram-generator",
    "linux",
    "linux-lts",
)


class CacheError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--require",
        action="store_true",
        help="verify the existing seed manifest instead of rebuilding; exit nonzero if it is absent or invalid",
    )
    return parser.parse_args()


def run(command: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode:
        raise CacheError(f"{' '.join(command)} failed with exit code {completed.returncode}")


def selected_packages() -> list[str]:
    packages = list(BASE_PACKAGES)
    seen = set(packages)
    for path in sorted(PROFILES_DIR.glob("*.json")):
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CacheError(f"cannot read profile {path}: {error}") from error
        listed = profile.get("packages")
        if profile.get("schema_version") != 2 or not isinstance(listed, list):
            raise CacheError(f"invalid package profile: {path}")
        for package in listed:
            if not isinstance(package, str) or not package or package in seen:
                continue
            packages.append(package)
            seen.add(package)
    return packages


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_private(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CacheError(f"seed staging entry is not a private regular file: {path.name}")
        os.fchown(descriptor, 0, 0)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def sha256_fd(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def verify_signature(archive_fd: int, signature_fd: int, name: str) -> None:
    completed = subprocess.run(
        [
            "gpg",
            "--homedir",
            str(ALPM_GNUPG),
            "--batch",
            "--status-fd",
            "1",
            "--verify",
            f"/proc/self/fd/{signature_fd}",
            f"/proc/self/fd/{archive_fd}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
        pass_fds=(archive_fd, signature_fd),
    )
    statuses = {
        fields[1]
        for line in completed.stdout.splitlines()
        if line.startswith("[GNUPG:] ") and len(fields := line.split()) > 1
    }
    if completed.returncode or "VALIDSIG" not in statuses or not {"TRUST_FULLY", "TRUST_ULTIMATE"} & statuses:
        raise CacheError(f"package signature verification failed: {name}")


def prepare(cache_dir: Path) -> None:
    if os.geteuid() != 0:
        raise CacheError("run as root so pacman can synchronize and verify the package seed")
    cache_dir = cache_dir.resolve()
    parent = cache_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{cache_dir.name}.tmp-{os.getpid()}"
    if temporary.exists():
        raise CacheError(f"temporary cache path already exists: {temporary}")
    temporary.mkdir(mode=0o755)
    database = temporary / ".pacman-db"
    database.mkdir(mode=0o755)
    alpm = pwd.getpwnam("alpm")
    alpm_group = grp.getgrnam("alpm")
    os.chown(temporary, alpm.pw_uid, alpm_group.gr_gid)
    os.chown(database, alpm.pw_uid, alpm_group.gr_gid)
    packages = selected_packages()
    common = [
        "pacman",
        "--config",
        str(PROFILE_ROOT / "pacman.conf"),
        "--dbpath",
        str(database),
        "--cachedir",
        str(temporary),
        "--logfile",
        str(temporary / ".pacman.log"),
        "--noconfirm",
    ]
    try:
        run([*common, "-Sy"], cwd=ROOT)
        run([*common, "-Sw", *packages], cwd=ROOT)
        # The download phase is over: lock the alpm user out of the staging
        # directory before anything is verified so nothing can be swapped
        # between verification and promotion.
        os.chown(temporary, 0, 0)
        temporary.chmod(0o700)
        shutil.rmtree(database)
        (temporary / ".pacman.log").unlink(missing_ok=True)
        unexpected = sorted(
            entry.name
            for entry in os.scandir(temporary)
            if not (entry.is_file(follow_symlinks=False) and ".pkg.tar." in entry.name)
        )
        if unexpected:
            raise CacheError(f"unexpected seed staging entries: {', '.join(unexpected)}")
        archives = sorted(temporary.glob("*.pkg.tar.*"))
        archives = [path for path in archives if not path.name.endswith(".sig")]
        if not archives:
            raise CacheError("pacman produced no package archives")
        files = []
        for archive in archives:
            signature = archive.with_name(archive.name + ".sig")
            if not signature.is_file():
                raise CacheError(f"package signature is missing: {signature.name}")
            archive_fd = open_private(archive)
            try:
                signature_fd = open_private(signature)
                try:
                    verify_signature(archive_fd, signature_fd, archive.name)
                    for path, descriptor in ((archive, archive_fd), (signature, signature_fd)):
                        os.fchmod(descriptor, 0o644)
                        files.append(
                            {
                                "name": path.name,
                                "bytes": os.fstat(descriptor).st_size,
                                "sha256": sha256_fd(descriptor),
                            }
                        )
                finally:
                    os.close(signature_fd)
            finally:
                os.close(archive_fd)
        manifest = {
            "schema_version": 1,
            "complete": True,
            "purpose": "online-installer-package-seed",
            "top_level_packages": packages,
            "files": files,
        }
        manifest_path = temporary / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_path.chmod(0o644)
        temporary.chmod(0o755)
        if cache_dir.exists():
            current_manifest = cache_dir / MANIFEST_NAME
            if not current_manifest.is_file():
                raise CacheError(f"refusing to replace unmanaged cache directory: {cache_dir}")
            shutil.rmtree(cache_dir)
        os.replace(temporary, cache_dir)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    total = sum(path.stat().st_size for path in cache_dir.glob("*.pkg.tar.*"))
    print(f"{cache_dir}: {len(archives)} packages, {total} bytes including signatures")


def require(cache_dir: Path) -> None:
    manifest_path = cache_dir / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CacheError(f"seed manifest is unreadable: {error}") from error
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("purpose") != "online-installer-package-seed"
        or manifest.get("complete") is not True
        or not isinstance(files, list)
        or not files
    ):
        raise CacheError(f"seed manifest is incomplete or invalid: {manifest_path}")
    names = set()
    for record in files:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("name"), str)
            or Path(record["name"]).name != record["name"]
            or not isinstance(record.get("bytes"), int)
            or isinstance(record.get("bytes"), bool)
            or record["bytes"] <= 0
            or not isinstance(record.get("sha256"), str)
        ):
            raise CacheError(f"seed manifest file inventory is invalid: {manifest_path}")
        path = cache_dir / record["name"]
        if not path.is_file() or path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
            raise CacheError(f"seed file failed verification: {record['name']}")
        names.add(record["name"])
    for name in names:
        if not name.endswith(".sig") and f"{name}.sig" not in names:
            raise CacheError(f"seed package is missing its detached signature: {name}")
    print(f"{cache_dir}: complete signed package seed verified")


def main() -> int:
    arguments = parse_args()
    try:
        if arguments.require:
            require(arguments.cache_dir.resolve())
        else:
            prepare(arguments.cache_dir)
    except (CacheError, OSError, KeyError) as error:
        print(f"installer cache preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
