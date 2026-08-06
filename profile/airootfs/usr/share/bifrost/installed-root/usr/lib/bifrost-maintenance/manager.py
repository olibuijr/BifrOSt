#!/usr/bin/python3
"""Read-only maintenance inventory and explicitly authorized system upgrades."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ARCH_NEWS_URL = "https://archlinux.org/feeds/news/"
BIFROST_PACKAGE = "bifrost-system"
BIFROST_REPOSITORY = "bifrost"
NEWS_STATE_FILE = Path.home() / ".local/state/bifrost-maintenance/news.json"
APP_MANAGER_PATH = Path("/usr/lib/bifrost-apps/manager.py")
PRIVILEGED_HELPER = Path("/usr/lib/bifrost-maintenance/system-upgrade")
PACMAN_LOG = Path("/var/log/pacman.log")
TRANSACTION_LOG = Path("/var/log/bifrost-maintenance/transactions.jsonl")
NEWS_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")
PACKAGE_NAME = re.compile(r"^[a-z0-9@._+:-]+$")
TRANSACTION_ID = re.compile(r"^[0-9]{1,16}-[0-9a-f]{32}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_HELPER_RESULT_BYTES = 16 * 1024
UPDATE_LINE = re.compile(r"^([a-z0-9@._+:-]+)\s+(\S+)\s+->\s+(\S+)$")
MAX_NEWS_BYTES = 2 * 1024 * 1024

CommandRunner = Callable[[list[str], Mapping[str, str]], subprocess.CompletedProcess[str]]
NewsFetcher = Callable[[str], bytes]
PrivilegeRunner = Callable[[list[str], str], subprocess.CompletedProcess[str]]
Clock = Callable[[], float]


class MaintenanceError(RuntimeError):
    """A stable, user-displayable backend failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str


@dataclass(frozen=True)
class PackageUpdate:
    name: str
    installed_version: str
    available_version: str


@dataclass(frozen=True)
class ArchStatus:
    ok: bool
    updates: tuple[PackageUpdate, ...]
    error: ErrorInfo | None = None


@dataclass(frozen=True)
class NewsItem:
    item_id: str
    title: str
    url: str
    published: str
    acknowledged: bool


@dataclass(frozen=True)
class NewsStatus:
    ok: bool
    items: tuple[NewsItem, ...]
    unread_ids: tuple[str, ...]
    error: ErrorInfo | None = None


@dataclass(frozen=True)
class SystemPayloadStatus:
    ok: bool
    installed: bool
    installed_version: str | None
    available_version: str | None
    repository: str | None
    signature_validated: bool
    update_available: bool
    error: ErrorInfo | None = None


@dataclass(frozen=True)
class ApplicationUpdate:
    app_id: str
    installed_version: str | None
    available_version: str


@dataclass(frozen=True)
class FlatpakStatus:
    ok: bool
    updates: tuple[ApplicationUpdate, ...]
    error: ErrorInfo | None = None


@dataclass(frozen=True)
class FirmwareUpdate:
    device_id: str
    device_name: str
    version: str


@dataclass(frozen=True)
class FirmwareStatus:
    ok: bool
    updates: tuple[FirmwareUpdate, ...]
    error: ErrorInfo | None = None


@dataclass(frozen=True)
class FailedUnit:
    unit: str
    description: str
    scope: str


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    reboot_required: bool
    reboot_evidence: tuple[str, ...]
    failed_system_units: tuple[FailedUnit, ...]
    failed_user_units: tuple[FailedUnit, ...]
    errors: tuple[ErrorInfo, ...]


@dataclass(frozen=True)
class TransactionLog:
    name: str
    path: str
    exists: bool


@dataclass(frozen=True)
class TransactionsStatus:
    logs: tuple[TransactionLog, ...]


@dataclass(frozen=True)
class MaintenanceSnapshot:
    generated_at: int
    arch: ArchStatus
    news: NewsStatus
    system_payload: SystemPayloadStatus
    flatpak: FlatpakStatus
    firmware: FirmwareStatus
    health: HealthStatus
    transactions: TransactionsStatus

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@dataclass(frozen=True)
class UpgradeResult:
    completed: bool
    returncode: int
    transaction_id: str
    transaction_log: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _error(error: MaintenanceError) -> ErrorInfo:
    return ErrorInfo(error.code, error.message)


