from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DISPATCH_PATH = ROOT / "dispatch-app-release.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dispatch = load_module("bifrost_app_dispatch_admission", DISPATCH_PATH)

APP_ID = "org.bifrost.Editor"
ARCH = "x86_64"
BRANCH = "stable"
APP_REF = f"app/{APP_ID}/{ARCH}/{BRANCH}"
REVISION = "b" * 40


def manifest_document(**overrides) -> dict:
    document = {
        "bundle_sha256": "a" * 64,
        "source_repository": "olibuijr/BifrOSt-Apps",
        "source_revision": REVISION,
        "workflow": {"path": ".github/workflows/release.yml", "run_id": "123456"},
        "app_id": APP_ID,
        "branch": BRANCH,
        "arch": ARCH,
    }
    document.update(overrides)
    return {key: value for key, value in document.items() if value is not None}


class ManifestLoadingTest(unittest.TestCase):
    def load(self, document) -> "dispatch.CandidateManifest":
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return dispatch.load_candidate_manifest(path)

    def test_valid_manifest_parses(self):
        manifest = self.load(manifest_document(bundle_sha256="A" * 64))
        self.assertEqual(manifest.bundle_sha256, "a" * 64)
        self.assertEqual(manifest.app_ref, APP_REF)
        self.assertEqual(manifest.workflow_run_id, "123456")

    def test_workflow_is_optional(self):
        manifest = self.load(manifest_document(workflow=None))
        self.assertIsNone(manifest.workflow_path)
        self.assertIsNone(manifest.workflow_run_id)

    def test_missing_required_key_is_rejected(self):
        for key in ("bundle_sha256", "source_repository", "source_revision", "app_id", "branch", "arch"):
            document = manifest_document()
            del document[key]
            with self.assertRaisesRegex(dispatch.DispatchError, "missing required keys"):
                self.load(document)

    def test_unexpected_key_is_rejected(self):
        with self.assertRaisesRegex(dispatch.DispatchError, "unexpected keys"):
            self.load(manifest_document(comment="looks fine to me"))

    def test_malformed_fields_are_rejected(self):
        malformed = {
            "bundle_sha256": "a" * 63,
            "source_repository": "https://github.com/olibuijr/BifrOSt-Apps",
            "source_revision": "HEAD",
            "app_id": "com.attacker.Fake",
            "branch": "stable/../evil",
            "arch": "x86_64;rm",
        }
        for key, value in malformed.items():
            with self.assertRaisesRegex(dispatch.DispatchError, "malformed"):
                self.load(manifest_document(**{key: value}))

    def test_workflow_shape_is_enforced(self):
        with self.assertRaisesRegex(dispatch.DispatchError, "workflow"):
            self.load(manifest_document(workflow={"path": ".github/workflows/release.yml"}))
        with self.assertRaisesRegex(dispatch.DispatchError, "workflow"):
            self.load(manifest_document(workflow={"path": "/etc/passwd", "run_id": "1"}))
        with self.assertRaisesRegex(dispatch.DispatchError, "workflow"):
            self.load(manifest_document(workflow={"path": ".github/workflows/release.yml", "run_id": "latest"}))

    def test_non_object_manifest_is_rejected(self):
        with self.assertRaisesRegex(dispatch.DispatchError, "JSON object"):
            self.load(["not", "an", "object"])

    def test_denylisted_app_id_is_rejected_even_when_well_formed(self):
        with self.assertRaisesRegex(dispatch.DispatchError, "denylisted"):
            self.load(manifest_document(app_id="org.bifrost.TemplateCheck"))

    def test_duplicate_digests_and_refs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            first.write_text(json.dumps(manifest_document()), encoding="utf-8")
            second.write_text(json.dumps(manifest_document(app_id="org.bifrost.Files")), encoding="utf-8")
            with self.assertRaisesRegex(dispatch.DispatchError, "distinct bundle digests"):
                dispatch.load_candidate_manifests([first, second])
            second.write_text(json.dumps(manifest_document(bundle_sha256="c" * 64)), encoding="utf-8")
            with self.assertRaisesRegex(dispatch.DispatchError, "distinct application refs"):
                dispatch.load_candidate_manifests([first, second])


def staged(digest: str, name: str = "editor.flatpak") -> "dispatch.StagedBundle":
    return dispatch.StagedBundle(name, Path("/nonexistent/staged.flatpak"), 4, digest)


def candidate(**overrides) -> "dispatch.CandidateManifest":
    values = dict(
        source_name="candidate.json",
        bundle_sha256="a" * 64,
        source_repository="olibuijr/BifrOSt-Apps",
        source_revision=REVISION,
        workflow_path=None,
        workflow_run_id=None,
        app_id=APP_ID,
        branch=BRANCH,
        arch=ARCH,
    )
    values.update(overrides)
    return dispatch.CandidateManifest(**values)


