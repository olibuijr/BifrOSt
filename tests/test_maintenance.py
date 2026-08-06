from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
INSTALLED_ROOT = ROOT / "profile/airootfs/usr/share/bifrost/installed-root"
MANAGER_PATH = INSTALLED_ROOT / "usr/lib/bifrost-maintenance/manager.py"
HELPER_PATH = INSTALLED_ROOT / "usr/lib/bifrost-maintenance/system-upgrade"
POLICY_PATH = INSTALLED_ROOT / "usr/share/polkit-1/actions/org.bifrost.maintenance.policy"


def load_python(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


manager_module = load_python("bifrost_maintenance_manager", MANAGER_PATH)
helper_module = load_python("bifrost_system_upgrade", HELPER_PATH)


NEWS = b"""<?xml version='1.0'?>
<rss><channel>
  <item><guid>news-2</guid><title>Second notice</title><link>https://archlinux.org/news/second/</link><pubDate>Thu, 02 Jan 2025 00:00:00 +0000</pubDate></item>
  <item><guid>news-1</guid><title>First notice</title><link>https://archlinux.org/news/first/</link><pubDate>Wed, 01 Jan 2025 00:00:00 +0000</pubDate></item>
</channel></rss>
"""


class FakeRunner:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, command, environment):
        self.calls.append((command, dict(environment)))
        response = self.responses.get(tuple(command), (0, "", ""))
        return subprocess.CompletedProcess(command, response[0], response[1], response[2])


class FakeApps:
    def catalog(self, *, refresh):
        return [
            type(
                "Record",
                (),
                {
                    "status": "update",
                    "app_id": "org.bifrost.Editor",
                    "installed_version": "1",
                    "available_version": "2",
                },
            )()
        ]


class MaintenanceManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "news.json"

    def tearDown(self):
        self.temporary.cleanup()

    def manager(self, *, runner=None, news=NEWS, privilege_runner=None):
        return manager_module.MaintenanceManager(
            command_runner=runner or FakeRunner(),
            news_fetcher=lambda _url: news,
            privilege_runner=privilege_runner or (
                lambda command, request: subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "completed": True,
                            "returncode": 0,
                            "transaction_id": "1234-" + "a" * 32,
                            "transaction_log": "/var/log/bifrost-maintenance/system-upgrade-1234-" + "a" * 32 + ".log",
                        }
                    ),
                    "",
                )
            ),
            clock=lambda: 1234,
            news_state_file=self.state,
            app_manager=FakeApps(),
            sync_db_parent=self.root,
            reboot_markers=(self.root / "reboot-required",),
            pacman_log=self.root / "pacman.log",
            transaction_log=self.root / "transactions.jsonl",
        )

    def test_checkupdates_uses_isolated_sync_database_and_parses_strictly(self):
        runner = FakeRunner(
            {
                ("/usr/bin/checkupdates", "--nocolor"): (
                    0,
                    "linux 6.1-1 -> 6.2-1\nbifrost-system 0.2.0-1 -> 0.2.1-1\n",
                    "",
                )
            }
        )
        status = self.manager(runner=runner)._arch_status()
        self.assertTrue(status.ok)
        self.assertEqual([item.name for item in status.updates], ["bifrost-system", "linux"])
        command, environment = runner.calls[0]
        self.assertEqual(command, ["/usr/bin/checkupdates", "--nocolor"])
        self.assertIn("CHECKUPDATES_DB", environment)
        self.assertNotEqual(environment["CHECKUPDATES_DB"], "/var/lib/pacman")
        self.assertFalse(Path(environment["CHECKUPDATES_DB"]).exists())

    def test_checkupdates_accepts_both_documented_no_update_exit_shapes(self):
        for returncode in (0, 2):
            with self.subTest(returncode=returncode):
                runner = FakeRunner({("/usr/bin/checkupdates", "--nocolor"): (returncode, "", "")})
                status = self.manager(runner=runner)._arch_status()
                self.assertTrue(status.ok)
                self.assertEqual(status.updates, ())


    def test_malformed_update_output_fails_closed(self):
        runner = FakeRunner({("/usr/bin/checkupdates", "--nocolor"): (0, "linux maybe-new\n", "")})
        status = self.manager(runner=runner)._arch_status()
        self.assertFalse(status.ok)
        self.assertEqual(status.updates, ())
        self.assertEqual(status.error.code, "malformed_updates")

    def test_signed_bifrost_payload_must_come_from_expected_repository(self):
        installed = "Name : bifrost-system\nVersion : 0.2.0-1\nValidated By : Signature\n"
        unsigned = (
            "Repository : attacker\nName : bifrost-system\nVersion : 9.9.9-1\n"
            "Validated By : None\n"
        )
        runner = FakeRunner(
            {
                ("/usr/bin/pacman", "-Qi", "--", "bifrost-system"): (0, installed, ""),
                ("/usr/bin/pacman", "-Si", "--", "bifrost-system"): (0, unsigned, ""),
            }
        )
        status = self.manager(runner=runner)._system_payload_status()
        self.assertFalse(status.ok)
        self.assertFalse(status.signature_validated)
        self.assertEqual(status.error.code, "untrusted_payload")

    def test_missing_sync_database_preserves_installed_payload_facts(self):
        installed = "Name : bifrost-system\nVersion : 0.2.1-1\nValidated By : Signature\n"
        runner = FakeRunner(
            {
                ("/usr/bin/pacman", "-Qi", "--", "bifrost-system"): (0, installed, ""),
                ("/usr/bin/pacman", "-Si", "--", "bifrost-system"): (1, "", "database missing"),
            }
        )
        status = self.manager(runner=runner)._system_payload_status()
        self.assertFalse(status.ok)
        self.assertTrue(status.installed)
        self.assertEqual(status.installed_version, "0.2.1-1")
        self.assertTrue(status.signature_validated)
        self.assertEqual(status.error.code, "pacman_sync_query_failed")

    def test_malformed_news_blocks_upgrade_without_invoking_privilege(self):
        invoked = []
        manager = self.manager(
            news=b"<rss><channel><item></channel>",
            privilege_runner=lambda command, request: invoked.append((command, request)),
        )
        with self.assertRaisesRegex(manager_module.MaintenanceError, "valid XML"):
            manager.apply_system_upgrade()
        self.assertEqual(invoked, [])

    def test_upgrade_requires_exact_unread_ids_and_persists_acknowledgement(self):
        requests = []

        def authorize(command, request):
            requests.append((command, json.loads(request)))
            result = {
                "completed": True,
                "returncode": 0,
                "transaction_id": "1234-" + "b" * 32,
                "transaction_log": "/var/log/bifrost-maintenance/system-upgrade-1234-" + "b" * 32 + ".log",
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

        manager = self.manager(privilege_runner=authorize)
        with self.assertRaisesRegex(manager_module.MaintenanceError, "exactly every unread"):
            manager.apply_system_upgrade(["news-2"])
        self.assertEqual(requests, [])
        result = manager.apply_system_upgrade(["news-2", "news-1"])
        self.assertTrue(result.completed)
        self.assertEqual(requests[0][0], ["/usr/bin/pkexec", "/usr/lib/bifrost-maintenance/system-upgrade"])
        self.assertEqual(requests[0][1]["operation"], "system-upgrade")
        self.assertEqual(requests[0][1]["news_ids"], ["news-2", "news-1"])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["acknowledged_ids"], ["news-1", "news-2"])

    def test_pacman_failure_returns_bounded_structured_status(self):
        transaction_id = "1234-" + "c" * 32
        payload = {
            "completed": False,
            "returncode": 1,
            "transaction_id": transaction_id,
            "transaction_log": f"/var/log/bifrost-maintenance/system-upgrade-{transaction_id}.log",
        }

        def authorize(command, request):
            return subprocess.CompletedProcess(command, 1, json.dumps(payload), "")

        result = self.manager(privilege_runner=authorize).apply_system_upgrade(["news-2", "news-1"])
        self.assertFalse(result.completed)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.transaction_id, transaction_id)

    def test_refresh_contract_is_deterministic_and_json_safe(self):
        installed = "Name : bifrost-system\nVersion : 0.2.0-1\nValidated By : Signature\n"
        available = (
            "Repository : bifrost\nName : bifrost-system\nVersion : 0.2.1-1\n"
            "Validated By : Signature\n"
        )
        runner = FakeRunner(
            {
                ("/usr/bin/checkupdates", "--nocolor"): (2, "", ""),
                ("/usr/bin/pacman", "-Qi", "--", "bifrost-system"): (0, installed, ""),
                ("/usr/bin/pacman", "-Si", "--", "bifrost-system"): (0, available, ""),
                ("/usr/bin/fwupdmgr", "get-updates", "--json"): (0, '{"Devices": []}', ""),
                ("/usr/bin/systemctl", "--failed", "--no-pager", "--output=json"): (0, "[]", ""),
                ("/usr/bin/systemctl", "--user", "--failed", "--no-pager", "--output=json"): (0, "[]", ""),
            }
        )
        snapshot = self.manager(runner=runner).refresh()
        document = json.loads(snapshot.to_json())
        self.assertEqual(document["generated_at"], 1234)
        self.assertEqual(
            set(document) - {"generated_at"},
            {"arch", "news", "system_payload", "flatpak", "firmware", "health", "transactions"},
        )
        self.assertEqual(document["flatpak"]["updates"][0]["app_id"], "org.bifrost.Editor")
        self.assertTrue(document["system_payload"]["signature_validated"])
        self.assertTrue(document["system_payload"]["update_available"])


