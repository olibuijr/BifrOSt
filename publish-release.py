#!/usr/bin/env python3
"""Verify and publish an immutable BifrOSt GitHub release without moving or replacing anything."""

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
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"
REVISION = re.compile(r"^[0-9a-f]{40}$")
FINGERPRINT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class PublicationError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument("--tag", required=True, help="existing signed annotated release tag")
    parser.add_argument("--source-revision", required=True, help="exact 40-hex commit revision")
    parser.add_argument(
        "--signer-fingerprint",
        required=True,
        help="trusted full primary signing-key fingerprint",
    )
    parser.add_argument("--asset-dir", required=True, type=Path, help="directory containing final evidence and ISO")
    parser.add_argument(
        "--qemu-evidence-dir",
        required=True,
        type=Path,
        help="successful exact-ISO standard and LUKS2 qualification evidence",
    )
    parser.add_argument("--notes-file", required=True, type=Path, help="release notes passed unchanged to GitHub")
    return parser.parse_args()


def canonical_version() -> str:
    try:
        lines = VERSION_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PublicationError(f"cannot read VERSION: {error}") from error
    if len(lines) != 1 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", lines[0]):
        raise PublicationError("VERSION must contain exactly one semantic release version")
    return lines[0]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key {key}")
            value[key] = item
        return value

    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source, object_pairs_hook=unique)
    except (OSError, UnicodeError, ValueError) as error:
        raise PublicationError(f"cannot read {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise PublicationError(f"{path.name} must contain a JSON object")
    return value


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
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
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed without an error message"
        raise PublicationError(f"{' '.join(command)} failed: {detail}")
    return completed.stdout.strip()


def verify_local_source(tag: str, revision: str, signer_fingerprint: str) -> None:
    head = run(["git", "rev-parse", "--verify", "HEAD^{commit}"]).lower()
    tagged = run(["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"]).lower()
    tag_type = run(["git", "cat-file", "-t", f"refs/tags/{tag}"])
    if tag_type != "tag":
        raise PublicationError(f"refusing lightweight or missing release tag: {tag}")
    verified = subprocess.run(
        ["git", "verify-tag", "--raw", tag],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if verified.returncode:
        detail = verified.stderr.strip() or verified.stdout.strip() or "signature verification failed"
        raise PublicationError(f"git verify-tag --raw {tag} failed: {detail}")
    valid_lines = [
        line.split()
        for line in (verified.stdout + "\n" + verified.stderr).splitlines()
        if line.startswith("[GNUPG:] VALIDSIG ")
    ]
    if len(valid_lines) != 1:
        raise PublicationError(f"tag {tag} did not produce exactly one VALIDSIG record")
    tag_fingerprints = {
        field.lower()
        for field in valid_lines[0][2:]
        if FINGERPRINT.fullmatch(field.lower())
    }
    if signer_fingerprint not in tag_fingerprints:
        raise PublicationError(
            f"tag {tag} is not rooted in trusted primary fingerprint {signer_fingerprint}"
        )
    if head != revision:
        raise PublicationError(f"checked-out HEAD {head} does not equal source revision {revision}")
    if tagged != revision:
        raise PublicationError(f"tag {tag} resolves to {tagged}, not source revision {revision}")


def github_request(repository: str, path: str, token: str) -> tuple[int, Any]:
    request = Request(
        f"https://api.github.com/repos/{repository}/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bifrost-release-publisher",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        raw = error.read()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        return error.code, body
    except (OSError, URLError) as error:
        raise PublicationError(f"GitHub API request failed: {error}") from error


def verify_remote_tag(repository: str, tag: str, revision: str, token: str) -> None:
    status, reference = github_request(repository, f"git/ref/tags/{quote(tag, safe='')}", token)
    if status != 200 or not isinstance(reference, dict):
        raise PublicationError(f"remote tag {tag} is unavailable (GitHub HTTP {status})")
    target = reference.get("object")
    if not isinstance(target, dict) or target.get("type") != "tag" or not isinstance(target.get("sha"), str):
        raise PublicationError(f"remote tag {tag} must be annotated; lightweight tags are refused")
    status, tag_object = github_request(repository, f"git/tags/{target['sha']}", token)
    if status != 200 or not isinstance(tag_object, dict):
        raise PublicationError(f"cannot resolve remote annotated tag {tag} (GitHub HTTP {status})")
    verification = tag_object.get("verification")
    tagged_object = tag_object.get("object")
    if not isinstance(verification, dict) or verification.get("verified") is not True:
        reason = verification.get("reason") if isinstance(verification, dict) else "missing verification"
        raise PublicationError(f"remote tag {tag} does not have a verified signature: {reason}")
    if not isinstance(tagged_object, dict) or tagged_object.get("type") != "commit":
        raise PublicationError(f"remote tag {tag} does not directly reference a commit")
    if str(tagged_object.get("sha", "")).lower() != revision:
        raise PublicationError(f"remote tag {tag} does not resolve to source revision {revision}")


def require_no_release(repository: str, tag: str, token: str) -> None:
    status, release = github_request(repository, f"releases/tags/{quote(tag, safe='')}", token)
    if status == 404:
        return
    if status == 200:
        immutable = release.get("immutable") if isinstance(release, dict) else None
        suffix = " (immutable)" if immutable else ""
        raise PublicationError(f"release {tag} already exists{suffix}; replacement is forbidden")
    raise PublicationError(f"cannot prove release {tag} is absent (GitHub HTTP {status})")


def nested(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise PublicationError(f"release evidence is missing {'.'.join(keys)}")
        current = current[key]
    return current


def verify_signature(content: Path, signature: Path, expected_fingerprint: str) -> None:
    if shutil.which("gpg") is None:
        raise PublicationError("gpg is required to verify release signatures")
    completed = subprocess.run(
        ["gpg", "--batch", "--no-tty", "--status-fd=1", "--verify", str(signature), str(content)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or "gpg rejected the signature"
        raise PublicationError(f"invalid detached signature {signature.name}: {detail}")
    valid_lines = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[:2] == ["[GNUPG:]", "VALIDSIG"]:
            valid_lines.append(fields)
    if len(valid_lines) != 1:
        raise PublicationError(f"signature {signature.name} did not produce exactly one VALIDSIG record")
    signature_fingerprints = {
        field.lower()
        for field in valid_lines[0][2:]
        if FINGERPRINT.fullmatch(field.lower())
    }
    if expected_fingerprint not in signature_fingerprints:
        raise PublicationError(
            f"signature {signature.name} is not rooted in expected primary fingerprint {expected_fingerprint}"
        )


def verify_assets(
    asset_dir: Path,
    version: str,
    tag: str,
    revision: str,
    trusted_signer: str,
) -> list[Path]:
    stem = f"bifrost-{version}-x86_64"
    expected_names = [
        f"{stem}.iso",
        f"{stem}.iso.sha256",
        f"{stem}.iso.sha256.asc",
        f"{stem}.packages.json",
        f"{stem}.build.json",
        f"{stem}.toolchain.json",
        f"{stem}.release.json",
        f"{stem}.release.json.asc",
    ]
    assets = [asset_dir / name for name in expected_names]
    missing = [path.name for path in assets if not path.is_file()]
    if missing:
        raise PublicationError(f"release asset set is incomplete: {', '.join(missing)}")

    iso, checksum, checksum_signature, packages, build_path, toolchain, attestation_path, attestation_signature = assets
    attestation = load_json(attestation_path)
    build = load_json(build_path)
    package_document = load_json(packages)
    toolchain_document = load_json(toolchain)

    if attestation.get("provenance_status") != "signed":
        raise PublicationError("final attestation must have provenance_status=signed")
    if attestation.get("version") != version or attestation.get("source_revision") != revision:
        raise PublicationError("attestation version/source revision does not match publication request")
    if attestation.get("source_tag") != tag:
        raise PublicationError("attestation source tag does not match publication tag")
    try:
        commit_epoch = int(run(["git", "show", "-s", "--format=%ct", revision]))
    except ValueError as error:
        raise PublicationError("tagged commit has an invalid timestamp") from error
    if attestation.get("source_date_epoch") != commit_epoch:
        raise PublicationError("attestation source epoch does not equal the tagged commit timestamp")
    if build.get("bifrost_version") != version or build.get("source_revision") != revision or build.get("source_tag") != tag:
        raise PublicationError("build metadata version/source identity does not match publication request")
    if nested(build, "attestation", "status") != "signed" or nested(build, "checksum_signature", "status") != "signed":
        raise PublicationError("build metadata must mark attestation and checksum as signed")
    if (
        package_document.get("bifrost_version") != version
        or package_document.get("source_revision") != revision
        or package_document.get("source_date_epoch") != attestation.get("source_date_epoch")
    ):
        raise PublicationError("package manifest source identity does not match the signed attestation")
    if not toolchain_document:
        raise PublicationError("toolchain manifest must be a non-empty JSON object")
    profile_hash = str(attestation.get("profile_sha256", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", profile_hash):
        raise PublicationError("attestation profile_sha256 must be a SHA-256 digest")

    signer = str(nested(attestation, "evidence", "signer_fingerprint")).lower()
    if not FINGERPRINT.fullmatch(signer):
        raise PublicationError("attestation signer_fingerprint must be a full fingerprint")
    if signer != trusted_signer:
        raise PublicationError(
            f"attestation signer {signer} does not equal trusted --signer-fingerprint {trusted_signer}"
        )
    if str(nested(build, "attestation", "signer_fingerprint")).lower() != signer:
        raise PublicationError("build and attestation signer fingerprints differ")
    if str(nested(build, "checksum_signature", "signer_fingerprint")).lower() != signer:
        raise PublicationError("checksum and attestation signer fingerprints differ")
    evidence_names = {
        "attestation_file": attestation_path.name,
        "checksum_file": checksum.name,
        "checksum_signature_file": checksum_signature.name,
        "detached_signature_file": attestation_signature.name,
    }
    for key, filename in evidence_names.items():
        if nested(attestation, "evidence", key) != filename:
            raise PublicationError(f"attestation {key} does not match the asset")
    if (
        nested(build, "attestation", "file") != attestation_path.name
        or nested(build, "attestation", "detached_signature_file") != attestation_signature.name
        or nested(build, "checksum_signature", "checksum_file") != checksum.name
        or nested(build, "checksum_signature", "detached_signature_file") != checksum_signature.name
    ):
        raise PublicationError("build metadata signature filenames do not match the assets")

    iso_hash = sha256_file(iso)
    if nested(attestation, "iso", "file") != iso.name or nested(attestation, "iso", "sha256") != iso_hash:
        raise PublicationError("attestation ISO identity/hash does not match the ISO asset")
    if nested(attestation, "iso", "bytes") != iso.stat().st_size:
        raise PublicationError("attestation ISO byte count does not match the ISO asset")
    if nested(build, "iso", "sha256") != iso_hash or nested(build, "iso", "file") != iso.name:
        raise PublicationError("build metadata ISO identity/hash does not match the ISO asset")
    if nested(build, "iso", "volume_id") != nested(attestation, "iso", "volume_id"):
        raise PublicationError("build metadata and attestation ISO volume identities differ")
    expected_checksum = f"{iso_hash}  {iso.name}\n"
    try:
        checksum_content = checksum.read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise PublicationError(f"cannot read checksum: {error}") from error
    if checksum_content != expected_checksum:
        raise PublicationError("checksum asset is not the exact SHA-256 record for the ISO")

    bindings = {
        "build_metadata_file": (build_path.name, sha256_file(build_path), "build_metadata_sha256"),
        "package_manifest_file": (packages.name, sha256_file(packages), "package_manifest_sha256"),
        "toolchain_manifest_file": (toolchain.name, sha256_file(toolchain), "toolchain_manifest_sha256"),
    }
    for filename_key, (filename, digest, digest_key) in bindings.items():
        if nested(attestation, "evidence", filename_key) != filename:
            raise PublicationError(f"attestation {filename_key} does not match the asset")
        if nested(attestation, "evidence", digest_key) != digest:
            raise PublicationError(f"attestation {digest_key} does not match the asset")
    if nested(build, "package_manifest", "sha256") != sha256_file(packages):
        raise PublicationError("build metadata package-manifest digest does not match the asset")
    if nested(build, "toolchain_manifest", "sha256") != sha256_file(toolchain):
        raise PublicationError("build metadata toolchain digest does not match the asset")
    if nested(build, "package_manifest", "file") != packages.name:
        raise PublicationError("build metadata package-manifest filename does not match the asset")
    if nested(build, "toolchain_manifest", "file") != toolchain.name:
        raise PublicationError("build metadata toolchain filename does not match the asset")
    if nested(build, "profile", "sha256") != attestation.get("profile_sha256"):
        raise PublicationError("build metadata and attestation profile digests differ")
    if build.get("source_date_epoch") != attestation.get("source_date_epoch"):
        raise PublicationError("build metadata and attestation source epochs differ")
    if build.get("build_id") != attestation.get("build_id"):
        raise PublicationError("build metadata and attestation build identities differ")

    verify_signature(checksum, checksum_signature, signer)
    verify_signature(attestation_path, attestation_signature, signer)
    return assets

def verify_qemu_evidence(evidence_dir: Path, version: str, iso: Path) -> None:
    if not evidence_dir.is_dir() or evidence_dir.is_symlink():
        raise PublicationError("--qemu-evidence-dir must be a real directory")
    result = load_json(evidence_dir / "result.json")
    expected_cases = ["standard", "luks2"]
    if (
        result.get("schema_version") != 1
        or result.get("version") != version
        or result.get("status") != "passed"
        or result.get("cases") != expected_cases
    ):
        raise PublicationError("QEMU evidence is not one successful all-case qualification")
    iso_record = result.get("iso")
    if (
        not isinstance(iso_record, dict)
        or Path(str(iso_record.get("path", ""))).name != iso.name
        or iso_record.get("bytes") != iso.stat().st_size
        or iso_record.get("sha256") != sha256_file(iso)
    ):
        raise PublicationError("QEMU evidence does not identify the exact release ISO")
    results = result.get("results")
    if not isinstance(results, list) or len(results) != len(expected_cases):
        raise PublicationError("QEMU evidence does not contain both required case results")
    by_case = {
        item.get("case"): item
        for item in results
        if isinstance(item, dict) and isinstance(item.get("case"), str)
    }
    if set(by_case) != set(expected_cases):
        raise PublicationError("QEMU evidence contains missing, duplicate, or unexpected cases")
    for case in expected_cases:
        record = by_case[case]
        if record.get("status") != "passed":
            raise PublicationError(f"QEMU {case} qualification did not pass")
    if by_case["luks2"].get("wrong_luks_passphrase_rejected") is not True:
        raise PublicationError("QEMU LUKS2 evidence does not prove wrong-passphrase rejection")



def publish(repository: str, tag: str, notes: Path, assets: list[Path], token: str) -> None:
    if shutil.which("gh") is None:
        raise PublicationError("gh is required to publish the verified release")
    if not notes.is_file():
        raise PublicationError(f"release notes file does not exist: {notes}")
    environment = os.environ.copy()
    environment["GH_TOKEN"] = token
    create_command = [
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        repository,
        "--verify-tag",
        "--draft",
        "--title",
        f"BifrOSt {tag.removeprefix('v')}",
        "--notes-file",
        str(notes.resolve()),
        "--latest=false",
        *[str(path.resolve()) for path in assets],
    ]
    run(create_command, env=environment)
    status, draft = github_request(repository, f"releases/tags/{quote(tag, safe='')}", token)
    if status != 200 or not isinstance(draft, dict) or draft.get("draft") is not True:
        raise PublicationError("uploaded release could not be proven to remain a draft")
    remote_assets = draft.get("assets")
    if not isinstance(remote_assets, list) or len(remote_assets) != len(assets):
        raise PublicationError("draft release asset count differs from the verified local set")
    remote_by_name = {
        item.get("name"): item
        for item in remote_assets
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for asset in assets:
        remote = remote_by_name.get(asset.name)
        if not isinstance(remote, dict):
            raise PublicationError(f"draft release is missing uploaded asset {asset.name}")
        if remote.get("size") != asset.stat().st_size:
            raise PublicationError(f"uploaded asset size differs for {asset.name}")
        if remote.get("digest") != f"sha256:{sha256_file(asset)}":
            raise PublicationError(f"GitHub digest differs for uploaded asset {asset.name}")
    run(
        [
            "gh",
            "release",
            "edit",
            tag,
            "--repo",
            repository,
            "--draft=false",
            "--latest=false",
        ],
        env=environment,
    )
    status, published = github_request(repository, f"releases/tags/{quote(tag, safe='')}", token)
    if status != 200 or not isinstance(published, dict) or published.get("draft") is not False:
        raise PublicationError("release publication could not be confirmed")


def main() -> int:
    args = parse_args()
    try:
        version = canonical_version()
        expected_tag = f"v{version}"
        repository = args.repository
        tag = args.tag
        revision = args.source_revision.lower()
        signer_fingerprint = args.signer_fingerprint.lower()
        if not REPOSITORY.fullmatch(repository):
            raise PublicationError("--repository must be owner/repository")
        if tag != expected_tag:
            raise PublicationError(f"--tag must equal canonical version tag {expected_tag}")
        if not REVISION.fullmatch(revision):
            raise PublicationError("--source-revision must be an exact lowercase/uppercase 40-hex commit")
        if not FINGERPRINT.fullmatch(signer_fingerprint):
            raise PublicationError("--signer-fingerprint must be a full 40- or 64-hex fingerprint")
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            raise PublicationError("GITHUB_TOKEN or GH_TOKEN is required")

        verify_local_source(tag, revision, signer_fingerprint)
        verify_remote_tag(repository, tag, revision, token)
        require_no_release(repository, tag, token)
        assets = verify_assets(args.asset_dir.resolve(), version, tag, revision, signer_fingerprint)
        verify_qemu_evidence(args.qemu_evidence_dir.resolve(), version, assets[0])
        require_no_release(repository, tag, token)
        publish(repository, tag, args.notes_file, assets, token)
        print(f"published verified release {repository} {tag} without replacing existing state")
        return 0
    except (OSError, PublicationError) as error:
        print(f"publish-release.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
