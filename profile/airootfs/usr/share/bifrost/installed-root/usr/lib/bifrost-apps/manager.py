#!/usr/bin/python3
"""Flatpak-backed update engine for first-party BifrOSt applications."""

from __future__ import annotations

import base64
import binascii
import configparser
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterable

REMOTE_NAME = "bifrost"
EXPECTED_REPOSITORY_URL = "https://olibuijr.github.io/BifrOSt/flatpak/repo/"
REMOTE_FILE = Path("/usr/share/bifrost/apps/bifrost.flatpakrepo")
STATE_FILE = Path.home() / ".local/state/bifrost-apps/history.json"
APP_ID = re.compile(r"^org\.bifrost\.[A-Za-z0-9][A-Za-z0-9._-]*$")
COMMIT = re.compile(r"^[0-9a-f]{64}$")
FINGERPRINT = re.compile(r"^[0-9A-F]{40}$")
OSTREE_FINGERPRINT = re.compile(r"(?:Key|Fingerprint):\s*([0-9A-Fa-f ]{40,64})")
MAX_HISTORY = 8


class UpdateError(RuntimeError):
    """A safe, user-displayable update failure."""


@dataclass(frozen=True)
class AppRecord:
    app_id: str
    name: str
    description: str
    branch: str
    installed_version: str | None
    available_version: str | None
    installed_commit: str | None
    available_commit: str | None
    status: str
    rollback_available: bool


@dataclass(frozen=True)
class UpdateResult:
    app_id: str
    status: str
    error: str | None = None


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _application_id(item: dict[str, Any]) -> str:
    value = item.get("application_id", item.get("application", ""))
    return value if isinstance(value, str) else ""


def _string(item: dict[str, Any], key: str, default: str = "") -> str:
    value = item.get(key, default)
    return value if isinstance(value, str) else default


def _safe_detail(value: str) -> str:
    compact = " ".join(value.split())
    printable = "".join(character if character.isprintable() else "?" for character in compact)
    return printable[:500] or "The application service failed without details"


def _default_flatpak_repository() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home) if data_home else Path.home() / ".local/share"
    return root / "flatpak/repo"


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
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


