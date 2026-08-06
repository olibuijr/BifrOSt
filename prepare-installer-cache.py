#!/usr/bin/env python3
"""Populate the ISO's signed package seed cache for fast installations."""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PROFILE_ROOT = ROOT / "profile"
PROFILES_DIR = PROFILE_ROOT / "airootfs/usr/share/bifrost/installed-root/usr/share/bifrost/profiles"
DEFAULT_CACHE = PROFILE_ROOT / "airootfs/usr/share/bifrost/installer-cache"
MANIFEST_NAME = "manifest.json"
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
        shutil.rmtree(database)
        (temporary / ".pacman.log").unlink(missing_ok=True)
        archives = sorted(temporary.glob("*.pkg.tar.*"))
        archives = [path for path in archives if not path.name.endswith(".sig")]
        if not archives:
            raise CacheError("pacman produced no package archives")
        files = []
        for archive in archives:
            signature = archive.with_name(archive.name + ".sig")
            if not signature.is_file():
                raise CacheError(f"package signature is missing: {signature.name}")
            for path in (archive, signature):
                path.chmod(0o644)
                files.append({"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)})
        manifest = {
            "schema_version": 1,
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


def main() -> int:
    try:
        prepare(parse_args().cache_dir)
    except (CacheError, OSError, KeyError) as error:
        print(f"installer cache preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