def _atomic_json(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _normalize_ids(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not NEWS_ID.fullmatch(value):
            raise MaintenanceError("invalid_news_id", "A news acknowledgement contains an invalid item ID")
        if value in seen:
            raise MaintenanceError("duplicate_news_id", "A news acknowledgement contains a duplicate item ID")
        seen.add(value)
        result.append(value)
    return tuple(result)


class MaintenanceManager:
    def __init__(
        self,
        *,
        command_runner: CommandRunner | None = None,
        news_fetcher: NewsFetcher | None = None,
        privilege_runner: PrivilegeRunner | None = None,
        clock: Clock | None = None,
        news_state_file: Path = NEWS_STATE_FILE,
        app_manager: Any | None = None,
        app_manager_path: Path = APP_MANAGER_PATH,
        checkupdates: str = "/usr/bin/checkupdates",
        pacman: str = "/usr/bin/pacman",
        fwupdmgr: str = "/usr/bin/fwupdmgr",
        systemctl: str = "/usr/bin/systemctl",
        pkexec: str = "/usr/bin/pkexec",
        privileged_helper: Path = PRIVILEGED_HELPER,
        reboot_markers: Iterable[Path] = (
            Path("/run/systemd/reboot-required"),
            Path("/run/reboot-required"),
            Path("/var/run/reboot-required"),
        ),
        pacman_log: Path = PACMAN_LOG,
        transaction_log: Path = TRANSACTION_LOG,
        sync_db_parent: Path | None = None,
    ) -> None:
        self.command_runner = command_runner or self._subprocess_runner
        self.news_fetcher = news_fetcher or self._fetch_news
        self.privilege_runner = privilege_runner or self._privilege_runner
        self.clock = clock or time.time
        self.news_state_file = news_state_file
        self.app_manager = app_manager
        self.app_manager_path = app_manager_path
        self.checkupdates = checkupdates
        self.pacman = pacman
        self.fwupdmgr = fwupdmgr
        self.systemctl = systemctl
        self.pkexec = pkexec
        self.privileged_helper = privileged_helper
        self.reboot_markers = tuple(reboot_markers)
        self.pacman_log = pacman_log
        self.transaction_log = transaction_log
        self.sync_db_parent = sync_db_parent

    @staticmethod
    def _subprocess_runner(command: list[str], environment: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            env=dict(environment),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    @staticmethod
    def _privilege_runner(command: list[str], request: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            input=request,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    @staticmethod
    def _fetch_news(url: str) -> bytes:
        request = Request(url, headers={"User-Agent": "BifrOSt-Maintenance/1"})
        with urlopen(request, timeout=20) as response:
            data = response.read(MAX_NEWS_BYTES + 1)
        if len(data) > MAX_NEWS_BYTES:
            raise MaintenanceError("news_too_large", "The Arch news feed exceeded the safe size limit")
        return data

    def _run(self, command: list[str], *, extra_environment: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        if extra_environment:
            environment.update(extra_environment)
        try:
            return self.command_runner(command, environment)
        except OSError as error:
            raise MaintenanceError("command_unavailable", f"Cannot execute {Path(command[0]).name}: {error}") from error

    @staticmethod
    def _command_error(name: str, completed: subprocess.CompletedProcess[str]) -> MaintenanceError:
        detail = (completed.stderr or "").strip() or (completed.stdout or "").strip()
        if detail:
            detail = detail.splitlines()[0][:300]
        else:
            detail = "the command failed without details"
        return MaintenanceError(f"{name}_failed", f"{name} failed: {detail}")

    def _arch_status(self) -> ArchStatus:
        try:
            with tempfile.TemporaryDirectory(prefix="bifrost-checkupdates-", dir=self.sync_db_parent) as database:
                completed = self._run(
                    [self.checkupdates, "--nocolor"],
                    extra_environment={"CHECKUPDATES_DB": database},
                )
            if completed.returncode not in (0, 2):
                raise self._command_error("checkupdates", completed)
            if completed.returncode == 2:
                if (completed.stdout or "").strip():
                    raise MaintenanceError("malformed_updates", "checkupdates reported no updates but returned package output")
                return ArchStatus(True, ())
            updates: list[PackageUpdate] = []
            for line in (completed.stdout or "").splitlines():
                if not line.strip():
                    continue
                match = UPDATE_LINE.fullmatch(line.strip())
                if not match:
                    raise MaintenanceError("malformed_updates", "checkupdates returned malformed package output")
                updates.append(PackageUpdate(match.group(1), match.group(2), match.group(3)))
            updates.sort(key=lambda item: item.name)
            return ArchStatus(True, tuple(updates))
        except MaintenanceError as error:
            return ArchStatus(False, (), _error(error))
        except OSError as error:
            return ArchStatus(
                False,
                (),
                ErrorInfo("checkupdates_database_failed", f"Cannot create the isolated sync database: {error}"),
            )

    @staticmethod
    def _pacman_fields(output: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        current: str | None = None
        for raw_line in output.splitlines():
            if not raw_line.strip():
                continue
            if raw_line[:1].isspace() and current:
                fields[current] += " " + raw_line.strip()
                continue
            if ":" not in raw_line:
                raise MaintenanceError("malformed_package_info", "pacman returned malformed package metadata")
            key, value = raw_line.split(":", 1)
            key = key.strip()
            if not key or key in fields:
                raise MaintenanceError("malformed_package_info", "pacman returned ambiguous package metadata")
            fields[key] = value.strip()
            current = key
        return fields

    def _system_payload_status(self) -> SystemPayloadStatus:
        installed = False
        installed_version: str | None = None
        installed_signature_validated = False
        try:
            installed_result = self._run([self.pacman, "-Qi", "--", BIFROST_PACKAGE])
            if installed_result.returncode not in (0, 1):
                raise self._command_error("pacman_query", installed_result)
            installed = installed_result.returncode == 0
            installed_fields = self._pacman_fields(installed_result.stdout or "") if installed else {}
            if installed and installed_fields.get("Name") != BIFROST_PACKAGE:
                raise MaintenanceError("wrong_payload_package", "pacman returned metadata for an unexpected installed package")
            installed_version = installed_fields.get("Version") if installed else None
            installed_signature_validated = installed and "Signature" in installed_fields.get("Validated By", "").split()
            if installed and not installed_signature_validated:
                raise MaintenanceError(
                    "untrusted_payload",
                    "The installed BifrOSt system package was not validated by a signature",
                )

            available_result = self._run([self.pacman, "-Si", "--", BIFROST_PACKAGE])
            if available_result.returncode:
                raise self._command_error("pacman_sync_query", available_result)
            available_fields = self._pacman_fields(available_result.stdout or "")
            if available_fields.get("Name") != BIFROST_PACKAGE:
                raise MaintenanceError("wrong_payload_package", "The sync database returned an unexpected package")
            repository = available_fields.get("Repository")
            validation = available_fields.get("Validated By", "")
            if repository != BIFROST_REPOSITORY or "Signature" not in validation.split():
                raise MaintenanceError(
                    "untrusted_payload",
                    "The BifrOSt system package is not from the signed BifrOSt repository",
                )
            available_version = available_fields.get("Version")
            if not available_version or (installed and not installed_version):
                raise MaintenanceError("malformed_package_info", "pacman omitted a required package version")
            return SystemPayloadStatus(
                ok=True,
                installed=installed,
                installed_version=installed_version,
                available_version=available_version,
                repository=repository,
                signature_validated=True,
                update_available=not installed or installed_version != available_version,
            )
        except MaintenanceError as error:
            return SystemPayloadStatus(
                False,
                installed,
                installed_version,
                None,
                None,
                installed_signature_validated and error.code != "untrusted_payload",
                False,
                _error(error),
            )

    def _load_app_manager(self) -> Any:
        if self.app_manager is not None:
            return self.app_manager
        try:
            spec = importlib.util.spec_from_file_location("bifrost_installed_app_manager", self.app_manager_path)
            if spec is None or spec.loader is None:
                raise OSError("cannot create a module loader")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            self.app_manager = module.manager_from_environment()
            return self.app_manager
        except (OSError, ImportError, AttributeError) as error:
            raise MaintenanceError("flatpak_manager_unavailable", f"Cannot load the BifrOSt application manager: {error}") from error

    def _flatpak_status(self, *, refresh: bool) -> FlatpakStatus:
        try:
            records = self._load_app_manager().catalog(refresh=refresh)
            updates: list[ApplicationUpdate] = []
            for record in records:
                if getattr(record, "status", None) != "update":
                    continue
                app_id = getattr(record, "app_id", None)
                installed = getattr(record, "installed_version", None)
                available = getattr(record, "available_version", None)
                if not isinstance(app_id, str) or not isinstance(available, str) or not (
                    installed is None or isinstance(installed, str)
                ):
                    raise MaintenanceError("malformed_flatpak_status", "The application manager returned malformed update data")
                updates.append(ApplicationUpdate(app_id, installed, available))
            updates.sort(key=lambda item: item.app_id)
            return FlatpakStatus(True, tuple(updates))
        except MaintenanceError as error:
            return FlatpakStatus(False, (), _error(error))
        except Exception as error:
            return FlatpakStatus(False, (), ErrorInfo("flatpak_status_failed", str(error)[:300]))

    def _firmware_status(self) -> FirmwareStatus:
        try:
            completed = self._run([self.fwupdmgr, "get-updates", "--json"])
            if completed.returncode == 2 and not (completed.stdout or "").strip():
                return FirmwareStatus(True, ())
            if completed.returncode:
                raise self._command_error("fwupd", completed)
            try:
                document = json.loads(completed.stdout or "")
            except json.JSONDecodeError as error:
                raise MaintenanceError("malformed_firmware", "fwupd returned invalid JSON") from error
            if not isinstance(document, dict) or not isinstance(document.get("Devices"), list):
                raise MaintenanceError("malformed_firmware", "fwupd returned an unexpected JSON document")
            updates: list[FirmwareUpdate] = []
            for device in document.get("Devices", []):
                if not isinstance(device, dict) or not isinstance(device.get("Releases", []), list):
                    raise MaintenanceError("malformed_firmware", "fwupd returned malformed device data")
                device_id = device.get("DeviceId", "")
                device_name = device.get("Name", device_id)
                if not isinstance(device_id, str) or not device_id or not isinstance(device_name, str):
                    raise MaintenanceError("malformed_firmware", "fwupd omitted a device identity")
                for release in device.get("Releases", []):
                    if not isinstance(release, dict) or not isinstance(release.get("Version"), str):
                        raise MaintenanceError("malformed_firmware", "fwupd returned malformed release data")
                    updates.append(FirmwareUpdate(device_id, device_name, release["Version"]))
            updates.sort(key=lambda item: (item.device_name, item.version, item.device_id))
            return FirmwareStatus(True, tuple(updates))
        except MaintenanceError as error:
            return FirmwareStatus(False, (), _error(error))

    def _failed_units(self, *, user: bool) -> tuple[FailedUnit, ...]:
        command = [self.systemctl]
        if user:
            command.append("--user")
        command.extend(["--failed", "--no-pager", "--output=json"])
        completed = self._run(command)
        if completed.returncode:
            raise self._command_error("systemctl_user" if user else "systemctl_system", completed)
        try:
            rows = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as error:
            raise MaintenanceError("malformed_units", "systemctl returned invalid JSON") from error
        if not isinstance(rows, list):
            raise MaintenanceError("malformed_units", "systemctl returned an unexpected JSON document")
        units: list[FailedUnit] = []
        for row in rows:
            if not isinstance(row, dict):
                raise MaintenanceError("malformed_units", "systemctl returned malformed unit data")
            unit = row.get("unit")
            description = row.get("description", "")
            if not isinstance(unit, str) or not unit or not isinstance(description, str):
                raise MaintenanceError("malformed_units", "systemctl omitted a unit identity")
            units.append(FailedUnit(unit, description, "user" if user else "system"))
        units.sort(key=lambda item: item.unit)
        return tuple(units)

    def _health_status(self) -> HealthStatus:
        evidence = tuple(str(path) for path in self.reboot_markers if path.exists())
        errors: list[ErrorInfo] = []
        try:
            system_units = self._failed_units(user=False)
        except MaintenanceError as error:
            system_units = ()
            errors.append(_error(error))
        try:
            user_units = self._failed_units(user=True)
        except MaintenanceError as error:
            user_units = ()
            errors.append(_error(error))
        return HealthStatus(not errors, bool(evidence), evidence, system_units, user_units, tuple(errors))

    def _acknowledged_news(self) -> set[str]:
        try:
            document = json.loads(self.news_state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError) as error:
            raise MaintenanceError("news_state_unreadable", f"Cannot read news acknowledgements: {error}") from error
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise MaintenanceError("malformed_news_state", "News acknowledgements have an unsupported format")
        values = document.get("acknowledged_ids")
        if not isinstance(values, list):
            raise MaintenanceError("malformed_news_state", "News acknowledgements have an unsupported format")
        return set(_normalize_ids(values))

    @staticmethod
    def _child_text(element: ET.Element, name: str) -> str:
        for child in element:
            if child.tag.rsplit("}", 1)[-1] == name:
                return (child.text or "").strip()
        return ""

    def _news_items(self) -> tuple[NewsItem, ...]:
        try:
            payload = self.news_fetcher(ARCH_NEWS_URL)
        except MaintenanceError:
            raise
        except Exception as error:
            raise MaintenanceError("news_fetch_failed", f"Cannot fetch Arch news: {error}") from error
        if not isinstance(payload, bytes) or len(payload) > MAX_NEWS_BYTES:
            raise MaintenanceError("malformed_news", "The Arch news fetcher returned invalid data")
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as error:
            raise MaintenanceError("malformed_news", "The Arch news feed is not valid XML") from error
        elements = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] in ("item", "entry")]
        if not elements:
            raise MaintenanceError("malformed_news", "The Arch news feed contains no news items")
        acknowledged = self._acknowledged_news()
        items: list[NewsItem] = []
        seen: set[str] = set()
        for element in elements:
            title = self._child_text(element, "title")
            item_id = self._child_text(element, "guid") or self._child_text(element, "id")
            link = self._child_text(element, "link")
            if not link:
                for child in element:
                    if child.tag.rsplit("}", 1)[-1] == "link":
                        link = child.attrib.get("href", "").strip()
                        if link:
                            break
            published = self._child_text(element, "pubDate") or self._child_text(element, "published") or self._child_text(element, "updated")
            if not item_id:
                item_id = link
            if (
                not NEWS_ID.fullmatch(item_id)
                or item_id in seen
                or not title
                or not link.startswith("https://archlinux.org/news/")
                or not published
            ):
                raise MaintenanceError("malformed_news", "The Arch news feed contains an invalid item")
            seen.add(item_id)
            items.append(NewsItem(item_id, title, link, published, item_id in acknowledged))
        return tuple(items)

    def _news_status(self) -> NewsStatus:
        try:
            items = self._news_items()
            unread = tuple(item.item_id for item in items if not item.acknowledged)
            return NewsStatus(True, items, unread)
        except MaintenanceError as error:
            return NewsStatus(False, (), (), _error(error))

    def acknowledge_news(self, item_ids: Iterable[str]) -> NewsStatus:
        requested = _normalize_ids(item_ids)
        items = self._news_items()
        available = {item.item_id for item in items}
        if not set(requested).issubset(available):
            raise MaintenanceError("unknown_news_id", "Refusing to acknowledge an item outside the current Arch news feed")
        acknowledged = self._acknowledged_news()
        acknowledged.update(requested)
        try:
            _atomic_json(
                self.news_state_file,
                {"schema_version": 1, "acknowledged_ids": sorted(acknowledged)},
            )
        except OSError as error:
            raise MaintenanceError("news_state_write_failed", f"Cannot save news acknowledgements: {error}") from error
        refreshed = tuple(
            NewsItem(item.item_id, item.title, item.url, item.published, item.item_id in acknowledged)
            for item in items
        )
        return NewsStatus(True, refreshed, tuple(item.item_id for item in refreshed if not item.acknowledged))

    def _transactions_status(self) -> TransactionsStatus:
        return TransactionsStatus(
            (
                TransactionLog("pacman", str(self.pacman_log), self.pacman_log.exists()),
                TransactionLog("bifrost-system-upgrades", str(self.transaction_log), self.transaction_log.exists()),
            )
        )

    def refresh(self, *, refresh_flatpak: bool = True) -> MaintenanceSnapshot:
        return MaintenanceSnapshot(
            generated_at=int(self.clock()),
            arch=self._arch_status(),
            news=self._news_status(),
            system_payload=self._system_payload_status(),
            flatpak=self._flatpak_status(refresh=refresh_flatpak),
            firmware=self._firmware_status(),
            health=self._health_status(),
            transactions=self._transactions_status(),
        )

    def _upgrade_request(self, acknowledged_news_ids: Iterable[str]) -> dict[str, Any]:
        explicitly_acknowledged = _normalize_ids(acknowledged_news_ids)
        news = self._news_status()
        if not news.ok:
            assert news.error is not None
            raise MaintenanceError(news.error.code, news.error.message)
        if set(explicitly_acknowledged) != set(news.unread_ids):
            raise MaintenanceError(
                "unread_news",
                "System upgrade refused: acknowledge exactly every unread Arch news item first",
            )
        if explicitly_acknowledged:
            news = self.acknowledge_news(explicitly_acknowledged)
        return {
            "schema_version": 1,
            "operation": "system-upgrade",
            "news_ids": [item.item_id for item in news.items],
            "acknowledged_news_ids": list(explicitly_acknowledged),
        }

    def apply_system_upgrade(self, acknowledged_news_ids: Iterable[str] = ()) -> UpgradeResult:
        request = self._upgrade_request(acknowledged_news_ids)
        payload = json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            completed = self.privilege_runner([self.pkexec, str(self.privileged_helper)], payload)
        except OSError as error:
            raise MaintenanceError("authorization_failed", f"Cannot start the authorized upgrade helper: {error}") from error
        output = completed.stdout or ""
        if len(output.encode("utf-8")) > MAX_HELPER_RESULT_BYTES:
            raise MaintenanceError("malformed_upgrade_result", "The authorized helper returned an oversized result")
        try:
            result = json.loads(output)
        except json.JSONDecodeError as error:
            failure_output = completed.stderr or ""
            if completed.returncode and len(failure_output.encode("utf-8")) <= MAX_HELPER_RESULT_BYTES:
                try:
                    failure = json.loads(failure_output)
                except json.JSONDecodeError:
                    failure = None
                if (
                    isinstance(failure, dict)
                    and set(failure) == {"completed", "error"}
                    and failure.get("completed") is False
                    and isinstance(failure.get("error"), dict)
                    and set(failure["error"]) == {"code", "message"}
                    and isinstance(failure["error"].get("code"), str)
                    and ERROR_CODE.fullmatch(failure["error"]["code"])
                    and isinstance(failure["error"].get("message"), str)
                    and len(failure["error"]["message"]) <= 300
                ):
                    raise MaintenanceError(failure["error"]["code"], failure["error"]["message"]) from error
            code = "authorization_failed" if completed.returncode else "malformed_upgrade_result"
            raise MaintenanceError(code, "The authorized upgrade helper returned no valid result") from error
        expected_keys = {"completed", "returncode", "transaction_id", "transaction_log"}
        if not isinstance(result, dict) or set(result) != expected_keys:
            raise MaintenanceError("malformed_upgrade_result", "The authorized helper returned an unsupported result")
        transaction_id = result.get("transaction_id")
        transaction_log = result.get("transaction_log")
        returncode = result.get("returncode")
        finished = result.get("completed")
        if (
            not isinstance(finished, bool)
            or not isinstance(returncode, int)
            or isinstance(returncode, bool)
            or not isinstance(transaction_id, str)
            or not TRANSACTION_ID.fullmatch(transaction_id)
            or not isinstance(transaction_log, str)
            or transaction_log != f"/var/log/bifrost-maintenance/system-upgrade-{transaction_id}.log"
            or finished != (returncode == 0)
            or completed.returncode != returncode
        ):
            raise MaintenanceError("malformed_upgrade_result", "The authorized helper returned inconsistent status")
        return UpgradeResult(finished, returncode, transaction_id, transaction_log)

    def status_json(self, *, refresh_flatpak: bool = True) -> str:
        return self.refresh(refresh_flatpak=refresh_flatpak).to_json()


def manager_from_environment() -> MaintenanceManager:
    """Create the production manager, with path overrides only for isolated tests."""
    return MaintenanceManager(
        news_state_file=Path(os.environ.get("BIFROST_MAINTENANCE_NEWS_STATE", str(NEWS_STATE_FILE))),
        app_manager_path=Path(os.environ.get("BIFROST_APP_MANAGER", str(APP_MANAGER_PATH))),
    )
