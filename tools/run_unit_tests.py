"""Run the whole unit suite and exit with the real result.

Qt can crash the interpreter while tearing down after the last test (seen on
CI: exit code 139 on Linux although every test passed). The exit code here is
the test outcome, and the process then ends without the crashing teardown.
Tests use fake instruments only; no hardware, serial port or network is opened.
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT))

suite = unittest.defaultTestLoader.discover(str(ROOT / "leanwatts_can" / "tests"), pattern="test_*.py",
                                            top_level_dir=str(ROOT / "leanwatts_can" / "tests"))
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.stdout.flush()
sys.stderr.flush()
os._exit(0 if result.wasSuccessful() else 1)
