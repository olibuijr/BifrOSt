#!/usr/bin/env python3
"""Verify a staged BifrOSt ALPM repository and optionally publish it to GitHub Pages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from typing import Iterable

FINGERPRINT = re.compile(r"^[0-9A-F]{40}$")
PACKAGE_NAME = "bifrost-system"
PACKAGE_VERSION = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)-([1-9][0-9]*)$")
REMOTE = "https://github.com/olibuijr/BifrOSt.git"
PUBLIC_URL = "https://olibuijr.github.io/BifrOSt/alpm/$arch"


class PublishError(RuntimeError):
    pass


def run(
    command: Iterable[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        list(command), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        if not detail:
            detail = completed.stdout.decode("utf-8", "replace").strip()
        raise PublishError(f"command failed ({' '.join(command)}): {detail or 'no details'}")
    return completed


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def primary_public_fingerprints(public_key: Path) -> set[str]:
    shown = run(["gpg", "--batch", "--with-colons", "--show-keys", str(public_key)]).stdout
    values: set[str] = set()
    waiting = False
    for raw in shown.decode("utf-8", "replace").splitlines():
        fields = raw.split(":")
        kind = fields[0] if fields else ""
        if kind == "pub":
            waiting = True
        elif waiting and kind == "fpr" and len(fields) > 9:
            values.add(fields[9].upper())
            waiting = False
        elif kind in {"pub", "sub"}:
            waiting = False
    return values


def valid_signature(path: Path, signature: Path, keyring: Path, fingerprint: str) -> None:
    completed = run(
        ["gpgv", "--keyring", str(keyring), "--status-fd", "1", str(signature), str(path)]
    )
    valid = set()
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if not line.startswith("[GNUPG:] VALIDSIG ") or len(fields) <= 2:
            continue
        primary = fields[-1].upper()
        valid.add(primary if len(fields) > 11 and FINGERPRINT.fullmatch(primary) else fields[2].upper())
    if valid != {fingerprint}:
        raise PublishError(f"signature does not resolve only to {fingerprint}: {signature.name}")


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
        raise PublishError("repository database crosses the bifrost-system-only package boundary")
    return identities[0][1]


def validate_stage(stage: Path, public_key: Path, expected_fingerprint: str | None) -> str:
    if not stage.is_dir() or stage.is_symlink():
        raise PublishError("--stage must be a real signed repository directory")
    if not public_key.is_file():
        raise PublishError(f"pinned public-key input is missing: {public_key}")
    fingerprint_path = stage / "alpm-repository-key.fingerprint"
    if not fingerprint_path.is_file():
        raise PublishError("staged signing fingerprint is missing")
    fingerprint = fingerprint_path.read_text(encoding="ascii").strip().upper()
    if not FINGERPRINT.fullmatch(fingerprint):
        raise PublishError("staged signing fingerprint is not a full 40-hex fingerprint")
    if expected_fingerprint and expected_fingerprint.upper() != fingerprint:
        raise PublishError("--fingerprint does not match the staged signing fingerprint")
    if primary_public_fingerprints(public_key) != {fingerprint}:
        raise PublishError("pinned public-key input does not match the staged fingerprint")
    staged_public_key = stage / "alpm-repository-key.asc"
    if not staged_public_key.is_file() or staged_public_key.read_bytes() != public_key.read_bytes():
        raise PublishError("staged public key differs from the pinned public-key input")
    keyring = stage / "alpm-repository-key.gpg"
    if not keyring.is_file():
        raise PublishError("staged public verification keyring is missing")

    manifest_path = stage / "manifest.json"
    manifest_signature = stage / "manifest.json.sig"
    if not manifest_path.is_file() or not manifest_signature.is_file():
        raise PublishError("signed staging manifest is incomplete")
    valid_signature(manifest_path, manifest_signature, keyring, fingerprint)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublishError(f"staging manifest is invalid: {error}") from error
    if (
        manifest.get("schema_version") != 1
        or manifest.get("repository") != PACKAGE_NAME
        or manifest.get("architecture") != "any"
        or manifest.get("signing_fingerprint") != fingerprint
        or not isinstance(manifest.get("package_version"), str)
        or not isinstance(manifest.get("files"), dict)
    ):
        raise PublishError("staging manifest identity or trust boundary is invalid")

    actual_regular = {
        path.name
        for path in stage.iterdir()
        if path.is_file() and not path.is_symlink() and path.name not in {"manifest.json", "manifest.json.sig"}
    }
    if set(manifest["files"]) != actual_regular:
        raise PublishError("staging manifest does not cover every regular repository file")
    for name, record in manifest["files"].items():
        path = stage / name
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "size"}
            or record["size"] != path.stat().st_size
            or record["sha256"] != sha256(path)
        ):
            raise PublishError(f"staging checksum failed: {name}")

    packages = sorted(stage.glob(f"{PACKAGE_NAME}-*.pkg.tar.zst"))
    if len(packages) != 1:
        raise PublishError("stage must contain exactly one bifrost-system package")
    package = packages[0]
    package_signature = package.with_name(package.name + ".sig")
    database = stage / "bifrost.db.tar.gz"
    database_signature = stage / "bifrost.db.tar.gz.sig"
    public_database = stage / "bifrost.db"
    public_database_signature = stage / "bifrost.db.sig"
    files_database = stage / "bifrost.files.tar.gz"
    files_signature = stage / "bifrost.files.tar.gz.sig"
    public_files = stage / "bifrost.files"
    public_files_signature = stage / "bifrost.files.sig"
    signed_pairs = (
        (package, package_signature),
        (database, database_signature),
        (public_database, public_database_signature),
        (files_database, files_signature),
        (public_files, public_files_signature),
    )
    for content, signature in signed_pairs:
        for required in (content, signature):
            if not required.is_file() or required.is_symlink():
                raise PublishError(f"mandatory signed repository file is missing or is a symlink: {required.name}")
        valid_signature(content, signature, keyring, fingerprint)
    metadata = run(["bsdtar", "-xOf", str(package), ".PKGINFO"]).stdout.decode("utf-8", "replace")
    values: dict[str, str] = {}
    for line in metadata.splitlines():
        key, separator, value = line.partition(" = ")
        if separator and key in {"pkgname", "pkgver"}:
            if key in values:
                raise PublishError(f"package metadata repeats {key}")
            values[key] = value
    package_version = values.get("pkgver")
    if (
        values.get("pkgname") != PACKAGE_NAME
        or not package_version
        or manifest.get("package_version") != package_version
        or not PACKAGE_VERSION.fullmatch(package_version)
        or not package_version.startswith(f"{manifest.get('version')}-")
    ):
        raise PublishError("package metadata crosses the bifrost-system-only boundary")
    databases = (database, public_database, files_database, public_files)
    if any(database_version(item) != values["pkgver"] for item in databases):
        raise PublishError("repository database version does not match the staged bifrost-system package")

    for path in stage.iterdir():
        if path.is_dir() or path.is_symlink():
            raise PublishError(f"nested content and symlinks are forbidden in the ALPM architecture directory: {path.name}")
    return fingerprint


def package_version_key(value: str) -> tuple[int, int, int, int]:
    match = PACKAGE_VERSION.fullmatch(value)
    if not match:
        raise PublishError(f"unsupported bifrost-system package version: {value}")
    return tuple(int(part) for part in match.groups())

def publish(stage: Path, pages: Path) -> None:
    pages.parent.mkdir(parents=True, exist_ok=True)
    if pages.is_symlink() or (pages.exists() and not pages.is_dir()):
        raise PublishError("--pages-directory must be a real directory or an unused path")
    if pages.exists():
        shutil.rmtree(pages)
    branch = run(["git", "ls-remote", "--heads", REMOTE, "gh-pages"]).stdout.strip()
    if branch:
        run(["git", "clone", "--depth", "1", "--branch", "gh-pages", REMOTE, str(pages)])
    else:
        pages.mkdir(parents=True)
        run(["git", "init", "--initial-branch=gh-pages"], cwd=pages)
        run(["git", "remote", "add", "origin", REMOTE], cwd=pages)
    run(["git", "config", "user.name", "BifrOSt Release Automation"], cwd=pages)
    run(["git", "config", "user.email", "olibuijr@users.noreply.github.com"], cwd=pages)
    destination = pages / "alpm/x86_64"
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise PublishError("gh-pages alpm/x86_64 must not be a file or symlink")
    new_version = database_version(stage / "bifrost.db.tar.gz")
    if destination.exists():
        existing_database = destination / "bifrost.db.tar.gz"
        if not existing_database.is_file() or existing_database.is_symlink():
            raise PublishError("existing published repository has no trustworthy database")
        existing_version = database_version(existing_database)
        if package_version_key(new_version) <= package_version_key(existing_version):
            raise PublishError(
                f"refusing non-monotonic repository replacement: published {existing_version}, candidate {new_version}"
            )
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(stage, destination, symlinks=True)
    (pages / ".nojekyll").write_text("\n", encoding="ascii")
    run(["git", "add", "alpm/x86_64", ".nojekyll"], cwd=pages)
    status = run(["git", "status", "--porcelain", "--", "alpm/x86_64", ".nojekyll"], cwd=pages).stdout
    if not status.strip():
        print("GitHub Pages already contains this verified signed repository.")
        return
    run(["git", "commit", "-m", "Publish signed BifrOSt system repository"], cwd=pages)
    run(["git", "push", "origin", "gh-pages"], cwd=pages)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Without --publish this command performs local verification only and never changes GitHub Pages.",
    )
    parser.add_argument("--stage", type=Path, default=Path("release/alpm/x86_64"), help="signed x86_64 repository stage")
    parser.add_argument("--public-key", type=Path, required=True, help="pinned armored public-key input")
    parser.add_argument("--fingerprint", required=True, help="independently pinned full 40-hex signing fingerprint")
    parser.add_argument("--pages-directory", type=Path, default=Path("release/pages"), help="temporary gh-pages checkout")
    parser.add_argument("--publish", action="store_true", help=f"publish only the verified stage to {PUBLIC_URL}")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if not FINGERPRINT.fullmatch(args.fingerprint.upper()):
            raise PublishError("--fingerprint must be exactly 40 hexadecimal characters")
        stage = args.stage.expanduser().absolute()
        fingerprint = validate_stage(stage, args.public_key.resolve(), args.fingerprint)
        print(f"Verified signed repository: {stage}")
        print(f"Signing fingerprint: {fingerprint}")
        if args.publish:
            publish(stage, args.pages_directory.expanduser().absolute())
            print(f"Published repository: {PUBLIC_URL}")
        return 0
    except (OSError, PublishError, tarfile.TarError) as error:
        print(f"publish-repository.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
