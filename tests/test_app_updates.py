from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

# Modules are loaded straight from the tracked profile/airootfs staging tree;
# bytecode caches must never be written back into it (validate-build.py
# rejects staged __pycache__ debris).
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = ROOT / "profile/airootfs/usr/share/bifrost/installed-root/usr/lib/bifrost-apps/manager.py"
DISPATCH_PATH = ROOT / "dispatch-app-release.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


manager_module = load_module("bifrost_app_manager", MANAGER_PATH)
dispatch_module = load_module("bifrost_app_dispatch", DISPATCH_PATH)


class FakeFlatpak:
    FINGERPRINT = "A" * 40

    def __init__(self, responses, *, configured_fingerprints=None):
        self.responses = responses
        self.commands = []
        self.configured_fingerprints = (
            {self.FINGERPRINT} if configured_fingerprints is None else configured_fingerprints
        )

    def __call__(self, command):
        self.commands.append(command)
        if command[0] == "gpg-test":
            response = f"pub:-:255:22:0000000000000000:0:0::::::\nfpr:::::::::{self.FINGERPRINT}:\n"
            return subprocess.CompletedProcess(command, 0, response, "")
        if command[0] == "ostree-test" and "gpg-list-keys" in command:
            response = "".join(
                f"Key: {' '.join(fingerprint[index:index + 4] for index in range(0, 40, 4))}\n"
                for fingerprint in sorted(self.configured_fingerprints)
            )
            return subprocess.CompletedProcess(command, 0, response, "")
        arguments = tuple(command[1:])
        response = self.responses.get(arguments)
        if response is None:
            return subprocess.CompletedProcess(command, 0, "", "")
        if isinstance(response, tuple):
            return subprocess.CompletedProcess(command, response[0], response[1], response[2])
        return subprocess.CompletedProcess(command, 0, response, "")


