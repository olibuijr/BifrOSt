from __future__ import annotations

import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_python(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


publisher = load_python("bifrost_publish_release", ROOT / "publish-release.py")
metadata = load_python("bifrost_generate_release_metadata", ROOT / "generate-release-metadata.py")

VERSION = "0.2.9"
TAG = f"v{VERSION}"
REVISION = "a" * 40
SIGNER = "b" * 40
EPOCH = 1786000000
STEM = f"bifrost-{VERSION}-x86_64"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value) -> str:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return sha256_bytes(path.read_bytes())


def make_asset_dir(directory: Path) -> None:
    """Create a complete, internally consistent release asset set.

    Signatures are placeholders; tests that walk the full verify_assets path
    replace verify_signature so every digest and identity binding before the
    signature step is exercised for real.
    """
    iso = directory / f"{STEM}.iso"
    iso.write_bytes(b"BIFROST-TEST-ISO-PAYLOAD" * 1024)
    iso_hash = sha256_bytes(iso.read_bytes())
    build_identifier = f"bifrost-{VERSION}-{REVISION[:12]}-{EPOCH}"
    volume_id = "BIFROST_TEST"
    profile_hash = "c" * 64

    packages_hash = write_json(
        directory / f"{STEM}.packages.json",
        {
            "bifrost_version": VERSION,
            "source_revision": REVISION,
            "source_date_epoch": EPOCH,
            "packages": [],
        },
    )
    toolchain_hash = write_json(
        directory / f"{STEM}.toolchain.json",
        {"mkarchiso": "test-toolchain"},
    )
    build_hash = write_json(
        directory / f"{STEM}.build.json",
        {
            "bifrost_version": VERSION,
            "source_revision": REVISION,
            "source_tag": TAG,
            "source_date_epoch": EPOCH,
            "build_id": build_identifier,
            "iso": {"file": iso.name, "sha256": iso_hash, "volume_id": volume_id},
            "attestation": {
                "status": "signed",
                "signer_fingerprint": SIGNER,
                "file": f"{STEM}.release.json",
                "detached_signature_file": f"{STEM}.release.json.asc",
            },
            "checksum_signature": {
                "status": "signed",
                "signer_fingerprint": SIGNER,
                "checksum_file": f"{STEM}.iso.sha256",
                "detached_signature_file": f"{STEM}.iso.sha256.asc",
            },
            "package_manifest": {"file": f"{STEM}.packages.json", "sha256": packages_hash},
            "toolchain_manifest": {"file": f"{STEM}.toolchain.json", "sha256": toolchain_hash},
            "profile": {"sha256": profile_hash},
        },
    )
    write_json(
        directory / f"{STEM}.release.json",
        {
            "provenance_status": "signed",
            "version": VERSION,
            "source_revision": REVISION,
            "source_tag": TAG,
            "source_date_epoch": EPOCH,
            "build_id": build_identifier,
            "profile_sha256": profile_hash,
            "iso": {
                "file": iso.name,
                "sha256": iso_hash,
                "bytes": iso.stat().st_size,
                "volume_id": volume_id,
            },
            "evidence": {
                "signer_fingerprint": SIGNER,
                "attestation_file": f"{STEM}.release.json",
                "checksum_file": f"{STEM}.iso.sha256",
                "checksum_signature_file": f"{STEM}.iso.sha256.asc",
                "detached_signature_file": f"{STEM}.release.json.asc",
                "build_metadata_file": f"{STEM}.build.json",
                "build_metadata_sha256": build_hash,
                "package_manifest_file": f"{STEM}.packages.json",
                "package_manifest_sha256": packages_hash,
                "toolchain_manifest_file": f"{STEM}.toolchain.json",
                "toolchain_manifest_sha256": toolchain_hash,
            },
        },
    )
    (directory / f"{STEM}.iso.sha256").write_text(f"{iso_hash}  {iso.name}\n", encoding="ascii")
    (directory / f"{STEM}.iso.sha256.asc").write_text("placeholder signature\n", encoding="ascii")
    (directory / f"{STEM}.release.json.asc").write_text("placeholder signature\n", encoding="ascii")


class ReleasePipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="bifrost-release-pipeline-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.workspace)], check=False)
        self._staging_before = set(ROOT.glob(".bifrost-publish-staging-*"))
        self.addCleanup(self._remove_new_staging_dirs)

    def _remove_new_staging_dirs(self) -> None:
        for leftover in set(ROOT.glob(".bifrost-publish-staging-*")) - self._staging_before:
            subprocess.run(["rm", "-rf", str(leftover)], check=False)

    def patch_module(self, module, name, replacement) -> None:
        original = getattr(module, name)
        setattr(module, name, replacement)
        self.addCleanup(setattr, module, name, original)

    def patch_verification_fakes(self, signature_calls: list | None = None) -> None:
        """Bypass git and gpg only; every digest/identity binding runs for real."""

        def fake_run(command, **_ignored):
            if command[:2] == ["git", "show"]:
                return str(EPOCH)
            raise AssertionError(f"unexpected external command during test: {command}")

        def fake_verify_signature(content, signature, expected_fingerprint):
            if signature_calls is not None:
                signature_calls.append((content.name, signature.name, expected_fingerprint))

        self.patch_module(publisher, "run", fake_run)
        self.patch_module(publisher, "verify_signature", fake_verify_signature)

    def make_assets(self) -> Path:
        asset_dir = self.workspace / "assets"
        asset_dir.mkdir()
        make_asset_dir(asset_dir)
        return asset_dir


class WrongTagRefusalTest(ReleasePipelineTestCase):
    def test_publisher_main_refuses_tag_that_is_not_the_canonical_version_tag(self) -> None:
        version_file = self.workspace / "VERSION"
        version_file.write_text(f"{VERSION}\n", encoding="utf-8")
        self.patch_module(publisher, "VERSION_FILE", version_file)

        def must_not_run(*arguments, **keywords):
            raise AssertionError("verification must never start for a wrong tag")

        for guarded in ("verify_local_source", "verify_remote_tag", "github_request", "run"):
            self.patch_module(publisher, guarded, must_not_run)

        argv = [
            "publish-release.py",
            "--repository", "olibuijr/BifrOSt",
            "--tag", "v9.9.9",
            "--source-revision", REVISION,
            "--signer-fingerprint", SIGNER,
            "--asset-dir", str(self.workspace),
            "--qemu-evidence-dir", str(self.workspace),
            "--notes-file", str(version_file),
        ]
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(
            "os.environ", {"GITHUB_TOKEN": "test-token"}
        ), contextlib.redirect_stderr(stderr):
            exit_code = publisher.main()
        self.assertEqual(exit_code, 1)
        self.assertIn(TAG, stderr.getvalue(), "refusal must name the required canonical tag")

    def test_metadata_generator_refuses_final_tag_mismatching_version(self) -> None:
        def must_not_run(arguments):
            raise AssertionError("git must never run for a wrong final tag")

        self.patch_module(metadata, "run_git", must_not_run)
        with self.assertRaises(metadata.ReleaseError):
            metadata.verify_final_source(VERSION, REVISION, "v0.0.1", EPOCH, SIGNER)


class MismatchedDigestRefusalTest(ReleasePipelineTestCase):
    def test_consistent_asset_set_passes_verification(self) -> None:
        asset_dir = self.make_assets()
        signature_calls: list = []
        self.patch_verification_fakes(signature_calls)
        assets = publisher.verify_assets(asset_dir, VERSION, TAG, REVISION, SIGNER)
        self.assertEqual([path.name for path in assets], publisher.expected_asset_names(VERSION))
        self.assertEqual(len(signature_calls), 2, "checksum and attestation must both be signature-checked")

    def test_tampered_iso_bytes_are_refused(self) -> None:
        asset_dir = self.make_assets()
        self.patch_verification_fakes()
        iso = asset_dir / f"{STEM}.iso"
        iso.write_bytes(iso.read_bytes() + b"tampered")
        with self.assertRaises(publisher.PublicationError):
            publisher.verify_assets(asset_dir, VERSION, TAG, REVISION, SIGNER)

    def test_checksum_asset_that_disagrees_with_iso_is_refused(self) -> None:
        asset_dir = self.make_assets()
        self.patch_verification_fakes()
        checksum = asset_dir / f"{STEM}.iso.sha256"
        checksum.write_text(f"{'0' * 64}  {STEM}.iso\n", encoding="ascii")
        with self.assertRaises(publisher.PublicationError):
            publisher.verify_assets(asset_dir, VERSION, TAG, REVISION, SIGNER)

    def test_attestation_manifest_digest_mismatch_is_refused(self) -> None:
        asset_dir = self.make_assets()
        self.patch_verification_fakes()
        packages = asset_dir / f"{STEM}.packages.json"
        document = json.loads(packages.read_text(encoding="utf-8"))
        document["packages"] = [{"name": "injected", "version": "1"}]
        packages.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(publisher.PublicationError):
            publisher.verify_assets(asset_dir, VERSION, TAG, REVISION, SIGNER)


