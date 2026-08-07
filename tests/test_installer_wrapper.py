from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = ROOT / "profile/airootfs/usr/local/lib/bifrost-installer-backend"
WRAPPER_HEREDOC = re.compile(
    r"cat >\"\$wrapper_dir/pacstrap\" <<'SH'\n(?P<body>.*?)\nSH\n",
    re.DOTALL,
)
LIVE_GNUPG_TARGET = "/mnt/etc/pacman.d/gnupg"
LIVE_PACSTRAP_TARGET = "/usr/bin/pacstrap"

FAKE_PACSTRAP = """#!/usr/bin/bash
set -Eeuo pipefail
: >"$BIFROST_TEST_ARGV_FILE"
for argument in "$@"; do
    printf '%s\\n' "$argument" >>"$BIFROST_TEST_ARGV_FILE"
done
exit "${BIFROST_TEST_PACSTRAP_RC:-0}"
"""


def extract_wrapper_body() -> str:
    text = BACKEND_PATH.read_text(encoding="utf-8")
    match = WRAPPER_HEREDOC.search(text)
    if match is None:
        raise AssertionError(
            "bifrost-installer-backend no longer writes the constrained pacstrap wrapper heredoc"
        )
    return match.group("body")


class InstallerPacstrapWrapperTest(unittest.TestCase):
    """Execute the exact runtime pacstrap wrapper the backend installs.

    The wrapper text is extracted verbatim from the backend script; only the
    two hardcoded absolute targets (/mnt keyring directory and the real
    pacstrap binary) are redirected into the sandbox so the constrained logic
    itself runs unmodified.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.wrapper_body = extract_wrapper_body()
        for marker in (LIVE_GNUPG_TARGET, LIVE_PACSTRAP_TARGET, "-K"):
            if marker not in cls.wrapper_body:
                raise AssertionError(f"pacstrap wrapper no longer references {marker}")

    def setUp(self) -> None:
        self.sandbox = Path(tempfile.mkdtemp(prefix="bifrost-wrapper-test-"))
        self.addCleanup(self._cleanup)
        self.bin_dir = self.sandbox / "bin"
        self.bin_dir.mkdir()
        self.gnupg_dir = self.sandbox / "target/etc/pacman.d/gnupg"
        self.argv_file = self.sandbox / "pacstrap.argv"

        fake_pacstrap = self.bin_dir / "pacstrap-real"
        fake_pacstrap.write_text(FAKE_PACSTRAP, encoding="utf-8")
        fake_pacstrap.chmod(0o700)

        body = self.wrapper_body.replace(LIVE_GNUPG_TARGET, str(self.gnupg_dir))
        body = body.replace(LIVE_PACSTRAP_TARGET, str(fake_pacstrap))
        wrapper = self.bin_dir / "pacstrap"
        wrapper.write_text(body + "\n", encoding="utf-8")
        wrapper.chmod(0o700)
        self.wrapper = wrapper

    def _cleanup(self) -> None:
        subprocess.run(["rm", "-rf", str(self.sandbox)], check=False)

    def run_wrapper(self, *arguments: str, returncode: int = 0, pacman_conf: str | None = None):
        environment = {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '/usr/bin')}",
            "BIFROST_TEST_ARGV_FILE": str(self.argv_file),
            "BIFROST_TEST_PACSTRAP_RC": str(returncode),
        }
        if pacman_conf is not None:
            environment["BIFROST_PACMAN_CONF"] = pacman_conf
        return subprocess.run(
            ["pacstrap", *arguments],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def recorded_argv(self) -> list[str]:
        return self.argv_file.read_text(encoding="utf-8").splitlines()

    def test_strips_exact_dash_k_and_preserves_everything_else(self) -> None:
        completed = self.run_wrapper("-C", "/etc/pacman.conf", "-K", "/mnt", "base", "-Keep")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            self.recorded_argv(),
            ["-C", "/etc/pacman.conf", "/mnt", "base", "-Keep"],
        )

    def test_empty_target_keyring_directory_is_removed(self) -> None:
        self.gnupg_dir.mkdir(parents=True)
        completed = self.run_wrapper("/mnt", "base")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(
            self.gnupg_dir.exists(),
            "empty Archinstall-created keyring directory must be removed so "
            "pacstrap copies the populated live keyring",
        )

    def test_populated_target_keyring_directory_is_preserved(self) -> None:
        self.gnupg_dir.mkdir(parents=True)
        keyring = self.gnupg_dir / "pubring.gpg"
        keyring.write_bytes(b"keyring-data")
        completed = self.run_wrapper("/mnt", "base")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(self.gnupg_dir.is_dir(), "populated keyring directory must be preserved")
        self.assertEqual(keyring.read_bytes(), b"keyring-data")

    def test_missing_target_keyring_directory_is_tolerated(self) -> None:
        completed = self.run_wrapper("/mnt", "base")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.recorded_argv(), ["/mnt", "base"])

    def test_pacstrap_failure_propagates_nonzero_exit(self) -> None:
        completed = self.run_wrapper("-K", "/mnt", "base", returncode=7)
        self.assertEqual(completed.returncode, 7)
        self.assertEqual(self.recorded_argv(), ["/mnt", "base"])

    def test_seeded_configuration_substitutes_pacman_conf_and_reuses_cache(self) -> None:
        seeded = str(self.sandbox / "seeded-pacman.conf")
        completed = self.run_wrapper(
            "-K", "-C", "/etc/pacman.conf", "/etc/pacman.conf", "/mnt", "base",
            pacman_conf=seeded,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            self.recorded_argv(),
            ["-c", "-C", seeded, seeded, "/mnt", "base"],
        )


if __name__ == "__main__":
    unittest.main()