class FlatpakAppManager:
    def __init__(
        self,
        *,
        remote_name: str = REMOTE_NAME,
        repository_file: Path = REMOTE_FILE,
        expected_url: str = EXPECTED_REPOSITORY_URL,
        state_file: Path = STATE_FILE,
        flatpak: str = "flatpak",
        ostree: str = "ostree",
        gpg: str = "gpg",
        flatpak_repository: Path | None = None,
        runner: Runner | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", remote_name):
            raise ValueError("invalid Flatpak remote name")
        self.remote_name = remote_name
        self.repository_file = repository_file
        self.expected_url = expected_url.rstrip("/") + "/"
        self.state_file = state_file
        self.flatpak = flatpak
        self.ostree = ostree
        self.gpg = gpg
        self.flatpak_repository = flatpak_repository or _default_flatpak_repository()
        self.runner = runner or self._subprocess_runner

    def _subprocess_runner(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["LC_ALL"] = "C.UTF-8"
        return subprocess.run(
            command,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _completed(self, command: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return self.runner(command)
        except OSError:
            return None

    def _execute(self, command: list[str]) -> str:
        completed = self._completed(command)
        if completed is None:
            raise UpdateError("A required application service is unavailable")
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise UpdateError(_safe_detail(detail))
        return completed.stdout

    def _run(self, arguments: Iterable[str]) -> str:
        return self._execute([self.flatpak, *arguments])

    def _json(self, arguments: Iterable[str]) -> list[dict[str, Any]]:
        output = self._run(arguments)
        if not output.strip():
            return []
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise UpdateError("Flatpak returned invalid application metadata") from error
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise UpdateError("Flatpak returned an unexpected application metadata document")
        return value

    def _full_commit(self, arguments: Iterable[str]) -> str:
        commit = self._run(arguments).strip()
        if not COMMIT.fullmatch(commit):
            raise UpdateError("Flatpak returned an invalid OSTree commit identity")
        return commit

    def _repository_definition(self) -> tuple[str, bytes]:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with self.repository_file.open(encoding="utf-8") as source:
                parser.read_file(source)
        except (OSError, configparser.Error) as error:
            raise UpdateError("Cannot read the trusted BifrOSt repository definition") from error
        if "Flatpak Repo" not in parser:
            raise UpdateError("The BifrOSt repository definition has no [Flatpak Repo] section")
        section = parser["Flatpak Repo"]
        url = section.get("Url", "").strip()
        encoded_key = "".join(section.get("GPGKey", "").split())
        if url.rstrip("/") + "/" != self.expected_url:
            raise UpdateError("The BifrOSt repository URL does not match the trusted system definition")
        if not encoded_key:
            raise UpdateError("The BifrOSt repository definition contains no signing key")
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except (binascii.Error, ValueError) as error:
            raise UpdateError("The BifrOSt repository definition contains an invalid signing key") from error
        if not key:
            raise UpdateError("The BifrOSt repository definition contains an empty signing key")
        return url, key

    def _embedded_key_fingerprints(self, key: bytes) -> set[str]:
        try:
            with tempfile.NamedTemporaryFile(prefix="bifrost-app-key-", suffix=".gpg") as key_file:
                os.fchmod(key_file.fileno(), 0o600)
                key_file.write(key)
                key_file.flush()
                listing = self._execute(
                    [self.gpg, "--batch", "--with-colons", "--show-keys", key_file.name]
                )
        except OSError as error:
            raise UpdateError("Cannot inspect the trusted BifrOSt signing key") from error
        fingerprints: set[str] = set()
        expect_fingerprint = False
        for line in listing.splitlines():
            fields = line.split(":")
            record_type = fields[0] if fields else ""
            if record_type == "pub":
                expect_fingerprint = True
            elif record_type == "fpr" and expect_fingerprint and len(fields) > 9:
                fingerprint = fields[9].upper()
                if FINGERPRINT.fullmatch(fingerprint):
                    fingerprints.add(fingerprint)
                expect_fingerprint = False
        if not fingerprints:
            raise UpdateError("The trusted BifrOSt signing key has no valid fingerprint")
        return fingerprints

    def _configured_key_fingerprints(self) -> set[str]:
        listing = self._execute(
            [
                self.ostree,
                f"--repo={self.flatpak_repository}",
                "remote",
                "gpg-list-keys",
                self.remote_name,
            ]
        )
        fingerprints: set[str] = set()
        for line in listing.splitlines():
            match = OSTREE_FINGERPRINT.search(line)
            if not match:
                continue
            fingerprint = re.sub(r"\s+", "", match.group(1)).upper()
            if FINGERPRINT.fullmatch(fingerprint):
                fingerprints.add(fingerprint)
        if not fingerprints:
            raise UpdateError("The configured BifrOSt remote has no trusted signing key")
        return fingerprints

    def _verify_remote_key(self, embedded_key: bytes) -> None:
        expected = self._embedded_key_fingerprints(embedded_key)
        configured = self._configured_key_fingerprints()
        if configured != expected:
            raise UpdateError("The configured BifrOSt remote signing key does not match the trusted system definition")

    def ensure_remote(self, *, refresh: bool = True) -> None:
        expected_url, embedded_key = self._repository_definition()
        rows = self._run(["remotes", "--user", "--columns=name,url,options"])
        found = False
        for raw_line in rows.splitlines():
            fields = raw_line.split("\t")
            if not fields or fields[0] != self.remote_name:
                continue
            found = True
            configured_url = fields[1] if len(fields) > 1 else ""
            options = set((fields[2] if len(fields) > 2 else "").split(","))
            if configured_url.rstrip("/") + "/" != expected_url.rstrip("/") + "/":
                raise UpdateError(f"Flatpak remote {self.remote_name!r} has an unexpected URL")
            if "no-gpg-verify" in options or "no-gpg-verify-summary" in options:
                raise UpdateError(f"Flatpak remote {self.remote_name!r} has signature verification disabled")
        if not found:
            self._run(
                [
                    "remote-add",
                    "--user",
                    "--from",
                    self.remote_name,
                    str(self.repository_file),
                ]
            )
        self._verify_remote_key(embedded_key)
        if refresh:
            self._run(["update", "--user", "--appstream", self.remote_name, "--noninteractive"])

    def _state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "apps": {}}
        except (OSError, json.JSONDecodeError) as error:
            raise UpdateError("Cannot read application update history") from error
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("apps"), dict):
            raise UpdateError("Update history has an unsupported format")
        return value

    def _history(self, app_id: str) -> list[dict[str, Any]]:
        value = self._state()["apps"].get(app_id, [])
        if not isinstance(value, list):
            raise UpdateError(f"Update history for {app_id} is invalid")
        valid: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            commit = item.get("commit")
            version = item.get("version")
            recorded_at = item.get("recorded_at")
            if isinstance(commit, str) and COMMIT.fullmatch(commit) and isinstance(version, str) and isinstance(recorded_at, int):
                valid.append({"commit": commit, "version": version, "recorded_at": recorded_at})
        return valid

    def _remember(self, app_id: str, commit: str, version: str) -> None:
        if not APP_ID.fullmatch(app_id) or not COMMIT.fullmatch(commit):
            raise UpdateError("Refusing to record an invalid application identity or commit")
        state = self._state()
        history = self._history(app_id)
        history = [item for item in history if item["commit"] != commit]
        history.append({"commit": commit, "version": version, "recorded_at": int(time.time())})
        state["apps"][app_id] = history[-MAX_HISTORY:]
        try:
            _atomic_json(self.state_file, state)
        except OSError as error:
            raise UpdateError("Cannot write application update history") from error

    def _listed_commit(
        self,
        item: dict[str, Any],
        key: str,
        fallback_arguments: list[str],
    ) -> str:
        commit = _string(item, key)
        if COMMIT.fullmatch(commit):
            return commit
        return self._full_commit(fallback_arguments)

    def catalog(self, *, refresh: bool = True) -> list[AppRecord]:
        self.ensure_remote(refresh=refresh)
        cached = [] if refresh else ["--cached"]
        available = self._json(
            [
                "remote-ls",
                "--user",
                "--app",
                *cached,
                "--columns=application,name,description,version,branch,origin,commit:full",
                "--json",
                self.remote_name,
            ]
        )
        installed = self._json(
            [
                "list",
                "--user",
                "--app",
                "--columns=application,name,description,version,branch,origin,active:full",
                "--json",
            ]
        )
        updates = self._json(
            [
                "remote-ls",
                "--user",
                "--app",
                "--updates",
                *cached,
                "--columns=application,name,description,version,branch,origin,commit:full",
                "--json",
                self.remote_name,
            ]
        )
        installed_by_id = {
            _application_id(item): item
            for item in installed
            if _string(item, "origin") == self.remote_name and APP_ID.fullmatch(_application_id(item))
        }
        update_ids = {
            _application_id(item)
            for item in updates
            if APP_ID.fullmatch(_application_id(item))
        }
        records: list[AppRecord] = []
        published_ids: set[str] = set()
        for item in available:
            app_id = _application_id(item)
            if not APP_ID.fullmatch(app_id):
                continue
            published_ids.add(app_id)
            current = installed_by_id.get(app_id)
            installed_commit = None
            if current:
                installed_commit = self._listed_commit(
                    current,
                    "active_commit",
                    ["info", "--user", "--show-commit", app_id],
                )
            available_commit = self._listed_commit(
                item,
                "commit",
                ["remote-info", "--user", *cached, "--show-commit", self.remote_name, app_id],
            )
            status = "available"
            if current:
                status = "update" if app_id in update_ids else "installed"
            rollback = any(entry["commit"] != installed_commit for entry in self._history(app_id))
            records.append(
                AppRecord(
                    app_id=app_id,
                    name=_string(item, "name") or app_id,
                    description=_string(item, "description"),
                    branch=_string(item, "branch", "stable"),
                    installed_version=(_string(current, "version") or "Unknown") if current else None,
                    available_version=_string(item, "version") or "Unknown",
                    installed_commit=installed_commit,
                    available_commit=available_commit,
                    status=status,
                    rollback_available=rollback,
                )
            )
        for app_id, current in installed_by_id.items():
            if app_id in published_ids:
                continue
            installed_commit = self._listed_commit(
                current,
                "active_commit",
                ["info", "--user", "--show-commit", app_id],
            )
            rollback = any(entry["commit"] != installed_commit for entry in self._history(app_id))
            records.append(
                AppRecord(
                    app_id=app_id,
                    name=_string(current, "name") or app_id,
                    description=_string(current, "description"),
                    branch=_string(current, "branch", "stable"),
                    installed_version=_string(current, "version") or "Unknown",
                    available_version=None,
                    installed_commit=installed_commit,
                    available_commit=None,
                    status="withdrawn",
                    rollback_available=rollback,
                )
            )
        return sorted(records, key=lambda record: (record.status != "update", record.name.casefold(), record.app_id))

    def _record_for(self, app_id: str, *, refresh: bool = False) -> AppRecord:
        if not APP_ID.fullmatch(app_id):
            raise UpdateError("Application ID is outside the trusted org.bifrost namespace")
        for record in self.catalog(refresh=refresh):
            if record.app_id == app_id:
                return record
        raise UpdateError(f"{app_id} is neither published nor installed as a BifrOSt application")

    def install(self, app_id: str) -> None:
        record = self._record_for(app_id)
        if record.status != "available":
            raise UpdateError(f"{app_id} is already installed")
        self._run(["install", "--user", "--noninteractive", self.remote_name, app_id])

    def _update_record(self, record: AppRecord) -> None:
        if record.installed_commit:
            self._remember(record.app_id, record.installed_commit, record.installed_version or "Unknown")
        self._run(["update", "--user", "--noninteractive", record.app_id])

    def update(self, app_id: str) -> None:
        record = self._record_for(app_id)
        if record.status == "available":
            raise UpdateError(f"{app_id} is not installed")
        if record.status != "update":
            raise UpdateError(f"No update is available for {app_id}")
        self._update_record(record)

    def update_all(self) -> list[UpdateResult]:
        results: list[UpdateResult] = []
        for record in self.catalog():
            if record.status != "update":
                continue
            try:
                self._update_record(record)
            except UpdateError as error:
                results.append(UpdateResult(record.app_id, "failed", str(error)))
            else:
                results.append(UpdateResult(record.app_id, "completed"))
        return results

    def remove(self, app_id: str) -> None:
        record = self._record_for(app_id, refresh=False)
        if record.status == "available":
            raise UpdateError(f"{app_id} is not installed")
        self._run(["uninstall", "--user", "--noninteractive", app_id])

    def _preflight_rollback(self, app_id: str, commit: str) -> None:
        local = self._completed(
            [self.ostree, f"--repo={self.flatpak_repository}", "show", commit]
        )
        if local is not None and local.returncode == 0:
            return
        remote = self._completed(
            [
                self.flatpak,
                "remote-info",
                "--user",
                "--show-commit",
                f"--commit={commit}",
                self.remote_name,
                app_id,
            ]
        )
        if remote is not None and remote.returncode == 0 and remote.stdout.strip() == commit:
            return
        raise UpdateError(f"The recorded rollback version for {app_id} is no longer available")

    def rollback(self, app_id: str) -> str:
        record = self._record_for(app_id, refresh=False)
        if record.status == "available" or not record.installed_commit:
            raise UpdateError(f"{app_id} is not installed or has no recorded commit")
        target = next(
            (item for item in reversed(self._history(app_id)) if item["commit"] != record.installed_commit),
            None,
        )
        if target is None:
            raise UpdateError(f"No previous BifrOSt-managed version is recorded for {app_id}")
        self._preflight_rollback(app_id, target["commit"])
        self._remember(app_id, record.installed_commit, record.installed_version or "Unknown")
        self._run(
            [
                "update",
                "--user",
                "--noninteractive",
                f"--commit={target['commit']}",
                app_id,
            ]
        )
        return target["commit"]

    def catalog_json(self, *, refresh: bool = True) -> str:
        return json.dumps([asdict(record) for record in self.catalog(refresh=refresh)], indent=2, sort_keys=True) + "\n"


def manager_from_environment() -> FlatpakAppManager:
    """Use production defaults unless an isolated test explicitly overrides them."""
    repository = os.environ.get("BIFROST_APP_FLATPAK_REPOSITORY")
    return FlatpakAppManager(
        remote_name=os.environ.get("BIFROST_APP_REMOTE", REMOTE_NAME),
        repository_file=Path(os.environ.get("BIFROST_APP_REPOSITORY_FILE", str(REMOTE_FILE))),
        expected_url=os.environ.get("BIFROST_APP_REPOSITORY_URL", EXPECTED_REPOSITORY_URL),
        state_file=Path(os.environ.get("BIFROST_APP_STATE_FILE", str(STATE_FILE))),
        flatpak=os.environ.get("BIFROST_APP_FLATPAK", "flatpak"),
        ostree=os.environ.get("BIFROST_APP_OSTREE", "ostree"),
        gpg=os.environ.get("BIFROST_APP_GPG", "gpg"),
        flatpak_repository=Path(repository) if repository else None,
    )