class AdmissionTest(unittest.TestCase):
    def test_bundle_without_manifest_is_rejected(self):
        with self.assertRaisesRegex(dispatch.DispatchError, "exactly one reviewed manifest"):
            dispatch.admit_candidates([staged("a" * 64)], [])

    def test_digest_mismatch_is_rejected(self):
        with self.assertRaisesRegex(dispatch.DispatchError, "matches no candidate manifest"):
            dispatch.admit_candidates([staged("d" * 64)], [candidate()])

    def test_ref_mismatch_is_rejected(self):
        with mock.patch.object(dispatch, "bundle_refs", return_value={"app/org.bifrost.Files/x86_64/stable"}):
            with self.assertRaisesRegex(dispatch.DispatchError, "admits only"):
                dispatch.admit_candidates([staged("a" * 64)], [candidate()])

    def test_extra_smuggled_ref_is_rejected(self):
        smuggled = {APP_REF, "app/org.bifrost.Backdoor/x86_64/stable"}
        with mock.patch.object(dispatch, "bundle_refs", return_value=smuggled):
            with self.assertRaisesRegex(dispatch.DispatchError, "admits only"):
                dispatch.admit_candidates([staged("a" * 64)], [candidate()])

    def test_denylisted_ref_is_rejected_even_with_matching_manifest(self):
        denied = {"app/org.bifrost.TemplateCheck/x86_64/stable"}
        with mock.patch.object(dispatch, "bundle_refs", return_value=denied):
            with self.assertRaisesRegex(dispatch.DispatchError, "denylisted"):
                dispatch.admit_candidates([staged("a" * 64)], [candidate()])

    def test_matching_manifest_is_admitted(self):
        with mock.patch.object(dispatch, "bundle_refs", return_value={APP_REF}):
            dispatch.admit_candidates([staged("a" * 64)], [candidate()])

    def test_repository_validation_refuses_denylisted_refs(self):
        with self.assertRaisesRegex(dispatch.DispatchError, "denylisted"):
            dispatch.validate_refs({"app/org.bifrost.TemplateCheck/x86_64/stable"})

    def test_auxiliary_refs_are_limited_to_manifest_architectures(self):
        self.assertEqual(
            dispatch.auxiliary_refs([candidate()]),
            {"appstream/x86_64", "appstream2/x86_64"},
        )


class MainFlowTest(unittest.TestCase):
    FINGERPRINT = "F" * 40

    def run_main(self, directory: Path, manifest_digest: str | None):
        bundle = directory / "editor.flatpak"
        payload = b"bundle-bytes"
        bundle.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        manifest_path = directory / "candidate.json"
        manifest_path.write_text(
            json.dumps(manifest_document(bundle_sha256=manifest_digest or digest)),
            encoding="utf-8",
        )
        argv = [
            "dispatch-app-release.py",
            "--bundle",
            str(bundle),
            "--candidate-manifest",
            str(manifest_path),
            "--gpg-key",
            self.FINGERPRINT,
            "--repository",
            str(directory / "repo"),
            "--definition",
            str(directory / "bifrost.flatpakrepo"),
        ]
        calls = {}

        def fake_stage_repository(repository, bundles, fingerprint, homedir, public_key, expected_refs, allowed_auxiliary):
            calls["expected_refs"] = expected_refs
            calls["allowed_auxiliary"] = allowed_auxiliary
            staging = directory / "staging"
            staging.mkdir(exist_ok=True)
            return staging

        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            dispatch, "validate_key", return_value=(self.FINGERPRINT, b"public-key")
        ), mock.patch.object(dispatch, "bundle_refs", return_value={APP_REF}), mock.patch.object(
            dispatch, "stage_repository", side_effect=fake_stage_repository
        ), mock.patch.object(dispatch, "replace_repository"), mock.patch.object(dispatch, "write_definition"):
            return dispatch.main(), calls

    def test_happy_path_reaches_signing_flow_with_reviewed_refs(self):
        with tempfile.TemporaryDirectory() as name:
            code, calls = self.run_main(Path(name), manifest_digest=None)
        self.assertEqual(code, 0)
        self.assertEqual(calls["expected_refs"], {APP_REF})
        self.assertEqual(calls["allowed_auxiliary"], {"appstream/x86_64", "appstream2/x86_64"})

    def test_mismatched_manifest_never_reaches_signing_flow(self):
        with tempfile.TemporaryDirectory() as name:
            code, calls = self.run_main(Path(name), manifest_digest="e" * 64)
        self.assertEqual(code, 1)
        self.assertEqual(calls, {})


if __name__ == "__main__":
    unittest.main()
