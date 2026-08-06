#!/usr/bin/env python3
"""Generate deterministic BifrOSt release evidence for an existing ISO."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

SOURCE_REVISION = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
RELEASE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
PACKAGE_ARCHIVE_SUFFIXES = ("zst", "xz", "gz", "bz2", "lrz", "lzo", "Z")


class ReleaseError(Exception):
    pass


def default_version() -> str:
    path = Path(__file__).resolve().parent / "profile/airootfs/usr/share/bifrost/os-release"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("VERSION_ID="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "0.2.0"


def epoch_value(value: str) -> int:
    try:
        epoch = int(value, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from error
    if epoch < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    try:
        datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise argparse.ArgumentTypeError("is outside the supported date range") from error
    return epoch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Unsigned runs create an explicitly named .sha256.unsigned file. "
            "Supplying --gpg-key creates a standard .sha256 file and detached armored .asc signature; "
            "the script never creates or exports a key."
        ),
    )
    parser.add_argument("--iso", required=True, type=Path, help="completed .iso artifact")
    parser.add_argument(
        "--alpm-root",
        required=True,
        type=Path,
        help="root filesystem whose var/lib/pacman/local database records the ISO package set",
    )
    parser.add_argument(
        "--package-cache",
        required=True,
        type=Path,
        help="complete package archive directory matching every installed ALPM record",
    )
    parser.add_argument("--output-dir", required=True, type=Path, help="directory for generated evidence")
    parser.add_argument(
        "--source-revision",
        required=True,
        help="exact 40- or 64-hex source commit revision (symbolic names such as HEAD are rejected)",
    )
    parser.add_argument(
        "--source-date-epoch",
        type=epoch_value,
        default=os.environ.get("SOURCE_DATE_EPOCH") or "0",
        help="non-negative build epoch (default: SOURCE_DATE_EPOCH or 0)",
    )
    parser.add_argument(
        "--version",
        default=os.environ.get("BIFROST_VERSION") or default_version(),
        help="release version (default: BIFROST_VERSION or profile VERSION_ID)",
    )
    parser.add_argument(
        "--gpg-key",
        metavar="FINGERPRINT",
        help="existing full GPG signing-key fingerprint; no key is generated or exported",
    )
    parser.add_argument("--force", action="store_true", help="replace evidence files for this ISO")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_file_evidence(path: Path) -> tuple[int, str]:
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise ReleaseError(f"file changed while hashing: {path}")
    return after.st_size, digest


def parse_alpm_desc(path: Path) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    current: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ReleaseError(f"cannot read ALPM record {path}: {error}") from error
    for line in lines:
        if len(line) > 2 and line.startswith("%") and line.endswith("%"):
            current = line[1:-1]
            fields.setdefault(current, [])
        elif line and current is not None:
            fields[current].append(line)
    return fields


def one(fields: dict[str, list[str]], key: str, path: Path) -> str:
    values = fields.get(key, [])
    if len(values) != 1:
        raise ReleaseError(f"ALPM record {path} must contain exactly one %{key}% value")
    return values[0]


def optional_integer(fields: dict[str, list[str]], key: str, path: Path) -> int | None:
    values = fields.get(key, [])
    if not values:
        return None
    if len(values) != 1:
        raise ReleaseError(f"ALPM record {path} contains multiple %{key}% values")
    try:
        return int(values[0], 10)
    except ValueError as error:
        raise ReleaseError(f"ALPM record {path} has a non-integer %{key}% value") from error


def find_package_archive(cache_files: list[Path], name: str, version: str, architecture: str) -> Path:
    filename_versions = {version, version.split(":", 1)[-1]}
    expected = {
        f"{name}-{filename_version}-{architecture}.pkg.tar.{suffix}"
        for filename_version in filename_versions
        for suffix in PACKAGE_ARCHIVE_SUFFIXES
    }
    matches = [path for path in cache_files if path.name in expected]
    if len(matches) != 1:
        names = ", ".join(path.name for path in matches) or "none"
        raise ReleaseError(
            f"package cache must contain exactly one archive for {name} {version} {architecture}; found: {names}"
        )
    return matches[0]


def package_manifest(alpm_root: Path, package_cache: Path) -> list[dict[str, Any]]:
    database = alpm_root / "var/lib/pacman/local"
    if not database.is_dir():
        raise ReleaseError(f"ALPM local database not found: {database}")
    if not package_cache.is_dir():
        raise ReleaseError(f"package cache not found: {package_cache}")
    cache_files = sorted(path for path in package_cache.iterdir() if path.is_file())
    packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for desc in sorted(database.glob("*/desc")):
        fields = parse_alpm_desc(desc)
        name = one(fields, "NAME", desc)
        if name in seen:
            raise ReleaseError(f"duplicate installed package record: {name}")
        seen.add(name)
        version = one(fields, "VERSION", desc)
        architecture = one(fields, "ARCH", desc)
        archive = find_package_archive(cache_files, name, version, architecture)
        archive_bytes, archive_sha256 = stable_file_evidence(archive)
        package: dict[str, Any] = {
            "name": name,
            "version": version,
            "architecture": architecture,
            "package_file": archive.name,
            "package_bytes": archive_bytes,
            "package_sha256": archive_sha256,
            "database_record_sha256": sha256_file(desc),
        }
        base = fields.get("BASE", [])
        if base:
            package["base"] = one(fields, "BASE", desc)
        build_date = optional_integer(fields, "BUILDDATE", desc)
        if build_date is not None:
            package["build_date"] = build_date
        packager = fields.get("PACKAGER", [])
        if packager:
            package["packager"] = one(fields, "PACKAGER", desc)
        validation = fields.get("VALIDATION", [])
        if validation:
            package["validation"] = validation
        packages.append(package)
    if not packages:
        raise ReleaseError(f"ALPM local database contains no package records: {database}")
    return sorted(packages, key=lambda package: package["name"])


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sign_checksum(checksum: Path, signature: Path, key: str) -> str:
    if shutil.which("gpg") is None:
        raise ReleaseError("--gpg-key requires gpg")
    completed = subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--no-tty",
            "--status-fd=1",
            "--local-user",
            key,
            "--armor",
            "--detach-sign",
            "--output",
            str(signature),
            str(checksum),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        signature.unlink(missing_ok=True)
        detail = completed.stderr.strip() or "gpg failed without an error message"
        raise ReleaseError(f"could not sign checksum: {detail}")
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[:2] == ["[GNUPG:]", "SIG_CREATED"]:
            return fields[-1]
    signature.unlink(missing_ok=True)
    raise ReleaseError("gpg created no verifiable SIG_CREATED status record")


def main() -> int:
    args = parse_args()
    iso = args.iso.resolve()
    output_dir = args.output_dir.resolve()
    source_revision = args.source_revision.lower()
    if not iso.is_file() or iso.suffix != ".iso":
        print(f"generate-release-metadata.py: ISO does not exist or lacks .iso suffix: {iso}", file=sys.stderr)
        return 2
    if not SOURCE_REVISION.fullmatch(source_revision):
        print("generate-release-metadata.py: --source-revision must be an exact 40- or 64-hex commit", file=sys.stderr)
        return 2
    if args.gpg_key and not SOURCE_REVISION.fullmatch(args.gpg_key):
        print("generate-release-metadata.py: --gpg-key must be a full 40- or 64-hex fingerprint", file=sys.stderr)
        return 2
    profile_version = default_version()
    if not RELEASE_VERSION.fullmatch(args.version):
        print("generate-release-metadata.py: --version must be a semantic release version", file=sys.stderr)
        return 2
    if args.version != profile_version:
        print(
            f"generate-release-metadata.py: --version {args.version} does not match profile VERSION_ID {profile_version}",
            file=sys.stderr,
        )
        return 2
    expected_name = f"bifrost-{args.version}-x86_64.iso"
    if iso.name != expected_name:
        print(f"generate-release-metadata.py: ISO filename must be {expected_name}", file=sys.stderr)
        return 2

    artifact_stem = iso.name[:-4]
    package_path = output_dir / f"{artifact_stem}.packages.json"
    build_path = output_dir / f"{artifact_stem}.build.json"
    checksum_suffix = ".sha256" if args.gpg_key else ".sha256.unsigned"
    checksum_path = output_dir / f"{iso.name}{checksum_suffix}"
    signature_path = output_dir / f"{iso.name}.sha256.asc"
    alternate_checksum = output_dir / f"{iso.name}{'.sha256.unsigned' if args.gpg_key else '.sha256'}"
    managed = (package_path, build_path, checksum_path, signature_path, alternate_checksum)
    written: list[Path] = []

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        existing = [path.name for path in managed if path.exists()]
        if existing and not args.force:
            raise ReleaseError(f"refusing to replace existing output(s): {', '.join(existing)}; use --force")
        if args.force:
            for path in managed:
                path.unlink(missing_ok=True)

        packages = package_manifest(args.alpm_root.resolve(), args.package_cache.resolve())
        created_at = datetime.fromtimestamp(args.source_date_epoch, timezone.utc).isoformat().replace("+00:00", "Z")
        package_document = {
            "schema_version": 1,
            "bifrost_version": args.version,
            "source_revision": source_revision,
            "source_date_epoch": args.source_date_epoch,
            "generated_at": created_at,
            "package_count": len(packages),
            "packages": packages,
        }
        write_bytes(package_path, json_bytes(package_document))
        written.append(package_path)

        iso_bytes, iso_hash = stable_file_evidence(iso)
        checksum_line = f"{iso_hash}  {iso.name}\n".encode("ascii")
        write_bytes(checksum_path, checksum_line)
        written.append(checksum_path)

        if args.gpg_key:
            signer_fingerprint = sign_checksum(checksum_path, signature_path, args.gpg_key)
            written.append(signature_path)
            signature = {
                "status": "signed",
                "checksum_file": checksum_path.name,
                "detached_signature_file": signature_path.name,
                "signer_fingerprint": signer_fingerprint,
            }
        else:
            signature = {
                "status": "unsigned",
                "checksum_file": checksum_path.name,
                "detached_signature_file": None,
            }

        build_document = {
            "schema_version": 1,
            "bifrost_version": args.version,
            "source_revision": source_revision,
            "source_date_epoch": args.source_date_epoch,
            "generated_at": created_at,
            "iso": {
                "file": iso.name,
                "bytes": iso_bytes,
                "sha256": iso_hash,
            },
            "package_manifest": {
                "file": package_path.name,
                "package_count": len(packages),
                "sha256": sha256_file(package_path),
            },
            "signature": signature,
        }
        write_bytes(build_path, json_bytes(build_document))
        written.append(build_path)
    except (OSError, ReleaseError) as error:
        for path in reversed(written):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        print(f"generate-release-metadata.py: {error}", file=sys.stderr)
        return 1

    print(build_path)
    print(package_path)
    print(checksum_path)
    if args.gpg_key:
        print(signature_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
