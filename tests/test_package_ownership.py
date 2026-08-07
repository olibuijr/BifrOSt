from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PKGBUILD_DIR = ROOT / "packaging/bifrost-system"
INSTALLED_ROOT = ROOT / "profile/airootfs/usr/share/bifrost/installed-root"

# 0.2.1-2 was rejected because the package payload shadowed paths owned by the
# official cosmic-bg and plymouth packages. These exact paths must never be
# owned (or shadowed) by bifrost-system again.
FORBIDDEN_PATHS = (
    "usr/share/cosmic/com.system76.CosmicBackground/v1/all",
    "etc/plymouth/plymouthd.conf",
)

BUILD_SCRIPT = """\
set -euo pipefail
cd "$BIFROST_TEST_PKGBUILD_DIR"
source ./PKGBUILD
pkgdir="$BIFROST_TEST_PKGDIR"
package
"""


def build_package_payload(pkgdir: Path, public_key: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "BIFROST_TEST_PKGBUILD_DIR": str(PKGBUILD_DIR),
            "BIFROST_TEST_PKGDIR": str(pkgdir),
            "BIFROST_SOURCE_ROOT": str(ROOT),
            "BIFROST_ALPM_PUBLIC_KEY": str(public_key),
            "BIFROST_ALPM_FINGERPRINT": "0" * 40,
        }
    )
    return subprocess.run(
        ["bash", "-c", BUILD_SCRIPT],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def payload_paths(pkgdir: Path) -> set[str]:
    paths: set[str] = set()
    for current, directories, files in os.walk(pkgdir):
        base = Path(current).relative_to(pkgdir)
        for name in directories + files:
            paths.add(str(base / name))
    return paths


class PackageOwnershipTest(unittest.TestCase):
    """Build the bifrost-system package() payload and prove it cannot collide
    with official-package-owned paths."""

    payload: set[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.stage = Path(tempfile.mkdtemp(prefix="bifrost-pkg-ownership-"))
        cls.pkgdir = cls.stage / "pkg"
        cls.pkgdir.mkdir()
        public_key = cls.stage / "alpm-repository-key.asc"
        public_key.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\ntest\n", encoding="utf-8")
        completed = build_package_payload(cls.pkgdir, public_key)
        if completed.returncode != 0:
            raise AssertionError(
                f"PKGBUILD package() failed ({completed.returncode}):\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
        cls.payload = payload_paths(cls.pkgdir)

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(["rm", "-rf", str(cls.stage)], check=False)

    def test_payload_is_nonempty_and_contains_expected_first_party_files(self) -> None:
        expected = {
            "usr/bin/bifrost-maintenance",
            "usr/lib/bifrost-maintenance/system-upgrade",
            "usr/share/bifrost/os-release",
            "usr/share/bifrost/branding/cosmic-background.ron",
            "usr/share/bifrost/branding/plymouthd.conf",
            "usr/share/plymouth/themes/bifrost",
            "usr/share/bifrost/keys/alpm-repository-key.asc",
            "usr/share/bifrost/keys/alpm-repository-key.fingerprint",
        }
        missing = expected - self.payload
        self.assertFalse(missing, f"package payload lost expected files: {sorted(missing)}")

    def test_payload_never_owns_official_package_paths(self) -> None:
        for forbidden in FORBIDDEN_PATHS:
            with self.subTest(path=forbidden):
                self.assertNotIn(
                    forbidden,
                    self.payload,
                    f"bifrost-system must never own /{forbidden}; "
                    "this collision caused the 0.2.1-2 rejection",
                )
                shadowed = sorted(
                    path for path in self.payload if path.startswith(forbidden + "/")
                )
                self.assertFalse(
                    shadowed,
                    f"bifrost-system must not ship anything under /{forbidden}: {shadowed}",
                )

    def test_installed_root_source_tree_does_not_shadow_official_paths(self) -> None:
        for forbidden in FORBIDDEN_PATHS:
            with self.subTest(path=forbidden):
                self.assertFalse(
                    os.path.lexists(INSTALLED_ROOT / forbidden),
                    f"installed-root must not contain /{forbidden}; package() copies "
                    "installed-root verbatim, so this would re-create the 0.2.1-2 conflict",
                )

    def test_relocated_branding_copies_match_their_live_sources(self) -> None:
        pairs = {
            "usr/share/bifrost/branding/plymouthd.conf": ROOT
            / "profile/airootfs/etc/plymouth/plymouthd.conf",
            "usr/share/bifrost/branding/cosmic-background.ron": ROOT
            / "profile/airootfs/usr/share/bifrost/cosmic-background.ron",
        }
        for packaged, source in pairs.items():
            with self.subTest(path=packaged):
                self.assertEqual(
                    (self.pkgdir / packaged).read_bytes(),
                    source.read_bytes(),
                    f"{packaged} must be the exact relocated copy of {source}",
                )


if __name__ == "__main__":
    unittest.main()
