#!/usr/bin/env python3
"""Prepare installed provenance or generate deterministic BifrOSt release evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"
DEFAULT_PROFILE = ROOT / "profile"
INSTALLED_RELEASE_RELATIVE = Path("airootfs/usr/share/bifrost/installed-root/usr/share/bifrost/release.json")
GENERATED_ALPM_RELATIVE = Path("airootfs/usr/share/bifrost/alpm")
SOURCE_REVISION = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
RELEASE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
TAG_NAME = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
PACKAGE_ARCHIVE_SUFFIXES = ("zst", "xz", "gz", "bz2", "lrz", "lzo", "Z")


class ReleaseError(Exception):
    pass


def canonical_version() -> str:
    try:
        lines = VERSION_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ReleaseError(f"cannot read canonical VERSION: {error}") from error
    if len(lines) != 1 or not RELEASE_VERSION.fullmatch(lines[0]):
        raise ReleaseError("VERSION must contain exactly one semantic release version")
    return lines[0]


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
            "Local development evidence is explicitly unsigned. --final requires an existing secret key, "
            "a signed annotated source tag, and a toolchain manifest; no key is created or exported. "
            "Existing outputs are never replaced."
        ),
    )
    parser.add_argument("--iso", type=Path, help="completed .iso artifact")
    parser.add_argument(
        "--alpm-root",
        type=Path,
        help="root filesystem whose var/lib/pacman/local database records the ISO package set",
    )
    parser.add_argument(
        "--package-cache",
        type=Path,
        help="complete package archive directory matching every installed ALPM record",
    )
    parser.add_argument("--output-dir", type=Path, help="directory for generated evidence")
    parser.add_argument(
        "--prepare-installed",
        type=Path,
        metavar="PATH",
        help="write build-input provenance to PATH before assembling the ISO, then exit",
    )
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=DEFAULT_PROFILE,
        help="ArchISO profile to bind (default: repository profile directory)",
    )
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
        help="must equal the canonical root VERSION when supplied",
    )
    parser.add_argument("--final", action="store_true", help="produce publication-grade signed evidence")
    parser.add_argument("--source-tag", help="signed annotated release tag; required with --final")
    parser.add_argument(
        "--toolchain-manifest",
        type=Path,
        help="JSON build-toolchain manifest; required with --final and copied into evidence",
    )
    parser.add_argument(
        "--gpg-key",
        metavar="FINGERPRINT",
        help="existing full GPG secret-key fingerprint; required with --final",
    )
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


def profile_digest(profile_root: Path) -> str:
    if not profile_root.is_dir():
        raise ReleaseError(f"profile root not found: {profile_root}")
    digest = hashlib.sha256()
    excluded_file = INSTALLED_RELEASE_RELATIVE.as_posix()
    excluded_tree = GENERATED_ALPM_RELATIVE.as_posix()

    def is_excluded(relative: str) -> bool:
        return relative == excluded_file or relative == excluded_tree or relative.startswith(excluded_tree + "/")

    entries = sorted(profile_root.rglob("*"), key=lambda path: path.relative_to(profile_root).as_posix())
    for path in entries:
        relative = path.relative_to(profile_root).as_posix()
        if is_excluded(relative):
            continue
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            kind = "symlink"
            content = os.readlink(path).encode("utf-8")
            digest.update(f"{kind}\0{mode:o}\0{relative}\0{len(content)}\0".encode("utf-8"))
            digest.update(content)
        elif path.is_dir():
            digest.update(f"directory\0{mode:o}\0{relative}\0{0}\0".encode("utf-8"))
        elif path.is_file():
            digest.update(f"file\0{mode:o}\0{relative}\0{info.st_size}\0".encode("utf-8"))
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = path.lstat()
            identity_before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if identity_before != identity_after:
                raise ReleaseError(f"profile file changed while hashing: {path}")
        else:
            raise ReleaseError(f"unsupported profile entry: {path}")
        digest.update(b"\0")
    names_before = [
        relative
        for path in entries
        if not is_excluded(relative := path.relative_to(profile_root).as_posix())
    ]
    names_after = sorted(
        relative
        for path in profile_root.rglob("*")
        if not is_excluded(relative := path.relative_to(profile_root).as_posix())
    )
    if names_before != names_after:
        raise ReleaseError("profile entries changed while hashing")
    return digest.hexdigest()


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
        package_version = one(fields, "VERSION", desc)
        architecture = one(fields, "ARCH", desc)
        archive = find_package_archive(cache_files, name, package_version, architecture)
        archive_bytes, archive_sha256 = stable_file_evidence(archive)
        package: dict[str, Any] = {
            "name": name,
            "version": package_version,
            "architecture": architecture,
            "package_file": archive.name,
            "package_bytes": archive_bytes,
            "package_sha256": archive_sha256,
            "database_record_sha256": sha256_file(desc),
        }
        if fields.get("BASE"):
            package["base"] = one(fields, "BASE", desc)
        build_date = optional_integer(fields, "BUILDDATE", desc)
        if build_date is not None:
            package["build_date"] = build_date
        if fields.get("PACKAGER"):
            package["packager"] = one(fields, "PACKAGER", desc)
        if fields.get("VALIDATION"):
            package["validation"] = fields["VALIDATION"]
        packages.append(package)
    if not packages:
        raise ReleaseError(f"ALPM local database contains no package records: {database}")
    return sorted(packages, key=lambda package: package["name"])


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if path.exists():
        raise ReleaseError(f"refusing to replace existing output: {path}")
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if path.exists():
            raise ReleaseError(f"refusing to replace concurrently created output: {path}")
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def write_prepared(path: Path, content: bytes, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReleaseError(f"refusing to replace unreadable installed provenance template {path}: {error}") from error
        if (
            not isinstance(current, dict)
            or current.get("provenance_status") != "unsigned-development"
            or current.get("version") != version
            or current.get("source_revision") is not None
            or current.get("build_id") is not None
        ):
            raise ReleaseError(
                f"refusing to replace installed provenance that is not the canonical unsigned template: {path}"
            )
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_git(arguments: list[str]) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed without an error message"
        raise ReleaseError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def verify_final_source(
    version: str,
    source_revision: str,
    source_tag: str,
    source_date_epoch: int,
    signer_fingerprint: str,
) -> None:
    expected_tag = f"v{version}"
    if source_tag != expected_tag or not TAG_NAME.fullmatch(source_tag):
        raise ReleaseError(f"final source tag must be exactly {expected_tag}")
    head = run_git(["rev-parse", "--verify", "HEAD^{commit}"]).lower()
    tagged = run_git(["rev-parse", "--verify", f"refs/tags/{source_tag}^{{commit}}"]).lower()
    object_type = run_git(["cat-file", "-t", f"refs/tags/{source_tag}"])
    if object_type != "tag":
        raise ReleaseError(f"final source tag must be annotated and signed: {source_tag}")
    verified = subprocess.run(
        ["git", "verify-tag", "--raw", source_tag],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if verified.returncode:
        detail = verified.stderr.strip() or verified.stdout.strip() or "signature verification failed"
        raise ReleaseError(f"git verify-tag --raw {source_tag} failed: {detail}")
    valid_lines = [
        line.split()
        for line in (verified.stdout + "\n" + verified.stderr).splitlines()
        if line.startswith("[GNUPG:] VALIDSIG ")
    ]
    if len(valid_lines) != 1:
        raise ReleaseError(f"tag {source_tag} did not produce exactly one VALIDSIG record")
    tag_fingerprints = {
        field.lower()
        for field in valid_lines[0][2:]
        if SOURCE_REVISION.fullmatch(field)
    }
    if signer_fingerprint not in tag_fingerprints:
        raise ReleaseError(
            f"tag {source_tag} is not rooted in requested signing fingerprint {signer_fingerprint}"
        )
    commit_epoch = run_git(["show", "-s", "--format=%ct", source_revision])
    if commit_epoch != str(source_date_epoch):
        raise ReleaseError(
            f"SOURCE_DATE_EPOCH {source_date_epoch} must equal tagged commit timestamp {commit_epoch}"
        )
    if head != source_revision:
        raise ReleaseError(f"source revision {source_revision} does not equal checked-out HEAD {head}")
    if tagged != source_revision:
        raise ReleaseError(f"tag {source_tag} resolves to {tagged}, not source revision {source_revision}")


def gpg_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    if shutil.which("gpg") is None:
        raise ReleaseError("signing requires gpg")
    return subprocess.run(
        ["gpg", "--batch", "--no-tty", *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def secret_key_fingerprint(key: str) -> str:
    if not SOURCE_REVISION.fullmatch(key):
        raise ReleaseError("--gpg-key must be a full 40- or 64-hex fingerprint")
    completed = gpg_command(["--with-colons", "--fingerprint", "--list-secret-keys", key])
    if completed.returncode:
        detail = completed.stderr.strip() or "secret key not found"
        raise ReleaseError(f"cannot use signing key {key}: {detail}")
    fingerprints = [line.split(":")[9].lower() for line in completed.stdout.splitlines() if line.startswith("fpr:")]
    expected = key.lower()
    if expected not in fingerprints:
        raise ReleaseError(f"gpg did not resolve the requested secret-key fingerprint exactly: {key}")
    return expected


def sign_detached(content: Path, signature: Path, key: str) -> str:
    if signature.exists():
        raise ReleaseError(f"refusing to replace existing signature: {signature}")
    completed = gpg_command(
        [
            "--yes",
            "--status-fd=1",
            "--local-user",
            key,
            "--armor",
            "--detach-sign",
            "--output",
            str(signature),
            str(content),
        ]
    )
    if completed.returncode:
        signature.unlink(missing_ok=True)
        detail = completed.stderr.strip() or "gpg failed without an error message"
        raise ReleaseError(f"could not sign {content.name}: {detail}")
    created_fingerprint = None
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[:2] == ["[GNUPG:]", "SIG_CREATED"]:
            created_fingerprint = fields[-1].lower()
            break
    if created_fingerprint:
        listed = gpg_command(["--with-colons", "--fingerprint", "--list-keys", key])
        key_fingerprints = {
            line.split(":")[9].lower()
            for line in listed.stdout.splitlines()
            if line.startswith("fpr:")
        }
        if listed.returncode == 0 and created_fingerprint in key_fingerprints:
            return key.lower()
    signature.unlink(missing_ok=True)
    raise ReleaseError(f"gpg signature for {content.name} was not made by the requested primary key or its subkey")


def iso_identity(version: str, epoch: int) -> tuple[str, str]:
    date = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%d")
    token = re.sub(r"[^A-Z0-9]", "", version.upper())[:12]
    return f"bifrost-{version}-x86_64.iso", f"BIFROST_{token}_{date}"


def build_id(version: str, source_revision: str, epoch: int) -> str:
    return f"bifrost-{version}-{source_revision[:12]}-{epoch}"


def evidence_names(iso_name: str, signed: bool) -> dict[str, str | None]:
    stem = iso_name[:-4]
    return {
        "checksum_file": f"{iso_name}.sha256" if signed else f"{iso_name}.sha256.unsigned",
        "checksum_signature_file": f"{iso_name}.sha256.asc" if signed else None,
        "package_manifest_file": f"{stem}.packages.json",
        "build_metadata_file": f"{stem}.build.json",
        "toolchain_manifest_file": f"{stem}.toolchain.json" if signed else None,
        "attestation_file": f"{stem}.release.json",
        "detached_signature_file": f"{stem}.release.json.asc" if signed else None,
        "signer_fingerprint": None,
    }


def release_document(
    *,
    version: str,
    source_revision: str,
    epoch: int,
    profile_sha256: str,
    volume_id: str,
    iso_name: str,
    iso_bytes: int | None,
    iso_sha256: str | None,
    evidence: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "provenance_status": status,
        "version": version,
        "source_revision": source_revision,
        "source_date_epoch": epoch,
        "build_id": build_id(version, source_revision, epoch),
        "profile_sha256": profile_sha256,
        "profile_digest_excludes": [
            INSTALLED_RELEASE_RELATIVE.as_posix(),
            GENERATED_ALPM_RELATIVE.as_posix() + "/**",
        ],
        "iso": {
            "file": iso_name,
            "volume_id": volume_id,
            "bytes": iso_bytes,
            "sha256": iso_sha256,
        },
        "evidence": evidence,
    }


def prepare_installed(args: argparse.Namespace, version: str, source_revision: str, digest: str) -> int:
    if args.final or args.gpg_key or args.source_tag or args.toolchain_manifest:
        raise ReleaseError("--prepare-installed cannot be combined with final signing options")
    if any(value is not None for value in (args.iso, args.alpm_root, args.package_cache, args.output_dir)):
        raise ReleaseError("--prepare-installed cannot be combined with ISO evidence options")
    iso_name, volume_id = iso_identity(version, args.source_date_epoch)
    evidence = evidence_names(iso_name, signed=False)
    document = release_document(
        version=version,
        source_revision=source_revision,
        epoch=args.source_date_epoch,
        profile_sha256=digest,
        volume_id=volume_id,
        iso_name=iso_name,
        iso_bytes=None,
        iso_sha256=None,
        evidence=evidence,
        status="build-input",
    )
    write_prepared(args.prepare_installed.resolve(), json_bytes(document), version)
    print(args.prepare_installed.resolve())
    return 0


def load_toolchain(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseError(f"cannot read toolchain manifest {path}: {error}") from error
    if not isinstance(value, dict) or not value:
        raise ReleaseError("toolchain manifest must be a non-empty JSON object")
    canonical = json_bytes(value)
    return value, canonical


def generate(args: argparse.Namespace, version: str, source_revision: str, digest: str) -> int:
    required = {"--iso": args.iso, "--alpm-root": args.alpm_root, "--package-cache": args.package_cache, "--output-dir": args.output_dir}
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ReleaseError(f"ISO evidence mode requires: {', '.join(missing)}")
    if args.final:
        if not args.gpg_key or not args.source_tag or not args.toolchain_manifest:
            raise ReleaseError("--final requires --gpg-key, --source-tag, and --toolchain-manifest")
        signer_fingerprint = secret_key_fingerprint(args.gpg_key)
        verify_final_source(
            version,
            source_revision,
            args.source_tag,
            args.source_date_epoch,
            signer_fingerprint,
        )
    else:
        if args.gpg_key or args.source_tag or args.toolchain_manifest:
            raise ReleaseError("--gpg-key, --source-tag, and --toolchain-manifest are accepted only with --final")
        signer_fingerprint = None

    iso = args.iso.resolve()
    output_dir = args.output_dir.resolve()
    expected_name, volume_id = iso_identity(version, args.source_date_epoch)
    if not iso.is_file() or iso.suffix != ".iso":
        raise ReleaseError(f"ISO does not exist or lacks .iso suffix: {iso}")
    if iso.name != expected_name:
        raise ReleaseError(f"ISO filename must be {expected_name}")

    names = evidence_names(iso.name, signed=args.final)
    if signer_fingerprint:
        names["signer_fingerprint"] = signer_fingerprint
    paths = {key: output_dir / value for key, value in names.items() if key != "signer_fingerprint" and value is not None}
    managed = [iso if iso.parent == output_dir else None, *paths.values()]
    conflicts = sorted(path.name for path in managed if path is not None and path.exists() and path != iso)
    if conflicts:
        raise ReleaseError(f"refusing to replace existing output(s): {', '.join(conflicts)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        packages = package_manifest(args.alpm_root.resolve(), args.package_cache.resolve())
        created_at = datetime.fromtimestamp(args.source_date_epoch, timezone.utc).isoformat().replace("+00:00", "Z")
        package_document = {
            "schema_version": 1,
            "bifrost_version": version,
            "source_revision": source_revision,
            "source_date_epoch": args.source_date_epoch,
            "generated_at": created_at,
            "package_count": len(packages),
            "packages": packages,
        }
        package_path = paths["package_manifest_file"]
        write_new(package_path, json_bytes(package_document))
        written.append(package_path)

        iso_bytes, iso_hash = stable_file_evidence(iso)
        checksum_path = paths["checksum_file"]
        write_new(checksum_path, f"{iso_hash}  {iso.name}\n".encode("ascii"))
        written.append(checksum_path)

        toolchain_record: dict[str, Any] | None = None
        if args.toolchain_manifest:
            _toolchain, canonical_toolchain = load_toolchain(args.toolchain_manifest.resolve())
            toolchain_path = paths.get("toolchain_manifest_file")
            if toolchain_path is None:
                raise ReleaseError("toolchain manifests are publication evidence and require --final")
            write_new(toolchain_path, canonical_toolchain)
            written.append(toolchain_path)
            toolchain_record = {
                "file": toolchain_path.name,
                "sha256": sha256_file(toolchain_path),
            }

        build_document = {
            "schema_version": 2,
            "bifrost_version": version,
            "source_revision": source_revision,
            "source_tag": args.source_tag,
            "source_date_epoch": args.source_date_epoch,
            "build_id": build_id(version, source_revision, args.source_date_epoch),
            "generated_at": created_at,
            "profile": {
                "sha256": digest,
                "digest_excludes": [
                    INSTALLED_RELEASE_RELATIVE.as_posix(),
                    GENERATED_ALPM_RELATIVE.as_posix() + "/**",
                ],
            },
            "toolchain_manifest": toolchain_record,
            "iso": {
                "file": iso.name,
                "volume_id": volume_id,
                "bytes": iso_bytes,
                "sha256": iso_hash,
            },
            "package_manifest": {
                "file": package_path.name,
                "package_count": len(packages),
                "sha256": sha256_file(package_path),
            },
            "attestation": {
                "file": names["attestation_file"],
                "detached_signature_file": names["detached_signature_file"],
                "status": "signed" if args.final else "unsigned-development",
                "signer_fingerprint": signer_fingerprint,
            },
            "checksum_signature": {
                "checksum_file": checksum_path.name,
                "detached_signature_file": names["checksum_signature_file"],
                "status": "signed" if args.final else "unsigned-development",
                "signer_fingerprint": signer_fingerprint,
            },
        }
        build_path = paths["build_metadata_file"]
        write_new(build_path, json_bytes(build_document))
        written.append(build_path)

        attestation_evidence = dict(names)
        attestation_evidence.update(
            {
                "build_metadata_sha256": sha256_file(build_path),
                "package_manifest_sha256": sha256_file(package_path),
                "toolchain_manifest_sha256": toolchain_record["sha256"] if toolchain_record else None,
            }
        )
        attestation = release_document(
            version=version,
            source_revision=source_revision,
            epoch=args.source_date_epoch,
            profile_sha256=digest,
            volume_id=volume_id,
            iso_name=iso.name,
            iso_bytes=iso_bytes,
            iso_sha256=iso_hash,
            evidence=attestation_evidence,
            status="signed" if args.final else "unsigned-development",
        )
        attestation["source_tag"] = args.source_tag
        attestation_path = paths["attestation_file"]
        write_new(attestation_path, json_bytes(attestation))
        written.append(attestation_path)

        if args.final:
            checksum_signature = paths["checksum_signature_file"]
            if sign_detached(checksum_path, checksum_signature, args.gpg_key) != signer_fingerprint:
                raise ReleaseError("checksum signer fingerprint did not match the requested signing key")
            written.append(checksum_signature)
            attestation_signature = paths["detached_signature_file"]
            if sign_detached(attestation_path, attestation_signature, args.gpg_key) != signer_fingerprint:
                raise ReleaseError("attestation signer fingerprint did not match the requested signing key")
            written.append(attestation_signature)
    except (OSError, ReleaseError):
        for path in reversed(written):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    for path in written:
        print(path)
    return 0


def main() -> int:
    args = parse_args()
    try:
        version = canonical_version()
        if args.version is not None and args.version != version:
            raise ReleaseError(f"--version {args.version} does not match canonical VERSION {version}")
        source_revision = args.source_revision.lower()
        if not SOURCE_REVISION.fullmatch(source_revision):
            raise ReleaseError("--source-revision must be an exact 40- or 64-hex commit")
        digest = profile_digest(args.profile_root.resolve())
        if args.prepare_installed:
            return prepare_installed(args, version, source_revision, digest)
        return generate(args, version, source_revision, digest)
    except (OSError, ReleaseError) as error:
        print(f"generate-release-metadata.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
