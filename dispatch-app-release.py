#!/usr/bin/env python3
"""Import signed BifrOSt Flatpak app bundles and optionally publish the repository."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import stat
import sys
import tempfile
from typing import Iterable

FINGERPRINT = re.compile(r"^[0-9A-F]{40}$")
TRUSTED_APP_REF = re.compile(r"^(?:app|runtime)/org\.bifrost\.[A-Za-z0-9._-]+/[A-Za-z0-9_]+/[A-Za-z0-9._-]+$")
AUXILIARY_REF = re.compile(r"^appstream2?/[A-Za-z0-9_]+$")
DEFAULT_URL = "https://olibuijr.github.io/BifrOSt/flatpak/repo/"
DEFAULT_GITHUB_REPOSITORY = "olibuijr/BifrOSt"
PUBLIC_KEY = Path("profile/airootfs/usr/share/bifrost/installed-root/usr/share/bifrost/apps/app-release-key.asc")


class DispatchError(RuntimeError):
    pass

@dataclass(frozen=True)
class StagedBundle:
    source_name: str
    path: Path
    size: int
    sha256: str


def stage_bundle_files(bundles: list[Path], directory: Path) -> list[StagedBundle]:
    """Copy each caller-controlled bundle once into a private, read-only staging area."""
    os.chmod(directory, 0o700)
    staged: list[StagedBundle] = []
    open_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    for index, bundle in enumerate(bundles):
        if bundle.suffix != ".flatpak":
            raise DispatchError("bundle path must have a .flatpak suffix")
        try:
            source_descriptor = os.open(bundle, open_flags)
        except OSError as error:
            raise DispatchError("bundle must be an accessible regular, non-symlink file") from error
        destination = directory / f"bundle-{index:04d}.flatpak"
        try:
            source_stat = os.fstat(source_descriptor)
            if not stat.S_ISREG(source_stat.st_mode):
                raise DispatchError("bundle must be a regular, non-symlink file")
            destination_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
            try:
                with os.fdopen(source_descriptor, "rb", closefd=False) as source, os.fdopen(
                    destination_descriptor, "wb", closefd=False
                ) as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.close(destination_descriptor)
        finally:
            os.close(source_descriptor)
        os.chmod(destination, 0o400)
        size, digest = stable_sha256(destination)
        staged.append(StagedBundle(bundle.name, destination, size, digest))
    return staged


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "The command stages changes in a temporary OSTree repository and signs every imported commit "
            "and repository summary. Without --publish it never changes GitHub Pages."
        ),
    )
    parser.add_argument("--bundle", type=Path, action="append", default=[], help=".flatpak bundle to import; repeatable")
    parser.add_argument("--repository", type=Path, default=Path("release/flatpak-repo"), help="local signed repository")
    parser.add_argument(
        "--definition",
        type=Path,
        default=Path("release/bifrost.flatpakrepo"),
        help="generated .flatpakrepo definition",
    )
    parser.add_argument("--gpg-key", default=os.environ.get("BIFROST_APP_GPG_KEY"), help="full release-key fingerprint")
    parser.add_argument("--gpg-homedir", type=Path, default=Path.home() / ".gnupg", help="GnuPG home containing the secret key")
    parser.add_argument("--public-key", type=Path, default=PUBLIC_KEY, help="tracked public release key")
    parser.add_argument("--repository-url", default=DEFAULT_URL, help="public HTTPS repository URL")
    parser.add_argument("--allow-local-url", action="store_true", help="allow file:// URL for an isolated test repository")
    parser.add_argument("--publish", action="store_true", help="publish the signed repository to GitHub Pages")
    parser.add_argument("--github-repository", default=DEFAULT_GITHUB_REPOSITORY, help="OWNER/REPOSITORY for Pages")
    parser.add_argument("--pages-directory", type=Path, default=Path("release/pages"), help="temporary gh-pages checkout")
    return parser.parse_args()


def run(command: Iterable[str], *, cwd: Path | None = None, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        if not detail:
            detail = completed.stdout.decode("utf-8", "replace").strip()
        raise DispatchError(f"command failed ({' '.join(command)}): {detail or 'no details'}")
    return completed


def stable_sha256(path: Path) -> tuple[int, str]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise DispatchError(f"bundle changed while hashing: {path}")
    return after.st_size, digest.hexdigest()


def fingerprints(arguments: list[str], homedir: Path) -> set[str]:
    completed = run(["gpg", "--homedir", str(homedir), "--batch", "--with-colons", *arguments])
    values: set[str] = set()
    for raw_line in completed.stdout.decode("utf-8", "replace").splitlines():
        fields = raw_line.split(":")
        if fields and fields[0] == "fpr" and len(fields) > 9:
            values.add(fields[9].upper())
    return values


def validate_key(fingerprint: str | None, homedir: Path, public_key: Path) -> tuple[str, bytes]:
    if not fingerprint:
        raise DispatchError("--gpg-key or BIFROST_APP_GPG_KEY is required")
    fingerprint = fingerprint.upper()
    if not FINGERPRINT.fullmatch(fingerprint):
        raise DispatchError("release-key fingerprint must be exactly 40 hexadecimal characters")
    if fingerprint not in fingerprints(["--list-secret-keys", fingerprint], homedir):
        raise DispatchError("the exact release secret key is unavailable")
    if not public_key.is_file():
        raise DispatchError(f"tracked public key is missing: {public_key}")
    shown = run(["gpg", "--batch", "--with-colons", "--show-keys", str(public_key)])
    public_fingerprints = {
        fields[9].upper()
        for line in shown.stdout.decode("utf-8", "replace").splitlines()
        if (fields := line.split(":")) and fields[0] == "fpr" and len(fields) > 9
    }
    if fingerprint not in public_fingerprints:
        raise DispatchError("tracked public key does not match the selected secret key")
    exported = run(["gpg", "--homedir", str(homedir), "--batch", "--export", fingerprint]).stdout
    if not exported:
        raise DispatchError("GnuPG exported an empty public key")
    return fingerprint, exported


def validate_url(url: str, *, allow_local: bool) -> str:
    normalized = url.rstrip("/") + "/"
    if normalized.startswith("https://"):
        return normalized
    if allow_local and normalized.startswith("file://"):
        return normalized
    raise DispatchError("repository URL must use HTTPS; file:// requires --allow-local-url")


def repository_refs(repository: Path) -> set[str]:
    completed = run(["ostree", f"--repo={repository}", "refs"])
    return {line.strip() for line in completed.stdout.decode().splitlines() if line.strip()}


def validate_refs(refs: set[str]) -> None:
    invalid = sorted(ref for ref in refs if not TRUSTED_APP_REF.fullmatch(ref) and not AUXILIARY_REF.fullmatch(ref))
    if invalid:
        raise DispatchError(f"repository contains refs outside the org.bifrost namespace: {', '.join(invalid)}")


def write_definition(path: Path, url: str, public_key: bytes) -> None:
    encoded_key = base64.b64encode(public_key).decode("ascii")
    text = (
        "[Flatpak Repo]\n"
        "Title=BifrOSt Applications\n"
        "Title[is]=BifrOSt forrit\n"
        "Comment=Signed first-party applications for BifrOSt\n"
        "Comment[is]=Undirrituð forrit frá BifrOSt\n"
        "Description=Official, GPG-signed BifrOSt application repository. Applications are installed per user and do not replace Arch Linux system updates.\n"
        "Description[is]=Opinber GPG-undirrituð forritageymsla BifrOSt. Forrit eru sett upp fyrir hvern notanda og koma ekki í stað kerfisuppfærslna Arch Linux.\n"
        f"Url={url}\n"
        "Homepage=https://github.com/olibuijr/BifrOSt\n"
        "DefaultBranch=stable\n"
        f"GPGKey={encoded_key}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(path)


def stage_repository(repository: Path, bundles: list[Path], fingerprint: str, homedir: Path, public_key: bytes) -> Path:
    repository.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{repository.name}.", dir=repository.parent))
    try:
        run(["ostree", f"--repo={staging}", "init", "--mode=archive-z2"])
        run(["ostree", f"--repo={staging}", "config", "set", "core.collection-id", "org.bifrost.Apps"])
        if (repository / "config").is_file():
            run(["ostree", f"--repo={staging}", "pull-local", "--depth=-1", str(repository)])
        with tempfile.NamedTemporaryFile(prefix="bifrost-app-key-", suffix=".gpg") as key_file:
            key_file.write(public_key)
            key_file.flush()
            for bundle in bundles:
                run(
                    [
                        "flatpak",
                        "build-import-bundle",
                        f"--gpg-sign={fingerprint}",
                        f"--gpg-homedir={homedir}",
                        "--update-appstream",
                        str(staging),
                        str(bundle),
                    ]
                )
            validate_refs(repository_refs(staging))
            run(
                [
                    "flatpak",
                    "build-update-repo",
                    "--title=BifrOSt Applications",
                    "--comment=Signed first-party applications for BifrOSt",
                    "--description=Official application repository for BifrOSt",
                    "--homepage=https://github.com/olibuijr/BifrOSt",
                    "--default-branch=stable",
                    "--collection-id=org.bifrost.Apps",
                    f"--gpg-import={key_file.name}",
                    f"--gpg-sign={fingerprint}",
                    f"--gpg-homedir={homedir}",
                    "--generate-static-deltas",
                    "--prune",
                    str(staging),
                ]
            )
        if not (staging / "summary").is_file() or not (staging / "summary.sig").is_file():
            raise DispatchError("Flatpak produced no signed repository summary")
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def replace_repository(repository: Path, staging: Path) -> None:
    backup = repository.with_name(f".{repository.name}.backup")
    if backup.exists():
        shutil.rmtree(backup)
    if repository.exists():
        repository.replace(backup)
    try:
        staging.replace(repository)
    except Exception:
        if backup.exists() and not repository.exists():
            backup.replace(repository)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def publish_pages(repository: Path, definition: Path, pages_directory: Path, github_repository: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github_repository):
        raise DispatchError("--github-repository must be OWNER/REPOSITORY")
    if pages_directory.exists():
        shutil.rmtree(pages_directory)
    remote = f"https://github.com/{github_repository}.git"
    branch = run(["git", "ls-remote", "--heads", remote, "gh-pages"]).stdout.strip()
    if branch:
        run(["git", "clone", "--depth", "1", "--branch", "gh-pages", remote, str(pages_directory)])
    else:
        pages_directory.mkdir(parents=True)
        run(["git", "init", "--initial-branch=gh-pages"], cwd=pages_directory)
        run(["git", "remote", "add", "origin", remote], cwd=pages_directory)
    run(["git", "config", "user.name", "BifrOSt Release Automation"], cwd=pages_directory)
    run(["git", "config", "user.email", "olibuijr@users.noreply.github.com"], cwd=pages_directory)
    destination = pages_directory / "flatpak/repo"
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repository, destination, symlinks=True)
    shutil.copy2(definition, destination.parent / "bifrost.flatpakrepo")
    (pages_directory / ".nojekyll").write_text("\n", encoding="ascii")
    run(["git", "add", "."], cwd=pages_directory)
    status = run(["git", "status", "--porcelain"], cwd=pages_directory).stdout
    if not status.strip():
        print("GitHub Pages already contains this signed repository.")
        return
    run(["git", "commit", "-m", "Dispatch BifrOSt application repository"], cwd=pages_directory)
    run(["git", "push", "origin", "gh-pages"], cwd=pages_directory)


def main() -> int:
    args = parse_args()
    try:
        repository = args.repository.resolve()
        definition = args.definition.resolve()
        homedir = args.gpg_homedir.resolve()
        public_key_path = args.public_key.resolve()
        url = validate_url(args.repository_url, allow_local=args.allow_local_url)
        fingerprint, public_key = validate_key(args.gpg_key, homedir, public_key_path)
        bundles = list(args.bundle)
        with tempfile.TemporaryDirectory(prefix="bifrost-app-bundles-") as bundle_directory_name:
            artifacts = stage_bundle_files(bundles, Path(bundle_directory_name))
            for artifact in artifacts:
                print(
                    f"Importing {artifact.source_name!r}: "
                    f"{artifact.size} bytes, sha256 {artifact.sha256}"
                )
            staging = stage_repository(
                repository,
                [artifact.path for artifact in artifacts],
                fingerprint,
                homedir,
                public_key,
            )
            replace_repository(repository, staging)
        write_definition(definition, url, public_key)
        print(f"Signed repository: {repository}")
        print(f"Repository definition: {definition}")
        print(f"Signing fingerprint: {fingerprint}")
        if args.publish:
            publish_pages(repository, definition, args.pages_directory.resolve(), args.github_repository)
            print(f"Published repository: {url}")
    except (OSError, DispatchError) as error:
        print(f"dispatch-app-release.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