class PrivilegedHelperTest(unittest.TestCase):
    def request(self):
        return {
            "schema_version": 1,
            "operation": "system-upgrade",
            "news_ids": ["news-1"],
            "acknowledged_news_ids": ["news-1"],
        }

    def test_request_schema_rejects_extra_fields_and_ids_outside_snapshot(self):
        request = self.request()
        request["pacman_arguments"] = ["--remove", "dangerous"]
        with self.assertRaises(helper_module.HelperError):
            helper_module.parse_request(json.dumps(request).encode())
        request = self.request()
        request["acknowledged_news_ids"] = ["different-news"]
        with self.assertRaises(helper_module.HelperError):
            helper_module.parse_request(json.dumps(request).encode())

    def test_only_complete_interactive_authorized_upgrade_command_is_constructible(self):
        self.assertEqual(
            helper_module.pacman_command(),
            ("/usr/bin/pacman", "-Syu"),
        )
        self.assertEqual(helper_module.main(["--noconfirm"]), 2)

    def test_polkit_policy_binds_authorization_to_exact_helper(self):
        root = ET.parse(POLICY_PATH).getroot()
        action = root.find("./action[@id='org.bifrost.maintenance.system-upgrade']")
        self.assertIsNotNone(action)
        annotations = {item.attrib["key"]: item.text for item in action.findall("annotate")}
        self.assertEqual(
            annotations["org.freedesktop.policykit.exec.path"],
            "/usr/lib/bifrost-maintenance/system-upgrade",
        )
        self.assertEqual(annotations["org.freedesktop.policykit.exec.allow_gui"], "true")
        self.assertEqual(action.findtext("./defaults/allow_active"), "auth_admin")
        self.assertEqual(action.findtext("./defaults/allow_any"), "no")


if __name__ == "__main__":
    unittest.main()
