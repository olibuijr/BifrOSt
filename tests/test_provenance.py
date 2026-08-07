from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_python(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


generate_module = load_python("bifrost_generate_release_metadata", ROOT / "generate-release-metadata.py")
publish_module = load_python("bifrost_publish_release", ROOT / "publish-release.py")

SUBKEY = "1a" * 20
PRIMARY = "2b" * 20


def validsig_line(signature_key: str = SUBKEY, primary_key: str = PRIMARY) -> str:
    return (
        f"[GNUPG:] VALIDSIG {signature_key.upper()} 2026-08-07 1754500000 0 4 0 22 8 00 "
        f"{primary_key.upper()}"
    )


class ValidsigParsingTest(unittest.TestCase):
    def helpers(self):
        return (
            (generate_module.validsig_fingerprints, generate_module.ReleaseError),
            (publish_module.validsig_fingerprints, publish_module.PublicationError),
        )

    def test_returns_signature_and_primary_fingerprints(self):
        status = "[GNUPG:] GOODSIG AAAA Test\n" + validsig_line() + "\n[GNUPG:] TRUST_ULTIMATE\n"
        for helper, _error in self.helpers():
            signature_key, primary_key = helper(status, "tag vX")
            self.assertEqual(signature_key, SUBKEY)
            self.assertEqual(primary_key, PRIMARY)

    def test_subkey_fingerprint_never_satisfies_primary_contract(self):
        # The historical parser collected every hex field, so a VALIDSIG whose
        # first (subkey) field matched the trusted value was accepted. The
        # primary key is only the tenth field.
        for helper, _error in self.helpers():
            _signature_key, primary_key = helper(validsig_line(), "tag vX")
            self.assertNotEqual(primary_key, SUBKEY)
            self.assertEqual(primary_key, PRIMARY)

    def test_missing_primary_field_fails(self):
        truncated = f"[GNUPG:] VALIDSIG {SUBKEY.upper()} 2026-08-07 1754500000 0 4 0 22 8 00"
        for helper, error in self.helpers():
            with self.assertRaises(error):
                helper(truncated, "tag vX")

    def test_zero_and_multiple_validsig_records_fail(self):
        for helper, error in self.helpers():
            with self.assertRaises(error):
                helper("[GNUPG:] GOODSIG AAAA Test", "tag vX")
            with self.assertRaises(error):
                helper(validsig_line() + "\n" + validsig_line(), "tag vX")

    def test_non_hex_primary_field_fails(self):
        malformed = validsig_line(primary_key="zz" * 20)
        for helper, error in self.helpers():
            with self.assertRaises(error):
                helper(malformed, "tag vX")


SECRET_LISTING = "\n".join(
    [
        "sec:u:255:22:AAAAAAAAAAAAAAAA:1:::u:::scSC:::::ed25519:::0:",
        f"fpr:::::::::{PRIMARY.upper()}:",
        "grp:::::::::0000:",
        "ssb:u:255:22:BBBBBBBBBBBBBBBB:1::::::s:::::ed25519::",
        f"fpr:::::::::{SUBKEY.upper()}:",
        "grp:::::::::1111:",
    ]
)


class PrimaryFingerprintAssociationTest(unittest.TestCase):
    def test_sec_association_excludes_subkeys(self):
        self.assertEqual(
            generate_module.primary_fingerprints(SECRET_LISTING, "sec"), {PRIMARY}
        )
        self.assertEqual(
            generate_module.primary_fingerprints(SECRET_LISTING, "ssb"), {SUBKEY}
        )

    def test_secret_key_fingerprint_rejects_subkey(self):
        original = generate_module.gpg_command
        generate_module.gpg_command = lambda arguments: subprocess.CompletedProcess(
            arguments, 0, SECRET_LISTING, ""
        )
        try:
            with self.assertRaises(generate_module.ReleaseError):
                generate_module.secret_key_fingerprint(SUBKEY.upper())
            self.assertEqual(
                generate_module.secret_key_fingerprint(PRIMARY.upper()), PRIMARY
            )
        finally:
            generate_module.gpg_command = original


def write_package_archive(path: Path, pkginfo: str | None) -> None:
    with tarfile.open(path, "w:gz") as archive:
        if pkginfo is not None:
            data = pkginfo.encode("utf-8")
            member = tarfile.TarInfo(".PKGINFO")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        payload = b"payload"
        member = tarfile.TarInfo("usr/share/demo/file")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))


class ArchiveIdentityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = Path(self.directory.name) / "demo-1.0-1-x86_64.pkg.tar.gz"

    def test_matching_pkginfo_passes(self):
        write_package_archive(
            self.archive, "# comment\npkgname = demo\npkgver = 1.0-1\narch = x86_64\n"
        )
        generate_module.verify_archive_identity(self.archive, "demo", "1.0-1", "x86_64")

    def test_disagreeing_pkginfo_fails(self):
        write_package_archive(
            self.archive, "pkgname = demo\npkgver = 9.9-9\narch = x86_64\n"
        )
        with self.assertRaises(generate_module.ReleaseError):
            generate_module.verify_archive_identity(self.archive, "demo", "1.0-1", "x86_64")

    def test_renamed_foreign_archive_fails(self):
        # An unrelated package renamed to the expected cache filename.
        write_package_archive(
            self.archive, "pkgname = other\npkgver = 1.0-1\narch = x86_64\n"
        )
        with self.assertRaises(generate_module.ReleaseError):
            generate_module.verify_archive_identity(self.archive, "demo", "1.0-1", "x86_64")

    def test_archive_without_pkginfo_fails(self):
        write_package_archive(self.archive, None)
        with self.assertRaises(generate_module.ReleaseError):
            generate_module.verify_archive_identity(self.archive, "demo", "1.0-1", "x86_64")

    def test_renamed_archive_is_not_selected(self):
        with self.assertRaises(generate_module.ReleaseError):
            generate_module.find_package_archive(
                [self.archive], "demo", "1.0-2", "x86_64"
            )


class ArchiveSignatureTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.archive = self.home / "demo-1.0-1-x86_64.pkg.tar.gz"
        self.archive.write_bytes(b"archive")
        self.signature = self.archive.with_name(self.archive.name + ".sig")

    def test_missing_detached_signature_fails(self):
        with self.assertRaises(generate_module.ReleaseError):
            generate_module.verify_archive_signature(
                self.home, self.archive, self.signature, "demo"
            )

    def test_bifrost_package_requires_pinned_primary(self):
        self.signature.write_bytes(b"sig")
        original = generate_module.gpg_command
        generate_module.gpg_command = lambda arguments: subprocess.CompletedProcess(
            arguments, 0, validsig_line() + "\n", ""
        )
        try:
            with self.assertRaises(generate_module.ReleaseError):
                generate_module.verify_archive_signature(
                    self.home, self.archive, self.signature, "bifrost-system"
                )
            self.assertEqual(
                generate_module.verify_archive_signature(
                    self.home, self.archive, self.signature, "demo"
                ),
                PRIMARY,
            )
            pinned = generate_module.ALPM_PRIMARY_FINGERPRINT
            generate_module.gpg_command = lambda arguments: subprocess.CompletedProcess(
                arguments, 0, validsig_line(primary_key=pinned) + "\n", ""
            )
            self.assertEqual(
                generate_module.verify_archive_signature(
                    self.home, self.archive, self.signature, "bifrost-system"
                ),
                pinned,
            )
        finally:
            generate_module.gpg_command = original


