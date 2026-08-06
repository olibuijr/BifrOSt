from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
    def __init__(self, responses):
        self.responses = responses
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
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

    def make_manager(self, responses):
        runner = FakeFlatpak(responses)
        manager = manager_module.FlatpakAppManager(
            remote_name="bifrost-test",
            repository_file=self.definition,
            expected_url=self.URL,
            state_file=self.state,
            flatpak="flatpak-test",
            runner=runner,
        )
        return manager, runner

    def responses(self, *, update=True):
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
        return {
            ("remotes", "--user", "--columns=name,url,options"): f"bifrost-test\t{self.URL}\tuser\n",
            ("update", "--user", "--appstream", "bifrost-test", "--noninteractive"): "",
            ("info", "--user", "--show-commit", self.APP): f"{self.OLD}\n",
            ("remote-info", "--user", "--show-commit", "bifrost-test", self.APP): f"{self.NEW}\n",
            (
                "remote-ls",
                "--user",
                "--app",
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

    def test_update_records_previous_commit_before_flatpak_update(self):
        manager, runner = self.make_manager(self.responses())
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


if __name__ == "__main__":
    unittest.main()
