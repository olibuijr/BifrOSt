"""Test package for the BifrOSt operating-system repository.

Tests import maintenance and installer modules directly from the tracked
``profile/airootfs`` staging tree. Interpreter bytecode caches must never be
written back into that tree (``validate-build.py`` rejects staged
``__pycache__`` debris), so bytecode writing is disabled for every test run.
"""

import os
import sys

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
