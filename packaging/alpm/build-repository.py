#!/usr/bin/env python3
"""Build, sign, verify, and stage the narrowly scoped BifrOSt ALPM repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterable

FINGERPRINT = re.compile(r"^[0-9A-F]{40}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PACKAGE_NAME = "bifrost-system"


class BuildError(RuntimeError):
    pass


def run(command: Iterable[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        list(command), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        if not detail:
            detail = completed.stdout.decode("utf-8", "replace").strip()
        raise BuildError(f"command failed ({' '.join(command)}): {detail or 'no details'}")
    return completed


def primary_fingerprints(colons: bytes, record: str) -> set[str]:
    fingerprints: set[str] = set()
    waiting = False
    for raw in colons.decode("utf-8", "replace").splitlines():
        fields = raw.split(":")
        kind = fields[0] if fields else ""
        if kind == record:
            waiting = True
        elif waiting and kind == "fpr" and len(fields) > 9:
            fingerprints.add(fields[9].upper())
            waiting = False
        elif kind in {"pub", "sec", "sub", "ssb"}:
            waiting = False
    return fingerprints


def validate_signing_key(fingerprint: str | None, homedir: Path, public_key: Path) -> tuple[str, bytes]:
    if not fingerprint:
        raise BuildError("--fingerprint or BIFROST_ALPM_FINGERPRINT is required")
    fingerprint = fingerprint.upper()
    if not FINGERPRINT.fullmatch(fingerprint):
        raise BuildError("the signing fingerprint must be exactly 40 hexadecimal characters")
    if not homedir.is_dir():
        raise BuildError("--gpg-homedir must be an existing isolated directory")
    if homedir == (Path.home() / ".gnupg").resolve():
        raise BuildError("the default personal GnuPG home is forbidden; use an isolated release homedir")
    if stat.S_IMODE(homedir.stat().st_mode) & 0o077:
        raise BuildError("the isolated GnuPG homedir must not be accessible by group or other users")
    if not public_key.is_file():
        raise BuildError(f"the pinned public-key input is missing: {public_key}")

    shown = run(["gpg", "--batch", "--with-colons", "--show-keys", str(public_key)]).stdout
    if primary_fingerprints(shown, "pub") != {fingerprint}:
        raise BuildError("the public-key input must contain exactly the selected primary key")
    secret_listing = run(
        ["gpg", "--homedir", str(homedir), "--batch", "--with-colons", "--list-secret-keys"]
    ).stdout
    if primary_fingerprints(secret_listing, "sec") != {fingerprint}:
        raise BuildError("the isolated homedir must contain exactly the selected primary secret key")
    return fingerprint, public_key.read_bytes()


def valid_signature(path: Path, signature: Path, fingerprint: str, homedir: Path) -> None:
    completed = run(
        [
            "gpg", "--homedir", str(homedir), "--batch", "--status-fd", "1",
            "--verify", str(signature), str(path),
        ]
    )
    valid = set()
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if not line.startswith("[GNUPG:] VALIDSIG ") or len(fields) <= 2:
            continue
        primary = fields[-1].upper()
        valid.add(primary if len(fields) > 11 and FINGERPRINT.fullmatch(primary) else fields[2].upper())
    if valid != {fingerprint}:
        raise BuildError(f"signature does not resolve only to {fingerprint}: {signature}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_identity(package: Path) -> tuple[str, str]:
    metadata = run(["bsdtar", "-xOf", str(package), ".PKGINFO"]).stdout.decode("utf-8", "replace")
    values: dict[str, str] = {}
    for line in metadata.splitlines():
        key, separator, value = line.partition(" = ")
        if separator and key in {"pkgname", "pkgver"}:
            if key in values:
                raise BuildError(f"package metadata repeats {key}")
            values[key] = value
    return values.get("pkgname", ""), values.get("pkgver", "")


def database_version(database: Path) -> str:
    with tarfile.open(database, "r:gz") as archive:
        members = archive.getmembers()
        roots = {member.name.split("/", 1)[0] for member in members if member.name}
        identities = []
        for member in members:
            if not member.isfile() or not member.name.endswith("/desc"):
                continue
            extracted = archive.extractfile(member)
            lines = extracted.read().decode("utf-8").splitlines() if extracted else []
            values = {}
            for field in ("%NAME%", "%VERSION%"):
                if field in lines and lines.index(field) + 1 < len(lines):
                    values[field] = lines[lines.index(field) + 1]
            identities.append((values.get("%NAME%"), values.get("%VERSION%")))
    if len(roots) != 1 or len(identities) != 1 or identities[0][0] != PACKAGE_NAME or not identities[0][1]:
        raise BuildError("repository database contains a package outside the bifrost-system boundary")
    return identities[0][1]


def atomic_replace(destination: Path, staged: Path) -> None:
    backup = destination.with_name(f".{destination.name}.backup")
    if backup.is_symlink() or (backup.exists() and not backup.is_dir()):
        raise BuildError(f"unsafe staging backup path: {backup}")
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise BuildError(f"unsafe staging destination: {destination}")
    if backup.exists():
        shutil.rmtree(backup)
    if destination.exists():
        destination.replace(backup)
    try:
        staged.replace(destination)
    except Exception:
        if backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def write_manifest(directory: Path, version: str, package_version: str, fingerprint: str) -> Path:
    files = {}
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.is_file() and not path.is_symlink() and path.name not in {"manifest.json", "manifest.json.sig"}:
            files[path.name] = {"sha256": sha256(path), "size": path.stat().st_size}
    manifest = {
        "schema_version": 1,
        "repository": PACKAGE_NAME,
        "version": version,
        "package_version": package_version,
        "architecture": "any",
        "signing_fingerprint": fingerprint,
        "files": files,
    }
    destination = directory / "manifest.json"
    destination.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def stage_installer(repository: Path, destination: Path) -> None:
    package_files = sorted(repository.glob(f"{PACKAGE_NAME}-*.pkg.tar.zst"))
    if len(package_files) != 1:
        raise BuildError("the repository stage must contain exactly one installer package")
    names = {
        package_files[0].name,
        package_files[0].name + ".sig",
        "manifest.json",
        "manifest.json.sig",
        "alpm-repository-key.asc",
        "alpm-repository-key.fingerprint",
        "alpm-repository-key.gpg",
    }
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        for name in names:
            source = repository / name
            if not source.is_file():
                raise BuildError(f"installer bootstrap input is missing: {source}")
            shutil.copy2(source, staging / name)
        atomic_replace(destination, staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpg-homedir", type=Path, required=True, help="isolated GnuPG home containing only the signing key")
    parser.add_argument("--fingerprint", default=os.environ.get("BIFROST_ALPM_FINGERPRINT"), help="full 40-hex signing fingerprint")
    parser.add_argument("--public-key", type=Path, required=True, help="pinned armored public-key input matching the fingerprint")
    parser.add_argument("--output", type=Path, default=Path("release/alpm/x86_64"), help="signed repository staging directory")
    parser.add_argument(
        "--installer-stage", type=Path,
        default=Path("profile/airootfs/usr/share/bifrost/alpm"),
        help="generated signed bootstrap payload included in the ISO",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        homedir = args.gpg_homedir.resolve()
        public_key_path = args.public_key.resolve()
        output = args.output.expanduser().absolute()
        installer_stage = args.installer_stage.expanduser().absolute()
        for label, destination in (("repository output", output), ("installer stage", installer_stage)):
            if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
                raise BuildError(f"{label} must be a real directory or an unused path")
        if output == installer_stage:
            raise BuildError("repository output and installer stage must be different directories")
        version = (root / "VERSION").read_text(encoding="ascii").strip()
        if not VERSION.fullmatch(version):
            raise BuildError("root VERSION must contain one semantic release version")
        pkgbuild = (root / "packaging/bifrost-system/PKGBUILD").read_text(encoding="utf-8")
        pkgrel_match = re.search(r"(?m)^pkgrel=([1-9][0-9]*)$", pkgbuild)
        if not pkgrel_match:
            raise BuildError("bifrost-system PKGBUILD must contain one positive integer pkgrel")
        expected_package_version = f"{version}-{pkgrel_match.group(1)}"
        fingerprint, public_key = validate_signing_key(args.fingerprint, homedir, public_key_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        installer_stage.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        build_dir = Path(tempfile.mkdtemp(prefix="bifrost-system-build-"))
        try:
            environment = os.environ.copy()
            environment.update(
                {
                    "GNUPGHOME": str(homedir),
                    "BIFROST_SOURCE_ROOT": str(root),
                    "BIFROST_ALPM_PUBLIC_KEY": str(public_key_path),
                    "BIFROST_ALPM_FINGERPRINT": fingerprint,
                    "PKGDEST": str(staging),
                    "BUILDDIR": str(build_dir),
                }
            )
            run(
                ["makepkg", "--cleanbuild", "--force", "--nodeps", "--noconfirm", "--sign", "--key", fingerprint],
                cwd=root / "packaging/bifrost-system",
                env=environment,
            )
            packages = sorted(staging.glob(f"{PACKAGE_NAME}-*.pkg.tar.zst"))
            if len(packages) != 1:
                raise BuildError("makepkg did not produce exactly one bifrost-system package")
            package = packages[0]
            signature = package.with_name(package.name + ".sig")
            if not signature.is_file():
                raise BuildError("makepkg did not produce the mandatory package signature")
            valid_signature(package, signature, fingerprint, homedir)
            name, built_version = package_identity(package)
            if name != PACKAGE_NAME or built_version != expected_package_version:
                raise BuildError(f"unexpected package identity: {name} {built_version}")

            database = staging / "bifrost.db.tar.gz"
            run(
                ["repo-add", "--sign", "--key", fingerprint, "--verify", str(database), str(package)],
                cwd=staging,
                env=environment,
            )
            database_signature = database.with_name(database.name + ".sig")
            files_database = staging / "bifrost.files.tar.gz"
            files_signature = files_database.with_name(files_database.name + ".sig")
            for signed_database, signature in (
                (database, database_signature),
                (files_database, files_signature),
            ):
                if not signature.is_file():
                    raise BuildError(f"repo-add did not sign {signed_database.name}")
                valid_signature(signed_database, signature, fingerprint, homedir)
            if database_version(database) != built_version:
                raise BuildError("repository database version does not match the staged bifrost-system package")
            # GitHub Pages must serve database bytes, not Git symlink blobs.
            for source, alias in (
                (database, staging / "bifrost.db"),
                (database_signature, staging / "bifrost.db.sig"),
                (files_database, staging / "bifrost.files"),
                (files_signature, staging / "bifrost.files.sig"),
            ):
                if alias.exists() or alias.is_symlink():
                    alias.unlink()
                shutil.copy2(source, alias)

            (staging / "alpm-repository-key.asc").write_bytes(public_key)
            (staging / "alpm-repository-key.fingerprint").write_text(fingerprint + "\n", encoding="ascii")
            run(
                ["gpg", "--batch", "--yes", "--dearmor", "--output", str(staging / "alpm-repository-key.gpg"), str(public_key_path)]
            )
            manifest = write_manifest(staging, version, built_version, fingerprint)
            manifest_signature = staging / "manifest.json.sig"
            run(
                [
                    "gpg", "--homedir", str(homedir), "--batch", "--detach-sign", "--local-user", fingerprint,
                    "--output", str(manifest_signature), str(manifest),
                ]
            )
            valid_signature(manifest, manifest_signature, fingerprint, homedir)
            atomic_replace(output, staging)
            stage_installer(output, installer_stage)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)
        print(f"Signed repository staged at {output}")
        print(f"Signed installer bootstrap staged at {installer_stage}")
        print(f"Signing fingerprint: {fingerprint}")
        return 0
    except (BuildError, OSError, tarfile.TarError) as error:
        print(f"build-repository.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
