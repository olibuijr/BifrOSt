from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CACHE_SCRIPT = ROOT / "prepare-installer-cache.py"
BACKEND_PATH = ROOT / "profile/airootfs/usr/local/lib/bifrost-installer-backend"


def load_python(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


cache_module = load_python("prepare_installer_cache", CACHE_SCRIPT)


def backend_seed_function() -> str:
    lines = BACKEND_PATH.read_text(encoding="utf-8").splitlines()
    start = lines.index("verify_package_seed() {")
    end = lines.index("}", start)
    return "\n".join(lines[start : end + 1])


def write_seed(directory: Path, *, tamper: str | None = None) -> None:
    archive = directory / "example-1.0-1-x86_64.pkg.tar.zst"
    signature = directory / "example-1.0-1-x86_64.pkg.tar.zst.sig"
    archive.write_bytes(b"package payload")
    signature.write_bytes(b"detached signature")
    files = []
    for path in (archive, signature):
        files.append(
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    manifest = {
        "schema_version": 1,
        "complete": True,
        "purpose": "online-installer-package-seed",
        "top_level_packages": ["example"],
        "files": files,
    }
    if tamper == "hash":
        manifest["files"][0]["sha256"] = "0" * 64
    elif tamper == "size":
        manifest["files"][0]["bytes"] += 1
    elif tamper == "missing-file":
        archive.unlink()
    elif tamper == "missing-signature":
        signature.unlink()
        manifest["files"] = [record for record in manifest["files"] if not record["name"].endswith(".sig")]
    elif tamper == "incomplete":
        manifest["complete"] = False
    elif tamper == "traversal":
        manifest["files"][0]["name"] = "../" + manifest["files"][0]["name"]
    (directory / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")


class RequireGateTests(unittest.TestCase):
    def run_require(self, prepare) -> int:
        with tempfile.TemporaryDirectory() as workspace:
            directory = Path(workspace)
            prepare(directory)
            return subprocess.run(
                [sys.executable, str(CACHE_SCRIPT), "--require", "--cache-dir", str(directory)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode

    def test_accepts_complete_seed(self):
        self.assertEqual(self.run_require(write_seed), 0)

    def test_rejects_absent_manifest(self):
        self.assertEqual(self.run_require(lambda directory: None), 1)

    def test_rejects_tampered_seed(self):
        for tamper in ("hash", "size", "missing-file", "missing-signature", "incomplete", "traversal"):
            with self.subTest(tamper=tamper):
                self.assertEqual(self.run_require(lambda d, t=tamper: write_seed(d, tamper=t)), 1)

    def test_require_function_reports_reason(self):
        with tempfile.TemporaryDirectory() as workspace:
            directory = Path(workspace)
            write_seed(directory, tamper="hash")
            with self.assertRaisesRegex(cache_module.CacheError, "failed verification"):
                cache_module.require(directory)

    def test_legacy_manifest_without_complete_fails_gate(self):
        def prepare(directory: Path) -> None:
            write_seed(directory)
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            del manifest["complete"]
            (directory / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")

        self.assertEqual(self.run_require(prepare), 1)


class BackendSeedTests(unittest.TestCase):
    def run_backend(self, prepare) -> subprocess.CompletedProcess[str]:
        driver = "\n".join(
            (
                "set -Eeuo pipefail",
                backend_seed_function(),
                'seed_cache=$1',
                "unset BIFROST_PACMAN_CONF",
                "if [[ -f $seed_cache/manifest.json ]]; then",
                '    if seed_error=$(verify_package_seed "$seed_cache" 2>&1); then',
                '        echo "SEED-ENABLED"',
                "    else",
                '        echo "warning: ignoring package seed at $seed_cache (${seed_error:-verification failed});'
                ' continuing with the online package cache"',
                "    fi",
                "fi",
            )
        )
        with tempfile.TemporaryDirectory() as workspace:
            directory = Path(workspace)
            prepare(directory)
            return subprocess.run(
                ["bash", "-c", driver, "driver", str(directory)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
            )

    def assert_falls_back(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(completed.returncode, 0)
        warnings = [line for line in completed.stdout.splitlines() if line.startswith("warning: ")]
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("SEED-ENABLED", completed.stdout)
        self.assertIn("continuing with the online package cache", warnings[0])

    def test_enables_verified_seed(self):
        completed = self.run_backend(write_seed)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("SEED-ENABLED", completed.stdout)

    def test_accepts_legacy_manifest_without_complete_flag(self):
        def prepare(directory: Path) -> None:
            write_seed(directory)
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            del manifest["complete"]
            (directory / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")

        completed = self.run_backend(prepare)
        self.assertIn("SEED-ENABLED", completed.stdout)

    def test_falls_back_on_any_tamper(self):
        for tamper in ("hash", "size", "missing-file", "missing-signature", "incomplete", "traversal"):
            with self.subTest(tamper=tamper):
                self.assert_falls_back(self.run_backend(lambda d, t=tamper: write_seed(d, tamper=t)))

    def test_falls_back_on_unparseable_manifest(self):
        def prepare(directory: Path) -> None:
            (directory / "manifest.json").write_text("{", encoding="utf-8")

        self.assert_falls_back(self.run_backend(prepare))

    def test_no_manifest_stays_silent(self):
        completed = self.run_backend(lambda directory: None)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
