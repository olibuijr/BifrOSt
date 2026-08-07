from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import stat as stat_module
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER_PATH = ROOT / "publish-release.py"


def load_python(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


publisher = load_python("bifrost_publish_release", PUBLISHER_PATH)

REVISION = "ab" * 20
REPOSITORY = "olibuijr/BifrOSt"


def write_assets(asset_dir: Path, version: str) -> None:
    for name in publisher.expected_asset_names(version):
        (asset_dir / name).write_bytes(f"payload of {name}\n".encode())


def evidence_result(version: str, iso: Path, *, install_seconds=41.5):
    cases = [
        {"case": "standard", "status": "passed"},
        {"case": "luks2", "status": "passed", "wrong_luks_passphrase_rejected": True},
    ]
    for case in cases:
        if install_seconds is not None:
            case["install_seconds"] = install_seconds
    result = {
        "schema_version": 1,
        "version": version,
        "status": "passed",
        "cases": ["standard", "luks2"],
        "iso": {
            "path": str(iso),
            "bytes": iso.stat().st_size,
            "sha256": publisher.sha256_file(iso),
        },
        "results": cases,
    }
    return result


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bifrost-test-staging-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for path in sorted(self.tmp.rglob("*"), reverse=True):
            os.chmod(path, 0o700) if path.is_dir() else os.chmod(path, 0o600)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stage_file_rejects_symlink_source(self):
        real = self.tmp / "real.txt"
        real.write_text("content")
        link = self.tmp / "link.txt"
        link.symlink_to(real)
        with self.assertRaises(publisher.PublicationError):
            publisher.stage_file(link, self.tmp / "staged.txt")

    def test_stage_file_rejects_missing_and_non_regular_source(self):
        with self.assertRaises(publisher.PublicationError):
            publisher.stage_file(self.tmp / "absent", self.tmp / "staged-a")
        directory = self.tmp / "a-directory"
        directory.mkdir()
        with self.assertRaises(publisher.PublicationError):
            publisher.stage_file(directory, self.tmp / "staged-b")

    def test_staged_copies_survive_post_stage_swap(self):
        version = "0.2.2"
        asset_dir = self.tmp / "assets"
        asset_dir.mkdir()
        write_assets(asset_dir, version)
        notes = self.tmp / "notes.md"
        notes.write_text("release notes")
        with mock.patch.object(publisher, "ROOT", self.tmp):
            staging_dir, staged_assets, staged_notes = publisher.stage_release_inputs(
                asset_dir, notes, version
            )
        self.assertEqual(
            [path.name for path in staged_assets], publisher.expected_asset_names(version)
        )
        self.assertTrue(str(staging_dir).startswith(str(self.tmp)))
        mode = stat_module.S_IMODE(staging_dir.stat().st_mode)
        self.assertEqual(mode, 0o700)
        iso = staged_assets[0]
        before = publisher.sha256_file(iso)
        self.assertEqual(stat_module.S_IMODE(iso.stat().st_mode), 0o400)
        # The attacker swaps the caller-supplied file after validation started.
        (asset_dir / iso.name).write_bytes(b"malicious replacement")
        self.assertEqual(publisher.sha256_file(iso), before)
        self.assertEqual(staged_notes.read_text(), "release notes")
        self.assertEqual(stat_module.S_IMODE(staged_notes.stat().st_mode), 0o400)

    def test_verify_assets_reads_only_staged_directory(self):
        # verify_assets constructs every path from the directory it is given, so
        # calling it on the staging directory can never touch caller-owned paths.
        version = "0.2.2"
        asset_dir = self.tmp / "assets"
        asset_dir.mkdir()
        with self.assertRaises(publisher.PublicationError) as caught:
            publisher.verify_assets(asset_dir, version, f"v{version}", REVISION, "f" * 40)
        self.assertIn("incomplete", str(caught.exception))


class QemuEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bifrost-test-evidence-"))
        self.addCleanup(self._rmtree)
        self.version = "0.2.2"
        self.iso = self.tmp / f"bifrost-{self.version}-x86_64.iso"
        self.iso.write_bytes(b"iso-bytes")
        self.evidence = self.tmp / "evidence"
        self.evidence.mkdir()

    def _rmtree(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_result(self, result) -> None:
        (self.evidence / "result.json").write_text(json.dumps(result))

    def verify(self):
        publisher.verify_qemu_evidence(self.evidence, self.version, self.iso)

    def test_missing_install_seconds_is_rejected(self):
        self.write_result(evidence_result(self.version, self.iso, install_seconds=None))
        with self.assertRaises(publisher.PublicationError) as caught:
            self.verify()
        self.assertIn("install_seconds", str(caught.exception))

    def test_non_positive_or_boolean_install_seconds_is_rejected(self):
        for bad in (0, -3.5, True, "42", float("nan"), float("inf")):
            self.write_result(evidence_result(self.version, self.iso, install_seconds=bad))
            with self.assertRaises(publisher.PublicationError):
                self.verify()

    def test_local_evidence_is_accepted_without_workflow_metadata(self):
        self.write_result(evidence_result(self.version, self.iso))
        with mock.patch.object(publisher, "run", side_effect=AssertionError("no gh calls")):
            self.verify()


class DraftResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bifrost-test-draft-"))
        self.addCleanup(self._rmtree)

    def _rmtree(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def staged(self, name: str, content: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(content)
        return path

    def test_published_release_hard_fails(self):
        release = {"tag_name": "v0.2.2", "draft": False, "id": 7}
        with mock.patch.object(publisher, "github_request", return_value=(200, release)):
            with self.assertRaises(publisher.PublicationError) as caught:
                publisher.require_publishable_state(REPOSITORY, "v0.2.2", "tok")
        self.assertIn("already exists", str(caught.exception))

    def test_draft_release_is_returned_for_resume(self):
        release = {"tag_name": "v0.2.2", "draft": True, "id": 7}
        with mock.patch.object(publisher, "github_request", return_value=(200, release)):
            with contextlib.redirect_stderr(io.StringIO()):
                resumed = publisher.require_publishable_state(REPOSITORY, "v0.2.2", "tok")
        self.assertEqual(resumed, release)

    def test_absent_release_found_via_listing_fallback(self):
        draft = {"tag_name": "v0.2.2", "draft": True, "id": 7}

        def fake_request(repository, path, token, *, method="GET"):
            if path.startswith("releases/tags/"):
                return 404, None
            if path.startswith("releases?"):
                return 200, [{"tag_name": "v9.9.9", "draft": True, "id": 1}, draft]
            raise AssertionError(path)

        with mock.patch.object(publisher, "github_request", side_effect=fake_request):
            self.assertEqual(publisher.find_release(REPOSITORY, "v0.2.2", "tok"), draft)

    def test_reconcile_deletes_mismatched_and_uploads_missing(self):
        good = self.staged("bifrost-0.2.2-x86_64.iso", b"good-bytes")
        bad = self.staged("bifrost-0.2.2-x86_64.iso.sha256", b"expected checksum")
        absent = self.staged("bifrost-0.2.2-x86_64.packages.json", b"{}")
        draft = {
            "tag_name": "v0.2.2",
            "draft": True,
            "id": 7,
            "assets": [
                {
                    "id": 1,
                    "name": good.name,
                    "size": good.stat().st_size,
                    "digest": f"sha256:{publisher.sha256_file(good)}",
                },
                {"id": 2, "name": bad.name, "size": 999, "digest": "sha256:" + "0" * 64},
                {"id": 3, "name": "stray.bin", "size": 1, "digest": "sha256:" + "1" * 64},
            ],
        }
        deleted = []
        uploads = []

        def fake_request(repository, path, token, *, method="GET"):
            self.assertEqual(method, "DELETE")
            deleted.append(path)
            return 204, None

        def fake_run(command, **kwargs):
            uploads.append(command)
            return ""

        with mock.patch.object(publisher, "github_request", side_effect=fake_request):
            with mock.patch.object(publisher, "run", side_effect=fake_run):
                with contextlib.redirect_stderr(io.StringIO()):
                    publisher.reconcile_draft_assets(
                        REPOSITORY,
                        "v0.2.2",
                        draft,
                        [good, bad, absent],
                        "tok",
                        {"GH_TOKEN": "tok"},
                    )
        self.assertEqual(deleted, ["releases/assets/2", "releases/assets/3"])
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        self.assertEqual(upload[:6], ["gh", "release", "upload", "v0.2.2", "--repo", REPOSITORY])
        uploaded_names = {Path(argument).name for argument in upload[6:]}
        self.assertEqual(uploaded_names, {bad.name, absent.name})

    def test_reconcile_refuses_failed_deletion(self):
        good = self.staged("bifrost-0.2.2-x86_64.iso", b"good-bytes")
        draft = {
            "tag_name": "v0.2.2",
            "draft": True,
            "id": 7,
            "assets": [{"id": 9, "name": "stray.bin", "size": 1, "digest": None}],
        }
        with mock.patch.object(publisher, "github_request", return_value=(403, None)):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(publisher.PublicationError):
                    publisher.reconcile_draft_assets(
                        REPOSITORY, "v0.2.2", draft, [good], "tok", {}
                    )


if __name__ == "__main__":
    unittest.main()