class ManagerTest(unittest.TestCase):
    URL = "https://example.invalid/bifrost/repo/"
    APP = "org.bifrost.Editor"
    OLD = "a" * 64
    NEW = "b" * 64

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.definition = self.root / "bifrost.flatpakrepo"
        self.definition.write_text(
            "[Flatpak Repo]\n"
            "Title=BifrOSt Applications\n"
            f"Url={self.URL}\n"
            "GPGKey=ZmFrZS1wdWJsaWMta2V5\n",
            encoding="utf-8",
        )
        self.state = self.root / "history.json"

    def tearDown(self):
        self.temporary.cleanup()

    def make_manager(self, responses, *, configured_fingerprints=None):
        runner = FakeFlatpak(responses, configured_fingerprints=configured_fingerprints)
        manager = manager_module.FlatpakAppManager(
            remote_name="bifrost-test",
            repository_file=self.definition,
            expected_url=self.URL,
            state_file=self.state,
            flatpak="flatpak-test",
            ostree="ostree-test",
            gpg="gpg-test",
            flatpak_repository=self.root / "flatpak/repo",
            runner=runner,
        )
        return manager, runner

    def responses(self, *, update=True, cached=False):
        available = [
            {
                "application_id": self.APP,
                "name": "BifrOSt Editor",
                "description": "First-party editor",
                "version": "2.0.0",
                "branch": "stable",
                "commit": self.NEW,
            },
            {
                "application_id": "com.attacker.Fake",
                "name": "Not trusted",
                "version": "9.9.9",
                "branch": "stable",
                "commit": "c" * 64,
            },
        ]
        installed = [
            {
                "application_id": self.APP,
                "name": "BifrOSt Editor",
                "version": "1.0.0",
                "branch": "stable",
                "origin": "bifrost-test",
                "active_commit": self.OLD,
            }
        ]
        updates = [available[0]] if update else []
        cache_arguments = ("--cached",) if cached else ()
        return {
            ("remotes", "--user", "--columns=name,url,options"): f"bifrost-test\t{self.URL}\tuser\n",
            ("update", "--user", "--appstream", "bifrost-test", "--noninteractive"): "",
            ("info", "--user", "--show-commit", self.APP): f"{self.OLD}\n",
            ("remote-info", "--user", "--show-commit", "bifrost-test", self.APP): f"{self.NEW}\n",
            (
                "remote-ls",
                "--user",
                "--app",
                *cache_arguments,
                "--columns=application,name,description,version,branch,origin,commit:full",
                "--json",
                "bifrost-test",
            ): json.dumps(available),
            (
                "list",
                "--user",
                "--app",
                "--columns=application,name,description,version,branch,origin,active:full",
                "--json",
            ): json.dumps(installed),
            (
                "remote-ls",
                "--user",
                "--app",
                "--updates",
                *cache_arguments,
                "--columns=application,name,description,version,branch,origin,commit:full",
                "--json",
                "bifrost-test",
            ): json.dumps(updates),
        }

    def test_catalog_filters_namespace_and_marks_update(self):
        manager, _runner = self.make_manager(self.responses())
        records = manager.catalog()
        self.assertEqual([record.app_id for record in records], [self.APP])
        self.assertEqual(records[0].status, "update")
        self.assertEqual(records[0].installed_commit, self.OLD)
        self.assertEqual(records[0].available_commit, self.NEW)

    def test_remote_with_disabled_signature_verification_is_rejected(self):
        responses = {
            ("remotes", "--user", "--columns=name,url,options"): f"bifrost-test\t{self.URL}\tuser,no-gpg-verify\n"
        }
        manager, _runner = self.make_manager(responses)
        with self.assertRaisesRegex(manager_module.UpdateError, "signature verification disabled"):
            manager.ensure_remote()

    def test_remote_url_mismatch_is_rejected(self):
        responses = {
            ("remotes", "--user", "--columns=name,url,options"): "bifrost-test\thttps://attacker.invalid/repo/\tuser\n"
        }
        manager, _runner = self.make_manager(responses)
        with self.assertRaisesRegex(manager_module.UpdateError, "unexpected URL"):
            manager.ensure_remote()

    def test_same_url_remote_with_wrong_key_is_rejected(self):
        manager, _runner = self.make_manager(
            self.responses(),
            configured_fingerprints={"B" * 40},
        )
        with self.assertRaisesRegex(manager_module.UpdateError, "signing key does not match"):
            manager.ensure_remote()

    def test_cached_catalog_does_not_refresh_remote_metadata(self):
        manager, runner = self.make_manager(self.responses(cached=True))
        manager.catalog(refresh=False)
        self.assertNotIn(
            ["flatpak-test", "update", "--user", "--appstream", "bifrost-test", "--noninteractive"],
            runner.commands,
        )
        remote_ls_commands = [command for command in runner.commands if "remote-ls" in command]
        self.assertTrue(remote_ls_commands)
        self.assertTrue(all("--cached" in command for command in remote_ls_commands))

    def test_installed_withdrawn_app_remains_visible(self):
        self.state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "apps": {
                        self.APP: [
                            {"commit": self.NEW, "version": "2.0.0", "recorded_at": 1},
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        responses = self.responses(update=False, cached=True)
        for command in list(responses):
            if command and command[0] == "remote-ls":
                responses[command] = "[]"
        manager, runner = self.make_manager(responses)
        records = manager.catalog(refresh=False)
        self.assertEqual([record.app_id for record in records], [self.APP])
        self.assertEqual(records[0].status, "withdrawn")
        self.assertIsNone(records[0].available_version)
        self.assertEqual(records[0].installed_commit, self.OLD)
        self.assertTrue(records[0].rollback_available)
        manager.remove(self.APP)
        self.assertIn(
            ["flatpak-test", "uninstall", "--user", "--noninteractive", self.APP],
            runner.commands,
        )

    def test_update_all_reports_completed_and_failed_apps(self):
        other_app = "org.bifrost.Viewer"
        responses = self.responses()
        for command, response in list(responses.items()):
            if not command or command[0] != "remote-ls":
                continue
            rows = json.loads(response)
            if "--updates" in command:
                rows.append(
                    {
                        "application_id": other_app,
                        "name": "BifrOSt Viewer",
                        "version": "2.0.0",
                        "branch": "stable",
                        "commit": "d" * 64,
                    }
                )
            else:
                rows.append(
                    {
                        "application_id": other_app,
                        "name": "BifrOSt Viewer",
                        "version": "2.0.0",
                        "branch": "stable",
                        "commit": "d" * 64,
                    }
                )
            responses[command] = json.dumps(rows)
        list_command = (
            "list",
            "--user",
            "--app",
            "--columns=application,name,description,version,branch,origin,active:full",
            "--json",
        )
        installed = json.loads(responses[list_command])
        installed.append(
            {
                "application_id": other_app,
                "name": "BifrOSt Viewer",
                "version": "1.0.0",
                "branch": "stable",
                "origin": "bifrost-test",
                "active_commit": "c" * 64,
            }
        )
        responses[list_command] = json.dumps(installed)
        responses[("update", "--user", "--noninteractive", other_app)] = (
            1,
            "",
            "network failed",
        )
        manager, _runner = self.make_manager(responses)
        results = manager.update_all()
        self.assertEqual(
            [(result.app_id, result.status) for result in results],
            [(self.APP, "completed"), (other_app, "failed")],
        )
        self.assertEqual(results[1].error, "network failed")

    def test_update_records_previous_commit_before_flatpak_update(self):
        manager, runner = self.make_manager(self.responses(cached=True))
        manager.update(self.APP)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["apps"][self.APP][0]["commit"], self.OLD)
        self.assertIn(["flatpak-test", "update", "--user", "--noninteractive", self.APP], runner.commands)

    def test_rollback_uses_only_recorded_commit(self):
        self.state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "apps": {
                        self.APP: [
                            {"commit": self.NEW, "version": "2.0.0", "recorded_at": 1},
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        manager, runner = self.make_manager(self.responses(update=False))
        target = manager.rollback(self.APP)
        self.assertEqual(target, self.NEW)
        self.assertIn(
            ["flatpak-test", "update", "--user", "--noninteractive", f"--commit={self.NEW}", self.APP],
            runner.commands,
        )

    def test_rollback_preflights_recorded_commit_before_changing_history(self):
        original_state = {
            "schema_version": 1,
            "apps": {
                self.APP: [
                    {"commit": self.NEW, "version": "2.0.0", "recorded_at": 1},
                ]
            },
        }
        self.state.write_text(json.dumps(original_state), encoding="utf-8")
        responses = self.responses(update=False, cached=True)
        responses[
            (f"--repo={self.root / 'flatpak/repo'}", "show", self.NEW)
        ] = (1, "", "missing locally")
        responses[
            (
                "remote-info",
                "--user",
                "--show-commit",
                f"--commit={self.NEW}",
                "bifrost-test",
                self.APP,
            )
        ] = (1, "", "missing remotely")
        manager, runner = self.make_manager(responses)
        with self.assertRaisesRegex(manager_module.UpdateError, "no longer available"):
            manager.rollback(self.APP)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8")), original_state)
        self.assertNotIn(
            ["flatpak-test", "update", "--user", "--noninteractive", f"--commit={self.NEW}", self.APP],
            runner.commands,
        )

    def test_application_outside_namespace_cannot_be_installed(self):
        manager, _runner = self.make_manager(self.responses())
        with self.assertRaisesRegex(manager_module.UpdateError, "trusted org.bifrost namespace"):
            manager.install("com.attacker.Fake")


class DispatchContractTest(unittest.TestCase):
    def test_public_repository_requires_https(self):
        with self.assertRaises(dispatch_module.DispatchError):
            dispatch_module.validate_url("http://example.invalid/repo", allow_local=False)
        self.assertEqual(
            dispatch_module.validate_url("https://example.invalid/repo", allow_local=False),
            "https://example.invalid/repo/",
        )

    def test_repository_rejects_non_bifrost_refs(self):
        dispatch_module.validate_refs({"app/org.bifrost.Editor/x86_64/stable", "appstream/x86_64"})
        with self.assertRaisesRegex(dispatch_module.DispatchError, "outside the org.bifrost namespace"):
            dispatch_module.validate_refs({"app/com.attacker.Fake/x86_64/stable"})

    def test_bundle_staging_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            source = root / "source.flatpak"
            source.write_bytes(b"trusted bundle")
            link = root / "link.flatpak"
            link.symlink_to(source)
            staging = root / "staging"
            staging.mkdir()
            with self.assertRaisesRegex(dispatch_module.DispatchError, "non-symlink"):
                dispatch_module.stage_bundle_files([link], staging)

    def test_dispatch_hash_and_import_source_are_the_same_staged_bytes(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            source = root / "application.flatpak"
            original = b"original signed application bytes"
            source.write_bytes(original)
            staging = root / "staging"
            staging.mkdir()
            artifacts = dispatch_module.stage_bundle_files([source], staging)
            source.write_bytes(b"attacker replacement")
            self.assertEqual(len(artifacts), 1)
            artifact = artifacts[0]
            self.assertNotEqual(artifact.path, source)
            self.assertEqual(artifact.path.read_bytes(), original)
            self.assertEqual(artifact.size, len(original))
            self.assertEqual(artifact.sha256, hashlib.sha256(original).hexdigest())


if __name__ == "__main__":
    unittest.main()