class PreExistingReleaseRefusalTest(ReleasePipelineTestCase):
    def fake_github(self, responses):
        calls = []

        def fake_github_request(repository, path, token, *, method="GET"):
            calls.append((repository, path, method))
            for prefix, response in responses:
                if path.startswith(prefix):
                    return response
            raise AssertionError(f"unexpected GitHub request: {path}")

        self.patch_module(publisher, "github_request", fake_github_request)
        return calls

    def test_published_release_refuses_replacement(self) -> None:
        self.fake_github(
            [("releases/tags/", (200, {"draft": False, "immutable": True, "id": 7}))]
        )
        with self.assertRaises(publisher.PublicationError):
            publisher.require_publishable_state("olibuijr/BifrOSt", TAG, "token")

    def test_absent_release_is_publishable(self) -> None:
        self.fake_github(
            [
                ("releases/tags/", (404, None)),
                ("releases?", (200, [])),
            ]
        )
        self.assertIsNone(publisher.require_publishable_state("olibuijr/BifrOSt", TAG, "token"))

    def test_hidden_published_release_in_listing_refuses_replacement(self) -> None:
        self.fake_github(
            [
                ("releases/tags/", (404, None)),
                ("releases?", (200, [{"tag_name": TAG, "draft": False, "id": 9}])),
            ]
        )
        with self.assertRaises(publisher.PublicationError):
            publisher.require_publishable_state("olibuijr/BifrOSt", TAG, "token")

    def test_unprovable_release_state_is_refused(self) -> None:
        self.fake_github([("releases/tags/", (500, None))])
        with self.assertRaises(publisher.PublicationError):
            publisher.require_publishable_state("olibuijr/BifrOSt", TAG, "token")

    def test_existing_draft_is_resumable_not_replaced(self) -> None:
        draft = {"draft": True, "id": 11, "tag_name": TAG}
        self.fake_github(
            [
                ("releases/tags/", (404, None)),
                ("releases?", (200, [draft])),
            ]
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            resumed = publisher.require_publishable_state("olibuijr/BifrOSt", TAG, "token")
        self.assertEqual(resumed, draft)


class ValidateThenSwapRefusalTest(ReleasePipelineTestCase):
    """Publication must never trust caller-controlled paths after validation."""

    def test_post_validation_swap_cannot_reach_publication_and_is_refused(self) -> None:
        asset_dir = self.make_assets()
        notes = self.workspace / "notes.md"
        notes.write_text("release notes\n", encoding="utf-8")
        self.patch_verification_fakes()

        staging_dir, staged_assets, staged_notes = publisher.stage_release_inputs(
            asset_dir, notes, VERSION
        )
        self.addCleanup(subprocess.run, ["rm", "-rf", str(staging_dir)], check=False)
        self.assertTrue(
            staging_dir.is_relative_to(ROOT) and staging_dir.name.startswith(".bifrost-publish-staging-")
        )

        staged_asset_dir = staged_assets[0].parent
        verified = publisher.verify_assets(staged_asset_dir, VERSION, TAG, REVISION, SIGNER)
        verified_digests = {path.name: publisher.sha256_file(path) for path in verified}

        # Attacker swaps every caller-controlled input after validation.
        (asset_dir / f"{STEM}.iso").write_bytes(b"malicious payload")
        notes.write_text("malicious notes\n", encoding="utf-8")

        for staged in staged_assets:
            self.assertEqual(
                publisher.sha256_file(staged),
                verified_digests[staged.name],
                f"staged copy of {staged.name} must be immune to post-validation swaps",
            )
        self.assertEqual(staged_notes.read_text(encoding="utf-8"), "release notes\n")

        # The immutable staged set still verifies; the swapped caller directory is refused.
        publisher.verify_assets(staged_asset_dir, VERSION, TAG, REVISION, SIGNER)
        with self.assertRaises(publisher.PublicationError):
            publisher.verify_assets(asset_dir, VERSION, TAG, REVISION, SIGNER)

    def test_symlinked_release_asset_is_refused_at_staging_time(self) -> None:
        asset_dir = self.make_assets()
        notes = self.workspace / "notes.md"
        notes.write_text("release notes\n", encoding="utf-8")
        attacker_payload = self.workspace / "attacker.iso"
        attacker_payload.write_bytes(b"attacker controlled")
        iso = asset_dir / f"{STEM}.iso"
        iso.unlink()
        iso.symlink_to(attacker_payload)
        with self.assertRaises(publisher.PublicationError):
            publisher.stage_release_inputs(asset_dir, notes, VERSION)


class MetadataImmutabilityTest(ReleasePipelineTestCase):
    def test_write_new_refuses_to_replace_existing_evidence(self) -> None:
        output = self.workspace / "evidence" / "release.json"
        metadata.write_new(output, b"{}\n")
        self.assertEqual(output.read_bytes(), b"{}\n")
        with self.assertRaises(metadata.ReleaseError):
            metadata.write_new(output, b'{"replaced": true}\n')
        self.assertEqual(output.read_bytes(), b"{}\n", "existing evidence must remain untouched")


if __name__ == "__main__":
    unittest.main()