@unittest.skipUnless(shutil.which("gpg"), "gpg is not installed")
class SubkeySignatureRejectionTest(unittest.TestCase):
    """End-to-end proof: a signature made by a signing subkey satisfies the
    contract only through its primary key, never through the subkey value."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.home.chmod(0o700)
        self.previous_home = os.environ.get("GNUPGHOME")
        os.environ["GNUPGHOME"] = str(self.home)
        self.addCleanup(self.restore_home)

    def restore_home(self):
        if self.previous_home is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = self.previous_home

    def gpg(self, *arguments: str) -> str:
        completed = subprocess.run(
            [
                "gpg", "--batch", "--no-tty", "--pinentry-mode", "loopback",
                "--passphrase", "", *arguments,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return completed.stdout

    def fingerprints(self, record: str) -> list[str]:
        listing = self.gpg("--with-colons", "--fingerprint", "--list-secret-keys")
        values = []
        waiting = False
        for line in listing.splitlines():
            fields = line.split(":")
            if fields[0] == record:
                waiting = True
            elif waiting and fields[0] == "fpr":
                values.append(fields[9].lower())
                waiting = False
            elif fields[0] in {"pub", "sec", "sub", "ssb"}:
                waiting = False
        return values

    def test_verify_signature_pins_primary_not_subkey(self):
        self.gpg("--quick-generate-key", "BifrOSt Test <test@invalid>", "ed25519", "cert", "never")
        primary = self.fingerprints("sec")[0]
        self.gpg("--quick-add-key", primary.upper(), "ed25519", "sign", "never")
        subkey = self.fingerprints("ssb")[0]
        self.assertNotEqual(primary, subkey)

        content = self.home / "content.txt"
        content.write_text("evidence\n", encoding="utf-8")
        signature = self.home / "content.txt.sig"
        self.gpg("--detach-sign", "--output", str(signature), str(content))

        publish_module.verify_signature(content, signature, primary)
        with self.assertRaises(publish_module.PublicationError):
            publish_module.verify_signature(content, signature, subkey)


@unittest.skipUnless(shutil.which("gpg"), "gpg is not installed")
class PackageManifestIntegrationTest(unittest.TestCase):
    """Full chain: pinned keyring build, detached-signature verification,
    .PKGINFO cross-check against the ALPM record, manifest evidence fields."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.signing_home = base / "signing"
        self.signing_home.mkdir(mode=0o700)
        self.alpm_root = base / "root"
        self.cache = base / "cache"
        self.cache.mkdir()
        record = self.alpm_root / "var/lib/pacman/local/demo-1.0-1"
        record.mkdir(parents=True)
        (record / "desc").write_text(
            "%NAME%\ndemo\n\n%VERSION%\n1.0-1\n\n%ARCH%\nx86_64\n",
            encoding="utf-8",
        )
        self.archive = self.cache / "demo-1.0-1-x86_64.pkg.tar.gz"
        write_package_archive(
            self.archive, "pkgname = demo\npkgver = 1.0-1\narch = x86_64\n"
        )
        self.gpg("--quick-generate-key", "BifrOSt ALPM Test <alpm@invalid>", "ed25519", "sign", "never")
        listing = self.gpg("--with-colons", "--fingerprint", "--list-secret-keys")
        self.fingerprint = generate_module.primary_fingerprints(listing, "sec").pop()
        self.public_key = base / "throwaway-key.asc"
        self.public_key.write_text(
            self.gpg("--armor", "--export", self.fingerprint.upper()), encoding="utf-8"
        )
        self.gpg("--detach-sign", "--output", str(self.archive) + ".sig", str(self.archive))

    def gpg(self, *arguments: str) -> str:
        completed = subprocess.run(
            [
                "gpg", "--homedir", str(self.signing_home), "--batch", "--no-tty",
                "--pinentry-mode", "loopback", "--passphrase", "", *arguments,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return completed.stdout

    def pinned(self, key_path: Path, fingerprint: str):
        original = (generate_module.ALPM_PUBLIC_KEY, generate_module.ALPM_PRIMARY_FINGERPRINT)
        generate_module.ALPM_PUBLIC_KEY = key_path
        generate_module.ALPM_PRIMARY_FINGERPRINT = fingerprint

        def restore():
            generate_module.ALPM_PUBLIC_KEY, generate_module.ALPM_PRIMARY_FINGERPRINT = original

        self.addCleanup(restore)

    def test_manifest_records_verified_provenance(self):
        self.pinned(self.public_key, self.fingerprint)
        packages = generate_module.package_manifest(self.alpm_root, self.cache)
        self.assertEqual(len(packages), 1)
        package = packages[0]
        self.assertEqual(package["name"], "demo")
        self.assertEqual(package["signature_file"], self.archive.name + ".sig")
        self.assertEqual(package["signature_primary_fingerprint"], self.fingerprint)

    def test_unknown_signer_fails_closed(self):
        # The real repository-pinned key does not know the throwaway signer.
        with self.assertRaises(generate_module.ReleaseError):
            generate_module.package_manifest(self.alpm_root, self.cache)

    def test_tampered_archive_fails_closed(self):
        self.pinned(self.public_key, self.fingerprint)
        with self.archive.open("ab") as handle:
            handle.write(b"tamper")
        with self.assertRaises(generate_module.ReleaseError):
            generate_module.package_manifest(self.alpm_root, self.cache)


if __name__ == "__main__":
    unittest.main()
